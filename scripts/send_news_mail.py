from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from googlenewsdecoder import gnewsdecoder
from trafilatura import extract


ROOT = pathlib.Path(__file__).resolve().parents[1]
LINKS_PATH = ROOT / "_site" / "links.json"
WEB_PATH = ROOT / "_site" / "index.html"
CONFIG_PATH = ROOT / "config" / "keywords.yml"
TIMEZONE = ZoneInfo("Asia/Tokyo")
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
PAYWALL_MARKERS = (
    "この記事は有料",
    "有料会員限定",
    "会員限定",
    "続きを読むには",
    "ログインしてください",
    "会員登録が必要",
    "会員登録してください",
)


def load_yaml_config() -> dict:
    """既存のYAML設定を読み込む。"""
    import yaml

    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_links() -> list[dict]:
    """生成済みのリンク一覧を読み込む。"""
    if not LINKS_PATH.exists():
        raise FileNotFoundError(
            f"{LINKS_PATH} が見つかりません。先に scripts/generate_links.py を実行してください。"
        )

    with LINKS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("_site/links.json の形式が不正です。")

    return data


def env_bool(name: str, default: bool) -> bool:
    """環境変数を真偽値として読み込む。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_title(title: str) -> str:
    """重複判定用にタイトルを正規化する。"""
    return re.sub(r"\s+", "", title).lower()


def select_candidates(items: list[dict], hours_back: int, max_candidates: int) -> list[dict]:
    """対象期間の新しい記事を候補として選び、タイトルの重複も除外する。"""
    cutoff = int((datetime.now(TIMEZONE) - timedelta(hours=hours_back)).timestamp())
    selected: list[dict] = []
    seen_titles: set[str] = set()

    for item in items:
        published_timestamp = int(item.get("published_timestamp") or 0)
        if published_timestamp and published_timestamp < cutoff:
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            continue

        title_key = normalize_title(title)
        if title_key in seen_titles:
            continue

        seen_titles.add(title_key)
        selected.append(item)

        if max_candidates > 0 and len(selected) >= max_candidates:
            break

    return selected


def decode_google_news_url(url: str) -> str:
    """Google Newsの中継URLを可能な範囲で配信元URLに戻す。"""
    host = urlparse(url).netloc.lower()
    if host != "news.google.com":
        return url

    try:
        result = gnewsdecoder(url, interval=0)
        if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
            return str(result["decoded_url"])
    except Exception as exc:
        print(f"URL展開に失敗しました: {exc}", file=sys.stderr)

    return url


def decode_response_html(response: requests.Response) -> str:
    """HTMLの文字コードを複数の手掛かりから判定してUnicode文字列へ変換する。"""
    raw = response.content
    candidates: list[str] = []

    content_type = response.headers.get("content-type", "")
    header_match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, flags=re.IGNORECASE)
    if header_match:
        candidates.append(header_match.group(1))

    head = raw[:16_384]
    meta_patterns = [
        rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)",
        rb"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([A-Za-z0-9._:-]+)",
    ]
    for pattern in meta_patterns:
        match = re.search(pattern, head, flags=re.IGNORECASE)
        if match:
            candidates.append(match.group(1).decode("ascii", errors="ignore"))

    apparent = response.apparent_encoding
    if apparent:
        candidates.append(apparent)

    candidates.extend(["utf-8", "cp932", "shift_jis", "euc_jp"])

    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.strip().lower().replace("_", "-")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return raw.decode("utf-8", errors="replace")


def fetch_article_text(session: requests.Session, url: str, max_chars: int) -> tuple[str, str]:
    """記事URLから本文を抽出し、実際に取得できたURLと本文を返す。"""
    source_url = decode_google_news_url(url)

    try:
        response = session.get(
            source_url,
            timeout=(5, 15),
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and "xhtml" not in content_type:
            return response.url, ""

        # 巨大ページをそのまま処理しないように上限を設ける。
        if len(response.content) > 6_000_000:
            return response.url, ""

        decoded_html = decode_response_html(response)
        text = extract(
            decoded_html,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        ) or ""
        text = text.strip()

        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n\n（本文は容量節約のため途中まで）"

        return response.url, text
    except Exception as exc:
        print(f"本文取得に失敗しました: {source_url} / {exc}", file=sys.stderr)
        return source_url, ""


def is_usable_article_text(text: str) -> bool:
    """本文として十分読める内容かを簡易判定する。"""
    stripped = text.strip()
    if len(stripped) < 120:
        return False

    # 短い課金・会員誘導だけが取れた記事は朝刊から除外する。
    if len(stripped) < 1500 and any(marker in stripped for marker in PAYWALL_MARKERS):
        return False

    return True


def truncate_for_mail(text: str, max_chars: int) -> str:
    """メール本文をWeb版の最初の表示と同じ長さに切り詰める。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    return text[:max_chars].rstrip() + "…"


def text_to_html(text: str) -> str:
    """抽出本文を表示用HTMLへ変換する。"""
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if not paragraphs and text.strip():
        paragraphs = [text.strip()]

    return "\n".join(f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs)


def article_meta_html(article: dict) -> str:
    """記事のキーワード・配信元・日時をHTMLにする。"""
    source = html.escape(str(article.get("source") or ""))
    published = html.escape(str(article.get("published") or ""))
    keyword = html.escape(str(article.get("keyword") or ""))

    meta_parts = []
    if keyword:
        meta_parts.append(f'<span class="keyword">{keyword}</span>')
    if source:
        meta_parts.append(source)
    if published:
        meta_parts.append(published)

    return " / ".join(meta_parts)


def render_title_index_html(articles: list[dict]) -> str:
    """ページ先頭に表示する、元記事へ直接移動できるタイトル一覧を作る。"""
    parts = [
        '<div class="toc" id="news-index">',
        '<div class="toc-title">ニュース一覧</div>',
        "<ol>",
    ]

    for article in articles:
        title = html.escape(str(article.get("title") or "無題"))
        source_url = html.escape(
            str(article.get("source_url") or article.get("link") or ""),
            quote=True,
        )
        if source_url:
            # 一覧のタイトルを押したら、朝刊内ではなく配信元の記事を直接開く。
            parts.append(
                f'<li><a href="{source_url}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#222;text-decoration:none;">{title}</a></li>'
            )
        else:
            parts.append(f"<li>{title}</li>")

    parts.extend(["</ol>", "</div>"])
    return "\n".join(parts)


def render_mail_html(
    site_title: str,
    articles: list[dict],
    generated_at: str,
    preview_chars: int,
) -> str:
    """Web版の冒頭表示と同じ長さに本文を抑えた朝刊メールHTMLを作る。"""
    parts = [
        "<!doctype html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(site_title)} 朝刊</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.75;max-width:760px;margin:0 auto;padding:20px;color:#222;background:#fff}",
        "h1{font-size:1.6rem}h2{font-size:1.18rem;margin-top:2.2rem;padding-top:1.2rem;border-top:1px solid #ddd}",
        ".meta{font-size:.86rem;color:#666}.keyword{display:inline-block;border:1px solid #bbb;border-radius:999px;padding:0 .55rem;margin-right:.4rem;font-size:.78rem}",
        ".toc{margin:1.5rem 0;padding:1rem 1.2rem;background:#f6f7f8;border-radius:10px}.toc-title{font-weight:700;margin-bottom:.5rem}.toc ol{margin:.4rem 0;padding-left:1.5rem}.toc li{margin:0;padding:.45rem 0;border-bottom:1px solid #e2e4e7}.toc li:last-child{border-bottom:0}.toc a{color:#222;text-decoration:none}",
        ".source-link{word-break:break-all}p{margin:.9rem 0}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(site_title)} 朝刊</h1>",
        f'<div class="meta">生成日時: {html.escape(generated_at)} JST / 収録: {len(articles)}記事</div>',
        render_title_index_html(articles),
    ]

    for index, article in enumerate(articles, start=1):
        title = html.escape(str(article.get("title") or "無題"))
        source_url = html.escape(str(article.get("source_url") or article.get("link") or ""), quote=True)
        article_text = truncate_for_mail(str(article.get("article_text") or "").strip(), preview_chars)
        meta = article_meta_html(article)

        parts.append(f'<a id="article-{index}" name="article-{index}"></a>')
        parts.append(f"<h2>{index}. {title}</h2>")
        if meta:
            parts.append(f'<div class="meta">{meta}</div>')

        parts.append(text_to_html(article_text))

        if source_url:
            parts.append(f'<p class="source-link"><a href="{source_url}">配信元の記事を開く</a></p>')

    parts.extend(
        [
            "<hr>",
            '<p class="meta">Google News RSSを起点に、個人のオフライン閲覧用として自動生成した朝刊です。本文を取得できない記事は除外しています。</p>',
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts)


def render_web_html(
    site_title: str,
    articles: list[dict],
    generated_at: str,
    preview_chars: int,
) -> str:
    """GitHub Pages向けに目次と長文折りたたみを備えた朝刊HTMLを作る。"""
    parts = [
        "<!doctype html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(site_title)} 朝刊</title>",
        "<style>",
        "html{scroll-behavior:smooth}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.75;max-width:760px;margin:0 auto;padding:20px;color:#222;background:#fff}",
        "h1{font-size:1.6rem}h2{font-size:1.18rem;margin-top:2.2rem;padding-top:1.2rem;border-top:1px solid #ddd;scroll-margin-top:1rem}",
        ".meta{font-size:.86rem;color:#666}.keyword{display:inline-block;border:1px solid #bbb;border-radius:999px;padding:0 .55rem;margin-right:.4rem;font-size:.78rem}",
        ".toc{margin:1.5rem 0;padding:1rem 1.2rem;background:#f6f7f8;border-radius:10px}.toc-title{font-weight:700;margin-bottom:.5rem}.toc ol{margin:.4rem 0;padding-left:1.5rem}.toc li{margin:0;padding:.45rem 0;border-bottom:1px solid #e2e4e7}.toc li:last-child{border-bottom:0}.toc a{color:#222;text-decoration:none}.toc a:hover{text-decoration:underline}",
        ".source-link{word-break:break-all}p{margin:.9rem 0}details{margin:.7rem 0 1rem}summary{cursor:pointer;font-weight:600;color:#1565c0}details[open] summary{margin-bottom:.7rem}.details-body{padding-left:.2rem}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(site_title)} 朝刊</h1>",
        f'<div class="meta">生成日時: {html.escape(generated_at)} JST / 収録: {len(articles)}記事</div>',
        render_title_index_html(articles),
    ]

    for index, article in enumerate(articles, start=1):
        title = html.escape(str(article.get("title") or "無題"))
        source_url = html.escape(str(article.get("source_url") or article.get("link") or ""), quote=True)
        article_text = str(article.get("article_text") or "").strip()
        meta = article_meta_html(article)

        parts.append(f'<h2 id="article-{index}">{index}. {title}</h2>')
        if meta:
            parts.append(f'<div class="meta">{meta}</div>')

        if preview_chars > 0 and len(article_text) > preview_chars:
            preview = article_text[:preview_chars].rstrip()
            remaining = article_text[preview_chars:].lstrip()
            parts.append(text_to_html(preview + "…"))
            parts.append("<details>")
            parts.append("<summary>全文を読む</summary>")
            parts.append(f'<div class="details-body">{text_to_html(remaining)}</div>')
            parts.append("</details>")
        else:
            parts.append(text_to_html(article_text))

        if source_url:
            parts.append(
                f'<p class="source-link"><a href="{source_url}" target="_blank" rel="noopener noreferrer">配信元の記事を開く</a></p>'
            )

    parts.extend(
        [
            "<hr>",
            '<p class="meta">Google News RSSを起点に、個人閲覧用として自動生成した朝刊です。本文を取得できない記事は除外しています。</p>',
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts)


def render_mail_text(
    site_title: str,
    articles: list[dict],
    generated_at: str,
    preview_chars: int,
) -> str:
    """HTMLを表示できないメールアプリ向けのプレーンテキストを作る。"""
    lines = [f"{site_title} 朝刊", f"生成日時: {generated_at} JST", "", "ニュース一覧"]

    for index, article in enumerate(articles, start=1):
        lines.append(f"{index}. {article.get('title', '無題')}")

    lines.append("")

    for index, article in enumerate(articles, start=1):
        lines.append(f"■ {index}. {article.get('title', '無題')}")
        meta = " / ".join(
            str(value)
            for value in (article.get("keyword"), article.get("source"), article.get("published"))
            if value
        )
        if meta:
            lines.append(meta)

        article_text = truncate_for_mail(str(article.get("article_text") or "").strip(), preview_chars)
        lines.append(article_text)

        source_url = str(article.get("source_url") or article.get("link") or "")
        if source_url:
            lines.append(source_url)
        lines.append("")

    return "\n".join(lines)


def make_message(
    subject: str,
    sender: str,
    recipient: str,
    site_title: str,
    articles: list[dict],
    generated_at: str,
    preview_chars: int,
) -> EmailMessage:
    """HTMLとプレーンテキストの両方を持つメールを作る。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(render_mail_text(site_title, articles, generated_at, preview_chars))
    message.add_alternative(
        render_mail_html(site_title, articles, generated_at, preview_chars),
        subtype="html",
    )
    return message


def collect_articles(
    items: list[dict],
    max_articles: int,
    max_fetch_chars: int,
    preview_chars: int,
    max_mail_bytes: int,
    site_title: str,
    sender: str,
    recipient: str,
    subject: str,
    generated_at: str,
) -> tuple[list[dict], EmailMessage]:
    """本文取得に成功した記事だけを集め、最大件数まで次の記事で補充する。"""
    session = requests.Session()
    articles: list[dict] = []
    skipped = 0
    message = make_message(
        subject,
        sender,
        recipient,
        site_title,
        articles,
        generated_at,
        preview_chars,
    )

    for index, item in enumerate(items, start=1):
        print(f"[{index}/{len(items)}] {item.get('title', '')}")
        source_url, article_text = fetch_article_text(
            session,
            str(item.get("link") or ""),
            max_fetch_chars,
        )

        if not is_usable_article_text(article_text):
            skipped += 1
            print("本文を十分取得できないため、この記事は除外します。")
            time.sleep(0.25)
            continue

        article = dict(item)
        article["source_url"] = source_url
        article["article_text"] = article_text

        candidate_articles = articles + [article]
        candidate_message = make_message(
            subject,
            sender,
            recipient,
            site_title,
            candidate_articles,
            generated_at,
            preview_chars,
        )

        if len(candidate_message.as_bytes()) > max_mail_bytes:
            print("メール容量上限に達したため、ここで収録を終了します。")
            break

        articles = candidate_articles
        message = candidate_message
        time.sleep(0.25)

        if len(articles) >= max_articles:
            break

    print(f"本文取得失敗などで除外した記事数: {skipped}")
    return articles, message


def write_web_page(
    site_title: str,
    articles: list[dict],
    generated_at: str,
    preview_chars: int,
) -> None:
    """目次と長文折りたたみを備えた朝刊HTMLをGitHub Pages用に保存する。"""
    WEB_PATH.parent.mkdir(exist_ok=True)
    WEB_PATH.write_text(
        render_web_html(site_title, articles, generated_at, preview_chars),
        encoding="utf-8",
    )
    print(f"GitHub Pages用の朝刊を生成しました: {WEB_PATH}")


def send_message(message: EmailMessage) -> None:
    """環境変数のSMTP設定を使ってメールを送信する。"""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_ssl = env_bool("SMTP_USE_SSL", True)

    if not username or not password:
        raise RuntimeError("SMTP_USERNAME と SMTP_PASSWORD をGitHub Secretsに設定してください。")

    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def main() -> None:
    """朝刊を生成し、GitHub Pagesへ反映するHTMLを作ってメール送信する。"""
    parser = argparse.ArgumentParser(description="Google News RSSのリンクからオフライン用朝刊を生成します。")
    parser.add_argument("--dry-run", action="store_true", help="送信せず news_mail_preview.eml を保存します。")
    args = parser.parse_args()

    config = load_yaml_config()
    mail_config = config.get("mail") or {}
    site_title = str(config.get("site_title", "毎日のリンク集"))
    hours_back = int(mail_config.get("hours_back", 36))
    max_articles = int(mail_config.get("max_articles", 30))
    max_candidates = int(mail_config.get("max_candidates", 120))
    max_fetch_chars = int(mail_config.get("max_fetch_chars_per_article", 8000))
    preview_chars = int(mail_config.get("web_preview_chars", 600))
    max_mail_bytes = int(mail_config.get("max_mail_bytes", 2_500_000))

    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    sender = os.getenv("MAIL_FROM", smtp_username or "offline-news@example.invalid").strip()
    recipient = os.getenv("MAIL_TO", smtp_username or "offline-news@example.invalid").strip()

    now = datetime.now(TIMEZONE)
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    subject = f"【朝刊】{site_title} {now.strftime('%Y-%m-%d')}"

    items = select_candidates(
        load_links(),
        hours_back=hours_back,
        max_candidates=max_candidates,
    )
    if not items:
        print("対象期間の記事がないため、朝刊生成を終了します。")
        return

    articles, message = collect_articles(
        items,
        max_articles=max_articles,
        max_fetch_chars=max_fetch_chars,
        preview_chars=preview_chars,
        max_mail_bytes=max_mail_bytes,
        site_title=site_title,
        sender=sender,
        recipient=recipient,
        subject=subject,
        generated_at=generated_at,
    )

    if not articles:
        print("朝刊に収録できる記事がありませんでした。")
        return

    print(f"収録記事数: {len(articles)}")
    print(f"メールサイズ: {len(message.as_bytes()):,} bytes")

    write_web_page(
        site_title,
        articles,
        generated_at,
        preview_chars,
    )

    if args.dry_run:
        preview_path = ROOT / "news_mail_preview.eml"
        preview_path.write_bytes(message.as_bytes())
        print(f"プレビューを保存しました: {preview_path}")
        return

    send_message(message)
    print(f"送信しました: {recipient}")


if __name__ == "__main__":
    main()

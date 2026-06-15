from __future__ import annotations

import html
import json
import pathlib
import sys
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "keywords.yml"
OUTPUT_DIR = ROOT / "_site"
TIMEZONE = ZoneInfo("Asia/Tokyo")


def google_news_rss_url(keyword: str) -> str:
    """GoogleニュースのRSS検索URLを作る。"""
    query = urllib.parse.quote(keyword)
    return f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"


def fetch_items(keyword: str, max_items: int) -> list[dict]:
    """キーワードに対応するRSSを取得して記事一覧に整形する。"""
    url = google_news_rss_url(keyword)
    feed = feedparser.parse(url)

    if getattr(feed, "bozo", False):
        print(f"RSSの読み込みで警告が出ました: {keyword}", file=sys.stderr)

    items: list[dict] = []
    for entry in feed.entries[:max_items]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()

        if not title or not link:
            continue

        source = ""
        if hasattr(entry, "source"):
            source = getattr(entry.source, "title", "") or ""

        published = getattr(entry, "published", "")

        items.append(
            {
                "keyword": keyword,
                "title": title,
                "link": link,
                "published": published,
                "source": source,
            }
        )

    return items


def load_config() -> dict:
    """設定ファイルを読み込む。"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if not isinstance(config.get("keywords", []), list):
        raise ValueError("config/keywords.yml の keywords はリストで指定してください")

    return config


def render_html(site_title: str, items_by_keyword: dict[str, list[dict]]) -> str:
    """リンク集ページのHTMLを生成する。"""
    now = datetime.now(TIMEZONE)
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="ja">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{html.escape(site_title)}</title>")
    parts.append(
        """
<style>
:root {
  color-scheme: light dark;
}
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 980px;
  margin: 40px auto;
  padding: 0 20px;
  line-height: 1.75;
}
header {
  border-bottom: 2px solid #ddd;
  margin-bottom: 2rem;
  padding-bottom: 1rem;
}
section {
  margin: 2.4rem 0;
}
ol {
  padding-left: 1.4rem;
}
li {
  margin-bottom: 1rem;
}
a {
  text-decoration-thickness: 0.08em;
  text-underline-offset: 0.18em;
}
.meta {
  opacity: 0.72;
  font-size: 0.92rem;
}
.keyword-nav a {
  display: inline-block;
  margin: 0 0.6rem 0.4rem 0;
}
.empty {
  opacity: 0.7;
}
footer {
  border-top: 1px solid #ddd;
  margin-top: 3rem;
  padding-top: 1rem;
  opacity: 0.75;
  font-size: 0.9rem;
}
</style>
"""
    )
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<header>")
    parts.append(f"<h1>{html.escape(site_title)}</h1>")
    parts.append(f'<div class="meta">生成日時: {html.escape(generated_at)} JST</div>')
    parts.append('<nav class="keyword-nav" aria-label="キーワード一覧">')

    for keyword in items_by_keyword:
        anchor = urllib.parse.quote(keyword)
        parts.append(f'<a href="#{anchor}">{html.escape(keyword)}</a>')

    parts.append("</nav>")
    parts.append("</header>")

    for keyword, items in items_by_keyword.items():
        anchor = urllib.parse.quote(keyword)
        parts.append(f'<section id="{anchor}">')
        parts.append(f"<h2>{html.escape(keyword)}</h2>")

        if not items:
            parts.append('<p class="empty">記事が見つかりませんでした。</p>')
            parts.append("</section>")
            continue

        parts.append("<ol>")
        for item in items:
            title = html.escape(item["title"])
            link = html.escape(item["link"])
            source = html.escape(item.get("source", ""))
            published = html.escape(item.get("published", ""))

            meta_parts = []
            if source:
                meta_parts.append(source)
            if published:
                meta_parts.append(published)
            meta = " / ".join(meta_parts)

            parts.append("<li>")
            parts.append(f'<a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a>')
            if meta:
                parts.append(f'<div class="meta">{meta}</div>')
            parts.append("</li>")

        parts.append("</ol>")
        parts.append("</section>")

    parts.append("<footer>")
    parts.append("Google News RSS検索結果を元に自動生成しています。リンク先の内容は各配信元を確認してください。")
    parts.append("</footer>")
    parts.append("</body>")
    parts.append("</html>")

    return "\n".join(parts)


def main() -> None:
    """設定に従ってリンク集ページを生成する。"""
    config = load_config()
    site_title = str(config.get("site_title", "毎日のリンク集"))
    keywords = [str(keyword).strip() for keyword in config.get("keywords", []) if str(keyword).strip()]
    max_items = int(config.get("max_items_per_keyword", 10))

    seen_links: set[str] = set()
    items_by_keyword: dict[str, list[dict]] = {}

    for keyword in keywords:
        items: list[dict] = []
        for item in fetch_items(keyword, max_items):
            link = item["link"]
            if link in seen_links:
                continue
            seen_links.add(link)
            items.append(item)
        items_by_keyword[keyword] = items

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (OUTPUT_DIR / "index.html").write_text(render_html(site_title, items_by_keyword), encoding="utf-8")
    (OUTPUT_DIR / "links.json").write_text(
        json.dumps(items_by_keyword, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

# Google News RSS キーワードリンク集

GitHub Actions と GitHub Pages で、Google News RSS検索結果から毎日リンク集ページを自動生成する構成です。

複数キーワードで取得した記事を1つの一覧にまとめ、公開日時が新しい順に表示します。

さらに、毎朝のリンク集生成後に記事本文をできる範囲で抽出し、iPhoneなどでオフライン閲覧しやすいHTMLメールとして自分宛てに送信できます。

## 使い方

1. このリポジトリをGitHubで利用する
2. GitHubの `Settings` → `Pages` → `Build and deployment` → `Source` を `GitHub Actions` にする
3. `config/keywords.yml` のキーワードを編集する
4. メール送信を使う場合は、後述のGitHub Secretsを設定する
5. Actionsタブから `毎日のリンク集を生成` を手動実行する、または翌日の定期実行を待つ

## 生成されるファイル

GitHub Actions 実行時に `_site` 配下へ以下を生成します。

- `index.html`: 公開用のリンク集ページ
- `links.json`: 取得結果のJSON。全記事を公開日時の新しい順に並べた配列です
- `.nojekyll`: Jekyll処理を無効化するための空ファイル

## 定期実行時刻

`.github/workflows/build.yml` では、毎日JST 06:30に相当するUTC 21:30で実行する設定にしています。

```yaml
- cron: "30 21 * * *"
```

GitHub Actions の `schedule` はUTC基準なので、日本時間にしたい場合は9時間引いた時刻で指定してください。

## キーワード変更

`config/keywords.yml` を編集します。

```yaml
site_title: "毎日のリンク集"
max_items_per_keyword: 10

keywords:
  - "ひむ太郎"
  - "はなしちゃお"
```

`max_items_per_keyword` は、各キーワードごとにRSSから取得する最大件数です。表示はキーワード別ではなく、全キーワードの記事をまとめて新しい順になります。

## オフライン用朝刊メール

`scripts/send_news_mail.py` が `_site/links.json` を読み込み、次の処理を行います。

- 最近の記事だけを抽出
- Google Newsの中継URLを可能な範囲で配信元URLへ変換
- 記事本文を抽出
- 画像を含めず、本文中心のHTMLメールを生成
- 本文取得に失敗した記事はタイトルとリンクだけ掲載
- メール全体が約2.5MBを超えないように記事数を自動調整
- SMTPで自分宛てに送信

メール設定は `config/keywords.yml` の `mail` で変更できます。

```yaml
mail:
  hours_back: 36
  max_articles: 30
  max_chars_per_article: 8000
  max_mail_bytes: 2500000
```

## Gmailで送信する場合

GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret` から次の3つを登録してください。

- `SMTP_USERNAME`: 送信に使うGmailアドレス
- `SMTP_PASSWORD`: Googleアカウントで発行したアプリパスワード
- `MAIL_TO`: 朝刊を受信するメールアドレス

通常のGoogleアカウントのログインパスワードをGitHubへ保存しないでください。

Secretsが未設定の場合、リンク集ページの生成と公開だけを行い、メール送信は自動的にスキップします。

## 手元でメール表示を確認する

リンク集を生成したあと、次を実行すると実際には送信せず `news_mail_preview.eml` を作成できます。

```bash
python scripts/generate_links.py
python scripts/send_news_mail.py --dry-run
```

作成された `.eml` ファイルをメールアプリで開くと、実際に届く朝刊の見た目を確認できます。

## 注意

Google News RSS検索は便利ですが、公式の安定APIとして保証されているものではありません。また、配信元サイトによってはJavaScript、会員認証、アクセス制限などにより本文を取得できません。その場合はメール内にタイトルとリンクだけ掲載します。

記事本文の保存・利用については各配信元の利用条件や著作権に従ってください。この仕組みは個人のオフライン閲覧用途を想定しています。

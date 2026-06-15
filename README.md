# Google News RSS キーワードリンク集

GitHub Actions と GitHub Pages で、Google News RSS検索結果から毎日リンク集ページを自動生成する最小構成です。

## 使い方

1. このZIPを展開する
2. 中身をGitHubリポジトリにpushする
3. GitHubの `Settings` → `Pages` → `Build and deployment` → `Source` を `GitHub Actions` にする
4. `config/keywords.yml` のキーワードを編集する
5. Actionsタブから `毎日のリンク集を生成` を手動実行する、または翌日の定期実行を待つ

## 生成されるファイル

GitHub Actions 実行時に `_site` 配下へ以下を生成します。

- `index.html`: 公開用のリンク集ページ
- `links.json`: 取得結果のJSON
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
  - "生成AI"
  - "GitHub Actions"
  - "ビットコイン"
```

## 注意

Google News RSS検索は便利ですが、公式の安定APIとして保証されているものではありません。重要な用途では、各サイトの正式RSSやニュースAPIに差し替える前提で使ってください。

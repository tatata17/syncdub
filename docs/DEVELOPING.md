# 開発メモ

## 公開手順
1. `syncdub.pyw` の `APP_VERSION` と `version.json` の `version` を同じ値に上げる(`notes` も更新)
2. `docs/changelog.json` の先頭に履歴を1件追記する
3. commit して `main` に push する(アプリはこの `version.json` を見て自動更新する)

## 使い方ページ
`docs/index.html` を GitHub Pages で公開している(Settings → Pages → Branch `main` / `/docs`)。
最新版の表示は `version.json`、更新履歴は `docs/changelog.json` から読み込む。

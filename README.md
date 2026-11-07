# syncdub

日本語吹き替え音声を、英語版の綺麗な映像に区間指定で合わせて合成するツール (Python / tkinter / FFmpeg)。

- `syncdub.pyw` をダブルクリックで起動 (単体ファイル)
- 使い方: 「かんたん」タブで ① 英語版の映像と日本語録画をドロップ → ② 自動で合わせる(自動解析) → ③ 結果を確認 → ④ 合成スタート
- 細かい直しは「手動調整」タブ (波形・フレームを見ながら区間を編集)
- 必要: Python 3, numpy, ffmpeg / ffprobe / ffplay (PATH)。ドラッグ&ドロップは `pip install tkinterdnd2` (任意)
- 更新: アプリが `version.json` を見て自動更新 (旧版は `syncdub_backups` に保存、「元に戻す」可)

## 公開手順
`syncdub.pyw` の `APP_VERSION` と `version.json` の `version` を同じ値に上げてから push する。

## 使い方ページ
`docs/index.html` (GitHub Pages: Settings → Pages → Branch `main` / `/docs`)。更新履歴は `docs/changelog.json` に新しい順で追記する。

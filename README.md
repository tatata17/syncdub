# syncdub 吹き替え音声シンクロツール

TV録画などに入っている**日本語吹き替えの音声**を、きれいな**英語版の映像**に合わせて、1本の動画(mkv)にする Windows 向けのツールです。映像は再エンコードせず、そのままコピーします。

- 音の波形を照合して、CM・暗転・カットされた場面を**自動で見つけて**合わせます
- うまくいかない所は、波形と画面を見ながら**手動で指定**できます(同じ場面・CM・暗転・カット)
- 出来上がりは、日本語音声が最初(既定)で英語音声も残った mkv
- ファイルは `syncdub.pyw` 1つだけ。更新は GitHub 経由で自動

**👉 [使い方・更新履歴のページ](https://tatata17.github.io/syncdub/)**

## はじめかた

1. 次を用意します
   - [Python 3](https://www.python.org/downloads/)(インストール時に「Add python.exe to PATH」にチェック)
   - `pip install numpy`
   - [ffmpeg](https://ffmpeg.org/download.html)(`ffmpeg` / `ffprobe` が PATH にあること)
   - (任意) `pip install tkinterdnd2` … ファイルのドラッグ&ドロップが使えます
2. [`syncdub.pyw` をダウンロード](https://raw.githubusercontent.com/tatata17/syncdub/main/syncdub.pyw)して、好きなフォルダに置きます
3. ダブルクリックで起動します
4. 「かんたん」タブで、英語版の映像と日本語の録画をドロップ → 「自動で合わせる」 → 結果を確認 → 「合成スタート」

詳しい使い方や困ったときの対処は、[使い方ページ](https://tatata17.github.io/syncdub/)をご覧ください。

## 更新

起動時に新しい版を自動で確認します(旧版は `syncdub_backups` に残り、「元に戻す」で戻せます)。

## 開発者向け

[docs/DEVELOPING.md](docs/DEVELOPING.md) を参照してください。

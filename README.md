# syncdub 吹き替え音声シンクロツール

TV録画などに入っている**日本語吹き替えの音声**を、きれいな**英語版の映像**に合わせて、1本の動画(mkv)にする Windows 向けのツールです。映像は再エンコードせず、そのままコピーします。ファイルは `syncdub.pyw` 1つだけです。

**👉 [使い方・更新履歴のページ](https://tatata17.github.io/syncdub/)**

- 音の波形を照合して、CM・暗転・カットされた場面を**自動で見つけて**合わせます
- うまくいかない所は、波形と画面を見ながら**手動で指定**できます(同じ場面・CM・暗転・カット)
- 出来上がりは、日本語音声が最初(既定)で英語音声も残った mkv
- 更新は GitHub 経由で自動

## 必要なもの

| もの | 説明 |
|---|---|
| Windows 10/11 | |
| Python 3 | [python.org](https://www.python.org/downloads/windows/) からインストールします。インストーラーで **「Add python.exe to PATH」** と **「py launcher」** にチェックを入れてください |
| numpy | `pip install numpy` |
| ffmpeg / ffprobe | **「full」版**を使います。syncdub は ffmpeg を名前で呼び出すので、PATHを通す必要があります |
| tkinterdnd2(任意) | `pip install tkinterdnd2`。入れると、ファイルのドラッグ&ドロップが使えます(なくても、クリックでファイルを選べます) |

**ffmpeg の入れかた:** Windows 10/11 なら、コマンドプロンプトで `winget install Gyan.FFmpeg` を実行すると、full版が入り、PATHも自動で設定されます。入れたあとは `ffmpeg -version` で確認して、syncdub を起動し直してください。手動で入れる方法や、PATHの設定のしかたは、[使い方のページ](https://tatata17.github.io/syncdub/)の「ffmpeg の入れかた」を見てください。

## 起動のしかた

1. [`syncdub.pyw` をダウンロード](https://raw.githubusercontent.com/tatata17/syncdub/main/syncdub.pyw)して、好きなフォルダに置きます(設定ファイルや旧版のバックアップは同じフォルダにできるので、専用のフォルダがおすすめです)
2. ダブルクリックで起動します
3. 「かんたん」タブで、英語版の映像と日本語の録画をドロップ → 「自動で合わせる」 → 結果を確認 → 「合成スタート」

切り出し、録画から探す、手動調整、ショートカットは、[使い方のページ](https://tatata17.github.io/syncdub/)にあります。

## 更新のしかた

起動時に新しい版を自動で確認します。旧版は `syncdub_backups` に残り、「元に戻す」で戻せます。

新しい機能を先に試せる**ベータ版**もあります(試している途中の版なので、正式版より不安定なことがあります)。正式版とは別のファイルで、並べて置けます。入れかたは、[ベータ版のページ](https://tatata17.github.io/syncdub/beta.html)にあります。

## 困ったとき

[使い方のページ](https://tatata17.github.io/syncdub/)の「困ったとき」を見てください。よくあるのは次の2つです。

| 症状 | 対処 |
|---|---|
| ffmpeg が「読み込めない」エラーになる | PATHが通っていません。上の「ffmpeg の入れかた」で入れ直し、syncdub を起動し直す |
| 声だけの場面が「要確認」になる | 音の照合が当たりにくい場面です。手動調整で「同じ場面」を足す |

## 更新履歴

[使い方のページ](https://tatata17.github.io/syncdub/)の「更新履歴」を見てください(内容は [docs/changelog.json](docs/changelog.json) です)。

## 開発者向け

[docs/DEVELOPING.md](docs/DEVELOPING.md) を参照してください。

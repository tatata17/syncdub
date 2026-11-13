# 開発メモ

## ファイルの役割
- `syncdub.pyw` … 正式版。利用者はこれを使い、`version.json` を見て自動更新する。
- `beta/syncdub_beta.pyw` … ベータ版(試験中)。ふだんの作業はこちらで行う。`beta/version.json` を見て自動更新する。正式版の利用者には届かない。
- `tools/release.py` … 公開前の確認と、`version.json` / `beta/version.json` の書き出し。

## ベータ版の公開手順
1. `beta/syncdub_beta.pyw` を直し、`BETA_BUILD` を1つ上げる(`BETA_REVIEW = True` のまま)
2. `python tools/release.py beta "更新内容のひとこと"` … 確認して `beta/version.json` を書く(SHA-256 も入る)
3. `beta/` だけを commit して `main` に push する

## 正式版の公開手順(ベータ版を正式版にする)
ベータ版をそのままコピーすると、正式版がベータの更新先を見にいってしまう。必ず次の順で行う。

1. `beta/syncdub_beta.pyw` を `syncdub.pyw` にコピーする(部分的に移さない。ベータを正として丸ごと置き換える)
2. `syncdub.pyw` の `BETA_REVIEW = False` にする
   - 確認: `grep -n "^BETA_REVIEW" syncdub.pyw` が `False` になっていること
3. `syncdub.pyw` の `APP_VERSION` を上げる(`beta/` 側は触らない)
4. `python tools/release.py official "更新内容のひとこと"` … `BETA_REVIEW` が `False` でなければ止まる。通れば `version.json` を書く
5. `docs/changelog.json` の先頭に履歴を1件追記する
6. 起動して確かめる
   - タイトルと右上のバージョン表示に「ベータ」が出ていない
   - 「録画から探す」のあとに確認ウインドウが勝手に開かない
   - かんたんタブの合成のあとに「自動チェック」が走らない
   - 「更新を確認」で「最新版です」と出る
7. `python -m py_compile syncdub.pyw beta/syncdub_beta.pyw` が通る
8. commit して `main` に push する
9. そのあとベータ版も同じ `APP_VERSION` に上げ、`BETA_BUILD` を上げて公開する(ベータ版の利用者が古い版に取り残されないように)

`BETA_REVIEW = True` のときだけ変わる所: 更新先(`beta/version.json`)、バックアップ先(`syncdub_beta_backups`)、
タイトルとバージョン表示の「ベータ」、確認ウインドウの自動表示、合成後の自動チェックと報告文。

## 正式版にする前の確認(回帰確認の最小セット)
テストの自動化はない。正式版にする前に、次の種類の素材で一度ずつ動かして確かめる。素材の実名は、ここや公開ページには書かない。

かんたん(1本に合わせる)
- ふつうの回 … CMをはさむ30分枠。区間の数と「使いません」の範囲が前の版と変わらない
- 冒頭の曲に、英語版だけナレーションが入る回 … 冒頭が1区間でつながり、途中が無音にならない
- CMの前後に、日本語の録画にだけジングルが入る回 … 英語が無音の所に日本語の音が入らない
- 英語版が無音・暗転で終わる回 … 最後に、日本語の録画の次の番組の音が入らない
- 音だけでは合わない回(「映像でも合わせる」を入れる) … 暗転の所に偽の区間ができない
- ベータ版では、合成後の自動チェックが「問題は見つかりませんでした」になること

録画から探す
- ふつうのCM(15秒・30秒・60秒)
- 短縮版(60秒 → 30秒など)
- 音が英語版と違う版(ナレーションや曲が違う)
- 12秒未満の短いCM
- 1時間を超える録画で、後半ほど音と映像がずれるもの … 音ズレの自動補正がかかり、補正後の測定がほぼ0になる
- 中止 … すぐ止まり、書きかけのファイルが残らない

## 使い方ページ
`docs/index.html` を GitHub Pages で公開している(Settings → Pages → Branch `main` / `/docs`)。
最新版の表示は `version.json`、更新履歴は `docs/changelog.json` から読み込む。

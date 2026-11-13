#!/usr/bin/env python3
"""公開前の確認と、version.json の書き出し。

  python tools/release.py beta "更新内容のひとこと"     … beta/version.json を書く
  python tools/release.py official "更新内容のひとこと" … version.json を書く

本体の APP_VERSION / BETA_BUILD / BETA_REVIEW を読み、食い違いがあれば止まる。
version.json には配布ファイルの SHA-256 も入れる(アプリが更新のときに照合する)。
"""
import hashlib
import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("beta", "official"):
        sys.exit(__doc__)
    beta = sys.argv[1] == "beta"
    notes = sys.argv[2]
    rel = "beta/syncdub_beta.pyw" if beta else "syncdub.pyw"
    src = ROOT / rel
    raw = src.read_bytes().replace(b"\r\n", b"\n")      # リポジトリの中身(LF)と同じにして計算する
    code = raw.decode("utf-8")
    py_compile.compile(str(src), doraise=True)

    def const(name):
        m = re.search(rf"^{name} = (.+?)\s*(#.*)?$", code, re.M)
        if not m:
            sys.exit(f"{rel}: {name} が見つかりません")
        return m.group(1).strip().strip('"')
    ver, review, build = const("APP_VERSION"), const("BETA_REVIEW"), int(const("BETA_BUILD"))
    if beta and review != "True":
        sys.exit(f"{rel}: ベータ版なのに BETA_REVIEW = {review} です (True にする)")
    if not beta and review != "False":
        sys.exit(f"{rel}: 正式版なのに BETA_REVIEW = {review} です。False にしてから公開してください")
    info = {"version": ver}
    if beta:
        info["beta"] = build
    info.update({"file": rel, "sha256": hashlib.sha256(raw).hexdigest(), "notes": notes})
    out = ROOT / ("beta/version.json" if beta else "version.json")
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        key = lambda d: (tuple(int(x) for x in re.findall(r"\d+", d["version"])), int(d.get("beta", 0)))  # noqa: E731
        if key(info) <= key(old) and info["sha256"] != old.get("sha256"):
            sys.exit(f"版番号が上がっていません (今 {old.get('version')} ベータ{old.get('beta', '-')})。"
                     f"{'BETA_BUILD' if beta else 'APP_VERSION'} を上げてください")
    out.write_text(json.dumps(info, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{out.relative_to(ROOT)} を書きました: v{ver}" + (f" ベータ{build}" if beta else ""))
    if not beta:
        print("忘れずに: docs/changelog.json の先頭に履歴を1件追記する")


if __name__ == "__main__":
    main()

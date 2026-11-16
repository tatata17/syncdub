#!/usr/bin/env python3
"""正式版にするときの確認。コピー → BETA_REVIEW = False → APP_VERSION を上げる → release.py official のあと、commit の前に動かす。

  python tools/promote_check.py

確かめること:
  1. syncdub.pyw と beta/syncdub_beta.pyw の違いが、BETA_REVIEW / APP_VERSION / BETA_BUILD の行だけ
  2. syncdub.pyw は BETA_REVIEW = False、beta/syncdub_beta.pyw は True
  3. version.json / beta/version.json の sha256 と版番号が、それぞれの本体(改行を LF にそろえたもの)と合っている
  4. どちらも py_compile が通る
1つでも合わなければ、内容を表示して終了コード 1 で終わる。
"""
import difflib
import hashlib
import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = re.compile(r"^(BETA_REVIEW|APP_VERSION|BETA_BUILD) = ")     # 正式版とベータ版で違ってよい行


def const(code, name):
    m = re.search(rf"^{name} = (.+?)\s*(#.*)?$", code, re.M)
    return m.group(1).strip().strip('"') if m else None


def main():
    bad = []
    files = {"正式版": ("syncdub.pyw", "version.json", "False"), "ベータ版": ("beta/syncdub_beta.pyw", "beta/version.json", "True")}
    code = {}
    for name, (rel, vj, review) in files.items():
        raw = (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")
        code[name] = raw.decode("utf-8")
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{rel}: py_compile が通りません: {e.msg.strip()}")
        if const(code[name], "BETA_REVIEW") != review:
            bad.append(f"{rel}: BETA_REVIEW = {const(code[name], 'BETA_REVIEW')} です ({name}は {review})")
        info = json.loads((ROOT / vj).read_text(encoding="utf-8"))
        if info.get("sha256") != hashlib.sha256(raw).hexdigest():
            bad.append(f"{vj}: sha256 が {rel} と合いません (tools/release.py {'official' if review == 'False' else 'beta'} をやり直す)")
        if info.get("version") != const(code[name], "APP_VERSION"):
            bad.append(f"{vj}: version {info.get('version')} が、{rel} の APP_VERSION {const(code[name], 'APP_VERSION')} と合いません")
        if review == "True" and str(info.get("beta")) != const(code[name], "BETA_BUILD"):
            bad.append(f"{vj}: beta {info.get('beta')} が、{rel} の BETA_BUILD {const(code[name], 'BETA_BUILD')} と合いません")

    a, b = code["正式版"].split("\n"), code["ベータ版"].split("\n")
    extra = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        la, lb = a[i1:i2], b[j1:j2]
        if len(la) == len(lb) and all(ALLOWED.match(x) and ALLOWED.match(y) and x.split(" = ")[0] == y.split(" = ")[0] for x, y in zip(la, lb)):
            continue
        extra.append((i1 + 1, la, j1 + 1, lb))
    if extra:
        n = sum(max(len(la), len(lb)) for _i, la, _j, lb in extra)
        bad.append(f"syncdub.pyw と beta/syncdub_beta.pyw に、想定外の違いが {len(extra)} か所 (約 {n} 行) あります。"
                   "ベータ版を丸ごとコピーし直してください")
        for i, la, j, lb in extra[:12]:
            for x in la[:3]:
                bad.append(f"    正式版 {i}行目: {x[:110]}")
            for y in lb[:3]:
                bad.append(f"    ベータ {j}行目: {y[:110]}")
            if len(la) > 3 or len(lb) > 3:
                bad.append(f"    … (正式版 {len(la)} 行 / ベータ {len(lb)} 行)")
        if len(extra) > 12:
            bad.append(f"    … ほか {len(extra) - 12} か所")
    if bad:
        print("NG")
        print("\n".join(bad))
        sys.exit(1)
    print(f"OK: 正式版 v{const(code['正式版'], 'APP_VERSION')} と ベータ版 v{const(code['ベータ版'], 'APP_VERSION')} ベータ{const(code['ベータ版'], 'BETA_BUILD')} の"
          "違いは、BETA_REVIEW / APP_VERSION / BETA_BUILD の行だけです")


if __name__ == "__main__":
    main()

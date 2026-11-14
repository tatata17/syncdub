#!/usr/bin/env python3
"""回帰チェックをコマンドから動かす(ベータ版のメニュー「記録と比べる」と同じ処理)。

  python tools/regress.py [ベータ版のあるフォルダ]

記録(syncdub_記録 フォルダ)の素材を、いまの beta/syncdub_beta.pyw でやり直して比べ、結果を表示する。
フォルダを省くと、beta/ を使う。記録と素材は利用者のPCにしかないので、リポジトリだけでは動かない。
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    src = ROOT / "beta" / "syncdub_beta.pyw"
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else src.parent
    loader = SourceFileLoader("syncdub_beta", str(src))
    spec = importlib.util.spec_from_loader("syncdub_beta", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    print(mod.run_regress(base, say=lambda t: print(t, file=sys.stderr)))


if __name__ == "__main__":
    main()

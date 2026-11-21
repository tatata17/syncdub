#!/usr/bin/env python3
"""実物の素材なしで回せる、答えの分かっている自動テスト。

  python tools/selftest.py            … 全部のケース
  python tools/selftest.py 2 5        … 番号を指定したケースだけ
  python tools/selftest.py --official … syncdub.pyw (正式版) を試す。省くと beta/syncdub_beta.pyw

人工の音(場面ごとに違う雑音と和音)で英語版と日本語版の組を作り、「かんたん」の自動合わせ(音)にかけて、
できた区間が仕込んだ答えと合っているかを確かめる。NG が1つでもあれば、終了コードは 1
(いまは通らないと分かっているケース KNOWN_NG は「既知のNG」と表示して、数えない)。
人工の音は本物の吹き替えとは性質が違うので、これが通っても実物で合うとは限らない。
実物の素材での回帰チェック(tools/regress.py)の代わりではなく、併用する。
"""
import importlib.util
import subprocess
import sys
import tempfile
import time
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SR = 16000
TOL = 0.05        # 区間のずれの許容差(秒)。本体の自動チェックが「ずれ」とみなす 0.1秒の半分
MARGIN = 1.5      # 境目の前後は、この秒数だけ採点しない(境目をどこに置くかは、音の切れ目しだいで動くため)
COVER = 0.9       # 合うはずの所のうち、区間に入っているべき割合


def sound(rng, dur):
    """場面ごとに違う「番組らしい音」。0.2〜1.2秒ごとに雑音か和音が変わり、ときどき無音が入る。"""
    n = int(dur * SR)
    x = np.zeros(n)
    t = 0
    while t < n:
        m = min(int(rng.uniform(0.2, 1.2) * SR), n - t)
        kind = rng.random()
        tt = np.arange(m) / SR
        if kind < 0.12:
            ev = np.zeros(m)                                   # 無音
        elif kind < 0.55:
            ev = rng.standard_normal(m)                        # 帯域のちがう雑音(効果音のかわり)
            k = int(rng.integers(2, 40))
            ev = np.convolve(ev, np.ones(k) / k, mode="same") * np.sqrt(k)
            ev *= np.exp(-tt * rng.uniform(0.5, 6))
        else:
            ev = sum(np.sin(2 * np.pi * f * tt + rng.uniform(0, 6)) for f in rng.uniform(150, 2500, 3))   # 和音(曲のかわり)
            ev *= 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(1, 6) * tt) ** 2
        fade = min(160, m // 2)
        if fade:
            ev[:fade] *= np.linspace(0, 1, fade)
            ev[-fade:] *= np.linspace(1, 0, fade)
        x[t:t + m] = ev * rng.uniform(0.3, 1.0)
        t += m
    return x


def stretch(x, r):
    """長さを r 倍にする(r > 1 で遅くなる = あとの方ほど遅れる)。"""
    n = int(len(x) * r)
    return np.interp(np.arange(n) / r, np.arange(len(x)), x)


def write_media(path, x):
    """音を wav にして、黒い映像を付けた mkv にする。"""
    wav = str(path) + ".wav"
    pk = np.abs(x).max() or 1.0
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x / pk * 0.7 * 32767).astype("<i2").tobytes())
    dur = len(x) / SR
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black:s=160x90:r=30000/1001:d={dur:.3f}",
                        "-i", wav, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(path)],
                       capture_output=True, text=True)
    Path(wav).unlink(missing_ok=True)
    if r.returncode:
        raise RuntimeError("ffmpeg で素材を作れませんでした: " + r.stderr[-300:])


def cases():
    """[(番号, 名前, 作り方)]。作り方は (英語の音, 日本語の音, 答え, 区間の数[, 境目の前後で採点しない秒数]) を返す。
    答え = [(英語の開始, 英語の終了, 日本語の開始, 速さ)] … 英語の時刻 t は、日本語の 開始 + (t − 英語の開始) × 速さ に当たる。"""
    def mk(seed):
        rng = np.random.default_rng(seed)
        prog = lambda d: sound(rng, d)                            # noqa: E731  両方に入る音(曲・効果音)
        both = lambda p: p + 0.3 * voice(rng, len(p))             # noqa: E731  その言語だけの「声」を重ねる
        return rng, prog, both

    def voice(rng, n):
        """言語ごとに違う「声」のかわり(英語版と日本語版で一致しない音)。"""
        v = rng.standard_normal(n)
        return np.convolve(v, np.ones(12) / 12, mode="same") * (np.sin(2 * np.pi * 3.1 * np.arange(n) / SR + rng.uniform(0, 6)) > 0.2)

    def c1():
        rng, prog, both = mk(1)
        a = prog(60)
        return both(a), np.concatenate([sound(rng, 5), both(a)]), [(0, 60, 5.0, 1.0)], 1

    def c2():
        rng, prog, both = mk(2)
        a, b = prog(40), prog(40)
        return (np.concatenate([both(a), both(b)]), np.concatenate([sound(rng, 5), both(a), sound(rng, 30), both(b)]),
                [(0, 40, 5.0, 1.0), (40, 80, 75.0, 1.0)], 2)

    def c3():
        rng, prog, both = mk(3)
        a, b = prog(40), prog(40)
        return (np.concatenate([both(a), sound(rng, 8), both(b)]), np.concatenate([sound(rng, 5), both(a), both(b)]),
                [(0, 40, 5.0, 1.0), (48, 88, 45.0, 1.0)], 2)

    def c4():
        rng, prog, both = mk(4)
        a = prog(120)
        r = 1.001      # 日本語側が 0.1% 遅い(2分で 0.12秒ずれる)
        return both(a), np.concatenate([sound(rng, 5), stretch(both(a), r)]), [(0, 120, 5.0, r)], None

    def c5():
        rng, prog, both = mk(5)
        a, b = prog(40), prog(40)
        return (np.concatenate([both(a), both(b)]), np.concatenate([sound(rng, 5), both(a), sound(rng, 8), both(b)]),
                [(0, 40, 5.0, 1.0), (40, 80, 53.0, 1.0)], 2)
    def c6():      # 日本語版で、場面が2か所カットされている
        rng, prog, both = mk(6)
        a, b, c = prog(30), prog(30), prog(30)
        return (np.concatenate([both(a), sound(rng, 6), both(b), sound(rng, 10), both(c)]),
                np.concatenate([sound(rng, 5), both(a), both(b), both(c)]),
                [(0, 30, 5.0, 1.0), (36, 66, 35.0, 1.0), (76, 106, 65.0, 1.0)], 3)

    def c7():      # 途中の15秒は、どちらも「声」だけ(合う音がない)。場面は続いているので、切り出してはいけない
        rng, prog, both = mk(7)
        a = prog(70)
        a[int(30 * SR):int(45 * SR)] = 0
        return both(a), np.concatenate([sound(rng, 5), both(a)]), [(0, 70, 5.0, 1.0)], 1

    def c8():      # 音が鳴り続けている途中で、英語側だけ 0.4秒長い。無音をはさまず、前後を少し伸ばしてつなぐ(区間は 3つ)
        rng, prog, both = mk(8)
        n = int(90 * SR)
        t = np.arange(n) / SR
        a = prog(90) + 0.4 * np.sin(2 * np.pi * 220 * t) * (1 + 0.5 * np.sin(2 * np.pi * 0.7 * t)) + 0.15 * rng.standard_normal(n)
        k = int(45 * SR)
        extra = a[k - int(0.4 * SR):k][::-1]
        return (np.concatenate([both(a[:k]), extra, both(a[k:])]), np.concatenate([sound(rng, 5), both(a)]),
                [(0, 45, 5.0, 1.0), (45.4, 90.4, 50.0, 1.0)], 3, 9.0)

    PAL = 25.0 / (24000.0 / 1001.0)

    def c9():      # 英語版が PAL版(約4%速い)。日本語側に CM (30秒)。答えの英語の時刻は 1/PAL 倍、日本語は PAL 倍の速さで進む
        en_x, jp_x, truth, _n = c2()
        return (stretch(en_x, 1 / PAL), jp_x, [(s / PAL, e / PAL, j, PAL) for s, e, j, _r in truth], 2, MARGIN, PAL)

    def c10():     # 日本語の録画が PAL版(約4%速い)。日本語側に CM
        en_x, jp_x, truth, _n = c2()
        return (en_x, stretch(jp_x, 1 / PAL), [(s, e, j / PAL, 1 / PAL) for s, e, j, _r in truth], 2, MARGIN, 1 / PAL)

    return [(1, "一定のずれだけ", c1), (2, "日本語側だけ CM (30秒) が入る", c2), (3, "英語側だけの場面 (8秒)", c3),
            (4, "後半ほどずれる (0.1% のドリフト)", c4), (5, "12秒未満の短い CM (8秒)", c5),
            (6, "英語側だけの場面が2か所 (6秒と10秒)", c6), (7, "途中が声だけの場面 (15秒。切り出さない)", c7),
            (8, "音の途中で、英語側だけ 0.4秒長い (伸ばしてつなぐ)", c8),
            (9, "英語版が PAL版 (約4%速い)", c9), (10, "日本語の録画が PAL版 (約4%速い)", c10)]


# いまの自動合わせでは通らないと分かっているケースの番号。NG でも終了コードには数えない(通るようになったら、ここから外す)。
KNOWN_NG = set()


def judge(segs, truth, n_exp, margin=MARGIN):
    """できた区間を答えと比べる。戻り値 (合格か, 説明の行のリスト)。"""
    out, ok = [], True
    segs = [s for s in segs if s.get("jp_start") is not None]

    def got(t):
        for s in segs:
            if s["en_start"] <= t < s["en_end"]:
                r = 1.0 if s.get("jp_end") is None else (s["jp_end"] - s["jp_start"]) / (s["en_end"] - s["en_start"])
                return s["jp_start"] + (t - s["en_start"]) * r
        return None
    worst, n_in, n_cov, bad_cov = 0.0, 0, 0, 0
    en_end = max(e for _s, e, _j, _r in truth)
    for t in np.arange(0.25, en_end, 0.5):
        tr = next(((s, e, j, r) for s, e, j, r in truth if s <= t < e), None)
        g = got(t)
        if tr is None:                       # 日本語版にない所: 区間に入っていてはいけない(境目の近くは見ない)
            if g is not None and all(abs(t - x) > margin for s, e, _j, _r in truth for x in (s, e)):
                bad_cov += 1
            continue
        s, e, j, r = tr
        if t - s < margin or e - t < margin:
            continue
        n_in += 1
        if g is not None:
            n_cov += 1
            worst = max(worst, abs(g - (j + (t - s) * r)))
    cov = n_cov / max(n_in, 1)
    out.append(f"    ずれの最大 {worst:.3f}秒 (許容 {TOL})  /  合うはずの所のうち区間に入った割合 {cov:.0%} (必要 {COVER:.0%})  /  区間 {len(segs)} 個"
               + (f" (答え {n_exp})" if n_exp else ""))
    if worst > TOL:
        ok = False
        out.append("    NG: ずれが許容差を超えています")
    if cov < COVER:
        ok = False
        out.append("    NG: 合うはずの所が、区間に入っていません")
    if bad_cov:
        ok = False
        out.append(f"    NG: 日本語版にない所が、区間に入っています ({bad_cov * 0.5:.1f}秒ぶん)")
    if n_exp and len(segs) != n_exp:
        ok = False
        out.append("    NG: 区間の数が答えと違います")
    if not ok:
        for s in segs:
            out.append(f"      英語 {s['en_start']:.2f}〜{s['en_end']:.2f} → 日本語 {s['jp_start']:.2f}"
                       + ("" if s.get("jp_end") is None else f"〜{s['jp_end']:.2f}"))
    return ok, out


def main():
    args = sys.argv[1:]
    official = "--official" in args
    want = {int(a) for a in args if a.isdigit()}
    src = ROOT / ("syncdub.pyw" if official else "beta/syncdub_beta.pyw")
    loader = SourceFileLoader("syncdub_selftest", str(src))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("syncdub_selftest", loader))
    loader.exec_module(mod)
    print(f"{src.relative_to(ROOT)}  v{mod.APP_VERSION}" + (f" ベータ{mod.BETA_BUILD}" if getattr(mod, "BETA_REVIEW", False) else ""))
    tmp = Path(tempfile.mkdtemp(prefix="syncdub_selftest_"))
    ng, fixed, t_all = 0, 0, time.time()
    try:
        for num, name, make in cases():
            if want and num not in want:
                continue
            t0 = time.time()
            try:
                en_x, jp_x, truth, n_exp, *more = make()
                pe, pj = tmp / f"en{num}.mkv", tmp / f"jp{num}.mkv"
                write_media(pe, en_x)
                write_media(pj, jp_x)
                if len(more) > 1:       # 速さの違う版: 速さの割合を指定して合わせる
                    segs, _info = mod.align_speed(mod.Media(str(pe)), mod.Media(str(pj)), False, more[1])
                else:
                    segs = mod.align_for_record(mod.Media(str(pe)), mod.Media(str(pj)), "音")
                ok, lines = judge(segs, truth, n_exp, *more[:1])
            except Exception as e:          # 合わせられなかった・素材を作れなかった、も NG として数える
                ok, lines = False, [f"    NG: {type(e).__name__}: {e}"]
            known = num in KNOWN_NG
            ng += (not ok) and not known
            fixed += ok and known
            print(f"[{'OK' if ok else '既知のNG' if known else 'NG'}] {num}. {name}  ({time.time() - t0:.1f}秒)"
                  + ("  ← 通るようになりました。KNOWN_NG から外してください" if ok and known else ""))
            print("\n".join(lines))
    finally:                                # 一時ファイルは、成功・失敗にかかわらず消す
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()
    print(f"{'全部 OK' if not ng else f'NG が {ng} 件'}  (合計 {time.time() - t_all:.1f}秒)")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""syncdub: 日本語吹き替え音声を英語版映像に区間指定で合わせるツール (単体ファイル版)。syncdub GUI: 日本語吹き替え音声を英語版映像に区間指定で合わせるツール。

操作の流れ
  1. 英語版映像と日本語録画を指定して「読み込み」
  2. 上段=英語版 / 下段=日本語録画 の波形。クリックでカーソル移動、
     ホイール=ズーム、Shift+ホイール or 右ドラッグ=横移動
  3. 英語側と日本語側で「同じ場面」にカーソルを置く(←→でフレーム単位、Shiftで1秒、Ctrlで10フレーム)
     「A 自動吸着」で英語側カーソル位置の音に日本語側カーソルを自動で合わせられる
  4. 「S 開始点」→ 区間の終わりで同様に合わせて「E 終了点」で区間を追加
     (CMなど日本語側だけにある部分は、どの区間にも入れなければ消える)
  5. 「区間を検証」でズレを確認 → 「合成」
キー: S 開始点 / E 終了点 / A 自動吸着 / Space 再生 / ←→ フレーム移動 / Tab 英語⇔日本語の切替

要件: Python 3 + numpy / ffmpeg・ffprobe・ffplay (PATH)
"""
import base64
import contextlib
import os
import urllib.request
import io
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # ドラッグ&ドロップ対応(任意): pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TkBase = TkinterDnD.Tk
except Exception:  # noqa: BLE001
    DND_FILES, _TkBase = None, tk.Tk

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 時刻の解釈


def parse_time(v, fps=None):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.fullmatch(r"(\d+)f", s)
    if m:
        if not fps:
            raise ValueError(f"フレーム指定 '{s}' には fps が必要です")
        return int(m.group(1)) / fps
    parts = s.split(":")
    if len(parts) == 4:  # HH:MM:SS:FF
        if not fps:
            raise ValueError(f"'{s}' には fps が必要です")
        h, mi, se, ff = parts
        return int(h) * 3600 + int(mi) * 60 + int(se) + int(ff) / fps
    if len(parts) in (2, 3):
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    return float(s)


# ---------------------------------------------------------------- ffprobe


def run(cmd, **kw):
    if sys.platform == "win32":  # .pyw から呼んでもコンソールが出ないように
        kw.setdefault("creationflags", 0x08000000)
    # 日本語Windowsの既定(cp932)で読むと文字化けするのでUTF-8固定
    return subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", **kw)


def probe(path):
    # ファイル名やタグ(日本語・記号を含みやすい)は取得せず、必要な数値項目だけ読む
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "format=start_time,duration:stream=index,codec_type,codec_name,channels,sample_rate,"
               "start_time,avg_frame_rate", "-of", "json", str(path)]).stdout
    try:
        return json.loads(out)
    except ValueError:  # 念のため: 不正なバックスラッシュを無害化して再試行
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", out))


def fmt_start(info):
    return float(info["format"].get("start_time") or 0.0)


def fmt_duration(info):
    return float(info["format"].get("duration") or 0.0)


def video_start_offset(info):
    """映像の最初のフレーム時刻 - コンテナ開始時刻。時刻指定の基準(=0)を映像の先頭に置くための補正量。"""
    for st in info["streams"]:
        if st["codec_type"] == "video" and st.get("start_time") not in (None, "N/A"):
            return max(0.0, float(st["start_time"]) - fmt_start(info))
    return 0.0


def audio_streams(info):
    return [s for s in info["streams"] if s["codec_type"] == "audio"]


def video_fps(info):
    for s in info["streams"]:
        if s["codec_type"] == "video":
            n, d = s.get("avg_frame_rate", "0/1").split("/")
            if int(d):
                return int(n) / int(d)
    return None


# ---------------------------------------------------------------- 区間の正規化


class Seg:
    def __init__(self, en_start, en_end, jp_start=None, jp_end=None, fit="stretch", note=""):
        self.en_start, self.en_end = en_start, en_end
        self.jp_start, self.jp_end = jp_start, jp_end
        self.fit, self.note = fit, note

    @property
    def silent(self):
        return self.jp_start is None

    @property
    def dur(self):
        return self.en_end - self.en_start


def load_segments(proj, en_dur, en_fps, jp_fps):
    segs = []
    for i, d in enumerate(proj["segments"]):
        t = d.get("type", "match")
        es = parse_time(d["en_start"], en_fps)
        ee = parse_time(d["en_end"], en_fps) if "en_end" in d else None
        if t == "silence":
            if ee is None:
                raise ValueError(f"segments[{i}]: silence には en_end が必要")
            segs.append(Seg(es, ee, note=d.get("note", "")))
            continue
        js = parse_time(d["jp_start"], jp_fps)
        je = parse_time(d["jp_end"], jp_fps) if "jp_end" in d else None
        if ee is None and je is None:
            raise ValueError(f"segments[{i}]: en_end か jp_end のどちらかが必要")
        if ee is None:
            ee = es + (je - js)
        fit = d.get("fit", "stretch")
        if fit not in ("stretch", "pad", "trim"):
            raise ValueError(f"segments[{i}]: fit は stretch/pad/trim")
        segs.append(Seg(es, ee, js, je, fit, d.get("note", "")))
    segs.sort(key=lambda s: s.en_start)
    for a, b in zip(segs, segs[1:]):
        if b.en_start < a.en_end - 1e-6:
            raise ValueError(f"区間が重なっています: {a.en_start}-{a.en_end} と {b.en_start}-{b.en_end}")
    for s in segs:
        if s.dur <= 0:
            raise ValueError(f"区間の長さが0以下: {s.en_start}-{s.en_end}")
    # 隙間と末尾を無音で埋める
    full, t = [], 0.0
    for s in segs:
        if s.en_start > t + 1e-6:
            full.append(Seg(t, s.en_start, note="(自動)隙間"))
        full.append(s)
        t = s.en_end
    if en_dur and en_dur > t + 1e-6:
        full.append(Seg(t, en_dur, note="(自動)末尾"))
    return full


# ---------------------------------------------------------------- フィルタ生成


def atempo_chain(r):
    """atempo は 0.5〜2.0 のみ。範囲外は連結。"""
    parts = []
    while r > 2.0:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5")
        r /= 0.5
    parts.append(f"atempo={r:.9f}")
    return ",".join(parts)


def build_filter(segs, jp_off, a_index, sr, layout, fade, in_idx=0, lead=0.0):
    fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={layout}"
    nsrc = sum(1 for s in segs if not s.silent)
    lines, labels, k = [], [], 0
    if nsrc:
        outs = "".join(f"[s{i}]" for i in range(nsrc))
        lines.append(f"[{in_idx}:a:{a_index}]{fmt},asplit={nsrc}{outs}" if nsrc > 1
                     else f"[{in_idx}:a:{a_index}]{fmt}[s0]")
    warns = []
    for i, s in enumerate(segs):
        D = s.dur
        f = min(fade, D / 2)
        if s.silent:
            lines.append(f"anullsrc=r={sr}:cl={layout},atrim=0:{D:.6f},asetpts=PTS-STARTPTS[o{i}]")
        else:
            # ffmpegは入力のタイムスタンプをコンテナ開始=0に正規化する。
            # 指定時刻は映像の先頭=0 なので、その差(jp_off)を足す。
            js = s.jp_start + jp_off
            je = (s.jp_end + jp_off) if s.jp_end is not None else js + D
            chain = [f"atrim={js:.6f}:{je:.6f}", "asetpts=PTS-STARTPTS"]
            jdur = je - js
            if s.fit == "stretch" and abs(jdur - D) > 1e-6:
                r = jdur / D
                if abs(r - 1) > 0.05:
                    warns.append(f"区間{i} ({s.en_start:.3f}-{s.en_end:.3f}): 速度比 {r:.3f} は大きい")
                chain.append(atempo_chain(r))
            chain.append(f"apad=whole_dur={D:.6f}")
            chain.append(f"atrim=0:{D:.6f}")
            chain.append("asetpts=PTS-STARTPTS")
            if f > 0:
                chain.append(f"afade=t=in:d={f:.4f}")
                chain.append(f"afade=t=out:st={D - f:.6f}:d={f:.4f}")
            lines.append(f"[s{k}]" + ",".join(chain) + f"[o{i}]")
            k += 1
        labels.append(f"[o{i}]")
    if lead > 1e-6:  # 英語版の映像先頭がコンテナ開始より遅い分だけ、先頭に無音を置く
        lines.append(f"anullsrc=r={sr}:cl={layout},atrim=0:{lead:.6f},asetpts=PTS-STARTPTS[olead]")
        labels.insert(0, "[olead]")
    lines.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    return ";\n".join(lines), warns


# ---------------------------------------------------------------- メイン


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--dry-run", action="store_true", help="ffmpegを実行せずコマンドを表示")
    ap.add_argument("--probe", action="store_true", help="入力の開始時刻などを表示して終了")
    ap.add_argument("--audio-only", metavar="FILE", help="音声のみ(wav/flac等)で出力して終了")
    a = ap.parse_args(argv)

    pp = Path(a.project)
    proj = json.loads(pp.read_text(encoding="utf-8"))
    base = pp.parent
    res = lambda p: str((base / p).resolve()) if p else None

    en_path, jp_path = res(proj["en_video"]), res(proj["jp_media"])
    en, jp = probe(en_path), probe(jp_path)
    en_start, jp_start = fmt_start(en), fmt_start(jp)
    en_dur = fmt_duration(en)

    if a.probe:
        for name, info, p in (("EN", en, en_path), ("JP", jp, jp_path)):
            print(f"[{name}] {p}\n  開始時刻 {fmt_start(info):.6f}s  長さ {fmt_duration(info):.3f}s  fps {video_fps(info)}")
            for i, s in enumerate(audio_streams(info)):
                print(f"  audio#{i}: {s.get('codec_name')} {s.get('channels')}ch {s.get('sample_rate')}Hz start={s.get('start_time')}")
        return

    en_fps = proj.get("en_fps") or video_fps(en)
    jp_fps = proj.get("jp_fps") or video_fps(jp)
    segs = load_segments(proj, en_dur, en_fps, jp_fps)

    sr = int(proj.get("sample_rate", 48000))
    layout = proj.get("channel_layout", "stereo")
    fade = float(proj.get("fade_ms", 5)) / 1000
    jp_a = int(proj.get("jp_audio_stream", 0))
    jp_off, en_off = video_start_offset(jp), video_start_offset(en)
    single = bool(a.audio_only)
    filt, warns = build_filter(segs, jp_off, jp_a, sr, layout, fade,
                               in_idx=0 if single else 1, lead=0.0 if single else en_off)

    print(f"EN映像先頭 +{en_off:.3f}s / JP映像先頭 +{jp_off:.3f}s (コンテナ開始比) / EN長さ {en_dur:.3f}s / 区間 {len(segs)}")
    for i, s in enumerate(segs):
        if s.silent:
            desc = "無音"
        else:
            je = f"{s.jp_end:.3f}" if s.jp_end is not None else "-"
            desc = f"JP {s.jp_start:.3f}-{je} fit={s.fit}"
        print(f"  [{i:2d}] EN {s.en_start:9.3f}-{s.en_end:9.3f}  {desc}  {s.note}")
    for w in warns:
        print("警告:", w, file=sys.stderr)

    script = base / (pp.stem + ".filter.txt")
    script.write_text(filt, encoding="utf-8")

    if a.audio_only:
        cmd = ["ffmpeg", "-y", "-i", jp_path, "-filter_complex_script", str(script),
               "-map", "[aout]", a.audio_only]
    else:
        out = res(proj["output"])
        codec = proj.get("audio_codec", "aac")
        bitrate = proj.get("audio_bitrate", "256k")
        keep_en = proj.get("keep_en_audio", True)
        cmd = ["ffmpeg", "-y", "-i", en_path, "-i", jp_path, "-filter_complex_script", str(script),
               "-map", "0:v:0", "-map", "[aout]"]
        if keep_en:
            cmd += ["-map", "0:a:0"]
        cmd += ["-c:v", "copy", "-c:a:0", codec]
        if codec in ("aac", "ac3", "eac3", "libopus"):
            cmd += ["-b:a:0", bitrate]
        if keep_en:
            cmd += ["-c:a:1", "copy"]
        cmd += ["-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese dub (synced)",
                "-disposition:a:0", "default"]
        if keep_en:
            cmd += ["-metadata:s:a:1", "language=eng", "-disposition:a:1", "0"]
        cmd += [out]
        if a.dry_run:
            print("\n$", " ".join(cmd))
            return
        run(cmd)
        print("完了:", out)
        return

    if a.dry_run:
        print("\n$", " ".join(cmd))
        return
    run(cmd)
    print("完了:", a.audio_only)


import numpy as np


APP_VERSION = "2026.11.07.5"
REPO = "tatata17/syncdub"
APP_FILE = "syncdub.pyw"
KEEP_BACKUPS = 5
TEST_BASE = os.environ.get("SYNCDUB_TEST_BASE")  # 試験用: ローカルサーバのURL
SR = 8000          # 波形・解析用のサンプルレート
PYR = 64           # 波形ピラミッドの縮約率
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
BAND_COLORS = ["#4c8bf5", "#e8833a", "#3aa76d", "#b455d6", "#d4a017", "#d9534f"]


# ------------------------------------------------------------------ GitHub自己更新
# 公開リポジトリ main の version.json {"version": "...", "file": "syncdub.pyw", "notes": ""} を見に行く。
# 配信キャッシュで版が食い違わないよう、最新コミットIDを調べてコミット固定URLから取得する。


def _http(url, accept=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "syncdub-updater", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def fetch_update_info():
    """戻り値: (info, ref)。ref はコミットID(取れなければ 'main')。"""
    if TEST_BASE:
        return json.loads(_http(f"{TEST_BASE}/version.json")), ""
    ref = "main"
    try:
        sha = _http(f"https://api.github.com/repos/{REPO}/commits/main",
                    accept="application/vnd.github.sha").strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            ref = sha
    except Exception:  # noqa: BLE001  (API制限などは main にフォールバック)
        pass
    return json.loads(_http(f"https://raw.githubusercontent.com/{REPO}/{ref}/version.json")), ref


def download_update_source(info, ref):
    base = TEST_BASE or f"https://raw.githubusercontent.com/{REPO}/{ref}"
    code = _http(f"{base}/{info.get('file', APP_FILE)}")
    m = re.search(r'^APP_VERSION = "([^"]+)"', code, re.M)
    if not m or m.group(1) != info["version"]:
        raise ValueError(f"版番号が一致しません (version.json {info['version']} / 本体 {m.group(1) if m else '?'})。"
                         "公開直後の可能性があります。少し待って再試行してください")
    compile(code, APP_FILE, "exec")  # 壊れたファイルで上書きしないための構文チェック
    return code


def install_update(code, me):
    bdir = me.parent / "syncdub_backups"
    bdir.mkdir(exist_ok=True)
    (bdir / f"syncdub_{APP_VERSION}.pyw").write_text(me.read_text(encoding="utf-8"), encoding="utf-8")
    for old in sorted(bdir.glob("syncdub_*.pyw"), key=lambda f: vkey(f.stem))[:-KEEP_BACKUPS]:
        old.unlink()
    tmp = me.with_suffix(me.suffix + ".new")
    tmp.write_text(code, encoding="utf-8")
    os.replace(tmp, me)


def latest_backup(me):
    files = sorted((me.parent / "syncdub_backups").glob("syncdub_*.pyw"), key=lambda f: vkey(f.stem))
    return files[-1] if files else None


# ------------------------------------------------------------------ 時刻の書式


def tc(t):
    sign = "-" if t < 0 else ""
    t = abs(t)
    return f"{sign}{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def parse_tc(text, fps):
    text = text.strip()
    neg = text.startswith("-")
    v = parse_time(text.lstrip("-"), fps)
    return -v if neg else v


# ------------------------------------------------------------------ メディア


class Media:
    def __init__(self, path, astream=0):
        self.path = path
        self.info = probe(path)
        self.cstart = fmt_start(self.info)
        self.voff = video_start_offset(self.info)
        self.fps = video_fps(self.info) or 29.97
        auds = audio_streams(self.info)
        if not auds:
            raise RuntimeError("音声ストリームがありません")
        self.astream = min(astream, len(auds) - 1)
        st = auds[self.astream].get("start_time")
        ast = float(st) if st not in (None, "N/A") else self.cstart
        self.a_origin = (ast - self.cstart) - self.voff  # 音声先頭の時刻(映像先頭=0基準)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{self.astream}", "-vn",
             "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            capture_output=True, check=True, **NOWIN).stdout
        self.x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        n = len(self.x) // PYR
        blk = self.x[: n * PYR].reshape(n, PYR)
        self.mn, self.mx = blk.min(axis=1), blk.max(axis=1)
        self.scale = float(np.percentile(np.abs(self.x[::7]), 99.8)) + 1e-6
        self.end = max(self.a_origin + len(self.x) / SR, fmt_duration(self.info) - self.voff)

    # --- 波形 ---
    def slice(self, t0, t1):
        i0 = int(round((t0 - self.a_origin) * SR))
        n = int(round((t1 - t0) * SR))
        out = np.zeros(max(n, 0), np.float32)
        a, b = max(i0, 0), min(i0 + n, len(self.x))
        if b > a:
            out[a - i0: b - i0] = self.x[a:b]
        return out

    def envelope(self, t0, t1, W):
        """表示範囲(t0,t1)をW画素に畳んだ (列番号, 最小, 最大) または折れ線 (xs, ys)。"""
        i0 = (t0 - self.a_origin) * SR
        span = (t1 - t0) * SR
        if span / W < 1.5:  # 十分拡大 → 生サンプルの折れ線
            a, b = max(int(i0), 0), min(int(i0 + span) + 1, len(self.x))
            if b - a < 2:
                return None
            idx = np.arange(a, b)
            xs = (idx - i0) / span * W
            return ("line", xs, self.x[a:b])
        edges = (i0 + np.arange(W + 1) * span / W).astype(np.int64)
        if span / W >= 2 * PYR:
            src_mn, src_mx, edges = self.mn, self.mx, edges // PYR
        else:
            src_mn, src_mx = self.x, self.x
        L = len(src_mn)
        e0, e1 = edges[:-1], edges[1:]
        valid = (e0 >= 0) & (e1 < L) & (e1 > e0)
        cols = np.where(valid)[0]
        if len(cols) == 0:
            return None
        pairs = np.stack([e0[cols], e1[cols]], 1).ravel()
        mn = np.minimum.reduceat(src_mn, pairs)[::2]
        mx = np.maximum.reduceat(src_mx, pairs)[::2]
        return ("env", cols, mn, mx)

    # --- フレーム ---
    def frame_png(self, t, width=320):
        ts = max(0.0, t + self.voff)
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{ts:.4f}", "-i", self.path, "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, **NOWIN)
        return r.stdout


def xcorr_find(ref, ref_t, tgt, tgt_t, win=8.0, search=60.0):
    """ref の [ref_t, ref_t+win] に最も似た音を tgt の tgt_t±search から探す。
    戻り値: (見つかった時刻, 正規化相関, 突出度z)"""
    a = ref.slice(ref_t, ref_t + win).astype(np.float64)
    if len(a) < SR or np.abs(a).max() < 1e-4:
        return None
    a = a - a.mean()
    s0 = tgt_t - search
    b = tgt.slice(s0, tgt_t + search + win).astype(np.float64)
    n = len(a)
    if len(b) <= n:
        return None
    N = 1 << int(np.ceil(np.log2(len(b) + n)))
    c = np.fft.irfft(np.fft.rfft(b, N) * np.conj(np.fft.rfft(a, N)), N)[: len(b) - n + 1]
    e = np.concatenate([[0], np.cumsum(b ** 2)])
    loc = np.sqrt(np.maximum(e[n:] - e[:-n], 0) + 1e-9) * np.sqrt((a ** 2).sum())
    nc = c / loc
    k = int(nc.argmax())
    z = (nc[k] - nc.mean()) / (nc.std() + 1e-9)
    return s0 + k / SR, float(nc[k]), float(z)


# ------------------------------------------------------------------ 自動検出

def _nc_local(a, b):
    """a(基準) と b(探索範囲) の正規化相関を全ラグで返す。長さ len(b)-len(a)+1。"""
    n, m = len(a), len(b)
    N = 1 << int(np.ceil(np.log2(m + n)))
    c = np.fft.irfft(np.fft.rfft(b, N) * np.conj(np.fft.rfft(a, N)), N)[: m - n + 1]
    e = np.concatenate([[0], np.cumsum(b.astype(np.float64) ** 2)])
    loc = np.sqrt(np.maximum(e[n:] - e[:-n], 0) + 1e-9) * np.sqrt((a.astype(np.float64) ** 2).sum() + 1e-12)
    return c / loc


def auto_align(en, jp, progress=lambda f, m: None):
    """英語版(en)の各時刻に対応する日本語録画(jp)の時刻を自動で見つけ、区間リストを返す。

    戻り値: (segs, info)
      segs: [{"en_start","en_end","jp_start","jp_end"(等速ならNone),"fit","note","lag","conf","flag"}]
      info: {"unused_jp": [(a,b)...], "ok": bool, "msg": str}
    """
    def dec(m):  # 4kHzに間引き(解析用)
        x = m.x
        n = len(x) // 2 * 2
        return x[:n].reshape(-1, 2).mean(1).astype(np.float64)
    fs = SR / 2
    ex, jx = dec(en), dec(jp)
    eo, jo = en.a_origin, jp.a_origin
    j_dur = len(jx) / fs
    t0e = max(eo, 0.0)           # 英語側の解析開始(映像の先頭)
    e_end = eo + len(ex) / fs    # 英語側の終端
    e_dur = e_end - t0e

    def esl(t, L):  # 英語側の切り出し (時刻→サンプル)
        i = int(round((t - eo) * fs))
        n = int(round(L * fs))
        out = np.zeros(n)
        a, b = max(i, 0), min(i + n, len(ex))
        if b > a:
            out[a - i: b - i] = ex[a:b]
        return out

    def jsl(t, L):
        i = int(round((t - jo) * fs))
        n = int(round(L * fs))
        out = np.zeros(n)
        a, b = max(i, 0), min(i + n, len(jx))
        if b > a:
            out[a - i: b - i] = jx[a:b]
        return out

    # ---- 英語側の「音がある所」の目安
    hop_rms = int(fs)
    nr = len(ex) // hop_rms
    rms_all = np.sqrt((ex[: nr * hop_rms].reshape(nr, hop_rms) ** 2).mean(1)) if nr else np.array([0.0])
    rms_thr = max(0.1 * float(np.percentile(rms_all, 75)), 1e-4)

    def loud(a):
        return float(np.sqrt((a ** 2).mean())) >= rms_thr

    # ---- 1) 全体から「ありそうなオフセット(日本語時刻-英語時刻)」を集める
    progress(0.02, "全体の音を照合しています…")
    W = 8.0
    n = int(W * fs)
    K = int(min(28, max(6, e_dur / 25)))
    N = 1 << int(np.ceil(np.log2(len(jx) + n)))
    FJ = np.fft.rfft(jx, N)
    ej = np.concatenate([[0], np.cumsum(jx ** 2)])
    cand = []
    starts = np.linspace(t0e, max(e_end - W, t0e), K)
    for q, t in enumerate(starts):
        a = esl(t, W)
        if not loud(a):
            continue
        a0 = a - a.mean()
        c = np.fft.irfft(FJ * np.conj(np.fft.rfft(a0, N)), N)[: len(jx) - n + 1]
        loc = np.sqrt(np.maximum(ej[n:] - ej[:-n], 0) + 1e-9) * np.sqrt((a0 ** 2).sum())
        nc = c / loc
        mu, sd = nc.mean(), nc.std() + 1e-9
        for _ in range(2):
            k = int(nc.argmax())
            z = (nc[k] - mu) / sd
            if z >= 8:
                cand.append(((jo + k / fs) - t, z))
            lo, hi = max(0, k - int(fs)), min(len(nc), k + int(fs))
            nc = nc.copy()
            nc[lo:hi] = mu
        progress(0.02 + 0.28 * (q + 1) / K, "全体の音を照合しています…")
    del FJ
    if not cand:
        return [], {"ok": False, "unused_jp": [],
                    "msg": "音が一致する部分が見つかりませんでした。別の音声番号を試すか、「手動調整」タブをお使いください。"}
    cand.sort()
    clusters, cur = [], [cand[0]]
    for o, z in cand[1:]:
        if o - cur[-1][0] <= 0.15:
            cur.append((o, z))
        else:
            clusters.append(cur)
            cur = [(o, z)]
    clusters.append(cur)
    offs = [float(np.median([o for o, _ in c])) for c in clusters]

    # ---- 2) 英語側を窓で区切り、各窓がどのオフセットに合うか判定
    WL, HOP, RAD = 5.0, 2.0, 0.5
    nw = int(WL * fs)
    wts = np.arange(t0e, max(e_end - WL, t0e) + 1e-9, HOP)
    lab = np.full(len(wts), -1)
    off_i = np.zeros(len(wts))
    z_i = np.zeros(len(wts))
    for wi, t in enumerate(wts):
        a = esl(t, WL)
        if not loud(a):
            lab[wi] = -2
            continue
        a0 = a - a.mean()
        best = (-1, 0.0, 0.0)
        for ci, o in enumerate(offs):
            b = jsl(t + o - RAD, WL + 2 * RAD)
            if np.abs(b).max() < 1e-6:
                continue
            nc = _nc_local(a0, b)
            k = int(nc.argmax())
            z = (nc[k] - nc.mean()) / (nc.std() + 1e-9)
            if z > best[2]:
                best = (ci, o - RAD + k / fs, z)
        if best[2] >= 7:
            lab[wi], off_i[wi], z_i[wi] = best[0], best[1], best[2]
        if wi % 20 == 0:
            progress(0.30 + 0.45 * wi / len(wts), "区間を判定しています…")

    # ---- 3) 平滑化: 短すぎる孤立区間を除き、同じ相手に挟まれた空白を埋める
    def runs_of(l):
        r, s = [], 0
        for i in range(1, len(l) + 1):
            if i == len(l) or l[i] != l[s]:
                r.append((l[s], s, i - 1))
                s = i
        return r
    for lb, s, e in runs_of(list(lab)):
        if lb >= 0 and e - s + 1 < 2:
            lab[s:e + 1] = -1
    inferred = np.zeros(len(wts), bool)
    rr = runs_of(list(lab))
    for i in range(1, len(rr) - 1):
        lb, s, e = rr[i]
        if lb < 0 and rr[i - 1][0] >= 0 and rr[i - 1][0] == rr[i + 1][0]:
            lab[s:e + 1] = rr[i - 1][0]
            inferred[s:e + 1] = True
            off_i[s:e + 1] = np.median(off_i[rr[i - 1][1]:rr[i - 1][2] + 1])
    rr = [r for r in runs_of(list(lab)) if r[0] >= 0]
    if not rr:
        return [], {"ok": False, "unused_jp": [],
                    "msg": "区間を判定できませんでした。BGMや効果音が少ない作品は「手動調整」タブをお使いください。"}

    # ---- 3.5) 少しずつ変化するオフセット(速度差)で別々の区間に割れたものを1つに戻す
    def merge_runs(rr):
        again = True
        while again:
            again = False
            for i in range(len(rr) - 1):
                (l0, s0, e0), (l1, s1, e1) = rr[i], rr[i + 1]
                if s1 - e0 > 5:
                    continue
                idx = [j for j in list(range(s0, e0 + 1)) + list(range(s1, e1 + 1)) if not inferred[j] and z_i[j] >= 7]
                if len(idx) < 4:
                    continue
                tc, oo = wts[idx] + WL / 2, off_i[idx]
                if abs(np.median(oo[:max(len(idx) // 2, 1)]) - np.median(oo[len(idx) // 2:])) > 1.0:
                    continue
                A = np.polyfit(tc, oo, 1)
                if abs(A[0]) < 0.02 and np.median(np.abs(np.polyval(A, tc) - oo)) < 0.03:
                    rr[i:i + 2] = [(l0, s0, e1)]
                    again = True
                    break
        return rr
    rr = merge_runs(rr)

    # ---- 4) 各区間のオフセット(一定 or わずかな速度差)を推定
    def fit_run(lb, s, e):
        idx = [i for i in range(s, e + 1) if not inferred[i] and z_i[i] >= 7]
        if not idx:
            idx = list(range(s, e + 1))
        tc = wts[idx] + WL / 2
        oo = off_i[idx]
        med = float(np.median(oo))
        slope, conf = 0.0, float(np.median(z_i[idx]))
        if len(idx) >= 6 and tc[-1] - tc[0] >= 15:
            A = np.polyfit(tc, oo, 1)
            res = np.abs(np.polyval(A, tc) - oo)
            if abs(A[0]) > 2e-4 and np.median(res) < 0.05:
                return A[1], A[0], conf
        return med, slope, conf
    fits = [fit_run(*r) for r in rr]

    # ---- 5) 区間の境界を精密に決める
    def score(a, jt, rad=0.1):
        b = jsl(jt - rad, len(a) / fs + 2 * rad)
        if np.abs(b).max() < 1e-6:
            return 0.0
        return float(_nc_local(a - a.mean(), b).max())

    def boundary(offA, offB, t0, t1, wlen, hop):
        ts = np.arange(t0, t1 - wlen + 1e-9, hop)
        if len(ts) < 2:
            return (t0 + t1) / 2, False
        d = []
        for t in ts:
            a = esl(t, wlen)
            d.append(score(a, t + offA) - score(a, t + offB) if loud(a) else 0.0)
        d = np.array(d)
        pre = np.concatenate([[0], np.cumsum(d)])
        V = 2 * pre - d.sum()
        k = int(V.argmax())
        c = ts + wlen / 2
        if k == 0:
            b = c[0]
        elif k == len(ts):
            b = c[-1]
        else:
            b = (c[k - 1] + c[k]) / 2
        return float(b), bool(abs(V[k]) >= 0.3 * np.abs(d).sum() and np.abs(d).sum() > 0.2)

    def off_at(f, t):
        return f[0] + f[1] * t

    bounds = []  # 各ペアの境界 (時刻, 確か?)
    for i in range(len(rr) - 1):
        (_, s0, e0), (_, s1, e1) = rr[i], rr[i + 1]
        t0 = wts[e0] + WL / 2 - 1.0
        t1 = wts[s1] + WL / 2 + 1.0
        t0, t1 = max(t0, t0e), min(t1, e_end)
        fa, fb = fits[i], fits[i + 1]
        mid = (t0 + t1) / 2
        b1, sure1 = boundary(off_at(fa, mid), off_at(fb, mid), t0 - 1.0, t1 + 1.0, 1.0, 0.1)
        b2, sure2 = boundary(off_at(fa, b1), off_at(fb, b1), b1 - 0.8, b1 + 0.8, 0.4, 0.02)
        bounds.append((b2, sure1 or sure2))
        progress(0.75 + 0.2 * (i + 1) / max(len(rr) - 1, 1), "境界を調整しています…")

    # ---- 6) 区間リストを組み立てる
    segs = []
    for i, ((lb, s, e), f) in enumerate(zip(rr, fits)):
        en_s = t0e if i == 0 else bounds[i - 1][0]
        en_e = e_end if i == len(rr) - 1 else bounds[i][0]
        jp_s = en_s + off_at(f, en_s)
        jp_e = en_e + off_at(f, en_e)
        # 日本語録画の範囲外にはみ出す分は詰める
        jmin, jmax = jo, jo + j_dur
        if jp_s < jmin:
            d = jmin - jp_s
            en_s += d
            jp_s += d
        if jp_e > jmax:
            en_e -= (jp_e - jmax)
            jp_e = jmax
        if en_e - en_s < 0.2:
            continue
        const = abs(f[1]) <= 2e-4
        reasons = []
        if f[2] < 10:
            reasons.append("一致度が低め")
        if i > 0 and not bounds[i - 1][1]:
            reasons.append("開始位置が不確か")
        if i < len(rr) - 1 and not bounds[i][1]:
            reasons.append("終了位置が不確か")
        if inferred[s:e + 1].any():
            reasons.append("声だけの場面を推定")
        segs.append({"en_start": round(float(en_s), 4), "en_end": round(float(en_e), 4), "jp_start": round(float(jp_s), 4),
                     "jp_end": None if const else round(float(jp_e), 4), "fit": "stretch", "note": "自動",
                     "lag": "", "conf": float(f[2]), "flag": "・".join(reasons)})
    unused = []
    prev_end = jo
    for s in segs:
        js = s["jp_start"]
        je = s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + (s["en_end"] - s["en_start"])
        if js - prev_end > 5.0:
            unused.append((prev_end, js))
        prev_end = max(prev_end, je)
    if jo + j_dur - prev_end > 5.0:
        unused.append((prev_end, jo + j_dur))
    progress(1.0, "完了")
    return segs, {"ok": True, "unused_jp": unused, "msg": ""}


# ------------------------------------------------------------------ 波形トラック


class Track(tk.Canvas):
    def __init__(self, master, app, key, title, **kw):
        super().__init__(master, height=110, bg="#15171c", highlightthickness=1,
                         highlightbackground="#444", takefocus=True, **kw)
        self.app, self.key, self.title = app, key, title
        self.media = None
        self.vs, self.vspan = 0.0, 60.0
        self.cursor, self.mark = 0.0, None
        self.bands = []
        self._pan = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self.on_click)
        self.bind("<MouseWheel>", self.on_wheel)
        self.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        self.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        self.bind("<ButtonPress-3>", self.pan_start)
        self.bind("<B3-Motion>", self.pan_move)

    def x_of(self, t):
        return (t - self.vs) / self.vspan * self.winfo_width()

    def t_of(self, x):
        return self.vs + x / max(self.winfo_width(), 1) * self.vspan

    def on_click(self, e):
        self.focus_set()
        self.app.set_active(self.key)
        self.app.set_cursor(self.key, self.t_of(e.x))

    def on_wheel(self, e, direction=None):
        d = direction if direction is not None else (1 if e.delta > 0 else -1)
        if e.state & 0x1:  # Shift: 横移動
            self.vs += -d * self.vspan * 0.2
        else:
            t = self.t_of(e.x)
            f = 0.75 if d > 0 else 1.0 / 0.75
            new = min(max(self.vspan * f, 0.05), 6 * 3600)
            self.vs = t - (t - self.vs) * new / self.vspan
            self.vspan = new
        self.redraw()

    def pan_start(self, e):
        self._pan = (e.x, self.vs)

    def pan_move(self, e):
        if self._pan:
            self.vs = self._pan[1] - (e.x - self._pan[0]) / self.winfo_width() * self.vspan
            self.redraw()

    def ensure_visible(self, t):
        if t < self.vs + self.vspan * 0.05 or t > self.vs + self.vspan * 0.95:
            self.vs = t - self.vspan / 2

    def redraw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 10:
            return
        mid = (H - 22) / 2 + 2
        self.create_line(0, mid, W, mid, fill="#2c303a")
        m = self.media
        if m is None:
            self.create_text(W / 2, H / 2, text="(未読み込み)", fill="#888")
        else:
            env = m.envelope(self.vs, self.vs + self.vspan, W)
            amp = (H - 26) / 2 * 0.95 / m.scale
            if env and env[0] == "env":
                _, cols, mn, mx = env
                top = np.column_stack([cols, mid - np.clip(mx * amp, -1e4, 1e4)])
                bot = np.column_stack([cols, mid - np.clip(mn * amp, -1e4, 1e4)])[::-1]
                pts = np.vstack([top, bot]).ravel().tolist()
                self.create_polygon(pts, fill="#6fa8dc", outline="#6fa8dc")
            elif env:
                _, xs, ys = env
                pts = np.column_stack([xs, mid - ys * amp]).ravel().tolist()
                self.create_line(pts, fill="#6fa8dc")
        # 区間の帯
        for a, b, i in self.bands:
            xa, xb = self.x_of(a), self.x_of(b)
            if xb < 0 or xa > W:
                continue
            col = BAND_COLORS[i % len(BAND_COLORS)]
            self.create_rectangle(max(xa, 0), H - 22, min(xb, W), H - 12, fill=col, outline="")
            if xb - xa > 16:
                self.create_text(max(xa, 0) + 3, H - 17, text=str(i + 1), anchor="w",
                                 fill="white", font=("", 7, "bold"))
        # 目盛り
        step = next((s for s in (0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
                     if s * W / self.vspan >= 70), 3600)
        t = np.ceil(self.vs / step) * step
        while t < self.vs + self.vspan:
            x = self.x_of(t)
            self.create_line(x, H - 12, x, H, fill="#777")
            self.create_text(x + 2, H - 6, text=tc(t)[:12] if step < 1 else tc(t)[:8],
                             anchor="w", fill="#aaa", font=("", 7))
            t += step
        if self.mark is not None:
            x = self.x_of(self.mark)
            self.create_line(x, 0, x, H - 12, fill="#3ddc84", width=2, dash=(4, 2))
        x = self.x_of(self.cursor)
        self.create_line(x, 0, x, H - 12, fill="#ff4d4d", width=1)
        self.create_text(4, 3, text=self.title, anchor="nw", fill="#ddd", font=("", 9, "bold"))


# ------------------------------------------------------------------ アプリ


class App(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("syncdub - 吹き替え音声シンクロツール")
        self.geometry("1180x860")
        self.media = {"en": None, "jp": None}
        self.segs = []
        self.active = "en"
        self.proj_path = None
        self.out_path = tk.StringVar()
        self.q = queue.Queue()
        self.play_proc = None
        self.pv_job = {}
        self.pv_token = {"en": 0, "jp": 0}
        self.pv_img = {}
        self._build()
        self.after(60, self._poll)
        self._setup_dnd()
        if self.auto_upd.get():
            self.after(1500, lambda: self.check_update(False))
        for k in ("s", "e", "a", "space", "Left", "Right", "Tab"):
            self.bind_all(f"<KeyPress-{k}>" if len(k) > 1 else f"<Key-{k}>", self._key)
        self.bind_all("<Shift-Left>", self._key)
        self.bind_all("<Shift-Right>", self._key)
        self.bind_all("<Control-Left>", self._key)
        self.bind_all("<Control-Right>", self._key)

    # ---------- UI ----------
    def _build(self):
        self.en_var, self.jp_var = tk.StringVar(), tk.StringVar()
        self.jp_ast = tk.IntVar(value=0)
        self._build_header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=4, pady=2)
        self.easy, self.manual = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.nb.add(self.easy, text="   かんたん   ")
        self.nb.add(self.manual, text="   手動調整 (上級者向け)   ")
        top = ttk.Frame(self.manual, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="英語版映像").grid(row=0, column=0, sticky="w")
        self.en_ent = ttk.Entry(top, textvariable=self.en_var)
        self.en_ent.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="参照", command=lambda: self._browse(self.en_var)).grid(row=0, column=2)
        ttk.Label(top, text="日本語録画").grid(row=1, column=0, sticky="w")
        self.jp_ent = ttk.Entry(top, textvariable=self.jp_var)
        self.jp_ent.grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="参照", command=lambda: self._browse(self.jp_var)).grid(row=1, column=2)
        f = ttk.Frame(top)
        f.grid(row=1, column=3, padx=6)
        ttk.Label(f, text="音声#").pack(side="left")
        ttk.Spinbox(f, from_=0, to=9, width=3, textvariable=self.jp_ast).pack(side="left")
        ttk.Button(top, text="読み込み", command=self.load_media).grid(row=0, column=3, rowspan=1, padx=6)
        ttk.Button(top, text="プロジェクトを開く", command=self.open_project).grid(row=0, column=4, padx=2)
        ttk.Button(top, text="保存", command=self.save_project).grid(row=1, column=4, padx=2)
        top.columnconfigure(1, weight=1)

        self.tr = {"en": Track(self.manual, self, "en", "英語版 (基準)"),
                   "jp": Track(self.manual, self, "jp", "日本語録画")}
        self.tr["en"].pack(fill="x", padx=6, pady=(2, 2))
        self.tr["jp"].pack(fill="x", padx=6, pady=(2, 2))

        mid = ttk.Frame(self.manual, padding=4)
        mid.pack(fill="x")
        self.pv = {}
        self.ent = {}
        self.fr_lbl = {}
        for col, (k, name) in enumerate((("en", "英語版"), ("jp", "日本語録画"))):
            fr = ttk.LabelFrame(mid, text=name)
            fr.grid(row=0, column=0 if k == "en" else 2, padx=4, sticky="n")
            lab = tk.Label(fr, width=44, height=10, bg="black", fg="#888", text="(プレビュー)")
            lab.pack(padx=4, pady=4)
            self.pv[k] = lab
            row = ttk.Frame(fr)
            row.pack(fill="x", padx=4, pady=(0, 4))
            var = tk.StringVar(value=tc(0))
            e = ttk.Entry(row, textvariable=var, width=14)
            e.pack(side="left")
            e.bind("<Return>", lambda ev, k=k: self.entry_apply(k))
            e.bind("<FocusIn>", lambda ev, k=k: self.set_active(k))
            self.ent[k] = var
            fl = ttk.Label(row, text="f 0")
            fl.pack(side="left", padx=6)
            self.fr_lbl[k] = fl
            ttk.Button(row, text="▶ 再生", width=7, command=lambda k=k: self.play(k)).pack(side="right")
        ctr = ttk.Frame(mid)
        ctr.grid(row=0, column=1, padx=8, sticky="n")
        for text, cmd in (("S 開始点を打つ", self.mark_start), ("E 終了点→区間追加", self.mark_end),
                          ("A 自動吸着 (日本語側)", self.auto_snap), ("開始点を取消", self.mark_clear),
                          ("区間を検証", self.verify)):
            ttk.Button(ctr, text=text, width=20, command=cmd).pack(pady=2)
        self.mark_lbl = ttk.Label(ctr, text="開始点: なし", foreground="#2a7")
        self.mark_lbl.pack(pady=4)
        ttk.Label(ctr, text="自動吸着の窓(秒)/探索(±秒)").pack()
        sf = ttk.Frame(ctr)
        sf.pack()
        self.win_v, self.srch_v = tk.StringVar(value="8"), tk.StringVar(value="60")
        ttk.Entry(sf, textvariable=self.win_v, width=5).pack(side="left")
        ttk.Entry(sf, textvariable=self.srch_v, width=5).pack(side="left", padx=4)

        # 区間表
        mid2 = ttk.Frame(self.manual, padding=(6, 2))
        mid2.pack(fill="both", expand=True)
        cols = ("n", "es", "ee", "js", "je", "ratio", "fit", "lag", "note")
        heads = ("#", "EN開始", "EN終了", "JP開始", "JP終了", "速度比", "差の扱い", "検証ズレ(ms)", "メモ")
        widths = (30, 100, 100, 100, 100, 70, 70, 140, 200)
        self.tree = ttk.Treeview(mid2, columns=cols, show="headings", height=6, selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("warn", background="#ffe9c7")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.edit_seg())
        bt = ttk.Frame(mid2)
        bt.pack(side="left", padx=6, anchor="n")
        for text, cmd in (("編集", self.edit_seg), ("削除", self.del_seg)):
            ttk.Button(bt, text=text, width=8, command=cmd).pack(pady=2)

        bot = ttk.Frame(self.manual, padding=6)
        bot.pack(fill="x")
        ttk.Label(bot, text="出力").pack(side="left")
        ttk.Entry(bot, textvariable=self.out_path).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(bot, text="参照", command=self.pick_out).pack(side="left")
        self.render_btn = ttk.Button(bot, text="合成", command=self.render)
        self.render_btn.pack(side="left", padx=6)
        self.log = tk.Text(self.manual, height=5, bg="#111", fg="#cfc", font=("Consolas", 9))
        self.log.pack(fill="x", padx=6, pady=(0, 2))
        self._build_easy()

    # ---------- ヘッダー(更新) / かんたんタブ ----------
    def _build_header(self):
        self.status = ttk.Label(self, text="", anchor="w", foreground="#555")
        self.status.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        h = ttk.Frame(self, padding=(8, 6, 8, 0))
        h.pack(fill="x")
        ttk.Label(h, text="syncdub  吹き替え音声シンクロツール", font=("", 13, "bold")).pack(side="left")
        ttk.Label(h, text=f"v{APP_VERSION}", foreground="#888").pack(side="right", padx=(8, 0))
        self.auto_upd = tk.BooleanVar(value=self._setting("auto_update", True))
        ttk.Checkbutton(h, text="起動時に更新を確認", variable=self.auto_upd, command=self._save_auto).pack(side="right", padx=4)
        ttk.Button(h, text="元に戻す", command=self.rollback).pack(side="right", padx=2)
        ttk.Button(h, text="更新を確認", command=lambda: self.check_update(True)).pack(side="right", padx=2)

    def _build_easy(self):
        f = self.easy
        pad = {"padx": 12, "pady": 5}
        tk.Label(f, justify="left", anchor="w", fg="#333", wraplength=1050, font=("", 10),
                 text="日本語吹き替えの音声を、きれいな英語版の映像に合わせて1本の動画にします。\n"
                      "手順:  ① ファイルを選ぶ  →  ② 自動で合わせる  →  ③ 結果を確認  →  ④ 合成する"
                 ).pack(fill="x", **pad)

        # ① ファイルを選ぶ
        c1 = ttk.LabelFrame(f, text=" ① ファイルを選ぶ (ドラッグ&ドロップ、またはクリック) ")
        c1.pack(fill="x", **pad)
        row = ttk.Frame(c1)
        row.pack(fill="x", padx=8, pady=8)
        row.columnconfigure((0, 1), weight=1, uniform="z")
        self.zone = {}
        for col, (k, title, sub) in enumerate((("en", "英語版の映像", "きれいな映像のファイル"),
                                                ("jp", "日本語の録画", "吹き替えの音声が入っているファイル (TV録画など)"))):
            z = tk.Frame(row, bg="#f4f7fb", highlightthickness=2, highlightbackground="#9db4d3", height=92, cursor="hand2")
            z.grid(row=0, column=col, sticky="ew", padx=6)
            z.grid_propagate(False)
            t = tk.Label(z, text=title, bg="#f4f7fb", font=("", 12, "bold"))
            t.pack(pady=(10, 0))
            n = tk.Label(z, text=sub + "\nここにドロップ / クリックして選択", bg="#f4f7fb", fg="#667", font=("", 9))
            n.pack()
            for w in (z, t, n):
                w.bind("<Button-1>", lambda e, k=k: self._pick(k))
            self.zone[k] = {"frame": z, "title": t, "name": n, "sub": sub}
        o = ttk.Frame(c1)
        o.pack(fill="x", padx=14, pady=(0, 8))
        ttk.Label(o, text="日本語の録画に音声が複数ある場合だけ、音声番号を変えます (通常は 0 のままでOK):").pack(side="left")
        ttk.Spinbox(o, from_=0, to=9, width=3, textvariable=self.jp_ast).pack(side="left", padx=6)
        self.en_var.trace_add("write", lambda *a: self._zone_update())
        self.jp_var.trace_add("write", lambda *a: self._zone_update())

        # ② 自動で合わせる
        c2 = ttk.LabelFrame(f, text=" ② 自動で合わせる ")
        c2.pack(fill="x", **pad)
        r2 = ttk.Frame(c2)
        r2.pack(fill="x", padx=8, pady=(8, 2))
        self.auto_btn = ttk.Button(r2, text="自動で合わせる", command=self.run_auto, state="disabled")
        self.auto_btn.pack(side="left", ipadx=14, ipady=4)
        self.pbar = ttk.Progressbar(r2, mode="determinate", maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True, padx=12)
        self.auto_msg = tk.Label(c2, text="ファイルを2つ選ぶと、自動で解析が始まります。", justify="left", anchor="w",
                                 wraplength=1050, fg="#333")
        self.auto_msg.pack(fill="x", padx=12, pady=(2, 8))

        # ③ 結果を確認
        c3 = ttk.LabelFrame(f, text=" ③ 結果を確認 ")
        c3.pack(fill="both", expand=True, **pad)
        r3 = ttk.Frame(c3)
        r3.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("n", "en", "jp", "st")
        self.etree = ttk.Treeview(r3, columns=cols, show="headings", height=5, selectmode="browse")
        for c, h, w in zip(cols, ("#", "英語版の時間", "日本語録画の時間", "状態"), (36, 220, 220, 330)):
            self.etree.heading(c, text=h)
            self.etree.column(c, width=w, anchor="w")
        self.etree.tag_configure("warn", background="#ffe9c7")
        self.etree.pack(side="left", fill="both", expand=True)
        self.etree.bind("<<TreeviewSelect>>", self.on_easy_select)
        bt = ttk.Frame(r3)
        bt.pack(side="left", padx=8, anchor="n")
        ttk.Button(bt, text="▶ 英語版を聞く", width=16, command=lambda: self._listen("en")).pack(pady=2)
        ttk.Button(bt, text="▶ 日本語を聞く", width=16, command=lambda: self._listen("jp")).pack(pady=2)
        ttk.Button(bt, text="手動で直す →", width=16, command=lambda: self.nb.select(self.manual)).pack(pady=(10, 2))
        tk.Label(c3, anchor="w", fg="#667", text="行を選ぶと、その区間の先頭を聞き比べられます。「要確認」の行は特に聞いて確かめてください。"
                 ).pack(fill="x", padx=12, pady=(0, 6))

        # ④ 合成
        c4 = ttk.LabelFrame(f, text=" ④ 合成する ")
        c4.pack(fill="x", **pad)
        r4 = ttk.Frame(c4)
        r4.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(r4, text="保存先:").pack(side="left")
        ttk.Entry(r4, textvariable=self.out_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r4, text="変更", command=self.pick_out).pack(side="left")
        self.easy_render = ttk.Button(r4, text="合成スタート", command=self.render, state="disabled")
        self.easy_render.pack(side="left", padx=10, ipadx=14, ipady=4)
        r5 = ttk.Frame(c4)
        r5.pack(fill="x", padx=8, pady=(0, 8))
        self.rbar = ttk.Progressbar(r5, mode="indeterminate", length=160)
        self.rbar.pack(side="left")
        self.easy_done = tk.Label(r5, text="", anchor="w", fg="#2a7")
        self.easy_done.pack(side="left", padx=10)
        self.open_btn = ttk.Button(r5, text="フォルダを開く", command=self.open_folder)

    @staticmethod
    def short(t):
        sign = "-" if t < 0 else ""
        t = abs(t)
        h, m, sec = int(t // 3600), int(t % 3600 // 60), t % 60
        return f"{sign}{h}:{m:02d}:{sec:04.1f}" if h else f"{sign}{m}:{sec:04.1f}"

    def _zone_update(self):
        for k, var in (("en", self.en_var), ("jp", self.jp_var)):
            z, p = self.zone[k], var.get().strip()
            if p:
                z["frame"].config(bg="#eaf7ee", highlightbackground="#3aa76d")
                for w in (z["title"], z["name"]):
                    w.config(bg="#eaf7ee")
                z["name"].config(text="✔ " + Path(p).name + "\n(クリックで選び直し)", fg="#1f6f43")
            else:
                z["frame"].config(bg="#f4f7fb", highlightbackground="#9db4d3")
                for w in (z["title"], z["name"]):
                    w.config(bg="#f4f7fb")
                z["name"].config(text=z["sub"] + "\nここにドロップ / クリックして選択", fg="#667")

    def _pick(self, k):
        p = filedialog.askopenfilename(title="英語版の映像を選択" if k == "en" else "日本語の録画を選択")
        if p:
            (self.en_var if k == "en" else self.jp_var).set(p)
            self._maybe_load()

    def _maybe_load(self):
        if self.en_var.get().strip() and self.jp_var.get().strip():
            self._auto_after_load = True
            self.load_media()
        else:
            self.say("もう一方のファイルも選んでください")

    def _busy(self, flag):
        st = ["disabled"] if flag else ["!disabled"]
        self.render_btn.state(st)
        has = bool(all(self.media.values()))
        self.auto_btn.state(["disabled"] if (flag or not has) else ["!disabled"])
        self.easy_render.state(["disabled"] if (flag or not self.segs) else ["!disabled"])

    # ---------- 自動で合わせる ----------
    def run_auto(self):
        if not all(self.media.values()):
            return
        en, jp = self.media["en"], self.media["jp"]
        self._busy(True)
        self.pbar.config(value=0)
        self.auto_msg.config(text="音を照合して、対応する場所を探しています…(長い動画は数分かかります)", fg="#333")
        self.easy_done.config(text="")

        def prog(fr, msg):
            self.q.put(lambda: (self.pbar.config(value=fr * 100), self.auto_msg.config(text=msg)))

        def work():
            try:
                segs, info = auto_align(en, jp, prog)
            except Exception as ex:  # noqa: BLE001
                segs, info = [], {"ok": False, "unused_jp": [], "msg": f"解析中にエラーが起きました: {ex}"}
            self.q.put(lambda: self._auto_done(segs, info))
        threading.Thread(target=work, daemon=True).start()

    def _auto_done(self, segs, info):
        if not info["ok"]:
            self.segs = []
            self.refresh()
            self.pbar.config(value=0)
            self.auto_msg.config(text="⚠ " + info["msg"], fg="#b33")
            self._busy(False)
            return
        self.segs = segs
        self.refresh()
        self.pbar.config(value=100)
        warn = sum(1 for s in segs if s.get("flag"))
        lines = [f"✔ 自動で合わせました (区間 {len(segs)} 個)。"]
        for a, b in info["unused_jp"][:5]:
            lines.append(f"・日本語の録画の {self.short(a)} 〜 {self.short(b)} (約{int(b - a)}秒) は使いません (CMなど)。")
        if len(info["unused_jp"]) > 5:
            lines.append(f"・ほか {len(info['unused_jp']) - 5} か所も使いません。")
        lines.append(f"・「要確認」が {warn} 件あります。下の表で聞き比べて確かめてください。" if warn
                     else "・すべて高い一致度でした。念のため下の表で聞き比べてから、合成してください。")
        self.auto_msg.config(text="\n".join(lines), fg="#1f6f43" if not warn else "#8a5a00")
        self._busy(False)
        self.say("自動で合わせました")

    def refresh_easy(self):
        self.etree.delete(*self.etree.get_children())
        for i, s in enumerate(self.segs):
            ed = s["en_end"] - s["en_start"]
            je = s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + ed
            flag = s.get("flag", "")
            st = "手動で設定" if "conf" not in s else ("要確認: " + flag if flag else "OK")
            self.etree.insert("", "end", iid=str(i), tags=("warn",) if flag else (), values=(
                i + 1, f"{self.short(s['en_start'])} 〜 {self.short(s['en_end'])}",
                f"{self.short(s['jp_start'])} 〜 {self.short(je)}", st))
        self.easy_render.state(["disabled"] if (not self.segs) else ["!disabled"])

    def on_easy_select(self, _):
        s = self.etree.selection()
        if s and self.media["en"]:
            seg = self.segs[int(s[0])]
            self.set_cursor("en", seg["en_start"], scroll=True)
            self.set_cursor("jp", seg["jp_start"], scroll=True)

    def _listen(self, k):
        if not self.etree.selection():
            self.say("先に、表の行を選んでください")
            return
        self.play(k)

    def open_folder(self):
        d = str(Path(self.out_path.get()).resolve().parent)
        try:
            if sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])
        except OSError:
            pass

    # ---------- GitHubからの自己更新 ----------
    def _setting(self, key, default):
        try:
            return json.loads((Path(__file__).resolve().parent / "syncdub_settings.json").read_text(encoding="utf-8")).get(key, default)
        except (OSError, ValueError):
            return default

    def _save_auto(self):
        try:
            (Path(__file__).resolve().parent / "syncdub_settings.json").write_text(
                json.dumps({"auto_update": self.auto_upd.get()}), encoding="utf-8")
        except OSError:
            pass

    def check_update(self, manual):
        def work():
            try:
                info, ref = fetch_update_info()
                if vkey(info["version"]) <= vkey(APP_VERSION):
                    if manual:
                        self.q.put(lambda: messagebox.showinfo("更新", f"最新版です (v{APP_VERSION})"))
                    return
                code = download_update_source(info, ref)
                self.q.put(lambda: self._update_found(info, code))
            except Exception as ex:  # noqa: BLE001
                if manual:
                    self.q.put(lambda: messagebox.showwarning("更新を確認できません", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def _restart(self, me):
        subprocess.Popen([sys.executable, str(me)], **NOWIN)
        self.destroy()

    def _update_found(self, info, code):
        notes = f"\n\n{info['notes']}" if info.get("notes") else ""
        if not messagebox.askyesno("更新", f"新しい版 v{info['version']} があります (現在 v{APP_VERSION})。{notes}\n更新して再起動しますか?"):
            return
        me = Path(__file__).resolve()
        try:
            install_update(code, me)
        except OSError as ex:
            messagebox.showerror("更新失敗", str(ex))
            return
        self._restart(me)

    def rollback(self):
        me = Path(__file__).resolve()
        bk = latest_backup(me)
        if not bk:
            messagebox.showinfo("元に戻す", "バックアップがありません")
            return
        if not messagebox.askyesno("元に戻す", f"{bk.name} に戻して再起動しますか?"):
            return
        try:
            os.replace(bk, me)
        except OSError as ex:
            messagebox.showerror("失敗", str(ex))
            return
        self._restart(me)

    # ---------- ドラッグ&ドロップ ----------
    def _setup_dnd(self):
        if not DND_FILES:
            self.say("ドラッグ&ドロップを使うには: pip install tkinterdnd2 (なくても動作します)")
            return
        targets = {self: None, self.en_ent: "en", self.tr["en"]: "en", self.pv["en"]: "en",
                   self.jp_ent: "jp", self.tr["jp"]: "jp", self.pv["jp"]: "jp"}
        for k, z in self.zone.items():
            for w in (z["frame"], z["title"], z["name"]):
                targets[w] = k
        for w, k in targets.items():
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", lambda ev, k=k: self._on_drop(ev, k))

    def _on_drop(self, ev, k):
        files = list(self.tk.splitlist(ev.data))
        if not files:
            return
        if files[0].lower().endswith(".json"):
            self.load_project(files[0])
            return
        if len(files) >= 2:
            self.en_var.set(files[0])
            self.jp_var.set(files[1])
        elif k:
            (self.en_var if k == "en" else self.jp_var).set(files[0])
        else:  # ウィンドウ全体に1つ → 空いている方へ
            (self.jp_var if self.en_var.get() and not self.jp_var.get() else self.en_var).set(files[0])
        self._maybe_load()

    # ---------- 共通 ----------
    def say(self, msg):
        self.status.config(text=msg)

    def logw(self, msg):
        self.log.insert("end", msg.rstrip() + "\n")
        self.log.see("end")

    def _poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(60, self._poll)

    def _browse(self, var):
        p = filedialog.askopenfilename(title="ファイルを選択")
        if p:
            var.set(p)

    def pick_out(self):
        p = filedialog.asksaveasfilename(defaultextension=".mkv", filetypes=[("MKV", "*.mkv"), ("MP4", "*.mp4"), ("すべて", "*.*")])
        if p:
            self.out_path.set(p)

    def set_active(self, k):
        self.active = k
        for kk, t in self.tr.items():
            t.config(highlightbackground="#ffcc00" if kk == k else "#444", highlightthickness=2 if kk == k else 1)

    # ---------- 読み込み ----------
    def load_media(self):
        en, jp = self.en_var.get().strip(), self.jp_var.get().strip()
        if not en or not jp:
            messagebox.showwarning("入力不足", "英語版映像と日本語録画を指定してください")
            return
        self.say("音声を読み込み中…(長い録画は少し時間がかかります)")
        self.auto_msg.config(text="ファイルを読み込んでいます…(長い録画は少し時間がかかります)", fg="#333")
        self.pbar.config(value=0)
        self._busy(True)
        ast = self.jp_ast.get()  # Tk変数はメインスレッドで読む

        def work():
            try:
                m = {"en": Media(en, 0), "jp": Media(jp, ast)}
            except Exception as ex:  # noqa: BLE001
                err = getattr(ex, "stderr", b"") or b""
                msg = f"{ex}\n{err.decode(errors='ignore')[-500:] if isinstance(err, bytes) else err}"
                self.q.put(lambda: (self.say("読み込み失敗"), self.auto_msg.config(text="⚠ 読み込めませんでした", fg="#b33"),
                                    messagebox.showerror("読み込みエラー", msg), self._busy(False)))
                return
            self.q.put(lambda: self._loaded(m))
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, m):
        self.media = m
        for k in ("en", "jp"):
            t = self.tr[k]
            t.media = m[k]
            t.vs, t.vspan = min(0, m[k].a_origin), min(60.0, max(m[k].end, 10))
            t.cursor = 0.0
        if not self.out_path.get():
            p = Path(self.en_var.get())
            self.out_path.set(str(p.with_name(p.stem + "_jpdub.mkv")))
        self._busy(False)
        self.say(f"読み込み完了  EN {m['en'].fps:.3f}fps / JP {m['jp'].fps:.3f}fps")
        self.set_active("en")
        self.refresh()
        for k in ("en", "jp"):
            self.update_preview(k)
        if getattr(self, "_auto_after_load", False):
            self._auto_after_load = False
            self.run_auto()

    # ---------- カーソル ----------
    def fps(self, k):
        m = self.media[k]
        return m.fps if m else 30.0

    def set_cursor(self, k, t, scroll=False):
        tr = self.tr[k]
        tr.cursor = t
        if scroll:
            tr.ensure_visible(t)
        tr.redraw()
        self.ent[k].set(tc(t))
        self.fr_lbl[k].config(text=f"f {round(t * self.fps(k))}")
        self.schedule_preview(k)

    def entry_apply(self, k):
        try:
            self.set_cursor(k, parse_tc(self.ent[k].get(), self.fps(k)), scroll=True)
        except ValueError:
            self.say("時刻の書式が不正です (例 00:01:02.500 / 125.5 / 3000f)")

    def step(self, k, frames=0, seconds=0.0):
        t = self.tr[k].cursor
        fps = self.fps(k)
        t = round(t * fps) / fps + frames / fps + seconds
        self.set_cursor(k, t, scroll=True)

    def _key(self, ev):
        w = self.focus_get()
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox)):
            return
        ks = ev.keysym
        k = self.active
        if ks in ("Left", "Right"):
            d = -1 if ks == "Left" else 1
            if ev.state & 0x1:
                self.step(k, seconds=d * 1.0)
            elif ev.state & 0x4:
                self.step(k, frames=d * 10)
            else:
                self.step(k, frames=d)
        elif ks == "s":
            self.mark_start()
        elif ks == "e":
            self.mark_end()
        elif ks == "a":
            self.auto_snap()
        elif ks == "space":
            self.play(k)
        elif ks == "Tab":
            self.set_active("jp" if k == "en" else "en")
        return "break"

    # ---------- プレビュー / 再生 ----------
    def schedule_preview(self, k):
        if self.pv_job.get(k):
            self.after_cancel(self.pv_job[k])
        self.pv_job[k] = self.after(120, lambda: self.update_preview(k))

    def update_preview(self, k):
        m = self.media[k]
        if not m:
            return
        self.pv_token[k] += 1
        tok, t = self.pv_token[k], self.tr[k].cursor

        def work():
            png = m.frame_png(t)
            if tok == self.pv_token[k]:
                self.q.put(lambda: self._show_frame(k, png))
        threading.Thread(target=work, daemon=True).start()

    def _show_frame(self, k, png):
        if not png:
            self.pv[k].config(image="", text="(映像なし)")
            return
        img = tk.PhotoImage(data=base64.b64encode(png))
        self.pv_img[k] = img
        self.pv[k].config(image=img, text="", width=img.width(), height=img.height())

    def play(self, k):
        if self.play_proc and self.play_proc.poll() is None:
            self.play_proc.terminate()
            self.play_proc = None
            return
        m = self.media[k]
        if not m:
            return
        t = max(0.0, self.tr[k].cursor + m.voff)
        try:
            self.play_proc = subprocess.Popen(
                ["ffplay", "-v", "quiet", "-nodisp", "-autoexit", "-ss", f"{t:.3f}", "-t", "20",
                 "-ast", str(m.astream), "-i", m.path], **NOWIN)
        except FileNotFoundError:
            self.say("ffplay が見つかりません (ffmpeg と同じ場所に入れてください)")

    # ---------- 区間 ----------
    def mark_start(self):
        if not all(self.media.values()):
            return
        self.start = (self.tr["en"].cursor, self.tr["jp"].cursor)
        self.tr["en"].mark, self.tr["jp"].mark = self.start
        self.mark_lbl.config(text=f"開始点: EN {tc(self.start[0])[3:]} / JP {tc(self.start[1])[3:]}")
        for t in self.tr.values():
            t.redraw()

    def mark_clear(self):
        self.start = None
        for t in self.tr.values():
            t.mark = None
            t.redraw()
        self.mark_lbl.config(text="開始点: なし")

    def mark_end(self):
        st = getattr(self, "start", None)
        if not st:
            self.say("先に開始点を打ってください (S)")
            return
        es, js = st
        ee, je = self.tr["en"].cursor, self.tr["jp"].cursor
        if ee <= es or je <= js:
            self.say("終了点は開始点より後にしてください")
            return
        seg = {"en_start": es, "en_end": ee, "jp_start": js,
               "jp_end": None if abs((je - js) - (ee - es)) <= 0.002 else je,
               "fit": "stretch", "note": "", "lag": ""}
        self.segs.append(seg)
        self.segs.sort(key=lambda s: s["en_start"])
        self.mark_clear()
        self.refresh()
        self.say(f"区間を追加 (計{len(self.segs)})")

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.segs):
            en_d = s["en_end"] - s["en_start"]
            jd = (s["jp_end"] - s["jp_start"]) if s["jp_end"] is not None else en_d
            r = jd / en_d
            self.tree.insert("", "end", iid=str(i), tags=("warn",) if abs(r - 1) > 0.02 else (), values=(
                i + 1, tc(s["en_start"]), tc(s["en_end"]), tc(s["jp_start"]),
                tc(s["jp_end"]) if s["jp_end"] is not None else "(同尺)",
                f"{r:.4f}", s["fit"], s.get("lag", ""), s["note"]))
        self.tr["en"].bands = [(s["en_start"], s["en_end"], i) for i, s in enumerate(self.segs)]
        self.tr["jp"].bands = [(s["jp_start"], s["jp_start"] + ((s["jp_end"] - s["jp_start"]) if s["jp_end"] is not None else s["en_end"] - s["en_start"]), i)
                               for i, s in enumerate(self.segs)]
        for t in self.tr.values():
            t.redraw()
        self.refresh_easy()

    def sel_idx(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def on_select(self, _):
        i = self.sel_idx()
        if i is None or not self.media["en"]:
            return
        s = self.segs[i]
        self.set_cursor("en", s["en_start"], scroll=True)
        self.set_cursor("jp", s["jp_start"], scroll=True)

    def del_seg(self):
        i = self.sel_idx()
        if i is not None:
            del self.segs[i]
            self.refresh()

    def edit_seg(self):
        i = self.sel_idx()
        if i is None:
            return
        s = self.segs[i]
        d = tk.Toplevel(self)
        d.title(f"区間 {i + 1} を編集")
        d.transient(self)
        vars_ = {}
        fe, fj = self.fps("en"), self.fps("jp")
        rows = [("EN開始", "en_start", fe), ("EN終了", "en_end", fe), ("JP開始", "jp_start", fj),
                ("JP終了 (空欄=同尺)", "jp_end", fj)]
        for r, (lab, key, fp) in enumerate(rows):
            ttk.Label(d, text=lab).grid(row=r, column=0, sticky="w", padx=6, pady=3)
            v = tk.StringVar(value="" if s[key] is None else tc(s[key]))
            ttk.Entry(d, textvariable=v, width=18).grid(row=r, column=1, padx=6)
            vars_[key] = (v, fp)
        ttk.Label(d, text="差の扱い").grid(row=4, column=0, sticky="w", padx=6)
        fit = tk.StringVar(value=s["fit"])
        ttk.Combobox(d, textvariable=fit, values=["stretch", "pad", "trim"], width=15, state="readonly").grid(row=4, column=1)
        ttk.Label(d, text="メモ").grid(row=5, column=0, sticky="w", padx=6, pady=3)
        note = tk.StringVar(value=s["note"])
        ttk.Entry(d, textvariable=note, width=28).grid(row=5, column=1, padx=6)

        def ok():
            try:
                new = dict(s)
                for key, (v, fp) in vars_.items():
                    txt = v.get().strip()
                    new[key] = None if (key == "jp_end" and not txt) else parse_tc(txt, fp)
                if new["en_end"] <= new["en_start"]:
                    raise ValueError("EN終了はEN開始より後にしてください")
            except ValueError as ex:
                messagebox.showerror("入力エラー", str(ex), parent=d)
                return
            new["fit"], new["note"] = fit.get(), note.get()
            self.segs[i] = new
            self.segs.sort(key=lambda x: x["en_start"])
            d.destroy()
            self.refresh()
        ttk.Button(d, text="OK", command=ok).grid(row=6, column=0, columnspan=2, pady=8)

    # ---------- 自動吸着 / 検証 ----------
    def _params(self):
        try:
            return float(self.win_v.get()), float(self.srch_v.get())
        except ValueError:
            return 8.0, 60.0

    def auto_snap(self):
        en, jp = self.media["en"], self.media["jp"]
        if not (en and jp):
            return
        win, srch = self._params()
        t_en, t_jp = self.tr["en"].cursor, self.tr["jp"].cursor
        # 開始点を打った後(=区間の終わりを探す時)は、カーソルより前の音で合わせる
        # (区間をまたぐ音で合わせると、CMなど別の部分に吸着しやすいため)
        before = getattr(self, "start", None) is not None
        off = win if before else 0.0
        self.say("解析中…")

        def work():
            r = xcorr_find(en, t_en - off, jp, t_jp - off, win, srch)
            if r:
                r = (r[0] + off, r[1], r[2])
            self.q.put(lambda: self._snap_done(r))
        threading.Thread(target=work, daemon=True).start()

    def _snap_done(self, r):
        if r is None:
            self.say("英語側のその位置が無音か範囲外で、解析できません")
            return
        t, c, z = r
        self.set_cursor("jp", t, scroll=True)
        quality = "良好" if z > 10 and c > 0.3 else "要確認" if z > 6 else "不確か (声だけの場面は合いにくい。BGM/効果音のある所で)"
        self.say(f"日本語側を {tc(t)} に吸着  相関 {c:.2f} / 突出度 {z:.1f}  → {quality}")

    def verify(self):
        en, jp = self.media["en"], self.media["jp"]
        if not (en and jp and self.segs):
            return
        self.say("区間を検証中…")
        segs = [dict(s) for s in self.segs]

        def work():
            res = []
            for s in segs:
                ed = s["en_end"] - s["en_start"]
                jd = (s["jp_end"] - s["jp_start"]) if s["jp_end"] is not None else ed
                ratio = jd / ed
                outs = []
                w = min(4.0, ed)
                for frac in (0.0, 0.5, 1.0):
                    t = s["en_start"] + (ed - w) * frac
                    exp = s["jp_start"] + (t - s["en_start"]) * ratio
                    r = xcorr_find(en, t, jp, exp, win=w, search=1.5)
                    outs.append("—" if r is None or r[2] < 6 else f"{(r[0] - exp) * 1000:+.0f}")
                res.append(" / ".join(outs))
            self.q.put(lambda: self._verify_done(res))
        threading.Thread(target=work, daemon=True).start()

    def _verify_done(self, res):
        for s, r in zip(self.segs, res):
            s["lag"] = r
        self.refresh()
        self.say("検証完了: 各区間の 先頭/中央/末尾 でのズレ(ms)。— は判定不能(無音など)。±40ms超は要調整")

    # ---------- プロジェクト入出力 ----------
    def project_dict(self):
        segs = []
        for s in self.segs:
            d = {"en_start": round(s["en_start"], 4), "en_end": round(s["en_end"], 4),
                 "jp_start": round(s["jp_start"], 4), "fit": s["fit"]}
            if s["jp_end"] is not None:
                d["jp_end"] = round(s["jp_end"], 4)
            if s["note"]:
                d["note"] = s["note"]
            segs.append(d)
        return {"en_video": self.en_var.get(), "jp_media": self.jp_var.get(),
                "jp_audio_stream": self.jp_ast.get(), "output": self.out_path.get(),
                "keep_en_audio": True, "audio_codec": "aac", "audio_bitrate": "256k",
                "segments": segs}

    def save_project(self, ask=True):
        if ask or not self.proj_path:
            p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("プロジェクト", "*.json")],
                                             initialfile=Path(self.proj_path).name if self.proj_path else "project.json")
            if not p:
                return None
            self.proj_path = p
        Path(self.proj_path).write_text(json.dumps(self.project_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        self.say(f"保存: {self.proj_path}")
        return self.proj_path

    def open_project(self):
        p = filedialog.askopenfilename(filetypes=[("プロジェクト", "*.json")])
        if p:
            self.load_project(p)

    def load_project(self, p):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        base = Path(p).parent
        self.proj_path = p
        self.en_var.set(str((base / d["en_video"])))
        self.jp_var.set(str((base / d["jp_media"])))
        self.jp_ast.set(d.get("jp_audio_stream", 0))
        self.out_path.set(str(base / d["output"]) if d.get("output") else "")
        fps = {"en": d.get("en_fps") or 24000 / 1001, "jp": d.get("jp_fps") or 30000 / 1001}
        self.segs = []
        for s in d.get("segments", []):
            if s.get("type", "match") != "match":
                continue
            je = s.get("jp_end")
            es = parse_time(s["en_start"], fps["en"])
            js = parse_time(s["jp_start"], fps["jp"])
            ee = parse_time(s["en_end"], fps["en"]) if "en_end" in s else None
            je = parse_time(je, fps["jp"]) if je is not None else None
            if ee is None:
                ee = es + (je - js)
            self.segs.append({"en_start": es, "en_end": ee, "jp_start": js, "jp_end": je,
                              "fit": s.get("fit", "stretch"), "note": s.get("note", ""), "lag": ""})
        self.segs.sort(key=lambda x: x["en_start"])
        self.refresh()
        self.say("プロジェクトを読み込みました。「読み込み」で素材の波形を読み込んでください")

    # ---------- 合成 ----------
    def render(self):
        if not self.segs:
            messagebox.showinfo("区間なし", "区間がありません。先に「自動で合わせる」を実行してください。")
            return
        if not self.out_path.get():
            self.pick_out()
            if not self.out_path.get():
                return
        out = Path(self.out_path.get())
        if out.exists() and not messagebox.askyesno("上書き確認", f"{out.name} は既にあります。上書きしますか?"):
            return
        # 設定ファイルは保存先の隣に自動で作る(質問しない)
        if not self.proj_path:
            self.proj_path = str(out.with_name(out.stem + ".syncdub.json"))
        self.save_project(ask=False)
        proj = self.proj_path
        self._busy(True)
        self.rbar.start(12)
        self.open_btn.pack_forget()
        self.easy_done.config(text="合成しています…(動画の長さによって数分かかります)", fg="#333")
        self.say("合成中…")
        self.logw(f"--- 合成開始: {proj}")

        def work():
            buf = io.StringIO()
            ok = True
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    main([proj])
            except subprocess.CalledProcessError as ex:
                buf.write("ffmpegエラー:\n" + (ex.stderr or "")[-1500:])
                ok = False
            except (ValueError, KeyError, SystemExit) as ex:
                buf.write(f"エラー: {ex}")
                ok = False
            self.q.put(lambda: self._render_done(buf.getvalue(), ok))
        threading.Thread(target=work, daemon=True).start()

    def _render_done(self, text, ok):
        self.logw(text)
        self.rbar.stop()
        self._busy(False)
        if ok:
            self.easy_done.config(text=f"✔ 完成しました: {Path(self.out_path.get()).name}", fg="#1f6f43")
            self.open_btn.pack(side="left", padx=8)
        else:
            self.easy_done.config(text="⚠ 合成に失敗しました (詳細は「手動調整」タブ下のログ)", fg="#b33")
            messagebox.showerror("合成に失敗", text[-600:])
        self.say("合成完了" if ok else "合成に失敗しました")


if __name__ == "__main__":
    app = App()
    if len(sys.argv) > 1:
        app.load_project(sys.argv[1])
    app.mainloop()

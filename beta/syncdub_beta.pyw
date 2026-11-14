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
import concurrent.futures
import contextlib
import os
import urllib.request
import io
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # ドラッグ&ドロップ対応(任意): pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TkBase = TkinterDnD.Tk
except Exception:  # noqa: BLE001
    DND_FILES, _TkBase = None, tk.Tk

import argparse
import hashlib
import json
import re
from datetime import datetime
import subprocess as _subprocess


class Cancelled(BaseException):
    """「中止」で ffmpeg を止めたときに投げる(普通の Exception では握りつぶされないように BaseException)。"""


_LIVE = set()                      # 動いている ffmpeg など
_LIVE_LOCK = threading.Lock()
_CANCEL = threading.Event()        # 立っている間は、新しい外部プログラムを始めない


def cancel_all():
    """動いている外部プログラム(ffmpeg)を止める。止めた処理は Cancelled を投げる。"""
    _CANCEL.set()
    with _LIVE_LOCK:
        for p in list(_LIVE):
            try:
                p.kill()
            except OSError:
                pass


class _Sub:
    """subprocess の代わり。run だけ、動いている間「止められる」ように登録する。"""

    def __getattr__(self, n):
        return getattr(_subprocess, n)

    def run(self, cmd, input=None, capture_output=False, check=False, **kw):
        if _CANCEL.is_set():
            raise Cancelled()
        if capture_output:
            kw["stdout"] = kw["stderr"] = _subprocess.PIPE
        if input is not None:
            kw["stdin"] = _subprocess.PIPE
        p = _subprocess.Popen(cmd, **kw)
        with _LIVE_LOCK:
            _LIVE.add(p)
        try:
            out, err = p.communicate(input)
        except BaseException:
            p.kill()
            p.wait()
            raise
        finally:
            with _LIVE_LOCK:
                _LIVE.discard(p)
        if _CANCEL.is_set() and p.returncode != 0:
            raise Cancelled()
        if check and p.returncode:
            raise _subprocess.CalledProcessError(p.returncode, cmd, out, err)
        return _subprocess.CompletedProcess(cmd, p.returncode, out, err)


subprocess = _Sub()


def rm_retry(path, tries=8):
    """書きかけのファイルを消す。止めた直後は使用中で消せないことがあるので、少し待って何度か試す。"""
    for _ in range(tries):
        try:
            Path(path).unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(0.3)
    return False
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
               "start_time,avg_frame_rate,field_order", "-of", "json", str(path)]).stdout
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
    def __init__(self, en_start, en_end, jp_start=None, jp_end=None, fit="stretch", note="", english=False):
        self.en_start, self.en_end = en_start, en_end
        self.jp_start, self.jp_end = jp_start, jp_end
        self.fit, self.note = fit, note
        self.english = english  # True: この区間は英語版の音声をそのまま使う

    @property
    def silent(self):
        return self.jp_start is None and not self.english

    @property
    def dur(self):
        return self.en_end - self.en_start


def load_segments(proj, en_dur, en_fps, jp_fps):
    segs = []
    for i, d in enumerate(proj["segments"]):
        t = d.get("type", "match")
        es = parse_time(d["en_start"], en_fps)
        ee = parse_time(d["en_end"], en_fps) if "en_end" in d else None
        if t in ("silence", "english"):
            if ee is None:
                raise ValueError(f"segments[{i}]: {t} には en_end が必要")
            segs.append(Seg(es, ee, note=d.get("note", ""), english=(t == "english")))
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


def build_filter(segs, jp_off, a_index, sr, layout, fade, in_idx=0, lead=0.0, en_idx=None, en_off=0.0):
    """en_idx: 英語版の音声を使う区間(type=english)がある場合の、英語版ファイルの入力番号。"""
    fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={layout}"
    nj = sum(1 for s in segs if not s.silent and not s.english)
    ne = sum(1 for s in segs if s.english)
    if ne and en_idx is None:
        ne = 0  # 英語版が入力にない時は無音にする
    lines, labels, kj, ke = [], [], 0, 0
    for tag, n, idx, ai in (("j", nj, in_idx, a_index), ("e", ne, en_idx, 0)):
        if n:
            outs = "".join(f"[s{tag}{i}]" for i in range(n))
            lines.append(f"[{idx}:a:{ai}]{fmt},asplit={n}{outs}" if n > 1 else f"[{idx}:a:{ai}]{fmt}[s{tag}0]")
    warns = []

    def src_end(x):
        return x.jp_end if x.jp_end is not None else x.jp_start + x.dur

    def continuous(a, b):
        """a の直後に b が来て、元の音も途切れずつながっているか (つながっているなら、境目にフェードは不要)。"""
        if a is None or a.silent or b.silent or a.english != b.english or abs(a.en_end - b.en_start) > 2e-3:
            return False
        if a.english:
            return True
        return abs(src_end(a) - b.jp_start) < 2e-3

    for i, s in enumerate(segs):
        D = s.dur
        f = min(fade, D / 2)
        f_in = 0.0 if i > 0 and continuous(segs[i - 1], s) else f
        f_out = 0.0 if i + 1 < len(segs) and continuous(s, segs[i + 1]) else f
        if s.silent or (s.english and not ne):
            lines.append(f"anullsrc=r={sr}:cl={layout},atrim=0:{D:.6f},asetpts=PTS-STARTPTS[o{i}]")
        else:
            if s.english:
                es = s.en_start + en_off  # 英語版: 映像の先頭=0 → コンテナ時刻へ
                chain = [f"atrim={es:.6f}:{es + D:.6f}", "asetpts=PTS-STARTPTS"]
                src = f"[se{ke}]"
                ke += 1
            else:
                # ffmpegは入力のタイムスタンプをコンテナ開始=0に正規化する。
                # 指定時刻は映像の先頭=0 なので、その差(jp_off)を足す。
                js = s.jp_start + jp_off
                je = (s.jp_end + jp_off) if s.jp_end is not None else js + D
                jdur = je - js
                if s.fit == "stretch" and abs(jdur - D) > 1e-6:
                    r = jdur / D
                    if abs(r - 1) > 0.05:
                        warns.append(f"区間{i} ({s.en_start:.3f}-{s.en_end:.3f}): 速度比 {r:.3f} は大きい")
                    # atempo は出力の頭と末尾に数msの欠け(無音)ができるため、前後に余白をつけて通し、
                    # 余白ぶんを切り捨てる (これをしないと、区間の境目で「ぶつぶつ」と音が途切れる)
                    a0 = min(0.1, max(js, 0.0))
                    a1 = 0.1
                    if abs(r - 1) <= 0.03:
                        # 速さの違いがわずか(3%以内)なら、テープの回転を少し変えるのと同じ方法(サンプリング周波数の変換)で合わせる。
                        # atempo は音の高さを保つかわりに波形をつなぎ直すので、前後の区間と波形がぴったりつながらず、
                        # 境目で「プツッ」と鳴ることがある。この方法なら波形がそのままつながる(音の高さは最大3%変わる)
                        stretch = f"asetrate={sr * r:.6f},aresample={sr}"
                    else:
                        stretch = atempo_chain(r)
                    chain = [f"atrim={js - a0:.6f}:{je + a1:.6f}", "asetpts=PTS-STARTPTS", stretch,
                             f"atrim={a0 / r:.6f}:{a0 / r + D:.6f}", "asetpts=PTS-STARTPTS"]
                else:
                    chain = [f"atrim={js:.6f}:{je:.6f}", "asetpts=PTS-STARTPTS"]
                src = f"[sj{kj}]"
                kj += 1
            chain.append(f"apad=whole_dur={D:.6f}")
            chain.append(f"atrim=0:{D:.6f}")
            chain.append("asetpts=PTS-STARTPTS")
            if f_in > 0:
                chain.append(f"afade=t=in:d={f_in:.4f}:curve=hsin")
            if f_out > 0:
                chain.append(f"afade=t=out:st={D - f_out:.6f}:d={f_out:.4f}:curve=hsin")
            lines.append(src + ",".join(chain) + f"[o{i}]")
        labels.append(f"[o{i}]")
    if lead > 1e-6:  # 英語版の映像先頭がコンテナ開始より遅い分だけ、先頭に無音を置く
        lines.append(f"anullsrc=r={sr}:cl={layout},atrim=0:{lead:.6f},asetpts=PTS-STARTPTS[olead]")
        labels.insert(0, "[olead]")
    lines.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    return ";\n".join(lines), warns


def clip_segments(segs, c0, c1):
    """英語版の c0〜c1 だけを残し、時刻を c0=0 に詰めた区間リストを返す (切り出し用)。"""
    out = []
    for s in segs:
        a, b = max(s.en_start, c0), min(s.en_end, c1)
        if b - a <= 1e-6:
            continue
        n = Seg(a - c0, b - c0, s.jp_start, s.jp_end, s.fit, s.note, s.english)
        if s.jp_start is not None and s.dur > 0:
            je = s.jp_end if s.jp_end is not None else s.jp_start + s.dur
            r = (je - s.jp_start) / s.dur  # 日本語側は、切り詰めた分だけ(速度比をかけて)進める
            n.jp_start = s.jp_start + (a - s.en_start) * r
            if s.jp_end is not None:
                n.jp_end = s.jp_start + (b - s.en_start) * r
        out.append(n)
    return out


_ENC_OK = {}


def encoder_works(name):
    """ffmpeg にそのエンコーダがあり、実際に動くか(短い試験で確かめる。GPUがない等で動かないものを除く)。"""
    if name not in _ENC_OK:
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x240:r=30:d=0.3",
                                "-c:v", name, "-pix_fmt", "yuv420p", "-f", "null", "-"],
                               capture_output=True, timeout=60, **NOWIN)
            _ENC_OK[name] = r.returncode == 0
        except Exception:  # noqa: BLE001
            _ENC_OK[name] = False
    return _ENC_OK[name]


ENC_LABEL = {"hevc_nvenc": "HEVC (NVIDIA)", "libx265": "HEVC (x265)", "libx264": "H.264 (x264)"}


def pick_video_encoder(prefer="auto", interlaced=False):
    """切り出しで使う映像エンコーダ。HEVC(NVIDIA → x265)を優先し、使えなければ H.264(x264)。
    prefer="h264" なら H.264 固定。インターレース映像は、縞を保てる x264 を使う。"""
    if prefer == "h264" or interlaced:
        return "libx264"
    for name in ("hevc_nvenc", "libx265"):
        if encoder_works(name):
            return name
    return "libx264"


def drop_cut_gaps(segs, fps, thr=0.5):
    """切り出しで、英語版にだけある長い隙間(日本語版でカットされた所)を取り除く。
    戻り値: (区間リスト, つなぐ範囲 [(開始, 終了), …])。範囲の端はフレームの境目に合わせる(映像と音のずれを防ぐ)。"""
    keep = [s for s in segs if not (s.silent and s.note.startswith("(自動)") and s.dur >= thr)]
    groups = []
    for s in keep:
        if groups and abs(groups[-1][-1].en_end - s.en_start) < 1e-3:
            groups[-1].append(s)
        else:
            groups.append([s])
    if len(groups) <= 1:
        return keep, [(keep[0].en_start, keep[-1].en_end)] if keep else []
    snap = lambda t: round(t * fps) / fps  # noqa: E731
    pieces = []
    for g in groups:
        a, b = snap(g[0].en_start), snap(g[-1].en_end)
        f0, f1 = g[0], g[-1]
        if f0.jp_start is not None and f0.dur > 0:  # 先頭を動かした分、日本語側の位置も進める
            je = f0.jp_end if f0.jp_end is not None else f0.jp_start + f0.dur
            f0.jp_start += (a - f0.en_start) * ((je - f0.jp_start) / f0.dur)
        f0.en_start = a
        f1.en_end = b
        pieces.append((a, b))
    return [s for g in groups for s in g if s.dur > 1e-3], pieces


# ---------------------------------------------------------------- メイン


def audio_lag(path, win=6.0, rng=3.0, sr=8000):
    """書き出したファイルの日本語音声(1本目)と英語音声(2本目)を、BGM・効果音で突き合わせて、
    全体が一定量ずれていないか調べる。戻り値: ずれ(秒; 負=日本語が早い)、または None(ずれなし・判定不能)"""
    try:
        raws = []
        for i in (0, 1):
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{i}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                               capture_output=True, check=True, **NOWIN).stdout
            raws.append(np.frombuffer(r, dtype=np.int16).astype(np.float32))
        j, e = raws
        n = min(len(j), len(e))
        W, R = int(win * sr), int(rng * sr)
        if n < W + 2 * R:
            # 短いCM(約12秒未満)は、全体を1回で突き合わせる(±2秒)。はっきりした山が1つだけあるときだけ採用する
            if n < 5 * sr:
                return None
            R = int(2.0 * sr)
            a, b = e[:n], j[:n]
            if a.std() < 30 or b.std() < 30:
                return None
            m = 1 << int(np.ceil(np.log2(2 * n)))
            full = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(a, m)), m)
            c = np.concatenate([full[m - R:], full[:R + 1]]) / np.sqrt(float((a * a).sum()) * float((b * b).sum()) + 1e-9)
            i = int(np.argmax(c))
            lag = (i - R) / sr
            far = np.abs(np.arange(len(c)) - i) > int(0.1 * sr)
            if c[i] < 0.45 or abs(lag) < 0.06 or c[R] > 0.5 * c[i] or c[far].max() > 0.8 * c[i]:
                return None
            return float(lag)
        lags, ok0 = [], 0
        for st in range(R, n - W - R + 1, W):
            a = e[st:st + W]
            b = j[st - R:st + W + R]
            if a.std() < 30 or b.std() < 30:
                continue
            m = 1 << int(np.ceil(np.log2(len(a) + len(b))))
            c = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(a, m)), m)[:len(b) - len(a) + 1]
            nrm = np.sqrt(np.convolve(b * b, np.ones(len(a), dtype=np.float32), "valid")[:len(c)] * float((a * a).sum()) + 1e-9)
            c = c / nrm
            i = int(np.argmax(c))
            if c[i] >= 0.2 and c[R] < 0.5 * c[i]:
                lags.append((i - R) / sr)
            elif c[R] >= 0.2:
                ok0 += 1
        if len(lags) < 3:
            return None
        med = float(np.median(lags))
        near = [x for x in lags if abs(x - med) < 0.03]
        # 少なくとも3か所が同じずれを指し、かつ「ずれなしで合っている所」より多いときだけ採用する
        if abs(med) < 0.06 or len(near) < 3 or len(near) < 2 * ok0 or len(near) < 0.5 * len(lags):
            return None
        return float(np.mean(near))
    except Exception:
        return None


def main(argv=None):
    tmp = []  # 作業用の一時ファイルは、成功・失敗にかかわらず最後に必ず消す
    try:
        _main(argv, tmp)
    finally:
        for f in tmp:
            Path(f).unlink(missing_ok=True)


def _main(argv, tmp):
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
    fade = float(proj.get("fade_ms", 3)) / 1000
    jp_a = int(proj.get("jp_audio_stream", 0))
    jp_off, en_off = video_start_offset(jp), video_start_offset(en)
    single = bool(a.audio_only)
    clip = None if single else proj.get("clip")
    if clip:
        # 切り出し: 英語版の c0〜c1 だけを書き出す。英語版の入力は c0 から読むので、音声の時刻も c0=0 に詰める
        c0 = max(0.0, float(clip["start"]))
        c1 = min(en_dur, float(clip["end"])) if en_dur else float(clip["end"])
        fps_c = en_fps or 30000 / 1001
        c0 = round(c0 * fps_c) / fps_c  # 先頭はフレームの境目に合わせる
        segs = clip_segments(segs, c0, c1)
        if not segs or c1 - c0 <= 0.05:
            raise ValueError("切り出す範囲が空です")
        segs, pieces = drop_cut_gaps(segs, fps_c)
        filt, warns = build_filter(segs, jp_off, jp_a, sr, layout, fade, in_idx=1, lead=0.0, en_idx=0, en_off=0.0)
        vchain = None
        if len(pieces) > 1:
            # 日本語版でカットされた(英語版にだけある)部分は、映像も音も飛ばしてつなぐ
            n_ = len(pieces)
            vl = ["[0:v]split=%d%s" % (n_, "".join(f"[vs{i}]" for i in range(n_)))]
            for i, (a_, b_) in enumerate(pieces):
                vl.append(f"[vs{i}]trim=start={a_:.6f}:end={b_:.6f},setpts=PTS-STARTPTS[vt{i}]")
            vl.append("".join(f"[vt{i}]" for i in range(n_)) + f"concat=n={n_}:v=1:a=0[vout]")
            ef, _w = build_filter([Seg(a_, b_, english=True) for a_, b_ in pieces], jp_off, jp_a, sr, layout, fade,
                                  in_idx=1, lead=0.0, en_idx=0, en_off=0.0)
            ef = re.sub(r"\[(se\d+|sj\d+|o\d+|olead|aout)\]", lambda m_: "[x" + m_.group(1) + "]", ef)
            vchain = ";\n".join(vl) + ";\n" + ef
            filt = filt + ";\n" + vchain
            print(f"カット部分を飛ばして {n_} か所をつなぎます: " + ", ".join(f"{a_:.2f}-{b_:.2f}" for a_, b_ in pieces))
    else:
        filt, warns = build_filter(segs, jp_off, jp_a, sr, layout, fade,
                                   in_idx=0 if single else 1, lead=0.0 if single else en_off,
                                   en_idx=1 if single else 0, en_off=en_off)

    print(f"EN映像先頭 +{en_off:.3f}s / JP映像先頭 +{jp_off:.3f}s (コンテナ開始比) / EN長さ {en_dur:.3f}s / 区間 {len(segs)}")
    for i, s in enumerate(segs):
        if s.english:
            desc = "英語版の音声のまま"
        elif s.silent:
            desc = "無音"
        else:
            je = f"{s.jp_end:.3f}" if s.jp_end is not None else "-"
            desc = f"JP {s.jp_start:.3f}-{je} fit={s.fit}"
        print(f"  [{i:2d}] EN {s.en_start:9.3f}-{s.en_end:9.3f}  {desc}  {s.note}")
    for w in warns:
        print("警告:", w, file=sys.stderr)

    fd, script = tempfile.mkstemp(prefix="syncdub_", suffix=".filter.txt")
    os.close(fd)
    script = Path(script)
    tmp.append(script)
    script.write_text(filt, encoding="utf-8")

    if a.audio_only:
        cmd = ["ffmpeg", "-y", "-i", jp_path, "-i", en_path, "-filter_complex_script", str(script),
               "-map", "[aout]", a.audio_only]
    else:
        out = res(proj["output"])
        codec = proj.get("audio_codec", "aac")
        bitrate = proj.get("audio_bitrate", "256k")
        keep_en = proj.get("keep_en_audio", True)
        pre = ["-ss", f"{c0 + en_off:.6f}", "-t", f"{c1 - c0:.6f}"] if clip else []
        cmd = ["ffmpeg", "-y"] + pre + ["-i", en_path, "-i", jp_path, "-filter_complex_script", str(script),
                                         "-map", "[vout]" if (clip and vchain) else "0:v:0", "-map", "[aout]"]
        if keep_en:
            cmd += ["-map", "[xaout]" if (clip and vchain) else "0:a:0"]
        if clip:  # 映像を再エンコード (キーフレームに関係なく、フレーム単位で切り出せる)
            fo = next((st.get("field_order") for st in en["streams"] if st["codec_type"] == "video"), None)
            interlaced = fo in ("tt", "bb", "tb", "bt")
            enc = pick_video_encoder(str(clip.get("codec", "auto")), interlaced)
            crf = int(clip.get("crf", 18))
            if enc == "hevc_nvenc":
                cmd += ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-forced-idr", "1", "-bf", "0"]
            elif enc == "libx265":
                cmd += ["-c:v", enc, "-preset", str(clip.get("preset", "medium")), "-crf", str(crf),
                        "-x265-params", "log-level=error:bframes=0:open-gop=0"]
            else:
                cmd += ["-c:v", "libx264", "-preset", str(clip.get("preset", "medium")), "-crf", str(crf), "-bf", "0"]
                if interlaced:  # インターレース映像は、そのまま(縞を保って)エンコードする
                    cmd += ["-flags", "+ilme+ildct", "-top", "1" if fo in ("tt", "tb") else "0"]
            # 約2秒ごとに必ずキーフレームを入れ、Bフレームなし・閉じたGOPにする (プレーヤーでシークしたとき、映像が乱れにくい単純な構造)
            cmd += ["-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*2)", "-avoid_negative_ts", "make_zero"]
            print(f"映像エンコーダ: {enc}")
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-c:a:0", codec]
        if codec in ("aac", "ac3", "eac3", "libopus"):
            cmd += ["-b:a:0", bitrate]
        if keep_en:
            # 切り出しでは、英語版の音声も(シークの位置ずれを避けるため)エンコードし直す。通常は無加工コピー
            cmd += ["-c:a:1", codec] if clip else ["-c:a:1", "copy"]
            if clip and codec in ("aac", "ac3", "eac3", "libopus"):
                cmd += ["-b:a:1", bitrate]
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


APP_VERSION = "2026.11.10.41"
BETA_REVIEW = True   # ベータ版用: 「録画から探す」の終わりに確認ウインドウを開く(正式版では False にする)
BETA_BUILD = 18       # ベータ版の通し番号(直すたびに上げる)。ベータ版は beta/version.json を見て、自分で更新する
BETA_FILE = "beta/syncdub_beta.pyw"
REPO = "tatata17/syncdub"
APP_FILE = "syncdub.pyw"
KEEP_BACKUPS = 5
TEST_BASE = os.environ.get("SYNCDUB_TEST_BASE")  # 試験用: ローカルサーバのURL
SR = 8000          # 波形・解析用のサンプルレート
PYR = 64           # 波形ピラミッドの縮約率
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
# ファイルを選ぶ画面で見せる種類(指定しないと、環境によっては .m2ts などが一覧に出ないことがある)
MEDIA_TYPES = [("動画・音声", "*.m2ts *.ts *.mts *.m2t *.mp4 *.mkv *.mov *.avi *.wmv *.mpg *.mpeg *.m4v *.flv *.webm *.vob "
                              "*.wav *.mp3 *.aac *.m4a *.ac3 *.flac *.mka *.ogg *.opus *.wma"), ("すべてのファイル", "*.*")]
# 区間ごとの色(薄い色=帯、濃い色=文字や境界線)。英語側・日本語側で同じ区間は同じ色になる
PASTEL = ["#ffd9d9", "#d9e8ff", "#dcf3d2", "#fff1c9", "#e8dcff", "#ffdcee", "#d3f2f1", "#ece9cf"]
DARK = ["#c0392b", "#2f6fd0", "#2e8b3d", "#b8860b", "#7b4fd0", "#c2307a", "#1f8f8c", "#7a7a2a"]
FIT_JA = {"stretch": "伸縮して合わせる", "pad": "足りない分は無音で埋める", "trim": "余った分は切り捨てる"}
JA_FIT = {v: k for k, v in FIT_JA.items()}


def seg_letter(i):
    """0→A, 1→B, … 25→Z, 26→AA"""
    i, out = i + 1, ""
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


# ------------------------------------------------------------------ GitHub自己更新
# 公開リポジトリ main の version.json {"version": "...", "file": "syncdub.pyw", "notes": ""} を見に行く。
# 配信キャッシュで版が食い違わないよう、最新コミットIDを調べてコミット固定URLから取得する。


def _http(url, accept=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "syncdub-updater", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _http_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "syncdub-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def fetch_update_info():
    """戻り値: (info, ref)。ref はコミットID(取れなければ 'main')。ベータ版は beta/version.json を見る。"""
    vj = "beta/version.json" if BETA_REVIEW else "version.json"
    if TEST_BASE:
        return json.loads(_http(f"{TEST_BASE}/{vj}")), ""
    ref = "main"
    try:
        sha = _http(f"https://api.github.com/repos/{REPO}/commits/main",
                    accept="application/vnd.github.sha").strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            ref = sha
    except Exception:  # noqa: BLE001  (API制限などは main にフォールバック)
        pass
    return json.loads(_http(f"https://raw.githubusercontent.com/{REPO}/{ref}/{vj}")), ref


def update_is_newer(info):
    """version.json の版が、いま動いている版より新しいか。ベータ版は (版番号, ベータの通し番号) で比べる。"""
    if BETA_REVIEW:
        return (vkey(info["version"]), int(info.get("beta", 0))) > (vkey(APP_VERSION), BETA_BUILD)
    return vkey(info["version"]) > vkey(APP_VERSION)


def ver_label(info=None):
    if info is None:
        return f"v{APP_VERSION}" + (f" ベータ{BETA_BUILD}" if BETA_REVIEW else "")
    return f"v{info['version']}" + (f" ベータ{info.get('beta', 0)}" if BETA_REVIEW else "")


def download_update_source(info, ref):
    base = TEST_BASE or f"https://raw.githubusercontent.com/{REPO}/{ref}"
    raw = _http_bytes(f"{base}/{info.get('file', BETA_FILE if BETA_REVIEW else APP_FILE)}")
    if info.get("sha256"):     # 配布ファイルが途中で壊れたり、別の版と入れ違ったりしていないかを確かめる
        # 改行を LF にそろえてから比べる(tools/release.py と同じ計算。改行コードの違いだけで更新が止まらないように)
        got = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        if got != str(info["sha256"]).lower():
            raise ValueError("ダウンロードしたファイルが、配布元の記録(SHA-256)と一致しません。\n"
                             "公開直後なら、数分待つと直ります。しばらく待っても同じときは、待っても直りません。"
                             "配布ページから新しいファイルを入れ直してください")
    code = raw.decode("utf-8").replace("\r\n", "\n")   # 保存のときに改行が二重にならないように、LF にそろえておく
    m = re.search(r'^APP_VERSION = "([^"]+)"', code, re.M)
    if BETA_REVIEW:
        mb = re.search(r"^BETA_BUILD = (\d+)", code, re.M)
        if not mb or int(mb.group(1)) != int(info.get("beta", 0)):
            raise ValueError(f"ベータの番号が一致しません (version.json {info.get('beta')} / 本体 {mb.group(1) if mb else '?'})。"
                             "公開直後の可能性があります。少し待って再試行してください")
    if not m or m.group(1) != info["version"]:
        raise ValueError(f"版番号が一致しません (version.json {info['version']} / 本体 {m.group(1) if m else '?'})。"
                         "公開直後の可能性があります。少し待って再試行してください")
    compile(code, APP_FILE, "exec")  # 壊れたファイルで上書きしないための構文チェック
    return code


def _backup_dir(me):   # ベータ版のバックアップは、正式版のものと混ざらないように別のフォルダに置く
    return me.parent / ("syncdub_beta_backups" if BETA_REVIEW else "syncdub_backups")


def install_update(code, me):
    bdir = _backup_dir(me)
    bdir.mkdir(exist_ok=True)
    tag = f"{APP_VERSION}.b{BETA_BUILD}" if BETA_REVIEW else APP_VERSION
    (bdir / f"syncdub_{tag}.pyw").write_text(me.read_text(encoding="utf-8"), encoding="utf-8")
    for old in sorted(bdir.glob("syncdub_*.pyw"), key=lambda f: vkey(f.stem))[:-KEEP_BACKUPS]:
        old.unlink()
    tmp = me.with_suffix(me.suffix + ".new")
    tmp.write_text(code, encoding="utf-8")
    os.replace(tmp, me)


def latest_backup(me):
    files = sorted(_backup_dir(me).glob("syncdub_*.pyw"), key=lambda f: vkey(f.stem))
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
    def __init__(self, path, astream=0, parts=None):
        """parts: [(開始, 終了), …] を渡すと、その区間の音声だけを読み込む(slice で使えるのはその区間の中だけ)。"""
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
        self.chunks = None
        if parts is not None:
            self.chunks = []
            for t0, t1 in parts:
                ta = max(t0 - 0.1, self.a_origin)
                tb = t1 + 0.1
                if tb <= ta:
                    continue
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", f"{ta + self.voff:.4f}", "-t", f"{tb - ta:.4f}", "-i", path,
                     "-map", f"0:a:{self.astream}", "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                    capture_output=True, check=True, **NOWIN).stdout
                self.chunks.append((int(round((ta - self.a_origin) * SR)), np.frombuffer(r, dtype=np.int16).astype(np.float32) / 32768))
            self.x = np.zeros(0, np.float32)
            self.mn = self.mx = np.zeros(0, np.float32)
            self.scale = 1.0
            self.end = fmt_duration(self.info) - self.voff
            return
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
        if self.chunks is not None:   # 区間だけ読み込んだ場合
            for cs, cx in self.chunks:
                a, b = max(i0, cs), min(i0 + n, cs + len(cx))
                if b > a:
                    out[a - i0: b - i0] = cx[a - cs: b - cs]
            return out
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
    def frame_png(self, t, width=192):
        ts = max(0.0, t + self.voff)
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{ts:.4f}", "-i", self.path, "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, **NOWIN)
        return r.stdout


class LightMedia:
    """探索専用の軽い読み込み。音声だけを4kHzで読み込む(波形表示用の処理をしない)。"""
    sr = 4000

    def __init__(self, path, astream=0):
        info = probe(path)
        cstart, voff = fmt_start(info), video_start_offset(info)
        auds = audio_streams(info)
        if not auds:
            raise RuntimeError("音声ストリームがありません")
        astream = min(astream, len(auds) - 1)
        st = auds[astream].get("start_time")
        ast = float(st) if st not in (None, "N/A") else cstart
        self.a_origin = (ast - cstart) - voff
        self.path, self.voff = path, voff
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{astream}", "-vn",
             "-ac", "1", "-ar", str(self.sr), "-f", "s16le", "-"],
            capture_output=True, check=True, **NOWIN).stdout
        self.x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


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


def _phat_local(a, b, fs):
    """位相相関(白色化した相関)。吹き替えの声のように「共通しない音」が大きくても、BGM・効果音の一致を拾いやすい。
    長さ len(b)-len(a)+1 の配列を返す。"""
    n, m = len(a), len(b)
    N = 1 << int(np.ceil(np.log2(m + n)))
    G = np.fft.rfft(b - b.mean(), N) * np.conj(np.fft.rfft(a - a.mean(), N))
    G = G / (np.abs(G) + 1e-9)
    f = np.fft.rfftfreq(N, 1 / fs)
    G = G * ((f > 250) & (f < fs * 0.48))
    return np.fft.irfft(G, N)[: m - n + 1]


def vscroll(tree):
    """表(Treeview)の右に縦のスクロールバーを付ける。tree.pack(side="left") の直後に呼ぶ。"""
    sb = ttk.Scrollbar(tree.master, orient="vertical", command=tree.yview)
    sb.pack(side="left", fill="y")
    tree.configure(yscrollcommand=sb.set)


def enable_multiselect(tree, on_delete=None):
    """一覧の複数選択(RangeSelect)に、Ctrl+A(全部選ぶ)と Delete(外す)を足す。"""
    tree._range = RangeSelect(tree)

    def all_(e):
        tree.selection_set(list(tree.get_children()))
        return "break"
    tree.bind("<Control-a>", all_)
    tree.bind("<Control-A>", all_)
    if on_delete:
        tree.bind("<Delete>", lambda e: (on_delete(), "break")[1])


def _thumb(m, t, W=48, H=36):
    """時刻 t(映像の先頭=0)の画面を、小さな白黒画像にして返す。取れない・真っ暗なときは None。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t + m.voff):.3f}", "-i", m.path, "-frames:v", "1",
                        "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True, **NOWIN)
    if len(r.stdout) != W * H:
        return None
    a = np.frombuffer(r.stdout, np.uint8).astype(np.float32).reshape(H, W)
    sd = float(a.std())
    return None if sd < 4.0 else (a - a.mean()) / sd


def _has_video(m):
    return any(s["codec_type"] == "video" for s in m.info["streams"])


def _fmt_t(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _pair_sim(a, b):
    return float((a * b).mean())


def _pick_offset_by_video(en, jp, js, dur, offs, offs_z, own, e_dur):
    """日本語の js〜js+dur に合う英語版の位置(オフセット)を、音の候補どうしで画面の似かたから比べる。
    戻り値: (最も似ているオフセット, 一致度, 自分の一致度, [(オフセット, 一致度), …])。判定できなければ None。"""
    ts = [js + dur * f for f in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    jt = [(t, _thumb(jp, t)) for t in ts]
    jt = [(t, a) for t, a in jt if a is not None]
    if len(jt) < 3:
        return None
    cands = {round(own, 3): own}
    order = sorted(range(len(offs)), key=lambda i: -offs_z[i])
    for i in order[:8]:
        if offs_z[i] >= 6:
            cands.setdefault(round(offs[i], 3), offs[i])
    res = []
    for o in cands.values():
        sc = []
        for t, a in jt:
            et = t - o
            if et < 0 or et > e_dur - 0.05:
                continue
            b = _thumb(en, et)
            if b is not None:
                sc.append(_pair_sim(a, b))
        if len(sc) >= 3:
            res.append((o, float(np.mean(sc))))
    if not res:
        return None
    own_v = next((v for o, v in res if abs(o - own) < 1e-3), None)
    if own_v is None:
        return None
    best = max(res, key=lambda x: x[1])
    return best[0], best[1], own_v, res


VID_MIN = 0.62   # 映像の似かた(相関)がこれ未満なら「映像が合わない」とみなして書き出さない(BGMだけ同じ別CMは0.61以下、本物は0.67以上だった)
VID_OK = 0.70    # これ以上なら映像が合っているとみなし、音の「一部だけ一致」「別のCMとも似ている」の要確認は付けない


def vid_good(d):
    """映像まで合っているとみなせるか。0.70以上、または音がとても強ければ0.60以上(本物は0.67〜0.98、偽物は0.61が最高だった)。"""
    v = d.get("v")
    return v is not None and (v >= VID_OK or (v >= 0.60 and d.get("z", 0) >= 40))


DRIFT_K = 0.00045   # 録画によっては音声の時刻が映像より時間に比例して遅れる(実測: 約0.00045秒/秒。1時間で約1.6秒)

def video_similarity(en, a, L, jp, t, cache, fr=(0.1, 0.3, 0.5, 0.7, 0.9), detail=None):
    """英語版CM(en の a〜a+L 秒)と、録画(jp)の t から L 秒の画面を、数か所で見比べた平均の似かた。比べられなければ None。
    後半の時刻では、音声の遅れ分だけ早い位置の映像でも見て、高いほうを採る。"""
    v = _video_sim(en, a, L, jp, t, cache, fr, detail)
    if t > 300 and (v is None or v < VID_OK):
        d2 = [] if detail is not None else None
        v2 = _video_sim(en, a, L, jp, t - DRIFT_K * t, cache, fr, d2)
        if v2 is not None and (v is None or v2 > v):
            if detail is not None:
                detail[:] = d2
            return v2
    return v


def _video_sim(en, a, L, jp, t, cache, fr, detail):
    sc = []
    for q in fr:
        key = ("e", round(a + L * q, 2))   # 時刻そのものをキーにする(同じ開始でも長さが違えば別の画面)
        if key not in cache:
            cache[key] = _thumb(en, a + L * q)
        x = cache[key]
        if x is None:
            continue
        jk = ("j", round(t + L * q, 1))
        if jk not in cache:
            cache[jk] = _thumb(jp, t + L * q)
        y = cache[jk]
        if y is not None:
            sc.append(_pair_sim(x, y))
    if detail is not None:
        detail.extend(round(float(c), 2) for c in sc)
    return round(float(np.mean(sc)), 2) if len(sc) >= 3 else None


def _best_peak(a0, b, fs):
    """通常の正規化相関と位相相関の両方で一番はっきりした山を探し、(位置, 突出度z) を返す。"""
    best = (0, -1.0)
    for c in (_nc_local(a0, b), _phat_local(a0, b, fs)):
        k = int(c.argmax())
        z = (c[k] - c.mean()) / (c.std() + 1e-9)
        if z > best[1]:
            best = (k, float(z))
    return best


def _refine_offsets(segs, esl, jsl, fs, rms_thr):
    """区間の中で、音の合う位置を3秒おきに測り直し、途中で時間差が少しだけ変わる所(0.03秒以上)があれば、そこで区間を分ける。
    区間全体が同じだけずれているときは、その分だけ直す。伸縮つきの区間も、「一定の時間差がいくつか続く形」のほうが
    よく合うなら、その形に直す。測れる所が少ない区間や、時間差が速く動き続ける区間は変えない。"""
    WL, RAD, TOL = 5.0, 0.35, 0.03
    out = []
    for sg in segs:
        d = sg["en_end"] - sg["en_start"]
        if d < 30.0:
            out.append(sg)
            continue
        o_s = sg["jp_start"] - sg["en_start"]
        o_e = o_s if sg.get("jp_end") is None else sg["jp_end"] - sg["en_end"]

        def o_now(t, sg=sg, o_s=o_s, o_e=o_e, d=d):     # いまの区間が、時刻 t に当てている時間差
            return o_s + (o_e - o_s) * (t - sg["en_start"]) / d
        smp = []
        for t in np.arange(sg["en_start"], sg["en_end"] - WL, 3.0):
            a = esl(t, WL)
            if float(np.sqrt((a ** 2).mean())) < rms_thr:
                continue
            oc = o_now(t + WL / 2)
            b = jsl(t + oc - RAD, WL + 2 * RAD)
            if np.abs(b).max() < 1e-6:
                continue
            k_, z_ = _best_peak(a - a.mean(), b, fs)
            lag = -RAD + k_ / fs
            if z_ >= 6.0 and abs(lag) < RAD - 0.03:
                smp.append((float(t), oc + lag))
        # 時間差がそろっているまとまりに分ける(1つだけ外れた値は捨て、2つ続けて新しい値になったら別のまとまり)
        groups, cur, pend = [], None, []
        for i, (t, o) in enumerate(smp):
            if cur is not None and abs(o - float(np.median([smp[k][1] for k in cur]))) <= TOL:
                cur.append(i)
                pend = []
            else:
                pend.append(i)
                if len(pend) >= 2 and abs(smp[pend[-1]][1] - smp[pend[-2]][1]) <= TOL:
                    cur = pend[-2:]
                    groups.append(cur)
                    pend = []
        big = [g for g in groups if len(g) >= 4]
        if not big or sum(len(g) for g in big) < 0.7 * len(smp):
            out.append(sg)                              # 測れる所が少ない・時間差が動き続けている → そのまま
            continue
        mg = []
        for g in big:
            a_ = float(np.median([smp[k][1] for k in g]))
            if mg and abs(a_ - mg[-1]["a"]) < TOL:
                mg[-1]["t_last"], mg[-1]["n"] = smp[g[-1]][0], mg[-1]["n"] + len(g)
            else:
                mg.append({"a": a_, "t_first": smp[g[0]][0], "t_last": smp[g[-1]][0], "n": len(g)})
        if all(abs(g["a"] - o_now(t_)) < TOL for g in mg for t_ in (g["t_first"], g["t_last"] + WL)):
            out.append(sg)                              # いまのままで合っている
            continue

        def ncc(t, o):
            a = esl(t, 1.0)
            b = jsl(t + o - 0.01, 1.02)
            a = a - a.mean()
            na = float(np.linalg.norm(a))
            if na < rms_thr * np.sqrt(len(a)):
                return None
            cc = np.correlate(b, a, "valid")
            e2 = np.convolve(b ** 2, np.ones(len(a)), "valid")
            return float((cc / (na * np.sqrt(np.maximum(e2, 1e-12)))).max())
        bnd = [sg["en_start"]]
        for g1, g2 in zip(mg, mg[1:]):
            # 切り替わる所は、測れた範囲の少し外(手前3秒〜先8秒)まで広げて探す。3秒おきの測定は、繰り返しのある音では
            # 端の1〜2個がとなりの時間差に引っぱられることがあり、その内側だけで探すと切り替わる所を早く(遅く)決めてしまう
            ts = np.arange(max(sg["en_start"] + 1.0, g1["t_last"] - 3.0),
                           min(sg["en_end"] - 2.0, max(g1["t_last"] + 0.25, g2["t_first"] + WL + 8.0)), 0.25)
            pf = []
            for t in ts:
                c1, c2 = ncc(t, g1["a"]), ncc(t, g2["a"])
                pf.append(0.0 if c1 is None or c2 is None or max(c1, c2) < 0.15 else c1 - c2)   # どちらにも合わない所は判断に使わない
            pf = np.array(pf)
            if not len(pf) or np.abs(pf).max() < 0.05:
                x = (g1["t_last"] + g2["t_first"] + WL) / 2
            else:
                k = int(np.argmax([pf[:i].sum() - pf[i:].sum() for i in range(len(pf) + 1)]))
                x = float(ts[k] if k < len(ts) else ts[-1] + 0.25)
            bnd.append(min(max(x, bnd[-1] + 1.0), sg["en_end"] - 1.0))
        bnd.append(sg["en_end"])
        prev_je = None
        for q, (g, x0, x1) in enumerate(zip(mg, bnd, bnd[1:])):
            js = x0 + g["a"]
            if prev_je is not None and js < prev_je:      # 日本語が重なる(英語版のほうが少し長い) → その分、英語側に間をあける
                x0, js = x0 + (prev_je - js), prev_je
            if x1 - x0 < 0.5:
                continue
            ns = dict(sg, en_start=round(float(x0), 4), en_end=round(float(x1), 4), jp_start=round(float(js), 4), jp_end=None)
            if q > 0:
                ns.pop("trim_s", None)
            if q < len(mg) - 1:
                ns.pop("trim_e", None)
            out.append(ns)
            prev_je = js + (x1 - x0)
    segs[:] = out


def _refine_edges(segs, esl, jsl, fs, rms_thr):
    """区間の頭と終わりの短い部分(題名の曲など、数秒だけ)が、区間の本体と少しだけ(0.03〜0.3秒)ずれているとき、そこを分ける。
    頭・終わりの3秒を1秒ずらして2回測り、2回とも同じだけずれていて、一致がはっきりしているときだけ直す。"""
    WL, RAD = 3.0, 0.35

    def lag_at(t, o):
        a = esl(t, WL)
        if float(np.sqrt((a ** 2).mean())) < rms_thr:
            return None
        b = jsl(t + o - RAD, WL + 2 * RAD)
        if np.abs(b).max() < 1e-6:
            return None
        k_, z_ = _best_peak(a - a.mean(), b, fs)
        lag = -RAD + k_ / fs
        return lag if z_ >= 8.0 and abs(lag) < RAD - 0.03 else None

    def ncc(t, o):
        a = esl(t, 1.0)
        b = jsl(t + o - 0.01, 1.02)
        a = a - a.mean()
        na = float(np.linalg.norm(a))
        if na < rms_thr * np.sqrt(len(a)):
            return None
        cc = np.correlate(b, a, "valid")
        e2 = np.convolve(b ** 2, np.ones(len(a)), "valid")
        return float((cc / (na * np.sqrt(np.maximum(e2, 1e-12)))).max())

    def edge(sg, head):
        o = sg["jp_start"] - sg["en_start"]
        ts = (sg["en_start"], sg["en_start"] + 1.0) if head else (sg["en_end"] - WL, sg["en_end"] - WL - 1.0)
        ls = [lag_at(t, o) for t in ts]
        if None in ls or abs(ls[0] - ls[1]) > 0.03 or not 0.03 <= abs(sum(ls) / 2) <= 0.3:
            return None
        oe = o + sum(ls) / 2
        # 本体に切り替わる所: 端から12秒までを0.25秒おきに見て、どちらの時間差に合うかで決める
        grid = np.arange(0.0, min(12.0, sg["en_end"] - sg["en_start"] - 4.0), 0.25)
        pf = []
        for u in grid:
            t = sg["en_start"] + u if head else sg["en_end"] - 1.0 - u
            c1, c2 = ncc(t, oe), ncc(t, o)
            pf.append(0.0 if c1 is None or c2 is None else c1 - c2)
        pf = np.array(pf)
        if not len(pf) or pf[:4].sum() < 0.2:      # 端の1秒で、はっきり端の時間差のほうが合っていること
            return None
        k = int(np.argmax([pf[:i].sum() - pf[i:].sum() for i in range(len(pf) + 1)]))
        if k < 4 or k >= len(grid):               # 1秒未満、または12秒以上 → 端だけのずれではない
            return None
        return oe, float(grid[k])

    out = []
    for sg in segs:
        if sg.get("jp_end") is not None or sg["en_end"] - sg["en_start"] < 10.0:
            out.append(sg)
            continue
        o = sg["jp_start"] - sg["en_start"]
        parts = [(sg["en_start"], sg["en_end"], o)]
        h = edge(sg, True)
        if h:
            parts = [(sg["en_start"], sg["en_start"] + h[1], h[0]), (sg["en_start"] + h[1], sg["en_end"], o)]
        t_ = edge(sg, False)
        if t_ and sg["en_end"] - t_[1] - parts[-1][0] > 4.0:
            x0, x1, oo = parts[-1]
            parts[-1:] = [(x0, sg["en_end"] - t_[1], oo), (sg["en_end"] - t_[1], x1, t_[0])]
        if len(parts) == 1:
            out.append(sg)
            continue
        prev_je = None
        if out:
            pv = out[-1]
            prev_je = pv["jp_end"] if pv.get("jp_end") is not None else pv["jp_start"] + (pv["en_end"] - pv["en_start"])
        for q, (x0, x1, oo) in enumerate(parts):
            js = x0 + oo
            if prev_je is not None and js < prev_je and prev_je - js < 0.5:   # 日本語が少し重なる → その分、英語側に間をあける
                x0, js = x0 + (prev_je - js), prev_je
            ns = dict(sg, en_start=round(float(x0), 4), en_end=round(float(x1), 4), jp_start=round(float(js), 4), jp_end=None)
            if q > 0:
                ns.pop("trim_s", None)
            if q < len(parts) - 1:
                ns.pop("trim_e", None)
            out.append(ns)
            prev_je = js + (x1 - x0)
    segs[:] = out


def _splice_at_quiet(segs, jsl, jthr, reach=2.0, esl=None, en_thr=None):
    """となり合う区間のつなぎ目が、日本語の音の鳴っている最中にあるとき、近く(前後 reach 秒)の日本語が静かな所へずらす。
    英語版だけ少し長い(短い)所では、日本語を少し飛ばす(間をあける)ことになる。その切れ目が曲の途中にあると
    音がプツッと切れて聞こえるので、無音の所に持っていく。大きく離れた区間(CMをまたぐ所)は対象にしない。"""
    def jq(t):      # 日本語の、時刻 t の前後0.1秒の音量
        return float(np.sqrt((jsl(t - 0.1, 0.2) ** 2).mean()))
    quiet = 0.5 * jthr
    for p, s in zip(segs, segs[1:]):
        if s.get("jp_end") is not None:
            continue                                  # 後ろが伸縮のある区間なら動かさない
        p_str = p.get("jp_end") is not None           # 前が伸縮つき: 英語の無音へ移す処理だけ行う
        rp = (p["jp_end"] - p["jp_start"]) / (p["en_end"] - p["en_start"]) if p_str else 1.0
        pe = p["jp_end"] if p_str else p["jp_start"] + (p["en_end"] - p["en_start"])
        g_en, g_jp = s["en_start"] - p["en_end"], s["jp_start"] - pe
        if -0.12 <= g_jp < -0.002 and s["en_end"] - s["en_start"] > 1.0 - g_jp:
            # 日本語が少し重なっている(同じ所を2度使う形) → 後ろの区間の頭をその分だけ遅らせて、日本語がひと続きになるようにする
            s["en_start"] = round(s["en_start"] - g_jp, 4)
            s["jp_start"] = round(pe, 4)
            g_en, g_jp = s["en_start"] - p["en_end"], 0.0
        if not (-0.01 <= g_en <= 3.0 and -0.01 <= g_jp <= 3.0) or (g_en < 0.02 and g_jp < 0.02):
            continue
        if max(jq(pe), jq(s["jp_start"])) < quiet:
            continue                                  # もう静かな所で切れている
        # 英語版だけが少し長い所(日本語はつながっている)は、近く(前後10秒)に「英語がその長さぶん無音」の所があれば、そこが本当の場所。
        # (英語版は場面の変わり目の無音が長く、日本語の録画は短い、という形が多い) 見つかれば、間をそこへ移す
        if esl is not None and en_thr is not None and g_en >= 0.05 and abs(g_jp) < 0.02:
            lo6 = -min(10.0, p["en_end"] - p["en_start"] - 1.0)
            hi6 = min(10.0, s["en_end"] - s["en_start"] - 1.0)

            def rms_of(x_):
                return float(np.sqrt((x_ ** 2).mean())) if len(x_) else 0.0
            cand = []
            for dd in np.arange(lo6, hi6 + 1e-6, 0.02):
                x = p["en_end"] + dd
                jr = rms_of(jsl(pe + dd * rp - 0.03, 0.06))          # 日本語を切る所(前後0.03秒)の音量
                if jr >= 0.5 * jthr:
                    continue                                          # 日本語が鳴っている最中では切らない
                er = rms_of(esl(x, g_en))                             # 間にする範囲の、英語の音量
                e_min = min(rms_of(esl(x + u, 0.1)) for u in np.arange(0.0, max(g_en - 0.1, 0.0) + 1e-6, 0.05))
                # 英語側は、その範囲のどこかで無音になり、全体としても静か(ふだんの半分以下)であること。
                # 曲の余韻が少し残っていてもよい(日本語の録画は、余韻と無音をまとめて短くしていることがある)
                if e_min < en_thr and er < 5.0 * en_thr:
                    cand.append((er, abs(dd), float(dd)))
            if cand:
                dd = min(cand)[2]
                p["en_end"] = round(p["en_end"] + dd, 4)
                if p_str:
                    p["jp_end"] = round(p["jp_end"] + dd * rp, 4)
                s["en_start"] = round(s["en_start"] + dd, 4)
                s["jp_start"] = round(pe + dd * rp + g_jp, 4)
                continue
        if p_str:
            continue
        lo = -min(reach, p["en_end"] - p["en_start"] - 1.0)
        hi = min(reach, s["en_end"] - s["en_start"] - 1.0)
        best = None
        for d in np.arange(0.05, max(hi, -lo) + 1e-6, 0.05):
            for dd in (d, -d):
                if lo <= dd <= hi and max(jq(pe + dd), jq(s["jp_start"] + dd)) < quiet:
                    best = float(dd)
                    break
            if best is not None:
                break
        if best is None:
            continue
        p["en_end"] = round(p["en_end"] + best, 4)
        s["en_start"] = round(s["en_start"] + best, 4)
        s["jp_start"] = round(s["jp_start"] + best, 4)
    # 静かな所が近くになく、音の鳴っている最中に小さな食い違い(0.12秒以下)が残ったつなぎ目は、
    # 無音を入れたり音を飛ばしたりせず、手前の数秒をごくわずかに伸び縮みさせて吸収する(プツッという途切れを作らない)
    out = []
    for q, sg in enumerate(segs):
        nx = segs[q + 1] if q + 1 < len(segs) else None
        if nx is None or sg.get("jp_end") is not None or nx.get("jp_end") is not None:
            out.append(sg)
            continue
        d = sg["en_end"] - sg["en_start"]
        pe = sg["jp_start"] + d
        g_en, g_jp = nx["en_start"] - sg["en_end"], nx["jp_start"] - pe
        if not (-0.002 <= g_en <= 0.12 and -0.002 <= g_jp <= 0.12) or abs(g_en - g_jp) < 0.005:
            out.append(sg)
            continue
        if max(jq(pe), jq(nx["jp_start"])) < quiet:
            out.append(sg)                               # 静かな所で切れているので、そのままで聞こえない
            continue
        T = min(max(3.0, abs(g_en - g_jp) / 0.01), d - 1.0)    # 伸び縮みは1%まで(最低3秒かけてならす)
        if T < 0.5:
            out.append(sg)
            continue
        x = round(sg["en_end"] - T, 4)
        br = dict(sg, en_start=x, en_end=nx["en_start"], jp_start=round(pe - T, 4), jp_end=nx["jp_start"])
        br["flag"] = ""          # つなぎの数秒は、前の区間の続き。要確認には数えない
        br.pop("trim_s", None)
        br.pop("trim_e", None)
        te = sg.pop("trim_e", None)
        sg["en_end"] = x
        out += [sg, br]
        if te:
            br["trim_e"] = te
    segs[:] = out


def _trim_to_en_silence(segs, esl, jsl, rms_thr, jthr, min_tail=0.3):
    """区間の端が英語側の無音に掛かっているとき、そこまで日本語を入れない(segs を書き換える)。"""
    # ---- 6.9) 区間の端が英語側の無音に掛かっているとき、そこまで日本語を入れない
    #      (CM明けのジングルなど、日本語にだけある音が英語の無音部分に混ざるのを防ぐ)
    def _cm_after(sg0):   # この区間のすぐあとに、日本語にだけある所(CM)が1秒以上あるか
        je0 = sg0["jp_end"] if sg0["jp_end"] is not None else sg0["jp_start"] + (sg0["en_end"] - sg0["en_start"])
        nx = [x for x in segs if x["en_start"] >= sg0["en_end"] - 1e-6 and x is not sg0]
        return bool(nx) and min(nx, key=lambda x: x["en_start"])["jp_start"] - je0 > 1.0

    QT = 1.0   # 英語の0.1秒の音量が、「音がある所」の目安のこの倍率より小さければ無音とみなす
    for sg_ in segs:
        d_en = sg_["en_end"] - sg_["en_start"]
        if d_en < 1.0:
            continue
        # 日本語がわずかに伸縮している区間(jp_end あり)は、その比率で日本語側の時刻も動かす
        r_ = 1.0 if sg_["jp_end"] is None else (sg_["jp_end"] - sg_["jp_start"]) / d_en

        def jrms(t0_, t1_):
            return float(np.sqrt((jsl(t0_, max(t1_ - t0_, 0.05)) ** 2).mean()))

        # 先頭: 英語が無音のあいだ進める(日本語に音があるときだけ。最大3秒)
        t_ = sg_["en_start"]
        while t_ - sg_["en_start"] < 3.0 and sg_["en_end"] - t_ > 0.5 and float(np.sqrt((esl(t_, 0.1) ** 2).mean())) < QT * rms_thr:
            t_ += 0.1
        if t_ - sg_["en_start"] >= 0.3:
            j0 = sg_["jp_start"]
            j1 = j0 + (t_ - sg_["en_start"]) * r_
            if jrms(j0, j1) >= 0.25 * jthr:
                sg_["en_start"], sg_["jp_start"] = round(float(t_), 4), round(float(j1), 4)
                sg_["trim_s"] = True
        # CM(日本語にだけある所)の直前: 末尾から4秒以内に英語の無音(0.4秒以上)があって、そこに日本語の音があるなら、無音の頭まで詰める
        #   (CMに入る前のジングルなどが、英語の暗転・無音に重なるのを防ぐ)
        if _cm_after(sg_):
            run_s, n_q = None, 0
            t_ = max(sg_["en_start"] + 0.5, sg_["en_end"] - 4.0)
            while t_ < sg_["en_end"] - 0.05:
                if float(np.sqrt((esl(t_, 0.1) ** 2).mean())) < QT * rms_thr:
                    if run_s is None:
                        run_s = t_
                    n_q += 1
                    if n_q >= 4:
                        break
                else:
                    run_s, n_q = None, 0
                t_ += 0.1
            if run_s is not None and n_q >= 4:
                je = sg_["jp_end"] if sg_["jp_end"] is not None else sg_["jp_start"] + (sg_["en_end"] - sg_["en_start"])
                j1 = je - (sg_["en_end"] - run_s) * r_
                if jrms(j1, j1 + 0.4 * r_) >= 0.25 * jthr or jrms(j1, je) >= 0.5 * jthr:
                    sg_["en_end"] = round(float(run_s), 4)
                    sg_["trim_e"] = True
                    if sg_["jp_end"] is not None:
                        sg_["jp_end"] = round(float(j1), 4)
        # 末尾
        t_ = sg_["en_end"]
        while sg_["en_end"] - t_ < 3.0 and t_ - sg_["en_start"] > 0.5 and float(np.sqrt((esl(t_ - 0.1, 0.1) ** 2).mean())) < QT * rms_thr:
            t_ -= 0.1
        if sg_["en_end"] - t_ >= min_tail - 1e-6:
            je = sg_["jp_end"] if sg_["jp_end"] is not None else sg_["jp_start"] + (sg_["en_end"] - sg_["en_start"])
            j1 = je - (sg_["en_end"] - t_) * r_
            if jrms(j1, je) >= 0.25 * jthr:
                sg_["en_end"] = round(float(t_), 4)
                sg_["trim_e"] = True
                if sg_["jp_end"] is not None:
                    sg_["jp_end"] = round(float(j1), 4)


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
    _f = np.fft.rfftfreq(N, 1 / fs)
    fmask = (_f > 250) & (_f < fs * 0.48)
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
        Gp = FJ * np.conj(np.fft.rfft(a0, N))
        Gp = Gp / (np.abs(Gp) + 1e-9) * fmask
        pc = np.fft.irfft(Gp, N)[: len(jx) - n + 1]
        for arr in (nc, pc):
            mu, sd = arr.mean(), arr.std() + 1e-9
            for _ in range(2):
                k = int(arr.argmax())
                z = (arr[k] - mu) / sd
                if z >= 8:
                    cand.append(((jo + k / fs) - t, z))
                lo, hi = max(0, k - int(fs)), min(len(arr), k + int(fs))
                arr = arr.copy()
                arr[lo:hi] = mu
        progress(0.02 + 0.28 * (q + 1) / K, "全体の音を照合しています…")
    del FJ
    if j_dur < 0.7 * e_dur:
        # 日本語のほうが短い(英語版が1本にまとまっていて、日本語はCM1本分など): 日本語側から窓を取り、英語版全体から探す
        Wj = float(min(W, max(2.0, j_dur * 0.8)))
        nj = int(Wj * fs)
        Ne = 1 << int(np.ceil(np.log2(len(ex) + nj)))
        FE = np.fft.rfft(ex, Ne)
        ee = np.concatenate([[0], np.cumsum(ex ** 2)])
        _f2 = np.fft.rfftfreq(Ne, 1 / fs)
        fmask2 = (_f2 > 250) & (_f2 < fs * 0.48)
        nrj = len(jx) // hop_rms
        rj = np.sqrt((jx[: nrj * hop_rms].reshape(nrj, hop_rms) ** 2).mean(1)) if nrj else np.array([0.0])
        thr_j = max(0.1 * float(np.percentile(rj, 75)), 1e-4)
        Kj = int(min(28, max(4, j_dur / 12)))
        weak = []
        for q, t in enumerate(np.linspace(jo, max(jo + j_dur - Wj, jo), Kj)):
            a = jsl(t, Wj)
            if float(np.sqrt((a ** 2).mean())) < thr_j:
                continue
            a0 = a - a.mean()
            G = FE * np.conj(np.fft.rfft(a0, Ne))
            c = np.fft.irfft(G, Ne)[: len(ex) - nj + 1]
            loc = np.sqrt(np.maximum(ee[nj:] - ee[:-nj], 0) + 1e-9) * np.sqrt((a0 ** 2).sum())
            nc = c / loc
            Gp = G / (np.abs(G) + 1e-9) * fmask2
            pc = np.fft.irfft(Gp, Ne)[: len(ex) - nj + 1]
            for arr in (nc, pc):
                mu, sd = arr.mean(), arr.std() + 1e-9
                for _ in range(2):
                    k = int(arr.argmax())
                    z = (arr[k] - mu) / sd
                    if z >= 8:
                        cand.append((t - (eo + k / fs), z))  # 日本語時刻 - 英語時刻
                    elif z >= 5.0:
                        weak.append((t - (eo + k / fs), z, q))  # 弱い候補: 別の窓でも同じ位置が出たときだけ採用
                    lo, hi = max(0, k - int(fs)), min(len(arr), k + int(fs))
                    arr = arr.copy()
                    arr[lo:hi] = mu
            progress(0.30 * (q + 1) / Kj, "日本語の音を英語版全体から探しています…")
        del FE
        weak.sort()
        grp = []
        for item in weak:
            if grp and item[0] - grp[-1][-1][0] <= 0.15:
                grp[-1].append(item)
            else:
                grp.append([item])
        for g_ in grp:
            if len({w_[2] for w_ in g_}) >= 2 and max(w_[1] for w_ in g_) >= 5.5:
                cand.append((float(np.median([w_[0] for w_ in g_])), max(w_[1] for w_ in g_)))
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
    offs_z = [max(z_ for _, z_ in c) for c in clusters]  # 各候補の、音の一致の強さ

    # ---- 2) 英語側を窓で区切り、各窓がどのオフセットに合うか判定
    #      (短い録画や効果音の少ない録画で判定できないときは、窓を短く・基準をゆるめて再挑戦する)
    def attempt(WL, HOP, RAD, ZM, MINRUN):
        # ---- 2) 英語側を窓で区切り、各窓がどのオフセットに合うか判定
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
                k, z = _best_peak(a0, b, fs)
                if z > best[2]:
                    best = (ci, o - RAD + k / fs, z)
            if best[2] >= ZM:
                lab[wi], off_i[wi], z_i[wi] = best[0], best[1], best[2]
            if wi % 20 == 0:
                progress(0.30 + 0.45 * wi / len(wts), "区間を判定しています…")

        # 測れたオフセットが近いもの(0.15秒以内)は、候補が別でも同じ相手として扱う (山の取り違えで区間が細切れになるのを防ぐ)
        li = [i for i in range(len(wts)) if lab[i] >= 0]
        if li:
            li.sort(key=lambda i: off_i[i])
            cid = 0
            lab[li[0]] = cid
            for a_, b_ in zip(li, li[1:]):
                if off_i[b_] - off_i[a_] > 0.15:
                    cid += 1
                lab[b_] = cid
        # ---- 3) 平滑化: 短すぎる孤立区間を除き、同じ相手に挟まれた空白を埋める
        def runs_of(l):
            r, s = [], 0
            for i in range(1, len(l) + 1):
                if i == len(l) or l[i] != l[s]:
                    r.append((l[s], s, i - 1))
                    s = i
            return r
        for lb, s, e in runs_of(list(lab)):
            if lb >= 0 and e - s + 1 < MINRUN:
                lab[s:e + 1] = -1
        # 日本語側の時刻が前後に逆戻りする組み合わせは、実際にはありえない(=偽の一致)。根拠の弱いほうを捨てる。
        def _runs_pos():
            return [(lb, s_, e_) for lb, s_, e_ in runs_of(list(lab)) if lb >= 0]

        def _span(r):
            lb, s_, e_ = r
            m_ = lab[s_:e_ + 1] == lb
            o_ = float(np.median(off_i[s_:e_ + 1][m_]))
            return wts[s_] + WL / 2 + o_, wts[e_] + WL / 2 + o_, float(z_i[s_:e_ + 1][m_].sum())  # 窓の中心で比べる
        changed = True
        while changed:
            changed = False
            runs_ = _runs_pos()
            for i_ in range(len(runs_) - 1):
                a_, b_ = _span(runs_[i_]), _span(runs_[i_ + 1])
                if b_[0] < a_[1] - 1.0:
                    weak = runs_[i_] if a_[2] < b_[2] else runs_[i_ + 1]
                    lab[weak[1]:weak[2] + 1] = np.where(lab[weak[1]:weak[2] + 1] == weak[0], -1, lab[weak[1]:weak[2] + 1])
                    changed = True
                    break
        # 3.5) 弱い一致の区間の延長: ナレーション付きの英語と音楽だけの日本語のように音の一致度が低い場面でも、
        #      隣の区間と「同じ位置」にピークが出続けるなら、同じ相手として延ばす(偽の一致で途切れるのを防ぐ)
        def supports(wi, o):
            a_ = esl(wts[wi], WL)
            if not loud(a_):
                return None   # 無音の窓は判断しない(通過させる)
            b_ = jsl(wts[wi] + o - RAD, WL + 2 * RAD)
            if np.abs(b_).max() < 1e-6:
                return False
            k_, z_ = _best_peak(a_ - a_.mean(), b_, fs)
            return abs(o - RAD + k_ / fs - o) <= 0.06 and z_ >= 3.5

        def weak_run(wi):
            if lab[wi] < 0:
                return True
            s_ = wi
            while s_ > 0 and lab[s_ - 1] == lab[wi]:
                s_ -= 1
            e_ = wi
            while e_ + 1 < len(lab) and lab[e_ + 1] == lab[wi]:
                e_ += 1
            return e_ - s_ + 1 <= 2

        for lb0, s0, e0 in sorted(_runs_pos(), key=lambda r: r[1] - r[2]):   # 長い区間から
            if not (lab[s0] == lb0 and lab[e0] == lb0):
                continue
            m0 = lab[s0:e0 + 1] == lb0
            if m0.sum() < 3:
                continue
            o0 = float(np.median(off_i[s0:e0 + 1][m0]))
            for step, edge in ((1, e0), (-1, s0)):
                wi, last, fails = edge + step, edge, 0
                while 0 <= wi < len(wts) and fails < 2:
                    if lab[wi] >= 0 and not weak_run(wi) and lab[wi] != lb0:
                        break
                    sp = supports(wi, o0)
                    if sp is None:
                        wi += step
                        continue
                    if sp:
                        last, fails = wi, 0
                    else:
                        fails += 1
                    wi += step
                for wj in range(edge + step, last + step, step):
                    if lab[wj] != -2 and (lab[wj] < 0 or weak_run(wj)):
                        lab[wj], off_i[wj] = lb0, o0
                        z_i[wj] = max(z_i[wj], ZM)
        inferred = np.zeros(len(wts), bool)
        rr = runs_of(list(lab))
        for i in range(1, len(rr) - 1):
            lb, s, e = rr[i]
            if lb < 0 and rr[i - 1][0] >= 0 and rr[i - 1][0] == rr[i + 1][0]:
                lab[s:e + 1] = rr[i - 1][0]
                inferred[s:e + 1] = True
                off_i[s:e + 1] = np.median(off_i[rr[i - 1][1]:rr[i - 1][2] + 1])
        rr = [r for r in runs_of(list(lab)) if r[0] >= 0]

        return WL, wts, lab, off_i, z_i, inferred, rr

    tries = [(5.0, 2.0, 0.5, 7.0, 2)]
    if min(e_dur, j_dur) < 60:
        tries += [(3.0, 1.0, 0.5, 6.0, 2), (2.0, 0.5, 0.5, 5.5, 3)]
    for WL, HOP, RAD, ZM, MINRUN in tries:
        WL, wts, lab, off_i, z_i, inferred, rr = attempt(WL, HOP, RAD, ZM, MINRUN)
        if rr:
            break
    if not rr:
        return [], {"ok": False, "unused_jp": [],
                    "msg": "区間を判定できませんでした。BGMや効果音が少ない作品は「手動調整」タブをお使いください。"}
    relaxed = (WL, ZM) != (5.0, 7.0)

    # ---- 3.5) 少しずつ変化するオフセット(速度差)で別々の区間に割れたものを1つに戻す
    def merge_runs(rr):
        again = True
        while again:
            again = False
            for i in range(len(rr) - 1):
                (l0, s0, e0), (_, s1, e1) = rr[i], rr[i + 1]
                if s1 - e0 > 5:
                    continue
                idx = [j for j in list(range(s0, e0 + 1)) + list(range(s1, e1 + 1)) if not inferred[j] and z_i[j] >= ZM]
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
        idx = [i for i in range(s, e + 1) if not inferred[i] and z_i[i] >= ZM]
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
        (_, _, e0), (_, s1, _) = rr[i], rr[i + 1]
        t0 = wts[e0] + WL / 2 - 1.0
        t1 = wts[s1] + WL / 2 + 1.0
        t0, t1 = max(t0, t0e), min(t1, e_end)
        fa, fb = fits[i], fits[i + 1]
        mid = (t0 + t1) / 2
        b1, sure1 = boundary(off_at(fa, mid), off_at(fb, mid), t0 - 1.0, t1 + 1.0, 1.0, 0.1)
        b2, sure2 = boundary(off_at(fa, b1), off_at(fb, b1), b1 - 0.8, b1 + 0.8, 0.4, 0.02)
        bounds.append((b2, sure1 or sure2))
        progress(0.75 + 0.2 * (i + 1) / max(len(rr) - 1, 1), "境界を調整しています…")

    # ---- 5.5) 一定のずれとみなした区間でも、細かい窓で測り直して「速度のずれ」を見つける (短い録画でも効く)
    def refine_drift(en_s, en_e, f):
        """f=(切片, 傾き, 一致度)。区間内を2秒窓で測り、日本語録画との時刻の差が直線的に変化していれば、その直線を返す。"""
        if en_e - en_s < 3.0:
            return f
        wl, rad = 2.0, 0.3
        pts_t, pts_o = [], []
        for t in np.arange(en_s, en_e - wl + 1e-9, 0.5):
            a = esl(t, wl)
            if not loud(a):
                continue
            a0 = a - a.mean()
            o_exp = off_at(f, t + wl / 2)
            b = jsl(t + o_exp - rad, wl + 2 * rad)
            if np.abs(b).max() < 1e-6:
                continue
            k, z = _best_peak(a0, b, fs)
            if z >= 6:
                pts_t.append(t + wl / 2)
                pts_o.append(o_exp - rad + k / fs)
        if len(pts_t) < 5 or pts_t[-1] - pts_t[0] < 0.5 * (en_e - en_s):
            return f
        tt, oo = np.array(pts_t), np.array(pts_o)
        keep = np.ones(len(tt), bool)
        A = None
        for _ in range(3):  # 外れ値を除いて引き直す
            if keep.sum() < 5:
                return f
            A = np.polyfit(tt[keep], oo[keep], 1)
            res = np.abs(np.polyval(A, tt) - oo)
            keep = res < max(0.01, 2.5 * float(np.median(res[keep])))
        if keep.sum() < 5 or tt[keep][-1] - tt[keep][0] < 0.5 * (en_e - en_s):
            return f
        res = np.abs(np.polyval(A, tt[keep]) - oo[keep])
        const_res = np.abs(float(np.median(oo[keep])) - oo[keep])
        # 傾きが小さい or 一定のずれと変わらない精度なら採用しない (誤検出で歪ませない)
        if abs(A[0]) < 2e-4 or abs(A[0]) > 0.03 or np.median(res) > 0.015 or np.median(res) > 0.7 * np.median(const_res):
            return f
        return (float(A[1]), float(A[0]), f[2])

    # ---- 6) 区間リストを組み立てる
    segs = []
    for i, ((lb, s, e), f) in enumerate(zip(rr, fits)):
        en_s = t0e if i == 0 else bounds[i - 1][0]
        en_e = e_end if i == len(rr) - 1 else bounds[i][0]
        if abs(f[1]) <= 2e-4:
            f = refine_drift(en_s, en_e, f)
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
        if relaxed:
            reasons.append("短い録画のため、ゆるい基準で判定")
        segs.append({"en_start": round(float(en_s), 4), "en_end": round(float(en_e), 4), "jp_start": round(float(jp_s), 4),
                     "jp_end": None if const else round(float(jp_e), 4), "fit": "stretch", "note": "自動",
                     "lag": "", "conf": float(f[2]), "flag": "・".join(reasons)})
    # ---- 6.5) 日本語が「英語版の一部をカットしたもの」のとき、境目を日本語側の時間で引き直す
    #      (英語側の区間は隙間なくつながる前提で作るので、カットがあると日本語側が重なってしまう。
    #       重なった範囲の中で「どこで切り替わるか」を日本語の音で決め、英語側にカットの隙間を作る)
    nrj2 = len(jx) // hop_rms
    rj2 = np.sqrt((jx[: nrj2 * hop_rms].reshape(nrj2, hop_rms) ** 2).mean(1)) if nrj2 else np.array([0.0])
    jthr = max(0.1 * float(np.percentile(rj2, 75)), 1e-4)

    def jscore(a, jt, o, rad=0.1):
        b = esl(jt - o - rad, len(a) / fs + 2 * rad)
        if np.abs(b).max() < 1e-6:
            return 0.0
        return float(_phat_local(a - a.mean(), b, fs).max())  # 位相相関のほうが、一致と不一致の差がはっきり出る

    def boundary_jp(oA, oB, j0, j1, wl, hop):
        ts = np.arange(j0, j1 - wl + 1e-9, hop)
        if len(ts) < 2:
            return (j0 + j1) / 2, False
        d = []
        for t in ts:
            a = jsl(t, wl)
            d.append(jscore(a, t, oA) - jscore(a, t, oB) if float(np.sqrt((a ** 2).mean())) >= jthr else 0.0)
        d = np.array(d)
        pre = np.concatenate([[0], np.cumsum(d)])
        V = 2 * pre - d.sum()
        k = int(V.argmax())
        c = ts + wl / 2
        b = c[0] if k == 0 else c[-1] if k == len(ts) else (c[k - 1] + c[k]) / 2
        return float(b), bool(abs(V[k]) >= 0.3 * np.abs(d).sum() and np.abs(d).sum() > 0.06)

    for i in range(len(segs) - 1):
        A, B = segs[i], segs[i + 1]
        if A["jp_end"] is not None or B["jp_end"] is not None:
            continue
        oA, oB = A["jp_start"] - A["en_start"], B["jp_start"] - B["en_start"]
        jsB, jeA = B["jp_start"], A["jp_start"] + (A["en_end"] - A["en_start"])
        if oA - oB < 0.5 or jeA - jsB < 0.5:
            continue  # カットではない
        b1, _ = boundary_jp(oA, oB, jsB - 1.0, jeA + 1.0, 1.0, 0.1)
        b2, sure = boundary_jp(oA, oB, b1 - 0.8, b1 + 0.8, 0.4, 0.02)
        jb = min(max(b2, jsB), jeA)
        A["en_end"] = round(float(jb - oA), 4)
        B["en_start"] = round(float(jb - oB), 4)
        B["jp_start"] = round(float(jb), 4)
        if not sure:
            for z_ in (A, B):
                z_["flag"] = ("・".join(x for x in (z_["flag"], "カットの位置が不確か") if x))
        progress(0.97, "カットの位置を調整しています…")
    segs = [s_ for s_ in segs if s_["en_end"] - s_["en_start"] >= 0.2]

    # ---- 6.8) 日本語のほうが短いとき: 同じBGMを使う別の場所と取り違えていないか、画面の似かたで確かめる
    if j_dur < 0.7 * e_dur and segs and _has_video(en) and _has_video(jp) and len(offs) > 1:
        progress(0.98, "画面の似かたで、取り違えがないか確かめています…")
        for sg_ in segs:
            if sg_["jp_end"] is not None:
                continue
            dur_ = sg_["en_end"] - sg_["en_start"]
            own = sg_["jp_start"] - sg_["en_start"]
            try:
                r_ = _pick_offset_by_video(en, jp, sg_["jp_start"], dur_, offs, offs_z, own, e_dur)
            except Exception:  # noqa: BLE001
                r_ = None
            if not r_:
                continue
            bo, bv, ov, allr = r_
            if abs(bo - own) > 0.3 and bv >= ov + 0.12 and bv >= 0.35:
                ns = sg_["jp_start"] - bo
                if ns >= 0 and ns + dur_ <= e_dur + 0.05:
                    sg_["en_start"], sg_["en_end"] = round(float(ns), 4), round(float(ns + dur_), 4)
                    sg_["flag"] = "・".join(x for x in (sg_["flag"], "画面の似かたで、音が似た別の場所から選び直しました") if x)
                    continue
            others = [o_ for o_, v_ in allr if abs(o_ - own) > 0.3 and v_ >= ov - 0.08 and v_ >= 0.3]
            if others:
                where = "、".join(_fmt_t(sg_["jp_start"] - o_) for o_ in others[:2])
                sg_["flag"] = "・".join(x for x in (sg_["flag"], f"似た場所が他にもあります(英語版 {where})") if x)

    _trim_to_en_silence(segs, esl, jsl, rms_thr, jthr)
    _splice_at_quiet(segs, jsl, jthr, esl=esl, en_thr=rms_thr)

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


# ------------------------------------------------------------------ 映像で合わせる(かんたん用)
def _content_box(path):
    """画面の中身がある範囲(上下左右の黒帯を除いた範囲)を調べる。戻り値: (幅, 高さ, x, y) / 分からなければ None。"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, **NOWIN)
        d = float(r.stdout.decode(errors="ignore").strip() or 0)
    except Exception:  # noqa: BLE001
        d = 0.0
    box = None
    for f in (0.12, 0.3, 0.5, 0.7, 0.88):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{d * f:.2f}", "-t", "3", "-i", path, "-an", "-sn",
                            "-vf", "cropdetect=24:2:0", "-f", "null", "-"], capture_output=True, **NOWIN)
        for w, h, x, y in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr.decode(errors="ignore")):
            w, h, x, y = int(w), int(h), int(x), int(y)
            if w < 16 or h < 16:
                continue
            box = [x, y, x + w, y + h] if box is None else [min(box[0], x), min(box[1], y), max(box[2], x + w), max(box[3], y + h)]
    if box is None:
        return None
    return box[2] - box[0], box[3] - box[1], box[0], box[1]


def _thumbs_box(path, fps, box, ss=0.0, dur=None):
    """_thumbs_fps と同じだが、黒帯を除いた範囲(box)だけを見る。"""
    W, H = 48, 36
    cmd = ["ffmpeg", "-v", "error"]
    if ss > 0:
        cmd += ["-ss", f"{ss:.4f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.4f}"]
    cr = f"crop={box[0]}:{box[1]}:{box[2]}:{box[3]}," if box else ""
    tail = ["-i", path, "-an", "-sn", "-vf", f"fps={fps},{cr}scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    r = None
    if dur is None:      # 全体を読むときは、使えればGPUで速くする
        r = subprocess.run(cmd + ["-hwaccel", "auto"] + tail, capture_output=True, **NOWIN)
    if r is None or r.returncode != 0 or len(r.stdout) < W * H:     # GPUで読めなかった・途中で止まったときは、ふつうに読み直す
        r = subprocess.run(cmd + tail, capture_output=True, **NOWIN)
    n = len(r.stdout) // (W * H)
    a = np.frombuffer(r.stdout[: n * W * H], np.uint8).reshape(n, W * H).astype(np.float32)
    sd = a.std(1)
    ok = sd >= 4.0
    return (a - a.mean(1, keepdims=True)) / np.where(ok, sd, 1.0)[:, None], ok


def _fit_offsets(samples):
    """[(英語版の時刻, 時間差)] から、時間差を「一定」または「少しずつ変わる(直線)」として求める。
    戻り値: (a, b, 合った数)  時間差 = a + b×時刻 / 決められなければ None。"""
    if not samples:
        return None
    t = np.array([s[0] for s in samples], float)
    o = np.array([s[1] for s in samples], float)
    med = float(np.median(o))
    inl = np.abs(o - med) <= 0.05
    if inl.sum() >= 2 and inl.mean() >= 0.6:
        return float(o[inl].mean()), 0.0, int(inl.sum())
    sl = [(o[j] - o[i]) / (t[j] - t[i]) for i in range(len(t)) for j in range(i + 1, len(t)) if t[j] - t[i] > 5.0]
    if len(sl) >= 3:
        b = float(np.median(sl))
        a = float(np.median(o - b * t))
        inl = np.abs(o - a - b * t) <= 0.05
        if inl.sum() >= 3 and inl.mean() >= 0.6 and abs(b) < 0.03:
            return a, b, int(inl.sum())
    return None


def _offset_groups(smp, zs, WL, merge=True):
    """音で測った [(英語版の時刻, 時間差)] を、時間差がそろっている(またはゆっくり動く)ひと続きごとに分ける。
    1つだけ外れた値は捨て、2つ続けて新しい値になったら、そこから別のまとまりにする。
    戻り値: [{"t_first","t_last","a","b","n","z"}]  時間差 = a + b×時刻"""
    groups, cur, pend = [], None, None
    for i, (t, o) in enumerate(smp):
        if cur is not None and abs(o - float(np.median([smp[k][1] for k in cur[-3:]]))) <= 0.05:
            cur.append(i)
            pend = None
        elif pend is not None and abs(o - smp[pend][1]) <= 0.05:
            cur = [pend, i]
            groups.append(cur)
            pend = None
        else:
            pend = i
    def line(g):
        t = np.array([smp[k][0] for k in g])
        o = np.array([smp[k][1] for k in g])
        b = 0.0
        if t[-1] - t[0] > 20.0 and float(o.max() - o.min()) > 0.04:
            sl = [(o[j] - o[i]) / (t[j] - t[i]) for i in range(len(t)) for j in range(i + 1, min(i + 12, len(t))) if t[j] - t[i] > 5.0]
            if sl:
                b = float(np.median(sl))
        a = float(np.median(o - b * t))
        return a, b, float(np.abs(o - a - b * t).max())
    # 少しずつ動いているだけ(速さがわずかに違う)なら、ひと続きに戻す
    mg = []
    for g in groups:
        if mg and merge:
            a, b, r = line(mg[-1] + g)
            if r <= 0.06 and abs(b) < 0.03:
                mg[-1] = mg[-1] + g
                continue
        mg.append(list(g))
    out = []
    for g in mg:
        a, b, _r = line(g)
        out.append({"t_first": float(smp[g[0]][0]), "t_last": float(smp[g[-1]][0]), "a": a, "b": b, "n": len(g),
                    "z": float(np.median([zs[k] for k in g]))})
    return out


def video_align(en, jp, progress=lambda f, m: None, ctx=None):
    """auto_align の映像版。画面の似かたで、英語版の各時刻に当たる日本語録画の時刻を見つける(音が違いすぎて合わないとき用)。
    大まかな位置は画面で決め、細かい位置は音で合わせ直す(音で決められない所は画面で1コマ単位に合わせる)。
    戻り値は auto_align と同じ (segs, info)。"""
    if not (_has_video(en) and _has_video(jp)):
        return [], {"ok": False, "unused_jp": [], "msg": "映像で合わせるには、英語版と日本語の録画の両方に映像が必要です。"}

    def dec(m):
        x = m.x
        n = len(x) // 2 * 2
        return x[:n].reshape(-1, 2).mean(1).astype(np.float64)
    fs = SR / 2
    ex, jx = dec(en), dec(jp)
    eo, jo = en.a_origin, jp.a_origin
    j_dur = len(jx) / fs

    def _sl(x, o0, t, L):
        i = int(round((t - o0) * fs))
        n = int(round(L * fs))
        out = np.zeros(n)
        a, b = max(i, 0), min(i + n, len(x))
        if b > a:
            out[a - i: b - i] = x[a:b]
        return out

    def esl(t, L):
        return _sl(ex, eo, t, L)

    def jsl(t, L):
        return _sl(jx, jo, t, L)

    def _thr(x):
        h = int(fs)
        n = len(x) // h
        r = np.sqrt((x[: n * h].reshape(n, h) ** 2).mean(1)) if n else np.array([0.0])
        return max(0.1 * float(np.percentile(r, 75)), 1e-4)
    rms_thr, jthr = _thr(ex), _thr(jx)

    # ---- 1) 両方の画面を小さくして読む(黒帯は除く)
    progress(0.02, "画面を読み込んでいます(英語版)…")
    F = 2 if max(en.end, jp.end) <= 45 * 60 else 1
    be, bj = _content_box(en.path), _content_box(jp.path)
    EX, eok = _thumbs_box(en.path, F, be)
    progress(0.30, "画面を読み込んでいます(日本語の録画)…")
    JX, jok = _thumbs_box(jp.path, F, bj)
    Te, Tj = len(EX), len(JX)
    if Te < 6 * F or Tj < 6 * F:
        return [], {"ok": False, "unused_jp": [], "msg": "映像を読み込めませんでした。"}
    progress(0.58, "画面を見比べています…")
    S = EX @ JX.T / EX.shape[1]
    S[~eok, :] = 0.0
    S[:, ~jok] = 0.0
    if ctx is not None:
        ctx.update({"S": S, "F": F, "eok": eok, "jok": jok, "trim": (esl, jsl, rms_thr, jthr), "fs": fs})

    # ---- 2) ありそうな時間差(コマ数)を集める
    votes = {}
    top = np.argsort(-S, axis=1)[:, :2]
    for i in range(Te):
        for j in top[i]:
            if S[i, j] >= 0.5:
                votes[int(j) - i] = votes.get(int(j) - i, 0) + 1
    sm = {d: votes.get(d - 1, 0) + c + votes.get(d + 1, 0) for d, c in votes.items()}
    good = sorted((d for d, c in sm.items() if c >= max(3, 2 * F)), key=lambda d: -sm[d])[:80]
    ds = sorted({d + k for d in good for k in (-1, 0, 1)})
    if not ds:
        return [], {"ok": False, "unused_jp": [], "msg": "画面の似ている所が見つかりませんでした。別の作品か、画面の作りが大きく違うようです。"}
    K = len(ds)
    # 各時間差での似かた(前後1秒でならす)
    EM = np.zeros((K, Te), np.float32)
    ii = np.arange(Te)
    ker = np.ones(2 * F + 1) / (2 * F + 1)
    for k, d in enumerate(ds):
        jj = ii + d
        okk = (jj >= 0) & (jj < Tj)
        v = np.zeros(Te, np.float32)
        v[okk] = S[ii[okk], jj[okk]]
        use = np.zeros(Te, np.float32)
        use[okk] = (eok[ii[okk]] & jok[jj[okk]]).astype(np.float32)
        den = np.convolve(use, ker, "same")       # 暗い画面などは数えずに平均する
        EM[k] = np.where(use > 0, np.convolve(v, ker, "same") / np.maximum(den, 1e-6), 0.0)
    NONE = 0.45
    em_none = np.where(eok, NONE, 0.0).astype(np.float32)
    adj = [[k2 for k2 in (k - 1, k + 1) if 0 <= k2 < K and abs(ds[k2] - ds[k]) == 1] for k in range(K)]
    C_ADJ, C_JMP, C_NONE = 0.6, 3.0 * F / 2 + 1.5, 1.5
    dp = np.concatenate([EM[:, 0], [em_none[0]]]).astype(np.float64)
    bk = np.zeros((Te, K + 1), np.int32)
    for i in range(1, Te):
        bst = int(dp[:K].argmax())
        nd = np.empty(K + 1)
        for k in range(K):
            c, p = dp[k], k
            for k2 in adj[k]:
                if dp[k2] - C_ADJ > c:
                    c, p = dp[k2] - C_ADJ, k2
            if dp[bst] - C_JMP > c:
                c, p = dp[bst] - C_JMP, bst
            if dp[K] - C_NONE > c:
                c, p = dp[K] - C_NONE, K
            nd[k] = c + EM[k, i]
            bk[i, k] = p
        c, p = dp[K], K
        if dp[bst] - C_NONE > c:
            c, p = dp[bst] - C_NONE, bst
        nd[K] = c + em_none[i]
        bk[i, K] = p
        dp = nd
        if i % 500 == 0:
            progress(0.58 + 0.12 * i / Te, "画面を見比べています…")
    st = np.zeros(Te, np.int32)
    st[-1] = int(dp.argmax())
    for i in range(Te - 1, 0, -1):
        st[i - 1] = bk[i, st[i]]

    # ---- 3) ひと続きの区間にまとめる(時間差が1コマずつ動くのは、わずかな速さの違いとして同じ区間にする)
    chains = []
    for i in range(Te):
        k = int(st[i])
        if k == K:
            continue
        if chains and chains[-1]["i1"] == i - 1 and abs(ds[k] - chains[-1]["d1"]) <= 1:
            chains[-1]["i1"], chains[-1]["d1"] = i, ds[k]
            chains[-1]["dd"].append(ds[k])
        else:
            chains.append({"i0": i, "i1": i, "d1": ds[k], "dd": [ds[k]]})
    chains = [c for c in chains if c["i1"] - c["i0"] + 1 >= 3 * F]
    if not chains:
        return [], {"ok": False, "unused_jp": [], "msg": "画面の合う所が見つかりませんでした。"}

    def raw(i, d):
        j = i + d
        return float(S[i, j]) if 0 <= j < Tj else 0.0
    def dark_run(a, b):   # a〜b のコマに、暗い画面が2秒以上続く所(場面の大きな切れ目)があるか
        n_ = 0
        for i_ in range(max(a, 0), min(b, Te)):
            n_ = 0 if eok[i_] else n_ + 1
            if n_ >= 2 * F:
                return True
        return False
    # 端を、ならす前の似かたで決め直す: 合っていない・暗いコマは端から外し、少し先(6秒以内)にまた合うコマがあればそこまで延ばす
    for c in chains:
        while c["i1"] - c["i0"] > 2 * F and raw(c["i1"], c["dd"][-1]) < 0.4:
            c["i1"] -= 1
            c["dd"].pop()
        while c["i1"] - c["i0"] > 2 * F and raw(c["i0"], c["dd"][0]) < 0.4:
            c["i0"] += 1
            c["dd"].pop(0)
    for q, c in enumerate(chains):
        lim = chains[q + 1]["i0"] if q + 1 < len(chains) else Te
        i, last = c["i1"] + 1, c["i1"]
        while i < lim and i - last <= 6 * F:
            if raw(i, c["dd"][-1]) >= 0.55 and not dark_run(last + 1, i):
                last = i
            i += 1
        c["dd"] += [c["dd"][-1]] * (last - c["i1"])
        c["i1"] = last
        lim = chains[q - 1]["i1"] if q else -1
        i, last = c["i0"] - 1, c["i0"]
        while i > lim and last - i <= 6 * F:
            if raw(i, c["dd"][0]) >= 0.55 and not dark_run(i + 1, last):
                last = i
            i -= 1
        c["dd"] = [c["dd"][0]] * (c["i0"] - last) + c["dd"]
        c["i0"] = last
    # 暗い画面や止め絵ばかりで、はっきり合ったコマが少ない(3秒ぶん未満)区間は信用しない
    chains = [c for c in chains
              if sum(1 for k_, i_ in enumerate(range(c["i0"], c["i1"] + 1)) if eok[i_] and raw(i_, c["dd"][k_]) >= 0.5) >= 3 * F]
    if not chains:
        return [], {"ok": False, "unused_jp": [], "msg": "画面の合う所が見つかりませんでした。"}
    # 同じ時間差で、間が短い(6秒以内)区間はつなぐ(日本語版だけ題名の画面が違う、など)
    mg = [chains[0]]
    for c in chains[1:]:
        p_ = mg[-1]
        if abs(c["dd"][0] - p_["dd"][-1]) <= 1 and c["i0"] - p_["i1"] <= 6 * F and not dark_run(p_["i1"] + 1, c["i0"]):
            p_["dd"] += [p_["dd"][-1]] * (c["i0"] - p_["i1"] - 1) + c["dd"]
            p_["i1"] = c["i1"]
        else:
            mg.append(c)
    chains = mg
    # 間が暗い画面だけ(場面の切れ目)なら、真ん中で分け合う(音の出入りは、あとで英語の無音に合わせて詰める)
    for p_, c in zip(chains, chains[1:]):
        g0, g1 = p_["i1"] + 1, c["i0"]
        if g1 > g0 and g1 - g0 <= 4 * F and not eok[g0:g1].any():
            mid = (g0 + g1) // 2
            p_["dd"] += [p_["dd"][-1]] * (mid - g0)
            p_["i1"] = mid - 1
            c["dd"] = [c["dd"][0]] * (g1 - mid) + c["dd"]
            c["i0"] = mid
    # 最後の区間のあとが暗い画面だけなら、英語版の終わりまで延ばす(先頭も同じ)
    c = chains[-1]
    if c["i1"] + 1 < Te and Te - c["i1"] - 1 <= 6 * F and not eok[c["i1"] + 1:].any():
        c["dd"] += [c["dd"][-1]] * (Te - 1 - c["i1"])
        c["i1"] = Te - 1
    c = chains[0]
    if 0 < c["i0"] <= 6 * F:     # 始まりは、ロゴや題名の画面が違っていても音はつながっていることが多いので、頭まで延ばす
        c["dd"] = [c["dd"][0]] * c["i0"] + c["dd"]
        c["i0"] = 0

    # ---- 4) 細かい位置を決める(まず音。音で決められなければ画面で1コマ単位)
    WL, RAD = 5.0, 0.9
    segs = []
    for q, c in enumerate(chains):
        progress(0.72 + 0.26 * q / len(chains), "細かい位置を合わせています…")
        e0, e1 = c["i0"] / F, min((c["i1"] + 1) / F, en.end)
        dd = c["dd"]

        def coarse(t, c=c, dd=dd):
            return dd[int(min(max(round(t * F) - c["i0"], 0), len(dd) - 1))] / F
        n_w = int(min(500, max(2, (e1 - e0) // 3)))
        smp, zs = [], []
        for t in np.linspace(e0, max(e0, e1 - WL), n_w):
            a = esl(t, WL)
            if float(np.sqrt((a ** 2).mean())) < rms_thr:
                continue
            o = coarse(t + WL / 2)
            b = jsl(t + o - RAD, WL + 2 * RAD)
            if np.abs(b).max() < 1e-6:
                continue
            k_, z_ = _best_peak(a - a.mean(), b, fs)
            lag = -RAD + k_ / fs
            if z_ >= 6.0 and abs(lag) < RAD - 0.05:
                smp.append((float(t), o + lag))
                zs.append(z_)
        def emit(x0, x1, fit, flag):
            a_, b_, n_ = fit
            o0, o1 = a_ + b_ * x0, a_ + b_ * x1
            const = abs(o1 - o0) < 0.04
            if const:
                o0 = o1 = a_ + b_ * (x0 + x1) / 2
            js = x0 + o0
            if js < jo:                       # 日本語の録画が始まる前には当てない
                x0, js = x0 + (jo - js), jo
            segs.append({"en_start": round(float(x0), 4), "en_end": round(float(x1), 4), "jp_start": round(float(js), 4),
                         "jp_end": None if const else round(float(x1 + o1), 4), "fit": "stretch", "note": "自動(映像)",
                         "lag": "", "conf": float(n_), "flag": flag})

        def ncc(t, o):
            a = esl(t, 1.0)
            b = jsl(t + o - 0.02, 1.04)
            a = a - a.mean()
            na = float(np.linalg.norm(a))
            if na < rms_thr * np.sqrt(len(a)):
                return None
            cc = np.correlate(b, a, "valid")
            e2 = np.convolve(b ** 2, np.ones(len(a)), "valid")
            return float((cc / (na * np.sqrt(np.maximum(e2, 1e-12)))).max())

        def switch_point(g1, g2):   # 時間差が変わる所を、音がどちらの時間差に合うかで決める
            ts = np.arange(g1["t_last"], max(g1["t_last"] + 0.25, g2["t_first"] + WL - 1.0), 0.25)
            pf = []
            for t in ts:
                c1, c2 = ncc(t, g1["a"] + g1["b"] * t), ncc(t, g2["a"] + g2["b"] * t)
                pf.append(0.0 if c1 is None or c2 is None else c1 - c2)
            pf = np.array(pf)
            if not len(pf) or np.abs(pf).max() < 0.05:
                return float((g1["t_last"] + WL / 2 + g2["t_first"] + WL / 2) / 2)
            sc = [pf[:k].sum() - pf[k:].sum() for k in range(len(pf) + 1)]
            k = int(np.argmax(sc))
            return float(ts[k] if k < len(ts) else ts[-1] + 0.25)

        groups = _offset_groups(smp, zs, WL)
        if groups:      # 音で細かい位置が決まった(途中で時間差が変わる所は、そこで区切る)
            bnd = [e0] + [min(max(switch_point(g1, g2), e0), e1) for g1, g2 in zip(groups, groups[1:])] + [e1]
            for g, x0, x1 in zip(groups, bnd, bnd[1:]):
                if x1 - x0 >= 0.5:
                    emit(x0, x1, (g["a"], g["b"], g["n"]), "一致度が低め" if g["z"] < 9.0 else "")
            continue
        fit, flag = None, ""
        if len(smp) == 1 and zs[0] >= 12.0:
            fit = (smp[0][1], 0.0, 1)
        if fit is None:
            # 画面で合わせる: 動きのある所を選んで、日本語側を細かく(1/30秒)見る
            idx = np.arange(c["i0"], c["i1"])
            mv = np.array([1.0 - float(EX[i] @ EX[i + 1]) / EX.shape[1] if eok[i] and eok[i + 1] else 0.0 for i in idx])
            pick = []
            for i in idx[np.argsort(-mv)]:
                if mv[i - c["i0"]] < 0.05 or len(pick) >= 10:
                    break
                if all(abs(i - p) >= 2 * F for p in pick):
                    pick.append(int(i))
            vs = []
            for i in pick:
                t = (i + 0.5) / F          # コマの切り替わる所の近く
                o = coarse(t)
                x, xo = _thumbs_box(en.path, 30, be, max(0.0, t + en.voff), 0.02)
                if len(x) < 1 or not xo[0]:
                    continue
                ssj = t + o - RAD + jp.voff
                if ssj < 0:
                    continue
                Y, yo = _thumbs_box(jp.path, 30, bj, ssj, 2 * RAD)
                if len(Y) < 10:
                    continue
                sc = Y @ x[0] / len(x[0])
                sc[~yo] = 0.0
                mx = float(sc.max())
                if mx < 0.6:
                    continue
                w = np.nonzero(sc >= mx - 0.015)[0]
                if w.max() - w.min() > 6:    # 同じ画面が長く続く所は使えない
                    continue
                vs.append((float(t), o - RAD + float(w.mean()) / 30.0))
            if vs:
                ov = float(np.median([v[1] for v in vs]))
                near = [o_ for (_t, o_) in smp if abs(o_ - ov) <= 0.1]
                if near:      # 画面で決めた位置の近くに、弱いながら音の一致がある → 音の位置を使う
                    fit = (float(np.median(near)), 0.0, len(near))
                    flag = "一致度が低め"
                else:
                    fit = (ov, 0.0, len(vs))
                    flag = "映像で合わせました(音では確かめられず)"
            else:
                fit = (float(np.median(dd)) / F, 0.0, 0)
                flag = "映像でおおまかに合わせただけです(手動で直してください)"
        emit(e0, e1, fit, flag)
    segs = [s for s in segs if s["en_end"] - s["en_start"] >= 1.0]
    # 日本語側が前の区間と重ならないようにする
    for p, s in zip(segs, segs[1:]):
        pe = p["jp_end"] if p["jp_end"] is not None else p["jp_start"] + (p["en_end"] - p["en_start"])
        if s["jp_start"] < pe and s["jp_start"] > p["jp_start"]:
            cut = pe - s["jp_start"]
            if cut < p["en_end"] - p["en_start"] - 1.0:
                p["en_end"] = round(p["en_end"] - cut, 4)
                if p["jp_end"] is not None:
                    p["jp_end"] = round(p["jp_end"] - cut, 4)
    _trim_to_en_silence(segs, esl, jsl, rms_thr, jthr, min_tail=0.1)
    _splice_at_quiet(segs, jsl, jthr, esl=esl, en_thr=rms_thr)
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


def combined_align(en, jp, progress=lambda f, m: None):
    """音(auto_align)と映像(video_align)の両方で合わせ、場所ごとに確かなほうを採る。
    ・両方が同じ答え → 音の結果(細かい位置が正確)   ・片方しか見つけていない → その結果
    ・答えが違う → 画面がはっきり合っているほう(差が小さければ音)。違っていた所は「要確認」にする。"""
    A, ia = auto_align(en, jp, lambda f, m: progress(0.35 * f, "(1/2 音) " + m))
    ctx = {}
    V, iv = video_align(en, jp, lambda f, m: progress(0.35 + 0.6 * f, "(2/2 映像) " + m), ctx)
    if not iv["ok"] or not V:
        if ia["ok"] and A:
            for s in A:
                s["flag"] = "・".join(x for x in (s["flag"], "映像では確かめられず") if x)
        return A, ia
    if not ia["ok"] or not A:
        return V, iv
    S, F, eok, jok = ctx["S"], ctx["F"], ctx["eok"], ctx["jok"]

    def fn(s):    # 区間 → 時間差の式 (基準の時刻, そこでの時間差, 傾き)
        d = s["en_end"] - s["en_start"]
        r = 0.0 if s["jp_end"] is None or d <= 0 else (s["jp_end"] - s["jp_start"]) / d - 1.0
        return s["en_start"], s["jp_start"] - s["en_start"], r

    def off(f, t):
        return f[1] + f[2] * (t - f[0])

    def vscore(a, b, f):
        sc = []
        for i in range(int(np.ceil(a * F)), int(b * F) + 1):
            j = int(round(i + off(f, i / F) * F))
            if 0 <= i < S.shape[0] and 0 <= j < S.shape[1] and eok[i] and jok[j]:
                sc.append(float(S[i, j]))
        return float(np.mean(sc)) if len(sc) >= 2 else None

    ex_ = en.x.astype(np.float64)
    h_ = int(SR)
    n_ = len(ex_) // h_
    r_ = np.sqrt((ex_[: n_ * h_].reshape(n_, h_) ** 2).mean(1)) if n_ else np.array([0.0])
    q_thr = max(0.1 * float(np.percentile(r_, 75)), 1e-4)

    def en_quiet(a, b):    # 英語版のこの範囲が、ほぼ無音か(0.5秒ごとに見て、音のある所が2割未満)
        i0, i1 = int((a - en.a_origin) * SR), int((b - en.a_origin) * SR)
        x = ex_[max(i0, 0): max(i1, 0)]
        w = int(SR / 2)
        k = len(x) // w
        if k == 0:
            return True
        rr = np.sqrt((x[: k * w].reshape(k, w) ** 2).mean(1))
        return float((rr >= q_thr).mean()) < 0.2

    def cover(L, t):
        for q, s in enumerate(L):
            if s["en_start"] - 1e-6 <= t < s["en_end"] - 1e-6:
                return q
        return None
    cuts = sorted({round(x, 4) for s in A + V for x in (s["en_start"], s["en_end"])})
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 1e-3:
            continue
        mid = (a + b) / 2
        qa, qv = cover(A, mid), cover(V, mid)
        if qa is None and qv is None:
            continue
        fa = fn(A[qa]) if qa is not None else None
        fv = fn(V[qv]) if qv is not None else None
        if fv is None:
            v = vscore(a, b, fa)
            pieces.append({"a": a, "b": b, "f": fa, "id": ("A", qa), "flag": "映像が合いません" if (v is not None and v < 0.3 and b - a >= 2.0) else ""})
        elif fa is None:
            # 音では見つからなかった所。英語版が無音(日本語の音を入れる必要がない)か、画面がはっきり合っていなければ使わない
            v = vscore(a, b, fv)
            if en_quiet(a, b) or v is None or v < 0.5:
                continue
            pieces.append({"a": a, "b": b, "f": fv, "id": ("V", qv), "flag": "音では見つからず、映像で合わせました"})
        elif abs(off(fa, mid) - off(fv, mid)) <= 0.12:
            v = vscore(a, b, fa)
            pieces.append({"a": a, "b": b, "f": fa, "id": ("A", qa), "flag": "", "ok": v is not None and v >= 0.55})
        else:
            va, vv = vscore(a, b, fa), vscore(a, b, fv)
            if vv is not None and (va is None or vv - va >= 0.15):
                pieces.append({"a": a, "b": b, "f": fv, "id": ("V", qv), "flag": "音と映像で違う結果(映像を採用)"})
            else:
                pieces.append({"a": a, "b": b, "f": fa, "id": ("A", qa), "flag": "音と映像で違う結果(音を採用)" if b - a >= 1.0 else ""})
    # 映像だけの所が、隣の音の区間とほぼ同じ時間差なら、音の区間の続きとして扱う(つなぎ目で音が飛ばないように)
    for _ in range(2):
        for p, q in list(zip(pieces, pieces[1:])) + list(zip(pieces[1:], pieces))[::-1]:
            if p["id"][0] == "A" and q["id"][0] == "V" and (abs(p["b"] - q["a"]) < 1e-3 or abs(q["b"] - p["a"]) < 1e-3):
                t = q["a"] if abs(p["b"] - q["a"]) < 1e-3 else q["b"]
                if abs(off(p["f"], t) - off(q["f"], t)) <= 0.12:
                    sa = A[p["id"][1]]
                    if (q["a"] >= sa["en_end"] - 1e-3 and sa.get("trim_e")) or (q["b"] <= sa["en_start"] + 1e-3 and sa.get("trim_s")):
                        q["id"] = ("X", 0)     # 音の方式が、英語の無音に合わせてわざと詰めた所 → 使わない
                        continue
                    q["f"], q["id"] = p["f"], p["id"]
                    if q["flag"].startswith("音では見つからず"):
                        q["flag"] = ""
    pieces = [p for p in pieces if p["id"][0] != "X"]
    segs = []
    for p in pieces:
        src = (A if p["id"][0] == "A" else V)[p["id"][1]]
        if segs and segs[-1]["_id"] == p["id"] and abs(segs[-1]["en_end"] - p["a"]) < 1e-3:
            g = segs[-1]
            g["en_end"] = p["b"]
            g["_flags"].append(p["flag"])
            g["_ok"] += (p["b"] - p["a"]) if p.get("ok") else 0.0
        else:
            segs.append({"en_start": p["a"], "en_end": p["b"], "_id": p["id"], "_f": p["f"], "_flags": [src["flag"], p["flag"]],
                         "_ok": (p["b"] - p["a"]) if p.get("ok") else 0.0,
                         "fit": "stretch", "note": p["id"][0], "lag": "", "conf": src.get("conf", 0.0)})
    out = []
    for g in segs:
        if g["en_end"] - g["en_start"] < 0.5:      # ごく短い切れ端は使わない
            continue
        src = (A if g["_id"][0] == "A" else V)[g["_id"][1]]
        f = g.pop("_f")
        g["jp_start"] = round(g["en_start"] + off(f, g["en_start"]), 4)
        g["jp_end"] = None if abs(f[2]) < 1e-9 else round(g["en_end"] + off(f, g["en_end"]), 4)
        fl = []
        for x in g.pop("_flags"):
            for y in x.split("・"):
                if y and y not in fl:
                    fl.append(y)
        if g.pop("_ok") >= 0.8 * (g["en_end"] - g["en_start"]):   # 映像でも同じ場所だと確かめられた → 音の一致が低いだけなら要確認にしない
            fl = [y for y in fl if y != "一致度が低め"]
        g["flag"] = "・".join(fl)
        if src.get("trim_s") and abs(g["en_start"] - src["en_start"]) < 1e-3:
            g["trim_s"] = True
        if src.get("trim_e") and abs(g["en_end"] - src["en_end"]) < 1e-3:
            g["trim_e"] = True
        g.pop("_id")
        out.append(g)
    # 日本語側が前の区間と重ならないようにする(映像だけで決めた区間のほうを削る。音で決めた区間は削らない)
    keep = []
    for s in out:
        if keep:
            p = keep[-1]
            pe = p["jp_end"] if p["jp_end"] is not None else p["jp_start"] + (p["en_end"] - p["en_start"])
            if p["jp_start"] < s["jp_start"] < pe:
                cut = pe - s["jp_start"]
                if s["note"] == "V" or p["note"] != "V":
                    if s["note"] == "V":
                        if s["en_end"] - s["en_start"] - cut < 1.0:
                            continue                      # 削ると残らない → 使わない
                        s["en_start"] = round(s["en_start"] + cut, 4)
                        s["jp_start"] = round(s["jp_start"] + cut, 4)
                    elif cut < p["en_end"] - p["en_start"] - 1.0:
                        p["en_end"] = round(p["en_end"] - cut, 4)
                        if p["jp_end"] is not None:
                            p["jp_end"] = round(p["jp_end"] - cut, 4)
                else:                                     # 前が映像だけ・後ろが音 → 前を削る
                    if p["en_end"] - p["en_start"] - cut < 1.0:
                        keep.pop()
                    else:
                        p["en_end"] = round(p["en_end"] - cut, 4)
                        if p["jp_end"] is not None:
                            p["jp_end"] = round(p["jp_end"] - cut, 4)
        keep.append(s)
    out = keep
    for s in out:
        s["note"] = "自動(音+映像)"
    # 区間の中で時間差が少しだけ変わる所(0.1秒ほどのずれ)を測り直して、そこで分ける
    _refine_offsets(out, ctx["trim"][0], ctx["trim"][1], ctx["fs"], ctx["trim"][2])
    _refine_edges(out, ctx["trim"][0], ctx["trim"][1], ctx["fs"], ctx["trim"][2])
    # 区間の組み合わせが変わったので、端が英語の無音に掛かる所(CM前のジングルなど)をもう一度詰める
    _trim_to_en_silence(out, *ctx["trim"])
    _splice_at_quiet(out, ctx["trim"][1], ctx["trim"][3], esl=ctx["trim"][0], en_thr=ctx["trim"][2])
    jo, j_end = jp.a_origin, jp.end
    unused, prev_end = [], jo
    for s in out:
        je = s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + (s["en_end"] - s["en_start"])
        if s["jp_start"] - prev_end > 5.0:
            unused.append((prev_end, s["jp_start"]))
        prev_end = max(prev_end, je)
    if j_end - prev_end > 5.0:
        unused.append((prev_end, j_end))
    progress(1.0, "完了")
    return out, {"ok": True, "unused_jp": unused, "msg": ""}


# ------------------------------------------------------------------ 指定 → 区間の計算

def pick_gaps(blacks, sil):
    """黒画面のうち、CMの切れ目とみなすものを選ぶ。
    無音をともなう黒画面は切れ目。無音がなくても0.4秒以上の黒画面は切れ目とする。
    ただし、ほとんどの切れ目に無音がある映像(切れ目を無音にして編集してある)では、
    音が続いたままの短い(1秒未満の)黒画面は、CMの途中の場面転換なので切れ目にしない。"""
    sg, ng = [], []
    for b0, b1 in blacks:
        if any(s0 < b1 + 0.3 and s1 > b0 - 0.3 for s0, s1 in sil):
            sg.append((b0, b1))
        elif b1 - b0 >= 0.4:
            ng.append((b0, b1))
    if len(sg) >= 3 and len(sg) >= 0.8 * (len(sg) + len(ng)):
        ng = [g for g in ng if g[1] - g[0] >= 1.0]
    return sorted(sg + ng)


def split_commercials(path, min_len=4.0):
    """英語版の長い映像を、黒い画面+無音を目印にCM(など)1本ずつに切り分ける。[(開始, 終了)] を返す。"""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", "blackdetect=d=0.08:pix_th=0.1",
         "-af", "silencedetect=n=-50dB:d=0.15", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", **NOWIN)
    txt = r.stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", txt)]
    sil, cur = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", txt):
        if m.group(1) == "start":
            cur = float(m.group(2))
        elif cur is not None:
            sil.append((cur, float(m.group(2))))
            cur = None
    if cur is not None:
        sil.append((cur, 1e9))
    dur = 0.0
    m = re.findall(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    if m:
        h, mi, se = m[0]
        dur = int(h) * 3600 + int(mi) * 60 + float(se)
    gaps = pick_gaps(blacks, sil)
    out, t = [], 0.0
    for b0, b1 in gaps:
        if b0 - t >= min_len:
            out.append((t, b0))
        t = max(t, b1)
    if dur - t >= min_len:
        out.append((t, dur))
    return out


def round5(sec):
    """秒数を5秒刻みに四捨五入 (28→30, 12→10, 13→15)。最小5。"""
    return max(5, int(sec / 5.0 + 0.5) * 5)


FIND_ALGO = 22     # 探索の判定方式の世代(変えると、古い探索記録は使わずにやりなおす)
ZZ_PASS = 14.0   # 窓(約10秒)ごとの照合で「一致」とみなす突出度。本物は16以上、誤検出は9以下だった
ZZ_SOLO = 25.0   # 前半/後半の片方しか合わない短いCMは、その窓がこれ以上はっきりしているときだけ採用(誤検出は18以下、本物は39)
ZZ_LAG = 0.15    # 窓の一致位置が全体の位置からずれてよい秒数


def _verify_hit(c, jx, i0, fs):
    """見つけた場所 i0 で、CMを窓(10秒以下は前半/後半、それより長ければ約10秒ずつ)に分けて、窓ごとに音が合うか確かめる。
    戻り値: {"zz": 一番強い窓の突出度, "np": 合った窓の数, "n": 窓の数, "pass": 採用してよいか, "strong": 全体がよく合うか}"""
    L = len(c) / fs
    n = 2 if L <= 10.5 else max(2, int(round(L / 10.0)))
    edges = [int(round(len(c) * q / n)) for q in range(n + 1)]
    npass, zmax = 0, 0.0
    for w in range(n):
        a = c[edges[w]: edges[w + 1]]
        lo = i0 + edges[w] - int(3 * fs)
        b = jx[max(lo, 0): i0 + edges[w + 1] + int(3 * fs)]
        if len(a) < fs or len(b) <= len(a):
            continue
        cc2 = _phat_local(a, b, fs)
        j = int(cc2.argmax())
        zz = float((cc2[j] - cc2.mean()) / (cc2.std() + 1e-9))
        if abs((max(lo, 0) + j) - (i0 + edges[w])) / fs > ZZ_LAG:
            continue
        zmax = max(zmax, zz)
        if zz >= ZZ_PASS:
            npass += 1
    need = 1 if n <= 2 else 2
    ok = npass >= need and not (n <= 2 and npass == 1 and zmax < ZZ_SOLO)
    return {"zz": round(zmax, 1), "np": npass, "n": n, "pass": ok,
            "strong": ok and npass >= max(need, (n + 1) // 2),
            # 音だけでは決めきれない(片方の窓だけ弱く合う)。映像が合えば採用するための候補
            "weak": (not ok) and n <= 2 and npass == 1 and zmax >= ZZ_PASS}


def find_in_recording(cms, jp, progress=lambda f: None, thr=6.5, dropped=None, loose=False, skip=()):
    """cms: [(音声 float32(SR), ...)]。録画(jp, Media)の中から各CMの音と一致する場所を全部探す。
    まずCM全体の相関で候補を広めに拾い(thr)、そのあと窓ごとの照合(_verify_hit)で本物だけを残す。
    戻り値: [[{"t": 録画の時刻(秒), "z": 突出度, "ok": 全体がよく合うか, "zz": 窓の最大, "np": 合った窓, "n": 窓の数}, ...] (cms と同じ順)]"""
    fs = SR / 2

    def dec(x):
        n = len(x) // 2 * 2
        return x[:n].reshape(-1, 2).mean(1).astype(np.float64)
    jx = jp.x.astype(np.float64) if getattr(jp, "sr", SR) == fs else dec(jp.x)
    cx = [dec(c) for c in cms]
    Lmax = max(len(c) for c in cx)
    jx = np.concatenate([jx, np.zeros(Lmax)])  # 録画の最後で切れているCMも探せるように
    N = 1 << int(np.ceil(np.log2(max(Lmax * 2, 1 << 19))))
    hop = N - Lmax
    f = np.fft.rfftfreq(N, 1 / fs)
    band = (f > 250) & (f < fs * 0.48)
    Fc = [None if k in skip else np.conj(np.fft.rfft(c - c.mean(), N)).astype(np.complex64) for k, c in enumerate(cx)]
    res = [[] for _ in cx]
    nchunk = max(1, int(np.ceil(max(len(jx) - Lmax, 1) / hop)))
    for ci in range(nchunk):
        s0 = ci * hop
        seg = jx[s0: s0 + N]
        if len(seg) < 2 * fs:
            break
        Fj = np.fft.rfft(seg - seg.mean(), N)
        for k, c in enumerate(cx):
            if k in skip:
                continue
            L = len(c)
            valid = len(seg) - L + 1
            if valid < 1:
                continue
            G = Fj * Fc[k]
            G = G / (np.abs(G) + 1e-9) * band
            cc = np.fft.irfft(G, N)[:valid]
            mu, sd = float(cc.mean()), float(cc.std()) + 1e-9
            w = cc.copy()
            for _ in range(12):
                i = int(w.argmax())
                z = (w[i] - mu) / sd
                if z < thr:
                    break
                res[k].append({"t": jp.a_origin + (s0 + i) / fs, "z": float(z)})
                w[max(0, i - L): i + L] = -1e9
        progress((ci + 1) / nchunk)
    # 重なった窓で同じ場所が二重に出た分をまとめ、窓ごとの照合で本物だけを残す
    for k, lst in enumerate(res):
        lst.sort(key=lambda d: -d["z"])
        keep = []
        for d in lst:
            if all(abs(d["t"] - e["t"]) > 0.5 for e in keep):
                keep.append(d)
        c = cx[k]
        out = []
        for d in keep:
            if loose:   # 短縮版の探索: 窓ごとの照合は省き、映像で確かめる前提で、ある程度はっきりした山を全部残す
                if d["z"] >= 8.0:
                    d["ok"], d["zz"], d["np"], d["n"], d["weak"] = False, None, None, None, True
                    out.append(d)
                continue
            i0 = int(round((d["t"] - jp.a_origin) * fs))
            v = _verify_hit(c, jx, i0, fs)
            if not v["pass"] and v.get("weak") and d["z"] >= 10:
                d["ok"] = False
                d["zz"], d["np"], d["n"] = v["zz"], v["np"], v["n"]
                d["weak"] = True
                out.append(d)
                continue
            if not v["pass"]:
                if dropped is not None and d["z"] >= 14:  # 後で原因を調べられるように、強く出たのに落としたものを記録する
                    dropped.append({"k": k, "t": round(d["t"], 2), "z": round(d["z"], 1), "zz": v["zz"], "np": v["np"], "n": v["n"]})
                continue
            d["ok"] = v["strong"]
            d["zz"], d["np"], d["n"] = v["zz"], v["np"], v["n"]
            out.append(d)
        out.sort(key=lambda d: d["t"])
        res[k] = out
    return res


VSCAN_SIM = 0.7     # 映像だけで探すとき、画面が「似ている」とみなす基準
VSCAN_WIN = 3.0     # 時間差がこの秒数の範囲に入るものを同じ並びとみなす
SHORT_MIN_L = 35.0   # この秒数以上の長いCMだけ、短縮版・途中で切れた版も探す
EDGE_SIM = 0.5       # 端を伸ばすとき、1コマの画面が「同じ」とみなす似かた


def make_pieces(cl, skip=()):
    """長いCMを約10秒ずつに分けた [(CMの番号, 開始, 終了), …]。短縮版・途中で切れた版を、部分ごとに探すため。"""
    out = []
    for k, (a, b) in enumerate(cl):
        L = b - a
        if L < SHORT_MIN_L or k in skip:
            continue
        n = max(3, int(round(L / 10.0)))
        for q in range(n):
            out.append((k, a + L * q / n, a + L * (q + 1) / n))
    return out


def _same_frame(en, jp, e, j, cache):
    """英語版の e 秒と録画の j 秒の画面が同じか。"""
    if j < 0:
        return False
    ke, kj = ("e", round(e, 2)), ("j", round(j, 2))
    if ke not in cache:
        cache[ke] = _thumb(en, e)
    if kj not in cache:
        cache[kj] = _thumb(jp, j)
    x, y = cache[ke], cache[kj]
    if x is None and y is None:
        return True      # どちらも真っ暗
    if x is None or y is None:
        return False
    return _pair_sim(x, y) >= EDGE_SIM


def _trim_edge_frames(en, jp, r, sides=(0, 1), win=0.5, fps=29.97):
    """区間 r(x0,x1,o)の先頭と末尾を、1コマ単位で整える。端の数コマだけ、直前・直後の別の場面が混ざっていたら、その分だけ削る。"""
    try:
        for side in sides:
            t_en = r["x0"] if side == 0 else r["x1"] - win
            if r["x1"] - r["x0"] < 4 * win:
                return
            EX, eok = _thumbs_fps(en.path, fps, t_en + en.voff, win)
            JX, jok = _thumbs_fps(jp.path, fps, t_en + r["o"] + jp.voff, win)
            n = min(len(EX), len(JX))
            if n < 6:
                continue
            sm = np.where(eok[:n] & jok[:n], (EX[:n] * JX[:n]).sum(1) / EX.shape[1], 0.0)
            if side == 1:
                sm = sm[::-1]
            k = 0
            while k < n - 3 and not (sm[k] >= 0.6 and sm[k + 1] >= 0.5 and sm[k + 2] >= 0.5):
                k += 1
            if k == 0 or k >= n - 3 or sm[:k].max() >= 0.6:
                continue    # 先頭から合っている/合う所が見つからない/途中に合うコマがある(暗転など)は触らない
            d = k / fps
            if side == 0:
                r["x0"] += d
            else:
                r["x1"] -= d
    except Exception:  # noqa: BLE001
        pass


def _edge_fit(en, jp, e, off, dirn, inner, outer, cache):
    """区間の端 e を、画面が合う範囲ぴったりに合わせる。dirn=-1 は先頭の端、+1 は終わりの端。
    端の内側 0.1 秒の画面が合っていれば、outer(英語版の時刻)に向かって合い続ける所まで広げ、
    合っていなければ inner に向かって合う所まで縮める。1秒刻みで進み、最後は0.25秒刻みで詰める。
    off = 録画の時刻 − 英語版の時刻。合う所がなければ None。"""
    def ok(x):
        pr = x - dirn * 0.1
        return _same_frame(en, jp, pr, pr + off, cache)
    if ok(e):
        cur = e
        while (outer - cur) * dirn > 1e-6:
            nxt = cur + dirn * min(1.0, abs(outer - cur))
            if ok(nxt):
                cur = nxt
                continue
            for s_ in (0.75, 0.5, 0.25):
                if s_ < abs(nxt - cur) and ok(cur + dirn * s_):
                    cur += dirn * s_
                    break
            break
        return cur
    cur = e
    while (inner - cur) * -dirn > 1e-6:
        nxt = cur - dirn * min(1.0, abs(inner - cur))
        if ok(nxt):
            for s_ in (0.25, 0.5, 0.75):   # 合わなかった位置と合った位置の間を詰める
                if s_ < abs(nxt - cur) and ok(cur - dirn * s_):
                    return cur - dirn * s_
            return nxt
        cur = nxt
    return None


def _discover_runs(en, jp, a_cm, b_cm, runs, jend, ecache):
    """音で見つかった区間のまわりの録画を、映像だけで調べて、英語版の残りの部分に合う区間を探す。
    英語版を1秒ごと、録画を0.5秒ごとに見比べて似た所を拾い、時間差がそろうものを区間にする。"""
    cov = sorted((r["x0"], r["x1"]) for r in runs)
    gaps, cur = [], a_cm
    for x0, x1 in cov:
        if x0 - cur >= 3.0:
            gaps.append((cur, x0))
        cur = max(cur, x1)
    if b_cm - cur >= 3.0:
        gaps.append((cur, b_cm))
    if not gaps:
        return []
    L = b_cm - a_cm
    lo = max(jp.a_origin, min(r["x0"] + r["o"] for r in runs) - L - 5)
    hi = min(jend, max(r["x1"] + r["o"] for r in runs) + L + 5)
    jt = []
    for jj in np.arange(lo, hi, 0.5):
        th = _thumb(jp, float(jj))
        if th is not None:
            jt.append((float(jj), th))
    if len(jt) < 5:
        return []
    out = []
    for g0, g1 in gaps:
        pts = []
        for e in np.arange(g0 + 0.5, g1, 1.0):
            x = _thumb(en, float(e))
            if x is None:
                continue
            best = max(((_pair_sim(x, th), jj) for jj, th in jt), key=lambda z_: z_[0])
            if best[0] >= 0.8:
                pts.append((float(e), best[1] - float(e)))
        cl_, cur_ = [], []
        for e, o in pts:
            if cur_ and abs(o - cur_[-1][1]) <= 0.8 and e - cur_[-1][0] <= 3.0:
                cur_.append((e, o))
            else:
                if len(cur_) >= 3:
                    cl_.append(cur_)
                cur_ = [(e, o)]
        if len(cur_) >= 3:
            cl_.append(cur_)
        for c_ in cl_:
            o0 = float(np.median([o for _e, o in c_]))
            probes = [e for e, _o in c_][:: max(1, len(c_) // 3)][:3]

            def score(o):
                sc = []
                for e in probes:
                    x, y = _thumb(en, e), _thumb(jp, e + o)
                    if x is not None and y is not None:
                        sc.append(_pair_sim(x, y))
                return float(np.mean(sc)) if sc else -1.0
            bo = max((o0 + d_ for d_ in np.arange(-0.6, 0.61, 0.1)), key=score)
            bo = max((bo + d_ for d_ in np.arange(-0.1, 0.101, 1 / 30)), key=score)
            out.append({"o": float(bo), "e0": c_[0][0] - 0.5, "e1": c_[-1][0] + 0.5, "n": 1, "z": 0.0, "vid": True})
    return out


def scan_jp_thumbs(path, stop=None):
    """録画全体を、1秒に1コマ(キーフレームだけを読むので速い)の小さな白黒画像にして返す。
    戻り値: (正規化した画像 (N, 1728) float32, 使える画像か (N,) bool)。止められたら None。"""
    W, H = 48, 36
    # キーフレームが約3秒以内ごとにある録画だけ、キーフレームだけ読んで速くする(間隔が長いと、1秒ごとの画面が取れない)
    fast = False
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0", "-read_intervals", "0%120",
                            "-show_entries", "frame=pts_time", "-of", "csv=p=0", path], capture_output=True, timeout=120, **NOWIN)
        fast = len([x for x in r.stdout.decode(errors="ignore").split() if x.strip()]) >= 40
    except Exception:  # noqa: BLE001
        pass
    pre_ = ["-skip_frame", "nokey"] if fast else ["-hwaccel", "auto"]
    p = subprocess.Popen(["ffmpeg", "-v", "error", *pre_, "-i", path, "-an", "-sn",
                          "-vf", f"fps=1,scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **NOWIN)
    chunks = []
    try:
        while True:
            b = p.stdout.read(W * H * 600)
            if not b:
                break
            chunks.append(b)
            if stop is not None and stop.is_set():
                p.kill()
                return None
    finally:
        p.stdout.close()
        p.wait()
    raw = b"".join(chunks)
    n = len(raw) // (W * H)
    if n < 10:
        return None
    a = np.frombuffer(raw[: n * W * H], np.uint8).reshape(n, W * H).astype(np.float32)
    sd = a.std(1)
    ok = sd >= 4.0
    X = (a - a.mean(1, keepdims=True)) / np.where(ok, sd, 1.0)[:, None]
    return X, ok, fast


def en_probes(en, cl, skip=()):
    """長いCMごとに、3秒おきの画面(小さな白黒画像)を取っておく。戻り値: {CM番号: [(英語版の時刻, 画像), …]}"""
    out = {}
    for k, (a, b) in enumerate(cl):
        if b - a < SHORT_MIN_L or k in skip:
            continue
        pr = []
        for e in np.arange(a + 1.5, b - 1.0, 3.0):
            x = _thumb(en, float(e))
            if x is not None:
                pr.append((float(e), x.reshape(-1)))
        if len(pr) >= 5:
            out[k] = pr
    return out


def video_scan_hits(en, jp, probes, th, max_anchor=8, diag=None):
    """録画の画面を1秒ごとに見て、英語版の長いCMの画面とよく似た所が、時間差そろって並んでいる場所を探す(音が合わない版用)。
    戻り値: {CM番号: [{"t","e","l","z"}, …]}  t=録画の時刻、e=英語版の開始、l=長さ"""
    X, ok = th[0], th[1]
    idx = np.where(ok)[0]
    if diag is not None:
        diag.update({"frames": int(len(ok)), "valid": int(ok.sum()), "fast": bool(th[2]) if len(th) > 2 else None})
    if len(idx) < 10:
        return {}
    J = X[idx]
    jsec = idx.astype(np.float64)
    res = {}
    allc = []
    for k, pr in probes.items():
        P = np.stack([x for _e, x in pr])
        S = P @ J.T / P.shape[1]
        pairs = []
        for i in range(S.shape[0]):
            for jj in np.nonzero(S[i] >= VSCAN_SIM)[0]:
                pairs.append((jsec[jj] - pr[i][0], i, float(S[i, jj])))
        if len(pairs) < 4:
            continue
        pairs.sort()
        os_ = np.array([q[0] for q in pairs])
        used = np.zeros(len(pairs), bool)
        cl_ = []
        for a_ in range(len(pairs)):
            if used[a_]:
                continue
            b_ = int(np.searchsorted(os_, os_[a_] + VSCAN_WIN))
            grp = [q for q, u in zip(pairs[a_:b_], used[a_:b_]) if not u]
            ids = {q[1] for q in grp}
            if diag is not None and len(ids) >= 2:
                dl = diag.setdefault("best", [])
                dl.append([int(k), len(ids), round(float(os_[a_]), 1), round(min(pr[i][0] for i in ids), 1), round(max(pr[i][0] for i in ids), 1)])
            if len(ids) >= 4:
                es = [pr[i][0] for i in ids]
                if max(es) - min(es) >= 9.0:
                    cl_.append((len(ids), grp, min(es), max(es)))
                    used[a_:b_] = True
        cl_.sort(key=lambda c: -c[0])
        for n_, grp, e0, e1 in cl_[:3]:
            best = {}
            for o, i, sm in grp:
                if i not in best or sm > best[i][1]:
                    best[i] = (o, sm)
            o0 = float(np.median([v[0] for v in best.values()]))
            allc.append((n_, k, o0, e0, e1, float(np.mean([v[1] for v in best.values()]))))
    allc.sort(key=lambda c: -c[0])
    for n_, k, o0, e0, e1, sm in allc[:max_anchor]:
        pe = [e for e, _x in probes[k]][:: max(1, n_ // 3)][:3]

        def score(o, pe=pe):
            sc = []
            for e in pe:
                x, y = _thumb(en, e), _thumb(jp, e + o)
                if x is not None and y is not None:
                    sc.append(_pair_sim(x, y))
            return float(np.mean(sc)) if sc else -1.0
        bo = max((o0 + d_ for d_ in np.arange(-1.0, 1.01, 0.25)), key=score)
        bo = max((bo + d_ for d_ in np.arange(-0.25, 0.251, 0.05)), key=score)
        res.setdefault(k, []).append({"t": e0 - 1.5 + bo, "e": e0 - 1.5, "l": e1 - e0 + 3.0, "z": 12.0, "vid": True})
    return res


def _thumbs_fps(path, fps, ss=0.0, dur=None):
    """動画の ss 秒から dur 秒分(省略すると最後まで)を、1秒に fps コマの小さな白黒画像にして返す。
    戻り値: (正規化した画像 (N,1728), 使えるか (N,))"""
    W, H = 48, 36
    cmd = ["ffmpeg", "-v", "error"]
    if ss > 0:
        cmd += ["-ss", f"{ss:.4f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.4f}"]
    cmd += ["-i", path, "-an", "-sn", "-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, **NOWIN)
    n = len(r.stdout) // (W * H)
    a = np.frombuffer(r.stdout[: n * W * H], np.uint8).reshape(n, W * H).astype(np.float32)
    sd = a.std(1)
    ok = sd >= 4.0
    return (a - a.mean(1, keepdims=True)) / np.where(ok, sd, 1.0)[:, None], ok


def fine_map_runs(en_path, jp_path, fps=8, jump_cost=0.35, min_sim=0.6, en_win=(0.0, None), jp_win=(0.0, None)):
    """日本語版(jp_path)の画面を細かく、英語版(en_path)のどこに当たるか1コマずつ調べ、連続する区間にまとめる。
    英語版を細かく編集し直した版(2〜4秒ごとに英語版の別の場所へ飛ぶもの)用。英語版では前に戻らない前提。
    en_win / jp_win = (開始秒, 長さ) で範囲を絞れる。戻り値の時刻は、それぞれ範囲の先頭が0の相対時刻。
    戻り値: [{"jp0","jp1","en0","en1","sim","good"}, …]"""
    EX, eok = _thumbs_fps(en_path, fps, *en_win)
    JX, jok = _thumbs_fps(jp_path, fps, *jp_win)
    S = JX @ EX.T / EX.shape[1]
    S[~jok, :] = 0.0
    S[:, ~eok] = 0.0
    J, E = S.shape
    NEG = -1e9
    dp = np.full((J, E), NEG, np.float32)
    bk = np.zeros((J, E), np.int32)      # 前のコマの英語版位置
    dp[0] = S[0]
    for j in range(1, J):
        prev = dp[j - 1]
        cont = np.full(E, NEG, np.float32)
        cont[1:] = prev[:-1]
        pm = np.maximum.accumulate(prev)        # e より手前のどこかから飛んでくる
        am = np.zeros(E, np.int32)
        best = 0
        for e in range(E):
            if prev[e] > prev[best]:
                best = e
            am[e] = best
        jmp = np.full(E, NEG, np.float32)
        jmp[1:] = pm[:-1] - jump_cost
        stay = prev - 0.05                  # 英語版の同じコマに留まる(日本語版だけの場面や、範囲の外のコマ用)
        best_prev = np.concatenate([[0], am[:-1]])
        cand = np.stack([cont, jmp, stay])
        pick = cand.argmax(0)
        dp[j] = S[j] + cand.max(0)
        bk[j] = np.where(pick == 0, np.arange(E) - 1, np.where(pick == 1, best_prev, np.arange(E)))
    e = int(dp[-1].argmax())
    path = [0] * J
    for j in range(J - 1, -1, -1):
        path[j] = e
        e = int(bk[j][e]) if j > 0 else e
    runs = []
    for j in range(J):
        e_ = path[j]
        sm = float(S[j, e_])
        stayed = j > 0 and e_ == path[j - 1]      # 英語版が進まないコマ=英語版に対応する場面がない
        if stayed:
            sm = min(sm, 0.0)
        if runs and e_ == runs[-1]["e_last"] + 1 and (sm >= min_sim) == runs[-1]["good"]:
            r = runs[-1]
            r["j_last"], r["e_last"] = j, e_
            r["sims"].append(sm)
        else:
            runs.append({"j0": j, "j_last": j, "e0": e_, "e_last": e_, "sims": [sm], "good": sm >= min_sim})
    out = []
    for r in runs:
        out.append({"jp0": r["j0"] / fps, "jp1": (r["j_last"] + 1) / fps, "en0": r["e0"] / fps, "en1": (r["e_last"] + 1) / fps,
                    "sim": float(np.mean(r["sims"])), "good": r["good"]})
    # 前後の似かたの低い区間(CMの外の場面)は、先に外す。同じ時間差でつながっていても、外側のコマは取り込まない
    # 前後の似かたの低い所の合計が0.6秒未満なら(暗転や音だけの出入り)残す。それ以上は、CMの外の場面として外す
    for _ in range(2):
        n_ = 0
        while n_ < len(out) and not out[n_]["good"]:
            n_ += 1
        if n_ and (n_ >= len(out) or out[n_ - 1]["jp1"] - out[0]["jp0"] >= 0.6):
            del out[:n_]
        out.reverse()
        for r in out:
            r["jp0"], r["jp1"], r["en0"], r["en1"] = -r["jp1"], -r["jp0"], -r["en1"], -r["en0"]
    # 時間差(英語版の時刻 − 日本語版の時刻)が同じ区間は、間に似かたの低いコマ(フラッシュなど)が挟まっていてもひとつにする
    merged = []
    for r in out:
        if merged and abs((r["en0"] - r["jp0"]) - (merged[-1]["en0"] - merged[-1]["jp0"])) <= 0.15:
            m_ = merged[-1]
            n0, n1 = m_["jp1"] - m_["jp0"], r["jp1"] - r["jp0"]
            m_["sim"] = (m_["sim"] * n0 + r["sim"] * n1) / (n0 + n1)
            m_["jp1"], m_["en1"] = r["jp1"], r["en1"]
            m_["good"] = m_["sim"] >= min_sim
        else:
            merged.append(dict(r))
    # 短い(0.4秒未満)似かたの低い区間は、前の区間の続きとして吸収する(日本語の音声は途切れさせない)
    res = []
    for r in merged:
        if res and not r["good"] and r["jp1"] - r["jp0"] < 0.4:
            m_ = res[-1]
            m_["jp1"] = r["jp1"]
            m_["en1"] = m_["en0"] + (m_["jp1"] - m_["jp0"])
        else:
            res.append(r)
    return res


def refine_fine_cuts(en, jp, runs, fps=29.97):
    """fine_map_runs の区間のつなぎ目を、1コマ単位に合わせ直す。前後の区間それぞれの対応(時間差)で画面を見比べ、
    前の区間の画面が続く最後のコマと、次の区間の画面が始まる最初のコマの間を、つなぎ目にする。
    en, jp は path と voff を持つもの(時刻は動画の先頭=0 の絶対時刻の runs を渡す)。"""
    step = 1.0 / fps
    for a, b in zip(runs, runs[1:]):
        tb = a["jp1"]
        oa, ob = a["en0"] - a["jp0"], b["en0"] - b["jp0"]
        cache = {}

        def sim(t, o):
            key = (round(t, 4), round(o, 4))
            if key not in cache:
                x, y = _thumb(jp, t), _thumb(en, t + o)
                cache[key] = -0.2 if (x is None or y is None) else _pair_sim(x, y)
            return cache[key]
        best, bt = -1e9, tb
        for c in [tb + k * step for k in range(-8, 9)]:   # c より前は a の対応、c 以降は b の対応
            sc = sum(sim(t, oa) for t in (c - 2 * step, c - step)) + sum(sim(t, ob) for t in (c, c + step))
            if sc > best:
                best, bt = sc, c
        a["jp1"], a["en1"] = bt, bt + oa
        b["jp0"], b["en0"] = bt, bt + ob
    return runs


def _fine_version_once(en, jp, a_cm, b_cm, j0, j1, margin=6.0, diag=None):
    """録画の j0〜j1 付近にある、英語版CM(a_cm〜b_cm)を細かく編集し直した版を調べる。
    日本語版の画面を英語版へ1コマずつ当てて、合う範囲が全体の85%以上なら、その区間の並びを返す。
    戻り値: None または {"runs":[[英開始,英終了,録画開始],…], "jp0","jp1","v"}"""
    js = max(jp.a_origin, j0 - margin)
    je = j1 + margin
    runs = fine_map_runs(en.path, jp.path, en_win=(a_cm + en.voff, b_cm - a_cm), jp_win=(js + jp.voff, je - js))
    for r in runs:   # 範囲の先頭が0の相対時刻 → 動画の先頭=0 の絶対時刻
        r["en0"] += a_cm
        r["en1"] += a_cm
        r["jp0"] += js
        r["jp1"] += js
    # 英語版のCMの終わりより先(次のCMなど)に当たった所は、CMの外なので切り落とす(終わりの余裕は0.6秒)
    lim = b_cm + 0.6
    cut = []
    for r in runs:
        if r["en0"] >= lim - 0.2:
            continue
        if r["en1"] > lim:
            r["jp1"] -= r["en1"] - lim
            r["en1"] = lim
        cut.append(r)
    runs = cut
    if diag is not None:
        diag["win"] = [round(js, 1), round(je, 1)]
        diag["raw"] = [[round(r["en0"], 1), round(r["en1"], 1), round(r["jp0"], 1), round(r["jp1"], 1), round(r["sim"], 2), int(r["good"])] for r in runs][:30]
    while runs and not runs[0]["good"]:
        runs.pop(0)
    while runs and not runs[-1]["good"]:
        runs.pop()
    if len(runs) < 2:
        if diag is not None:
            diag["why"] = "runs<2"
        return None
    tot = runs[-1]["jp1"] - runs[0]["jp0"]
    good = sum(r["jp1"] - r["jp0"] for r in runs if r["good"])
    if tot < 8.0 or good < 0.85 * tot:
        if diag is not None:
            diag["why"] = "good %.1f/%.1f" % (good, tot)
        return None
    sim = sum(r["sim"] * (r["jp1"] - r["jp0"]) for r in runs if r["good"]) / good
    if sim < 0.85:
        if diag is not None:
            diag["why"] = "sim %.2f" % sim
        return None
    runs = refine_fine_cuts(en, jp, runs)
    runs = [r for r in runs if r["jp1"] - r["jp0"] >= 0.1]
    if len(runs) < 2 or any(b["en0"] < a["en1"] - 0.05 for a, b in zip(runs, runs[1:])):
        if diag is not None:
            diag["why"] = "overlap"
        return None   # 英語版の時刻が前に戻る/重なるものは対象外
    return {"runs": [[round(r["en0"], 4), round(r["en1"], 4), round(r["jp0"], 4)] for r in runs],
            "jp0": runs[0]["jp0"], "jp1": runs[-1]["jp1"], "v": round(sim, 2)}


def fine_version(en, jp, a_cm, b_cm, j0, j1, margin=6.0, diag=None):
    """_fine_version_once を呼ぶ。見つかった範囲が英語版CMの頭や終わりに届かない(=日本語版の前後にまだ続きがあるかもしれない)ときは、
    日本語版の調べる範囲をCMの長さぶんまで広げて、もう一度調べる。"""
    r = _fine_version_once(en, jp, a_cm, b_cm, j0, j1, margin, diag)
    wide = min(70.0, b_cm - a_cm)
    if wide <= margin + 1.0:
        return r
    need = r is None
    if r is not None:
        need = r["runs"][0][0] - a_cm > 1.5 or b_cm - r["runs"][-1][1] > 1.5
    if need:
        d2 = {} if diag is not None else None
        r2 = _fine_version_once(en, jp, a_cm, b_cm, j0, j1, wide, d2)
        if r2 is not None and (r is None or (r2["jp1"] - r2["jp0"]) > (r["jp1"] - r["jp0"]) + 0.3):
            return r2
    return r


def find_short_versions(en, jp, cl, pieces, phits, vcache, keep, dropped, cap=40, plog=None, extra=None):
    """長いCMの短縮版・途中で切れた版を探す。pieces(CMを約10秒ずつに分けたもの)の各部分を録画から探した結果 phits のうち、
    画面も合うものを、録画と英語版の時間差(オフセット)が同じ部分ごとにまとめて区間にし、画面の合う範囲に両端をぴったり合わせる。
    区間が順番どおりに並び、合計2か所以上で合っていれば1つの短縮版とみなす。
    戻り値: keep に足す項目 [{"k","t","z","ok","L","v","short","out_len","runs":[[英開始,英終了,録画開始],…]}, …]"""
    cand = []
    for pi, hl in enumerate(phits):
        for h in hl:
            cand.append((pi, h))
    cand.sort(key=lambda x: -x[1]["z"])
    hits = {}
    jend = jp.a_origin + len(jp.x) / jp.sr
    for pi, h in cand[:cap]:
        k, a, b = pieces[pi]
        try:
            vf = []
            v = video_similarity(en, a, b - a, jp, h["t"], vcache, detail=vf)
        except Exception:  # noqa: BLE001
            v, vf = None, []
        if plog is not None:
            plog.append({"k": k, "e": round(a, 1), "t": round(h["t"], 2), "z": round(h["z"], 1), "v": v, "vf": vf})
        # 部分だけ合う(一部が日本語版でカットされた)ことがあるので、平均が低くても、画面がはっきり一致する所が2か所以上あれば候補にする
        if v is None or not (vid_good({"v": v, "z": h["z"]}) or sum(1 for f_ in vf if f_ >= 0.8) >= 2):
            continue
        hits.setdefault(k, []).append({"t": h["t"], "e": a, "l": b - a, "z": h["z"]})
    for k, hl_ in (extra or {}).items():   # 映像だけで見つけた場所(音が合わない版)。音で見つかったCMには使わない(時間差の誤差が混ざるため。残りの部分は後で映像で探す)
        if k not in hits:
            hits[k] = list(hl_)
    out = []
    ecache = {}
    for k, hl in hits.items():
        a_cm, b_cm = cl[k]
        groups = []
        for h in sorted(hl, key=lambda x: x["e"]):
            o = h["t"] - h["e"]
            for g in groups:
                if abs(o - g["o"]) <= 0.4:
                    g["h"].append(h)
                    g["o"] = float(np.median([x["t"] - x["e"] for x in g["h"]]))
                    break
            else:
                groups.append({"o": o, "h": [h]})
        runs = [{"o": g["o"], "e0": min(x["e"] for x in g["h"]), "e1": max(x["e"] + x["l"] for x in g["h"]),
                 "n": len(g["h"]), "z": max(x["z"] for x in g["h"])} for g in groups]
        runs.sort(key=lambda r: r["e0"])
        for i, r in enumerate(runs):
            lo = runs[i - 1]["e1"] if i else a_cm
            hi = runs[i + 1]["e0"] if i + 1 < len(runs) else b_cm
            lo = max(min(lo, r["e0"]), jp.a_origin - r["o"], a_cm)
            hi = min(max(hi, r["e1"]), jend - r["o"], b_cm)
            r["x0"] = _edge_fit(en, jp, r["e0"], r["o"], -1, r["e1"], lo, ecache)
            r["x1"] = _edge_fit(en, jp, r["e1"], r["o"], +1, r["e0"], hi, ecache)
        runs = [r for r in runs if r["x0"] is not None and r["x1"] is not None and r["x1"] - r["x0"] >= 2.0]
        if runs and (b_cm - a_cm) - sum(r["x1"] - r["x0"] for r in runs) >= 3.0:
            try:
                extra = _discover_runs(en, jp, a_cm, b_cm, runs, jend, ecache)
            except Exception:  # noqa: BLE001
                extra = []
            for r in extra:
                r["x0"] = _edge_fit(en, jp, r["e0"], r["o"], -1, r["e1"], max(a_cm, jp.a_origin - r["o"]), ecache)
                r["x1"] = _edge_fit(en, jp, r["e1"], r["o"], +1, r["e0"], min(b_cm, jend - r["o"]), ecache)
                z_ = max(x_["z"] for x_ in runs)
                r["z"] = z_
                if r["x0"] is not None and r["x1"] is not None and r["x1"] - r["x0"] >= 2.0:
                    runs.append(r)
            runs.sort(key=lambda r: r["x0"])
            # 重なる区間は、音で見つけたほうを優先する
            fixed = []
            for r in runs:
                if fixed and r["x0"] < fixed[-1]["x1"] - 0.5 and r.get("vid"):
                    continue
                if fixed and fixed[-1].get("vid") and r["x0"] < fixed[-1]["x1"] - 0.5:
                    fixed.pop()
                fixed.append(r)
            runs = fixed
        # 区間が重なるときは、根拠の多い(音や映像の一致が多い・長い)ほうを残し、弱いほうの重なる分を削る
        runs.sort(key=lambda r: r["x0"])
        i_ = 0
        while i_ + 1 < len(runs):
            r, r2 = runs[i_], runs[i_ + 1]
            if r["x1"] > r2["x0"] + 1e-6:
                if (r["n"], r["x1"] - r["x0"]) >= (r2["n"], r2["x1"] - r2["x0"]):
                    r2["x0"] = max(r2["x0"], r["x1"])
                else:
                    r["x1"] = min(r["x1"], r2["x0"])
                if r2["x1"] - r2["x0"] < 2.0:
                    runs.pop(i_ + 1)
                    continue
                if r["x1"] - r["x0"] < 2.0:
                    runs.pop(i_)
                    continue
            i_ += 1
        # 録画で順番どおりに、ほぼ続けて並ぶ区間を1つの短縮版にまとめる
        chains = []
        for r in runs:
            for ch in chains:
                last = ch[-1]
                gap = (r["x0"] + r["o"]) - (last["x1"] + last["o"])
                if -0.7 <= gap <= 3.0:
                    ch.append(r)
                    break
            else:
                chains.append([r])
        for ch in chains:
            # 録画のうえで前の区間の終わりより手前から始まる(同じ所を2回使う)と、繰り返しに聞こえる。重ならないように直す
            for r1, r2 in zip(ch, ch[1:]):
                gap = (r2["x0"] + r2["o"]) - (r1["x1"] + r1["o"])
                if gap < 0:
                    if r2.get("vid"):
                        r2["o"] -= gap          # 映像だけで見つけた側は時間差の誤差があり得るので、そちらを直す
                    elif r1["x1"] + gap - r1["x0"] >= 2.0:
                        r1["x1"] += gap
            tot = sum(r["x1"] - r["x0"] for r in ch)
            if tot < 8.0 or (sum(r["n"] for r in ch) < 2 and tot < 15.0):
                continue
            _trim_edge_frames(en, jp, ch[0], (0, 1) if len(ch) == 1 else (0,))
            if len(ch) > 1:
                _trim_edge_frames(en, jp, ch[-1], (1,))
            tot = sum(r["x1"] - r["x0"] for r in ch)
            j0, j1 = ch[0]["x0"] + ch[0]["o"], ch[-1]["x1"] + ch[-1]["o"]
            # 同じ場所を、CM全体の一致がすでに押さえていれば、それを優先する
            ov = [e_ for e_ in keep if (e_.get("v") is None or e_["v"] >= VID_MIN)
                  and min(j1, e_["t"] + e_["L"]) - max(j0, e_["t"]) > 0.5 * min(j1 - j0, e_["L"])]
            ws = vs = 0.0
            for r in ch:
                try:
                    v = video_similarity(en, r["x0"], r["x1"] - r["x0"], jp, r["x0"] + r["o"], vcache)
                except Exception:  # noqa: BLE001
                    v = None
                if v is not None:
                    ws += r["x1"] - r["x0"]
                    vs += v * (r["x1"] - r["x0"])
            vm = round(vs / ws, 2) if ws >= 0.6 * tot else None
            z = max(r["z"] for r in ch)
            if vm is None or not vid_good({"v": vm, "z": z}):
                if not ov:   # 細かく編集し直された版(2〜4秒ごとに英語版の別の場所へ飛ぶもの)かもしれない。1コマずつ当てて調べる
                    fd_ = {}
                    try:
                        fv = fine_version(en, jp, a_cm, b_cm, j0, j1, diag=fd_)
                    except Exception as ex_:  # noqa: BLE001
                        fv = None
                        fd_["why"] = "例外 %s" % ex_
                    if fv is not None and not any(abs(o_["t"] - fv["jp0"]) < 3.0 and o_["k"] == k for o_ in out):
                        out.append({"k": k, "t": fv["jp0"], "z": z, "ok": True, "L": fv["jp1"] - fv["jp0"], "zz": None, "np": None, "n": None,
                                    "v": fv["v"], "vf": [], "short": True, "fine": True, "out_len": fv["jp1"] - fv["jp0"],
                                    "runs": fv["runs"], "jp_span": [fv["jp0"], fv["jp1"]], "voff": jp.voff})
                        continue
                if dropped is not None:
                    dropped.append({"k": k, "t": round(j0, 2), "z": round(z, 1), "v": vm, "short": True,
                                    "fine_diag": dict(fd_, span=[round(j0, 1), round(j1, 1)]) if not ov else "ov"})
                continue
            short = tot < 0.95 * (b_cm - a_cm)
            if short and not ov:   # 見つかった区間の前後にも、細かく編集された続きがあるか調べる(あれば、そちらのほうが長く正確)
                try:
                    fv = fine_version(en, jp, a_cm, b_cm, j0, j1)
                except Exception:  # noqa: BLE001
                    fv = None
                if fv is not None and (fv["jp1"] - fv["jp0"]) > (j1 - j0) + 2.0:
                    out.append({"k": k, "t": fv["jp0"], "z": z, "ok": True, "L": fv["jp1"] - fv["jp0"], "zz": None, "np": None, "n": None,
                                "v": fv["v"], "vf": [], "short": True, "fine": True, "out_len": fv["jp1"] - fv["jp0"],
                                "runs": fv["runs"], "jp_span": [fv["jp0"], fv["jp1"]], "voff": jp.voff})
                    continue
            if ov:
                # 一部の画面だけ合わないCM全体の一致(終わりが途中で切れた版など)は、画面の合う範囲だけの短縮版に置きかえる
                if not (short and all((not e_.get("runs")) and e_.get("vf") and min(e_["vf"]) < 0.4 and vm >= e_["v"] + 0.1 for e_ in ov)):
                    continue
                for e_ in ov:
                    keep.remove(e_)
            out.append({"k": k, "t": j0, "z": z, "ok": True, "L": j1 - j0, "zz": None, "np": None, "n": None,
                        "v": vm, "vf": [], "short": short, "out_len": tot,
                        "runs": [[round(r["x0"], 4), round(r["x1"], 4), round(r["x0"] + r["o"], 4)] for r in ch]})
    return out


def build_from_items(items, en_rng, jp_rng):
    """指定(同じ場面/CM/暗転・長さ違い/カット)から、エンジン用の区間リストを組み立てる。

    考え方: 英語側から「カット(英語にしかない所)」を、日本語側から「CM(日本語にしかない所)」を
    取り除いた軸の上で、「同じ場面」の点を直線でつなぐ。そのあと取り除いた所を元に戻して区間にする。
    戻り値: (segs, extra, warns)  extra = 英語の音声をそのまま使う区間
    """
    pts = [(i["en"], i["jp"]) for i in items if i["k"] == "pt"]
    dims = [i for i in items if i["k"] == "dim"]
    for d in dims:
        pts += [(d["en0"], d["jp0"]), (d["en1"], d["jp1"])]
    cms = sorted((min(i["jp0"], i["jp1"]), max(i["jp0"], i["jp1"])) for i in items if i["k"] == "cm")
    cuts = sorted((min(i["en0"], i["en1"]), max(i["en0"], i["en1"]), i.get("audio", "silence"))
                  for i in items if i["k"] == "cut")
    warns = []
    if not pts:
        return [], [], ["「同じ場面」を1つ以上登録してください"]
    cut_r = [(a, b) for a, b, _ in cuts]

    def rm_before(x, ranges):
        tot = 0.0
        for a, b in ranges:
            if x >= b:
                tot += b - a
            elif x > a:
                tot += x - a
        return tot

    to_enp = lambda t: t - rm_before(t, cut_r)   # noqa: E731
    to_jpp = lambda x: x - rm_before(x, cms)     # noqa: E731

    # --- 「同じ場面」の点を、取り除いたあとの軸に変換して並べる
    knots = []
    for en, jp in sorted(pts):
        u, q = to_enp(en), to_jpp(jp)
        if knots and abs(u - knots[-1][0]) < 1e-6:
            if abs(q - knots[-1][1]) > 0.02:
                warns.append(f"英語 {en:.2f}秒 の「同じ場面」が重複しています")
            continue
        if knots and q < knots[-1][1] - 1e-3:
            warns.append(f"英語 {en:.2f}秒 の「同じ場面」は、日本語側の順番が前後しているため使いません")
            continue
        knots.append((u, q))
    L = 1e7
    ext = [(knots[0][0] - L, knots[0][1] - L)] + knots + [(knots[-1][0] + L, knots[-1][1] + L)]

    def qmap(u):
        for (ua, qa), (ub, qb) in zip(ext, ext[1:]):
            if u <= ub:
                return qa + (qb - qa) * (u - ua) / (ub - ua)
        return ext[-1][1]

    def qinv(q):
        for (ua, qa), (ub, qb) in zip(ext, ext[1:]):
            if qb > qa and qa <= q <= qb:
                return ua + (q - qa) * (ub - ua) / (qb - qa)
        return None

    t0e, t1e = en_rng
    U0, U1 = to_enp(t0e), to_enp(t1e)
    bps = {U0, U1}
    for u, _ in knots:
        if U0 < u < U1:
            bps.add(u)
    for a, _b in cut_r:
        ap = to_enp(a)
        if U0 < ap < U1:
            bps.add(ap)
    for c, _d in cms:
        u = qinv(to_jpp(c))
        if u is not None and U0 < u < U1:
            bps.add(u)
    bl = sorted(bps)
    cut_pts = [(to_enp(a), b - a) for a, b in cut_r]
    cm_pts = [(to_jpp(c), d - c) for c, d in cms]

    segs = []
    for u0, u1 in zip(bl, bl[1:]):
        if u1 - u0 < 1e-4:
            continue
        q0, q1 = qmap(u0), qmap(u1)
        t0 = u0 + sum(ln for ap, ln in cut_pts if ap <= u0 + 1e-9)
        t1 = u1 + sum(ln for ap, ln in cut_pts if ap < u1 - 1e-9)
        x0 = q0 + sum(ln for cp, ln in cm_pts if cp <= q0 + 1e-9)
        x1 = q1 + sum(ln for cp, ln in cm_pts if cp < q1 - 1e-9)
        fit, in_dim = "stretch", False
        for d in dims:
            if d["en0"] - 1e-3 <= t0 and t1 <= d["en1"] + 1e-3:
                fit, in_dim = d.get("fit", "stretch"), True
        ratio = (x1 - x0) / (t1 - t0)
        if not in_dim and abs(ratio - 1) > 0.02:
            warns.append(f"英語 {t0:.1f}〜{t1:.1f}秒: 音の速さが {ratio:.3f} 倍になります。"
                         "「同じ場面」の位置がずれている可能性があります。暗転などの長さ違いなら「暗転・長さ違い」で指定してください")
        # 日本語録画の範囲外にはみ出す分は詰める
        if x0 < jp_rng[0]:
            d0 = jp_rng[0] - x0
            t0 += d0 / ratio
            x0 += d0
        if x1 > jp_rng[1]:
            t1 -= (x1 - jp_rng[1]) / ratio
            x1 = jp_rng[1]
        if t1 - t0 < 0.05:
            continue
        segs.append({"en_start": t0, "en_end": t1, "jp_start": x0,
                     "jp_end": None if abs((x1 - x0) - (t1 - t0)) <= 0.002 else x1,
                     "fit": fit, "note": ""})
    # 連続していて同じ速さの区間は1つにまとめる(点を増やしても区間が細切れにならないように)
    merged = []
    for s in segs:
        if merged:
            p = merged[-1]
            pe = p["jp_end"] if p["jp_end"] is not None else p["jp_start"] + (p["en_end"] - p["en_start"])
            pr = (pe - p["jp_start"]) / (p["en_end"] - p["en_start"])
            se = s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + (s["en_end"] - s["en_start"])
            sr = (se - s["jp_start"]) / (s["en_end"] - s["en_start"])
            if (abs(p["en_end"] - s["en_start"]) < 1e-3 and abs(pe - s["jp_start"]) < 1e-3
                    and abs(pr - sr) < 1e-4 and p["fit"] == s["fit"]):
                p["en_end"] = s["en_end"]
                ne = se
                p["jp_end"] = None if abs((ne - p["jp_start"]) - (p["en_end"] - p["en_start"])) <= 0.002 else ne
                continue
        merged.append(dict(s))
    extra = [{"type": "english", "en_start": max(a, t0e), "en_end": min(b, t1e)}
             for a, b, au in cuts if au == "english" and min(b, t1e) > max(a, t0e)]
    return merged, extra, warns


def items_from_segs(segs, en_end=None):
    """区間リスト(自動検出の結果など) → 指定(同じ場面/CM/カット) に変換。
    en_end: 英語版の長さ。先頭・末尾を英語の無音に合わせて詰めた区間は、その外側を「カット(無音)」として残す。"""
    items, prev = [], None
    ss = sorted(segs, key=lambda s: s["en_start"])
    if ss and ss[0].get("trim_s") and ss[0]["en_start"] > 0.05:
        items.append({"k": "cut", "en0": 0.0, "en1": ss[0]["en_start"], "audio": "silence"})
    if ss and ss[-1].get("trim_e") and en_end is not None and en_end - ss[-1]["en_end"] > 0.05:
        tail = {"k": "cut", "en0": ss[-1]["en_end"], "en1": float(en_end), "audio": "silence"}
    else:
        tail = None
    for s in ss:
        ed = s["en_end"] - s["en_start"]
        je = s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + ed
        items.append({"k": "pt", "en": s["en_start"], "jp": s["jp_start"], "note": ""})
        if s["jp_end"] is not None and abs((je - s["jp_start"]) - ed) > 0.002:
            items.append({"k": "pt", "en": s["en_end"], "jp": je, "note": ""})
        if prev:
            pen, pje = prev
            # ごく小さな食い違い(0.005秒以上)も、そのまま「間」や「飛ばす所」として残す。
            # (残さないと、前の区間を少し伸び縮みさせてつじつまを合わせることになり、区間の中で音が少しずつずれる)
            if s["en_start"] - pen > 0.005:
                items.append({"k": "cut", "en0": pen, "en1": s["en_start"], "audio": "silence"})
            if s["jp_start"] - pje > 0.005:
                items.append({"k": "cm", "jp0": pje, "jp1": s["jp_start"]})
        prev = (s["en_end"], je)
    if tail:
        items.append(tail)
    return items


# ------------------------------------------------------------------ ツールチップ (マウスを乗せると説明が出る)


class Tooltip:
    DELAY = 500

    def __init__(self, widget, text):
        self.w, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _enter(self, e=None):
        self._hide()
        self.job = self.w.after(self.DELAY, self._show)

    def _show(self):
        self.job = None
        try:
            x = self.w.winfo_pointerx() + 14
            y = self.w.winfo_pointery() + 18
            t = tk.Toplevel(self.w)
            t.wm_overrideredirect(True)
            tk.Label(t, text=self.text() if callable(self.text) else self.text, justify="left", bg="#fffbe6",
                     fg="#333", relief="solid", borderwidth=1, padx=8, pady=5, wraplength=360,
                     font=("", 9)).pack()
            t.update_idletasks()
            sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
            x = max(0, min(x, sw - t.winfo_reqwidth() - 8))
            y = y if y + t.winfo_reqheight() < sh - 8 else self.w.winfo_pointery() - t.winfo_reqheight() - 8
            t.wm_geometry(f"+{x}+{y}")
            self.tip = t
        except Exception:
            self.tip = None

    def _hide(self, e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


# ボタン等の表示文字 → 説明
TIPS = {
    "参照": "ファイルを選ぶ画面を開きます。",
    "読み込む": "選んだ2つのファイルを読み込んで、波形と画面を表示します。",
    "設定を開く": "前に保存した作業(同じ場面・CMなどの指定)を開きます。",
    "設定を保存": "いまの作業を保存します。あとで「設定を開く」で続きから再開できます。",
    "全体 (Home)": "波形を全体が見える大きさに戻します。(Home キー)",
    "縮小 ↓": "波形を縮小して、広い範囲を見ます。(↓ キー / ホイール)",
    "拡大 ↑": "波形を拡大して、細かく見ます。(↑ キー / ホイール)",
    "▶ 再生": "カーソルの位置から、約20秒の音を再生します。もう一度押すと止まります。(Space)",
    "A 音で探して合わせる": "上(英語)のカーソル付近の音を、下(日本語)から探して、自動でカーソルを合わせます。",
    "S この2つは『同じ場面』": "上下のカーソルの位置は同じ場面(同じフレーム)、と登録します。音合わせの基準になります。(S キー)",
    "登録するとき、音で微調整する": "登録した位置のわずかなずれを、音の照合で自動的に直します。通常はオンのままで構いません。",
    "CM": "波形で選んだ下(日本語)の範囲を『CM』として、使わない部分にします。",
    "暗転・長さ違い": "上下で選んだ範囲は同じ場面だが長さが違うとき(暗転など)に使います。速さを変える・無音で埋める・切る、から選べます。",
    "カット": "波形で選んだ上(英語)の範囲は、日本語版でカットされている部分として登録します。",
    "選択を解除": "波形で選んだ範囲を取り消します。(Esc キー)",
    "編集": "選んだ行の内容を変更します。(行をダブルクリックでも可)",
    "削除": "選んだ行を削除します。(Delete キー。Ctrl を押しながら複数選べます)",
    "全部削除": "一覧の指定をすべて消します。(Ctrl+Z で戻せます)",
    "変更": "保存先のファイル名と場所を変えます。",
    "合成スタート": "日本語の音声を合わせて、1本の mkv に書き出します。映像は再エンコードしません。",
    "自動で合わせる": "2つの音を照合して、対応する場所を自動で探します。長い動画は数分かかります。",
    "▶ 英語版を聞く": "選んだ区間の先頭付近を、英語版の音で再生します。",
    "▶ 日本語を聞く": "選んだ区間の先頭付近を、日本語の録画の音で再生します。聞き比べて確かめます。",
    "手動で直す →": "手動調整のタブを開きます。自動でうまくいかない区間を直せます。",
    "フォルダを開く": "出来上がったファイルのあるフォルダを開きます。",
    "追加...": "日本語のファイルを選んで一覧に足します(複数まとめて選べます)。",
    "外す": "一覧で選んだファイルを外します(ファイルそのものは消えません)。",
    "全部外す": "一覧を空にします。",
    "CMを切り分ける": "英語版の映像を、黒い画面と無音を目印にCM1本ずつに分けて一覧にします。",
    "探してスタート": "日本語の録画を1本ずつ調べ、英語版の各CMと同じ音の場所を全部探して、日本語音声つきで書き出します。",
    "切り出しスタート": "一覧のファイルを1本ずつ自動で合わせ、一致した部分だけを英語版から切り出して書き出します。",
}


def attach_tips(root, extra=()):
    """root 以下のボタン等に TIPS の説明を付ける。extra は (部品, 説明) の追加分。"""
    def walk(w):
        for c in w.winfo_children():
            try:
                t = c.cget("text")
            except Exception:
                t = None
            if isinstance(t, str) and t in TIPS and c.winfo_class() in ("TButton", "TCheckbutton", "Button"):
                Tooltip(c, TIPS[t])
            walk(c)
    walk(root)
    for w, text in extra:
        Tooltip(w, text)


# ------------------------------------------------------------------ 波形トラック


class Track(tk.Canvas):
    KIND_STYLE = {"cm": ("#bfc3ca", "CM (使わない)", "#444"),
                  "cut": ("#ffd8a8", "カット (日本語版にない)", "#8a5300"),
                  "dim": ("#e6d9ff", "暗転・長さ違い", "#5b3aa8")}

    def __init__(self, master, app, key, title, **kw):
        super().__init__(master, height=78, bg="#fbfbfd", highlightthickness=1,
                         highlightbackground="#bbb", takefocus=True, **kw)
        self.app, self.key, self.title = app, key, title
        self.media = None
        self.vs, self.vspan = 0.0, 60.0
        self.cursor = 0.0
        self.bands = []    # [(開始, 終了, 区間番号)]  色つきの帯
        self.marks = []    # [(開始, 終了, 種類)]      CM / カット / 暗転
        self.pts = []      # [(時刻, ラベル)]          「同じ場面」の点
        self.sel = None    # 強調する区間番号
        self.selr = None   # ドラッグで選んだ範囲 (開始, 終了)
        self._press = None
        self._pan = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_motion)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<MouseWheel>", self.on_wheel)
        self.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        self.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        self.bind("<ButtonPress-3>", self.pan_start)
        self.bind("<B3-Motion>", self.pan_move)

    def x_of(self, t):
        return (t - self.vs) / self.vspan * self.winfo_width()

    def t_of(self, x):
        return self.vs + x / max(self.winfo_width(), 1) * self.vspan

    # --- マウス: クリック=カーソル / ドラッグ=範囲選択 / ホイール=拡大縮小 / 右ドラッグ=横移動
    def on_press(self, e):
        self.focus_set()
        self.app.set_active(self.key)
        self._press = (e.x, False)

    def on_motion(self, e):
        if not self._press:
            return
        x0, dragging = self._press
        if not dragging and abs(e.x - x0) < 4:
            return
        self._press = (x0, True)
        a, b = sorted((self.t_of(x0), self.t_of(e.x)))
        self.selr = (a, b)
        self.redraw()

    def on_release(self, e):
        if not self._press:
            return
        _x0, dragging = self._press
        self._press = None
        if dragging:
            self.app.on_range(self.key)
        else:
            self.app.set_cursor(self.key, self.t_of(e.x))

    def on_wheel(self, e, direction=None):
        d = direction if direction is not None else (1 if e.delta > 0 else -1)
        if e.state & 0x1:  # Shift: 横移動
            self.vs += -d * self.vspan * 0.2
            self.redraw()
        else:
            self.app.zoom(self.key, 0.75 if d > 0 else 1.0 / 0.75, self.t_of(e.x))

    def zoom_by(self, f, anchor):
        new = min(max(self.vspan * f, 0.05), 6 * 3600)
        self.vs = anchor - (anchor - self.vs) * new / self.vspan
        self.vspan = new

    def pan_start(self, e):
        self._pan = (e.x, self.vs)

    def pan_move(self, e):
        if self._pan:
            self.vs = self._pan[1] - (e.x - self._pan[0]) / self.winfo_width() * self.vspan
            self.redraw()

    def ensure_visible(self, t):
        if t < self.vs + self.vspan * 0.05 or t > self.vs + self.vspan * 0.95:
            self.vs = t - self.vspan / 2

    @staticmethod
    def tick_label(t, step):
        sign = "-" if t < -1e-9 else ""
        t = abs(t)
        h, m, sec = int(t // 3600), int(t % 3600 // 60), t % 60
        if step < 1:
            body = f"{m}:{sec:05.2f}" if not h else f"{h}:{m:02d}:{sec:05.2f}"
        else:
            body = f"{m}:{int(round(sec)):02d}" if not h else f"{h}:{m:02d}:{int(round(sec)):02d}"
        return sign + body

    def redraw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 10:
            return
        body = H - 22  # 波形エリアの高さ (下は目盛り)
        mid = body / 2
        v1 = self.vs + self.vspan
        # --- 区間の色帯 (波形の下地)
        for a, b, i in self.bands:
            xa, xb = self.x_of(a), self.x_of(b)
            if xb < 0 or xa > W:
                continue
            self.create_rectangle(max(xa, 0), 0, min(xb, W), body, fill=PASTEL[i % len(PASTEL)], outline="")
        # --- どの区間にも入らない部分 (灰色の斜線)
        if self.bands:
            cur, gaps = self.vs, []
            for a, b in sorted((a, b) for a, b, _ in self.bands):
                if a > cur:
                    gaps.append((cur, min(a, v1)))
                cur = max(cur, b)
                if cur >= v1:
                    break
            if cur < v1:
                gaps.append((cur, v1))
            for a, b in gaps:
                if b > a:
                    self.create_rectangle(max(self.x_of(a), 0), 0, min(self.x_of(b), W), body,
                                          fill="#cfd2d8", stipple="gray50", outline="")
        # --- CM / カット / 暗転 の指定
        for a, b, kind in self.marks:
            xa, xb = self.x_of(a), self.x_of(b)
            if xb < 0 or xa > W:
                continue
            col, label, tcol = self.KIND_STYLE[kind]
            x0, x1 = max(xa, 0), min(xb, W)
            self.create_rectangle(x0, 0, x1, body, fill=col, stipple="gray50", outline=tcol, dash=(3, 2))
            if x1 - x0 > 40:
                self.create_text((x0 + x1) / 2, body - 11, text=label, fill=tcol, font=("", 9, "bold"))
        # --- ドラッグで選んだ範囲
        if self.selr:
            xa, xb = self.x_of(self.selr[0]), self.x_of(self.selr[1])
            self.create_rectangle(xa, 0, xb, body, fill="#cfe3ff", outline="#2f6fd0", width=2)
        self.create_line(0, mid, W, mid, fill="#c9cfd8")
        # --- 波形
        m = self.media
        if m is None:
            self.create_text(W / 2, H / 2, text="(ファイルを読み込むとここに波形が出ます)", fill="#888")
        else:
            env = m.envelope(self.vs, v1, W)
            amp = (body - 6) / 2 * 0.95 / m.scale
            if env and env[0] == "env":
                _, cols, mn, mx = env
                top = np.column_stack([cols, mid - np.clip(mx * amp, -1e4, 1e4)])
                bot = np.column_stack([cols, mid - np.clip(mn * amp, -1e4, 1e4)])[::-1]
                self.create_polygon(np.vstack([top, bot]).ravel().tolist(), fill="#3b6ea5", outline="#3b6ea5")
            elif env:
                _, xs, ys = env
                self.create_line(np.column_stack([xs, mid - ys * amp]).ravel().tolist(), fill="#3b6ea5")
        # --- 区間の境界線・名前 (A, B, C…)
        for a, b, i in self.bands:
            xa, xb = self.x_of(a), self.x_of(b)
            if xb < 0 or xa > W:
                continue
            col = DARK[i % len(DARK)]
            w = 3 if i == self.sel else 1
            for x in (xa, xb):
                if 0 <= x <= W:
                    self.create_line(x, 0, x, body, fill=col, width=w)
            if i == self.sel:
                self.create_line(max(xa, 0), 1, min(xb, W), 1, fill=col, width=3)
                self.create_line(max(xa, 0), body - 1, min(xb, W), body - 1, fill=col, width=3)
            if xb - xa > 14:
                self.create_text(max(xa, 0) + 5, 11, text=seg_letter(i), anchor="w", fill=col, font=("", 12, "bold"))
        # --- 目盛り
        self.create_rectangle(0, body, W, H, fill="#eef0f4", outline="")
        step = next((s for s in (0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
                     if s * W / self.vspan >= 70), 3600)
        t = np.ceil(self.vs / step) * step
        while t < v1:
            x = self.x_of(t)
            self.create_line(x, body, x, body + 5, fill="#777")
            self.create_text(x + 2, body + 12, text=self.tick_label(t, step), anchor="w", fill="#666", font=("", 7))
            t += step
        # --- 「同じ場面」の点
        for t, label in self.pts:
            x = self.x_of(t)
            if -20 < x < W + 20:
                self.create_line(x, 0, x, body, fill="#1a9e57", width=2)
                self.create_rectangle(x, 0, x + 8 + 7 * len(label), 15, fill="#1a9e57", outline="")
                self.create_text(x + 4, 8, text=label, anchor="w", fill="white", font=("", 8, "bold"))
        x = self.x_of(self.cursor)
        self.create_line(x, 0, x, body, fill="#e0262d", width=1)
        if self.selr:
            xa, xb = self.x_of(self.selr[0]), self.x_of(self.selr[1])
            self.create_text((xa + xb) / 2, 24, text=f"選択 {self.selr[1] - self.selr[0]:.2f}秒",
                             fill="#2f6fd0", font=("", 9, "bold"))
        self.create_text(W - 6, 11, text=self.title, anchor="e", fill="#333", font=("", 9, "bold"))


# ------------------------------------------------------------------ タスクバーの独立表示とアイコン

TASKBAR_APP_ID = "tatata17.syncdub"
APP_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAGWElEQVR42u2bWVcURxTH+QZ8BGL2nayKZCFGjVnFJW4sDuIyIIaoLOJuoiZvjjIsA2pGBUWNxqjgjqOJScwIDorvvCQa8zIf4eZW092n7y17qqetOUcn1Dm/U3X+fbvvrd/zTE7O2Bpbnlbe+zuKkVDeeztiJvCIkTDniuKcASRX08VDm5EkAo8hKCPkT8QTRTvzkQQCjzlJpDi9y3+wKx9JIpBFBDxdftyk5nwk2y5vgPcKeBGQQCBLSY6bFM5zv/yH4c0IZDkxVwFPTm5JIvA/IF++/JTWYgR08cfQX+BcOr+tgZAk4KkpbSEEdHGdCdD5bQ0kZAFT22MI6OL60N9UgKK+pP4kqd914AbonIcjCXj6o0gMAV1wAar6kvpTkgCd83BkAdM6QCfXbzEBivrSBiaga0D5jnOJfmnOWEQEPPNxJ/iBD2HlXIDqO6UNp0l9Mwrw29sjXMBu8AMd4q6dizMVMJo3dw2SvKyx18jFTgUM+u7tESrg2U/2gB/4EFbOBVh5c7csQOSSAKzz29sjTMCne8EP0hBmLgkw8+bum1TAmj4jFzsVcFPZwy33CBXw3Gc/QCqc68/bd5W5ODuXlYcPUgHlTWeMXOzOJep092ZwAVFIBW12T5mLMx1iNA8fTDABZ41c7FRAQntvBhXw/Of7IBW8mSrnQ1i5JGDtWSMXOxeguzeDCfgCwxRIzRS5NISZhw9RAQvXnjNysRMBWKe7N4MJmL4fUiE1U+TSEGYePjREBaw7b+RipwKGtPdmUAEvFB+AVJBmw/eUuTg7l5W39DAB688budidS9Tp7s3gArogFYH1F2xmruy1czrEP3YuznSI0byl5xbJxfes71MBt5Q93HIxn3NelztRAS/O6AY/8CGsnAuwcknAhotGLnYuwHpHPLOYtapP2dsjTMDMg+AHaQgzlwSYecvh27IAzCUBWOe3t0eogJdmHQI/OFcch7DyOBNg5VODJ6Fi4yWbgrJjRi7OztWKAvz29ggX0AN+qNjUbzO77pydx4fvMwGK72zsZwKGle9QAffTnZ0KeHn2YdBJ/A4VoKqv2HSZCjgy7OkdCyE/zRmZgC+PgE64gHjLPICj79rw+kWbuYA7oHsmBhXwypyjoBOVAF5fGDiBEmI205b3gu6ZGEzA3B9BJ/E7/1IBrUyA4v1FNSFS37alDjTPSAW8OvcY6GRO40Wo3HLVJt46n1xI9X5lzU4moB40z8gEzDsOmUQSoKivXPEAAYp3nPWin6KeCsif/xPowDnEDRzCym8wAarvLF6xi9S3f9Pgu7cLTMCCE6ADaQgzlwSYubiYM1/8VbORi10S4LO3C2MCiIDXSn4GHZAh2hbYuTg7n1l5+7eNJF9SGzZysRMBWKfq4Za7wASUnoR0kJopckmAmUsCvm4xcrFLAvT2pgJeLzsF6cCbqXI+hJVHtq5hAlqNXOzOXNRp7s0ElJ+GdJCaKXJpCDPnApaubDVysUsC9PamAt4o74V0oM1KlLk4O59ZeWRrExPQZuRipwKadPdmAhb2QTqQZu0lylycyRBmHtnGBKxqN3KxEwFYp7k3FfBm4Aykg/ODA9hAlQ+wIay8Y9taki9b3W7kYnfmok5zbyag4iykw7LVEZsFtfvsXBrCzKUhzLxjOxcQsb9PBGCdqodbLuZzzmvmVMBbi86BDugQpXYuzs5nVt6xfR0VUNdh5GKnAtbZ74hnFiW1+5W9XeACzoMO5CFGc1nAaM4FBOs6jVzssgB/vV2gAt6uvAA6IENESu1cnJ3PrHx6zVEI1nfaTAqeMnJxdtZ3frfed28XmIDFFxMIPCxVDbttSld22bkkQPGdIL4vCVC8IwlIWX+B/ox+/JJLMQQyxUCkjAyoqq9q2EMFfL/B0zsWpau6U9ZKvxIbv6Q/ikCmkAWkrq9qfJAAbfOMyAKW9gcmLL0MmWKQCVDVT67ug+rGvTYzao9rmwXvGpUETFh2OReBTDHYwQRksJcHHvwPkgnBWBSBTFBe1wPVTVGbTPXxwIjrz+ULgldyC6quJBHIWoJXilL+Y6Sg6moxAllKyNP/hiZW/xJAIMuIpvXPsYnLfw0gSQSygJCv/w4WLr+WV1hzLYbAY8oI3qHoof89WljzWz4SQhIIPOKMIFGkOCeT650Vvxc9YuTmjK3013/PaO/sco8vJgAAAABJRU5ErkJggg=="
)


def set_taskbar_identity():
    """pythonw.exe で動く他の .pyw とタスクバーで1つにまとめられないよう、専用IDを付ける(Windowsのみ)。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(TASKBAR_APP_ID)
    except Exception:  # noqa: BLE001
        pass


def set_window_taskbar_identity(win):
    """ウインドウ側にも同じIDを付ける(プロセスIDだけで分かれない環境への保険)。失敗しても無視。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

        class PKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]

        def guid(txt):
            import uuid
            u = uuid.UUID(txt)
            g = GUID()
            g.a, g.b, g.c = u.time_low, u.time_mid, u.time_hi_version
            for i, v in enumerate(u.bytes[8:]):
                g.d[i] = v
            return g

        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        iid = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")  # IID_IPropertyStore
        store = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store))
        if hr != 0 or not store.value:
            return
        vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        key = PKEY(guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)  # PKEY_AppUserModel_ID
        text = TASKBAR_APP_ID
        nbytes = (len(text) + 1) * 2
        ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
        mem = ole32.CoTaskMemAlloc(nbytes)
        ctypes.memmove(mem, ctypes.create_unicode_buffer(text), nbytes)
        pv = PROPVARIANT()
        pv.vt, pv.p = 31, mem  # VT_LPWSTR
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PKEY), ctypes.POINTER(PROPVARIANT))
        proto(vtbl[6])(store, ctypes.byref(key), ctypes.byref(pv))               # SetValue
        ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])(store)       # Commit
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(store)      # Release
        ole32.CoTaskMemFree(ctypes.c_void_p(mem))
    except Exception:  # noqa: BLE001
        pass


class RangeSelect:
    """Treeview の複数選択をエクスプローラーと同じ動きにする (起点を1つだけ持つ)。
    クリック/Ctrl+クリック=その行が起点 / Shift+クリック=起点からの範囲 / Ctrl+Shift+クリック=範囲を追加 /
    Shift+↑↓・PageUp/Down・Home/End=起点から広げる / ↑↓のみ=移動先が新しい起点 / Ctrl+A=全選択"""

    def __init__(self, tree):
        self.t, self.anchor = tree, None
        tree.configure(selectmode="extended")
        tree.bind("<Button-1>", self.click, add="+")
        tree.bind("<Control-a>", self.all)
        tree.bind("<Control-A>", self.all)
        for k in ("Up", "Down", "Prior", "Next", "Home", "End"):
            tree.bind(f"<Shift-{k}>", lambda e, k=k: self.shift_key(k))
            tree.bind(f"<KeyPress-{k}>", lambda e: self.after_move(), add="+")

    def rows(self):
        return list(self.t.get_children())

    def _anchor(self):
        rows = self.rows()
        if self.anchor in rows:
            return self.anchor
        sel = self.t.selection()
        self.anchor = self.t.focus() or (sel[0] if sel else (rows[0] if rows else None))
        return self.anchor

    def click(self, e):
        row = self.t.identify_row(e.y)
        if not row:
            return
        shift, ctrl = bool(e.state & 0x1), bool(e.state & 0x4)
        if shift:
            a, rows = self._anchor(), self.rows()
            if a in rows:
                i, j = sorted((rows.index(a), rows.index(row)))
                rng = rows[i:j + 1]
                self.t.selection_set(list(self.t.selection()) + rng if ctrl else rng)
                self.t.focus(row)
                return "break"
        self.anchor = row

    def shift_key(self, k):
        rows = self.rows()
        if not rows:
            return "break"
        a = self._anchor()
        cur = self.t.focus() if self.t.focus() in rows else a
        i = rows.index(cur)
        ni = {"Up": i - 1, "Down": i + 1, "Prior": i - 10, "Next": i + 10, "Home": 0, "End": len(rows) - 1}[k]
        ni = min(max(ni, 0), len(rows) - 1)
        lo, hi = sorted((rows.index(a), ni))
        self.t.selection_set(rows[lo:hi + 1])
        self.t.focus(rows[ni])
        self.t.see(rows[ni])
        return "break"

    def after_move(self):
        self.t.after_idle(lambda: setattr(self, "anchor", self.t.focus() or self.anchor))

    def all(self, _e=None):
        self.t.selection_set(self.rows())
        return "break"


# ------------------------------------------------------------------ アプリ


def audio_sync_info(path, sr=8000, rng=2.0):
    """書き出したファイルの日本語音声(1本目)と英語音声(2本目)を全体で突き合わせ、(ずれ秒, 山の強さ) を返す。
    ずれ: 負=日本語が早い。判定できなければ None。確認ウインドウの参考表示用。"""
    try:
        raws = []
        for i in (0, 1):
            r = _subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{i}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                                capture_output=True, check=True, **NOWIN).stdout
            raws.append(np.frombuffer(r, dtype=np.int16).astype(np.float32))
        j, e = raws
        n = min(len(j), len(e), 90 * sr)
        if n < 3 * sr:
            return None
        a, b = e[:n], j[:n]
        if a.std() < 30 or b.std() < 30:
            return None
        R = int(rng * sr)
        m = 1 << int(np.ceil(np.log2(2 * n)))
        full = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(a, m)), m)
        c = np.concatenate([full[m - R:], full[:R + 1]]) / np.sqrt(float((a * a).sum()) * float((b * b).sum()) + 1e-9)
        i = int(np.argmax(c))
        return ((i - R) / sr, float(c[i]))
    except Exception:  # noqa: BLE001
        return None


def _mmss(t):
    t = max(float(t), 0.0)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def output_check(path, sr=8000, cuts=()):
    """書き出した動画の日本語音声(1本目)と英語音声(2本目)を比べて、おかしそうな所を探す(ベータ版の自動チェック用)。
    cuts: 区間の切れ目(出力の時刻)。ひと続きの区間の中では日本語の音は切れ目なくつながっているので、
          前後が合っているのに途中の1か所だけずれる、ということは起きない(測定の外れとして扱う)。
    戻り値: (問題の一覧 [文字列], 補足の一覧 [文字列])。比べられなければ (None, [理由])。"""
    try:
        raws = []
        for i in (0, 1):
            r = _subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{i}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                                capture_output=True, **NOWIN)
            if r.returncode != 0 or len(r.stdout) < 2 * sr:
                return None, ["出力に音声が2本(日本語・英語)入っていないため、比べられませんでした。"]
            raws.append(np.frombuffer(r.stdout, dtype=np.int16))   # 長い動画でもメモリを食わないよう、16ビットのまま持つ(計算する所だけ変換)
    except Exception as ex:  # noqa: BLE001
        return None, [f"出力を読めませんでした: {ex}"]
    j, e = raws
    n = min(len(j), len(e))
    j, e = j[:n], e[:n]
    notes = [f"出力の長さ {_mmss(n / sr)} / 日本語と英語の音声の長さの差 {abs(len(raws[0]) - len(raws[1])) / sr:.2f}秒"]
    issues = []
    # 1) 片方だけ音がある所
    h = sr // 4
    k = n // h
    def block_rms(x):     # 0.25秒ごとの音量。少しずつ(約2分ぶん)変換して計算する
        out = np.empty(k, np.float32)
        step = 4096
        for q in range(0, k, step):
            c = x[q * h: min(q + step, k) * h].astype(np.float32).reshape(-1, h)
            out[q: q + len(c)] = np.sqrt((c ** 2).mean(1))
        return out
    rj, re_ = block_rms(j), block_rms(e)
    pj = float(np.percentile(rj[rj > 1], 75)) if (rj > 1).any() else 0.0
    pe = float(np.percentile(re_[re_ > 1], 75)) if (re_ > 1).any() else 0.0
    if pj <= 0 or pe <= 0:
        return None, ["日本語か英語の音声が、ほぼ無音でした。"]

    def runs(mask, min_len):
        out, s0 = [], None
        for i, m in enumerate(list(mask) + [False]):
            if m and s0 is None:
                s0 = i
            elif not m and s0 is not None:
                if (i - s0) * h / sr >= min_len:
                    out.append((s0 * h / sr, i * h / sr))
                s0 = None
        return out
    for a, b in runs((re_ < 0.03 * pe) & (rj >= 0.15 * pj), 0.75):
        issues.append(f"{_mmss(a)}〜{_mmss(b)} ({b - a:.1f}秒): 英語は無音なのに、日本語の音が入っています")
    quiet_j = runs((rj < 0.03 * pj) & (re_ >= 0.15 * pe), 2.0)
    for a, b in quiet_j:
        issues.append(f"{_mmss(a)}〜{_mmss(b)} ({b - a:.1f}秒): 英語は音があるのに、日本語が無音です")
    # 1.5) 一瞬の途切れ: 日本語が鳴っている最中に、0.02〜2秒だけ完全な無音になる所(つなぎ目に無音が入った跡)
    hb = sr // 100
    kb = n // hb
    mxj = np.abs(j[: kb * hb].reshape(kb, hb)).max(1)
    i = 0
    while i < kb:
        if mxj[i] > 12:
            i += 1
            continue
        i0 = i
        while i < kb and mxj[i] <= 12:
            i += 1
        ln = (i - i0) * hb / sr
        if not 0.02 <= ln < 2.0:
            continue
        a_, b_ = i0 * hb, i * hb
        w = int(0.2 * sr)

        def rms(x, p0, p1):
            x = x[max(p0, 0): min(p1, n)].astype(np.float32)
            return float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
        if (rms(j, a_ - w, a_) >= 0.15 * pj and rms(j, b_, b_ + w) >= 0.15 * pj and rms(e, a_, b_) >= 0.1 * pe):
            issues.append(f"{_mmss(a_ / sr)} あたり ({ln:.2f}秒): 日本語の音が、鳴っている最中に一瞬途切れています")
    # 2) ずれ: 6秒ごとに、日本語を前後3秒動かしていちばん合う位置を見る(BGM・効果音が同じ所でだけ分かる)
    W, R = 6 * sr, 3 * sr
    lags = []
    for s0 in range(0, n - W, W):
        a = e[s0: s0 + W].astype(np.float64)
        b0, b1 = max(0, s0 - R), min(n, s0 + W + R)
        b = j[b0:b1].astype(np.float64)
        if a.std() < 0.05 * pe or b.std() < 0.05 * pj:
            continue
        a = a - a.mean()
        m = 1 << int(np.ceil(np.log2(len(a) + len(b))))
        cc = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(a, m)), m)[: len(b) - len(a) + 1]
        en_ = np.sqrt(np.maximum(np.convolve(b ** 2, np.ones(len(a)), "valid"), 1e-9)) * float(np.linalg.norm(a))
        c = cc / en_
        i = int(np.argmax(c))
        i0 = s0 - b0
        # 「ずれなし」の近く(前後0.04秒)でいちばん合う値。震える音や細かく刻む音では、合う位置の候補が等間隔に並び、
        # 測定がとなりの山を選ぶことがある。ずれなしの山も同じくらい高ければ、ずれとは見なさない
        w0 = int(0.04 * sr)
        near0 = float(c[max(i0 - w0, 0): i0 + w0 + 1].max()) if 0 <= i0 < len(c) else 0.0
        lags.append((s0 / sr, (i - i0) / sr, float(c[i]), near0))
    good = [x for x in lags if x[2] >= 0.25]
    bad = [x for x in good if abs(x[1]) > 0.1 and x[3] < 0.7 * x[2]]
    amb = [x for x in good if abs(x[1]) > 0.1 and x[3] >= 0.7 * x[2]]
    # 1か所だけ外れていて、すぐ前とすぐ後(30秒以内)がどちらも合っている弱い一致(0.4未満)は、測定の外れとして扱う
    iso = []
    for x in list(bad):
        k = good.index(x)
        pv = good[k - 1] if k > 0 else None
        nx = good[k + 1] if k + 1 < len(good) else None
        if not (x[2] < 0.4 and pv and nx and abs(pv[1]) <= 0.05 and abs(nx[1]) <= 0.05):
            continue
        near = x[0] - pv[0] <= 30.0 and nx[0] - x[0] <= 30.0
        same_seg = bool(cuts) and not any(pv[0] < c_ < nx[0] + 6.0 for c_ in cuts)   # 前後の測定と同じ区間の中
        if near or same_seg:
            bad.remove(x)
            iso.append(x)
    cur = None
    for t, lg, c, c0 in bad + [(None, None, None, None)]:
        if cur and t is not None and abs(t - cur[1] - 6.0) < 0.01 and abs(lg - cur[2]) <= 0.05:
            cur[1] = t
            continue
        if cur:
            issues.append(f"{_mmss(cur[0])}〜{_mmss(cur[1] + 6)}: 日本語の音が {abs(cur[2]):.2f}秒 {'遅れて' if cur[2] > 0 else '早く'}入っているようです (一致 {cur[3]:.2f})")
        cur = [t, t, lg, c] if t is not None else None
    if good:
        ok_n = sum(1 for x in good if abs(x[1]) <= 0.1) + len(amb)
        if iso:
            notes.append("1か所だけ外れた測定(前後は合っているので、ずれとは数えていません): "
                         + " ".join(f"{_mmss(x[0])}={x[1]:+.2f}" for x in iso[:20]))
        if amb:
            notes.append("繰り返しのある音で測定があいまいな所(ずれとは数えていません): " + " ".join(_mmss(x[0]) for x in amb[:20]))
        notes.append(f"ずれを測れた所 {len(good)} か所のうち、合っていた所 {ok_n} か所 (測れなかった所 {len(lags) - len(good)} か所)")
        notes.append("測れた所のずれ(秒): " + " ".join(f"{_mmss(x[0])}={x[1]:+.2f}" for x in good[:80]))
    else:
        notes.append("ずれは測れませんでした(日本語と英語で、同じBGM・効果音がはっきり重なる所がない)")
    return issues, notes


def preview_map(segs, x):
    """出力の x 秒目に出ている映像は、日本語の録画の何秒か(録画の映像の先頭=0基準)。segs は書き出しの区間(つないで出力される)。"""
    ox = 0.0
    for sg in segs:
        ln = sg["en_end"] - sg["en_start"]
        if x <= ox + ln + 1e-6 or sg is segs[-1]:
            f = min(max(x - ox, 0.0), ln)
            if sg.get("jp_end") is not None and ln > 0:
                return sg["jp_start"] + f * (sg["jp_end"] - sg["jp_start"]) / ln
            return sg["jp_start"] + f
        ox += ln
    return None


class ReviewWindow(tk.Toplevel):
    """ベータ版用の確認ウインドウ: 書き出したもののうち怪しいものを、日本語版と英語版の画面を並べて見比べ、判定をつけて保存する。"""
    JUDGES = ["未確認", "OK", "音ズレ", "別物(映像が違う)", "その他"]
    N = 4
    W = 200

    def __init__(self, app, results, outd):
        super().__init__(app)
        self.app, self.results, self.outd = app, results, Path(outd)
        self.title(f"確認ウインドウ (ベータ版) - {len(results)} 本")
        self.geometry("1360x800")
        self.cache, self.tok, self.cur = {}, 0, None
        self.judge = {id(r): "未確認" for r in results}
        self.memo = {id(r): "" for r in results}
        self.lag = {}
        top = ttk.Frame(self)
        top.pack(fill="both", expand=True)
        pw = ttk.PanedWindow(top, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=1)
        pw.add(right, weight=3)
        # --- 左: 一覧と判定
        self.only = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="怪しいものだけ表示(要確認・短縮版・音ズレ補正・映像0.8未満)", variable=self.only, command=self._fill).pack(anchor="w", padx=6, pady=4)
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, padx=6)
        self.tree = ttk.Treeview(lf, columns=("j", "cm", "v", "lag", "jp"), show="headings", selectmode="browse")
        for c, h, w in (("j", "判定", 110), ("cm", "CM", 70), ("v", "映像", 50), ("lag", "音ズレ測定", 130), ("jp", "録画", 160)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        for j_, col in (("OK", "#1f6f43"), ("音ズレ", "#b3541e"), ("別物(映像が違う)", "#b0262f"), ("その他", "#555")):
            self.tree.tag_configure(j_, foreground=col)
        self.tree.pack(side="left", fill="both", expand=True)
        vscroll(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._select())
        jf = ttk.LabelFrame(left, text=" 判定(キー 1〜4) ")
        jf.pack(fill="x", padx=6, pady=6)
        self.jv = tk.StringVar(value="未確認")
        for i, nm in enumerate(self.JUDGES[1:], 1):
            ttk.Radiobutton(jf, text=f"{i} {nm}", value=nm, variable=self.jv, command=self._set_judge).pack(anchor="w", padx=8)
        ttk.Radiobutton(jf, text="0 未確認に戻す", value="未確認", variable=self.jv, command=self._set_judge).pack(anchor="w", padx=8)
        mf = ttk.Frame(left)
        mf.pack(fill="x", padx=6)
        ttk.Label(mf, text="メモ:").pack(side="left")
        self.mv = tk.StringVar()
        me = ttk.Entry(mf, textvariable=self.mv)
        me.pack(side="left", fill="x", expand=True, padx=4)
        me.bind("<KeyRelease>", lambda e: self._set_memo())
        bf = ttk.Frame(left)
        bf.pack(fill="x", padx=6, pady=6)
        ttk.Button(bf, text="結果を保存", command=self._save).pack(side="left")
        ttk.Button(bf, text="結果をコピー(会話に貼る用)", command=self._copy).pack(side="left", padx=6)
        self.note = ttk.Label(left, text="", foreground="#1f6f43", wraplength=460, justify="left")
        self.note.pack(fill="x", padx=6, pady=(0, 6))
        for k in "01234":
            self.bind(k, lambda e, k=k: self._key(k))
        # --- 右: 画面の比較
        self.info = ttk.Label(right, text="", justify="left", wraplength=980)
        self.info.pack(anchor="w", padx=8, pady=6)
        self.rows = []
        for title in ("日本語版(録画)", "英語版(書き出した映像)"):
            ttk.Label(right, text=title, font=("", 10, "bold")).pack(anchor="w", padx=8)
            fr = ttk.Frame(right)
            fr.pack(anchor="w", padx=8, pady=(2, 10))
            cells = []
            for _ in range(self.N):
                c = ttk.Frame(fr)
                c.pack(side="left", padx=(0, 8))
                im = ttk.Label(c, text="", anchor="center", width=int(self.W / 7))
                im.pack()
                tx = ttk.Label(c, text="", foreground="#667")
                tx.pack()
                cells.append((im, tx))
            self.rows.append(cells)
        self._fill()
        self.after(200, lambda: pw.sashpos(0, 500))
        threading.Thread(target=self._measure, daemon=True).start()

    def _suspicious(self, r):
        return bool(r["why"] or r["short"] or r["lagfix"] is not None or (r["v"] is not None and r["v"] < 0.8))

    def _fill(self):
        sel = self.cur
        self.tree.delete(*self.tree.get_children())
        self.shown = [r for r in self.results if (not self.only.get()) or self._suspicious(r)]
        for i, r in enumerate(self.shown):
            self.tree.insert("", "end", iid=str(i), tags=(self.judge[id(r)],), values=self._vals(r))
        if self.shown:
            i = self.shown.index(sel) if sel in self.shown else 0
            self.tree.selection_set(str(i))
            self.tree.focus(str(i))
            self.tree.see(str(i))

    def _vals(self, r):
        lg = self.lag.get(id(r))
        lgs = "…" if id(r) not in self.lag else ("判定不能" if lg is None else ("ほぼ0" if abs(lg[0]) < 0.06 else f"{lg[0]:+.2f}秒"))
        tag = ("補正済 " if r["lagfix"] is not None else "") + ("短縮 " if r["short"] else "") + ("要確認 " if r["why"] else "")
        return (self.judge[id(r)], r["cm"], f"{r['v']:.2f}" if r["v"] is not None else "-", tag + lgs, r["jp_name"])

    def _refresh_row(self, r):
        if r in self.shown:
            self.tree.item(str(self.shown.index(r)), values=self._vals(r), tags=(self.judge[id(r)],))

    def _measure(self):
        for r in self.results:
            try:
                self.lag[id(r)] = audio_sync_info(r["out"])
            except Exception:  # noqa: BLE001
                self.lag[id(r)] = None
            try:
                self.app.q.put(lambda r=r: self._refresh_row(r))
            except tk.TclError:
                return

    def _key(self, k):
        if isinstance(self.focus_get(), ttk.Entry):
            return
        if k == "0":
            self.jv.set("未確認")
        else:
            self.jv.set(self.JUDGES[int(k)])
        self._set_judge()
        i = self.shown.index(self.cur) if self.cur in self.shown else -1
        if k != "0" and 0 <= i < len(self.shown) - 1:   # 判定したら次へ進む
            self.tree.selection_set(str(i + 1))
            self.tree.focus(str(i + 1))
            self.tree.see(str(i + 1))

    def _set_judge(self):
        if self.cur is not None:
            self.judge[id(self.cur)] = self.jv.get()
            self._refresh_row(self.cur)

    def _set_memo(self):
        if self.cur is not None:
            self.memo[id(self.cur)] = self.mv.get()

    def _select(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.shown[int(sel[0])]
        self.cur = r
        self.jv.set(self.judge[id(r)])
        self.mv.set(self.memo[id(r)])
        bits = [f"{r['cm']}  {r['name']}", f"録画: {r['jp_name']}  (録画の {r['t']:.1f} 秒付近)"]
        fl = []
        if r["why"]:
            fl.append("要確認: " + "・".join(r["why"]))
        if r["short"]:
            fl.append("短縮版")
        if r["lagfix"] is not None:
            fl.append(f"音ズレを自動補正({r['lagfix']:+.2f}秒)")
        fl.append(f"映像 {r['v']:.2f}" if r["v"] is not None else "映像 -")
        fl.append(f"音の強さ {r['z']:.1f}")
        self.info.config(text="\n".join(bits) + "\n" + " / ".join(fl))
        self._thumbs(r)

    def _thumbs(self, r):
        self.tok += 1
        tok = self.tok
        for row in self.rows:
            for im, tx in row:
                im.config(image="", text="…")
                im._img = None
                tx.config(text="")

        def grab(path, t):
            key = (path, round(t, 2))
            png = self.cache.get(key)
            if png is None:
                try:
                    png = _subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
                                           "-vf", f"scale={self.W}:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                                          capture_output=True, **NOWIN).stdout or b""
                except Exception:  # noqa: BLE001
                    png = b""
                if png:
                    self.cache[key] = png
            return png

        def work():
            try:
                dur = float(probe(r["out"])["format"]["duration"])
            except Exception:  # noqa: BLE001
                dur = max(1.0, sum(sg["en_end"] - sg["en_start"] for sg in r["segs"]))
            try:
                jvoff = video_start_offset(probe(r["jp"]))
            except Exception:  # noqa: BLE001
                jvoff = 0.0
            xs = [dur * (0.06 + 0.88 * i / (self.N - 1)) for i in range(self.N)]
            for i, x in enumerate(xs):
                if tok != self.tok:
                    return
                jt = preview_map(r["segs"], x)
                pj = grab(r["jp"], jt + jvoff) if jt is not None else b""
                pe = grab(r["out"], x)
                self.app.q.put(lambda i=i, x=x, jt=jt, pj=pj, pe=pe: self._show(tok, i, x, jt, pj, pe))
        threading.Thread(target=work, daemon=True).start()

    def _show(self, tok, i, x, jt, pj, pe):
        if tok != self.tok:
            return
        for row, png, cap in ((self.rows[0], pj, f"録画 {jt:.1f}秒" if jt is not None else "-"), (self.rows[1], pe, f"出力 {x:.1f}秒")):
            im, tx = row[i]
            try:
                img = tk.PhotoImage(data=base64.b64encode(png)) if png else None
            except tk.TclError:
                img = None
            im._img = img
            im.config(image=img if img else "", text="" if img else "(取れませんでした)")
            tx.config(text=cap)

    def _rows(self):
        out = []
        for r in self.results:
            lg = self.lag.get(id(r))
            out.append({"判定": self.judge[id(r)], "メモ": self.memo[id(r)], "CM": r["cm"], "出力": r["name"], "録画": r["jp_name"],
                        "録画の時刻": round(r["t"], 2), "映像": r["v"], "音の強さ": round(r["z"], 1), "短縮版": r["short"], "細かい編集": r["fine"],
                        "要確認の理由": r["why"], "音ズレ自動補正": r["lagfix"],
                        "音ズレ測定": (None if not lg else round(lg[0], 3)), "音ズレ測定の強さ": (None if not lg else round(lg[1], 2)),
                        "区間": r["segs"]})
        return out

    def _text(self):
        head = f"syncdub 確認結果 {ver_label()}  {datetime.now():%Y-%m-%d %H:%M}"
        lines = [head, "判定\tCM\t映像\t音\t短縮\t要確認\t自動補正\t音ズレ測定\t録画\t録画の時刻\t出力\tメモ"]
        for x in self._rows():
            lines.append("\t".join(str(v) for v in (
                x["判定"], x["CM"], x["映像"], x["音の強さ"], "短縮" if x["短縮版"] else "", "・".join(x["要確認の理由"]),
                "" if x["音ズレ自動補正"] is None else f"{x['音ズレ自動補正']:+.2f}", "" if x["音ズレ測定"] is None else f"{x['音ズレ測定']:+.2f}",
                x["録画"], x["録画の時刻"], x["出力"], x["メモ"])))
        return "\n".join(lines) + "\n"

    def _save(self):
        try:
            self.outd.mkdir(parents=True, exist_ok=True)
            (self.outd / "syncdub_確認結果.json").write_text(json.dumps({"version": APP_VERSION, "items": self._rows()}, ensure_ascii=False, indent=1), encoding="utf-8")
            (self.outd / "syncdub_確認結果.txt").write_text(self._text(), encoding="utf-8")
            self.note.config(text=f"保存しました: {self.outd / 'syncdub_確認結果.json'}(と .txt)", foreground="#1f6f43")
        except OSError as ex:
            self.note.config(text=f"保存できませんでした: {ex}", foreground="#b0262f")

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._text())
        self.note.config(text="コピーしました。会話に貼り付けてください。", foreground="#1f6f43")


class App(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("syncdub - 吹き替え音声シンクロツール" + ("  【ベータ版】" if BETA_REVIEW else ""))
        try:  # 専用アイコン (True: 後から開くウインドウにも同じアイコン)
            self._icon = tk.PhotoImage(data=APP_ICON_PNG_B64)
            self.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass
        self.geometry("1280x980")
        self.media = {"en": None, "jp": None}
        self.items = []        # ユーザーの指定 (同じ場面 / CM / 暗転・長さ違い / カット)
        self.segs = []         # items から自動計算した区間 (エンジンに渡す)
        self.extra_segs = []   # 英語版の音声をそのまま使う区間
        self.warns = []
        self.undo_st, self.redo_st, self._last = [], [], []  # 元に戻す用
        self.auto_flags = None  # 自動検出直後の「要確認」情報 (手で直すと消える)
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
        for k in ("s", "a", "space", "Left", "Right", "Tab", "Up", "Down", "Escape", "Home"):
            self.bind_all(f"<KeyPress-{k}>" if len(k) > 1 else f"<Key-{k}>", self._key)
        # ボタンを押したあと、フォーカスが残って Space/Enter で再実行されないようにする
        self.bind_class("TButton", "<ButtonRelease-1>", lambda e: self.after(50, self._unfocus), add="+")
        for seq, fn in (("<Control-z>", self.undo), ("<Control-Z>", self.undo), ("<Control-y>", self.redo),
                        ("<Control-Y>", self.redo)):
            self.bind_all(seq, lambda e, fn=fn: None if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, tk.Text)) else fn())
        def ok_(fn):
            return lambda e: None if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, tk.Text)) else (fn(), "break")[1]
        for seq, fn in (("<Control-Shift-z>", self.redo), ("<Control-Shift-Z>", self.redo),
                        ("<Control-s>", lambda: self.save_project(ask=False)), ("<Control-S>", lambda: self.save_project(ask=True)),
                        ("<Control-o>", self.open_project), ("<Control-0>", self.fit_all),
                        ("<Control-plus>", lambda: self.zoom_key(0.7)), ("<Control-equal>", lambda: self.zoom_key(0.7)),
                        ("<Control-minus>", lambda: self.zoom_key(1 / 0.7))):
            self.bind_all(seq, ok_(fn))
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
        self.easy, self.manual, self.clipt, self.findt = (ttk.Frame(self.nb), ttk.Frame(self.nb), ttk.Frame(self.nb),
                                                          ttk.Frame(self.nb))
        self.nb.add(self.easy, text="   かんたん (1本に合わせる)   ")
        self.nb.add(self.clipt, text="   切り出し (長い映像から一部だけ)   ")
        self.nb.add(self.findt, text="   録画から探す (CMの日本語版を集める)   ")
        self.nb.add(self.manual, text="   手動調整 (細かく直す)   ")
        self._hdr.lift()
        top = ttk.Frame(self.manual, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="英語版の映像").grid(row=0, column=0, sticky="w")
        self.en_ent = ttk.Entry(top, textvariable=self.en_var)
        self.en_ent.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="参照", command=lambda: self._browse(self.en_var)).grid(row=0, column=2)
        ttk.Label(top, text="日本語の録画").grid(row=1, column=0, sticky="w")
        self.jp_ent = ttk.Entry(top, textvariable=self.jp_var)
        self.jp_ent.grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="参照", command=lambda: self._browse(self.jp_var)).grid(row=1, column=2)
        f = ttk.Frame(top)
        f.grid(row=1, column=3, padx=6)
        ttk.Label(f, text="日本語の音声番号").pack(side="left")
        ttk.Spinbox(f, from_=0, to=9, width=3, textvariable=self.jp_ast).pack(side="left")
        ttk.Button(top, text="読み込む", command=self.load_media).grid(row=0, column=3, rowspan=1, padx=6)
        ttk.Button(top, text="設定を開く", command=self.open_project).grid(row=0, column=4, padx=2)
        ttk.Button(top, text="設定を保存", command=self.save_project).grid(row=1, column=4, padx=2)
        top.columnconfigure(1, weight=1)

        tk.Label(self.manual, justify="left", anchor="w", bg="#fff8e1", fg="#444", font=("", 9), wraplength=1220,
                 text="① 上(英語)と下(日本語)で『同じ場面』を探して登録 → ② CM・暗転・カットがあれば、波形をドラッグして範囲を選び、"
                      "ボタンで指定。色つきの帯(A・B・C…)が自動でできます(同じ色=対応する区間 / 灰色の斜線=使わない部分)。"
                 ).pack(fill="x", padx=6, pady=(2, 0))
        hr = ttk.Frame(self.manual)
        hr.pack(fill="x", padx=6, pady=(2, 0))
        self.keys_lbl = tk.Label(self.manual, fg="#777", font=("", 8), justify="left", anchor="w", text="ドラッグ: 範囲を選ぶ / クリック: カーソルを置く / ホイール・↑↓キー: 拡大縮小 / "
                 "Shift+ホイール・右ドラッグ: 横にずらす / ←→: 1コマ移動 / Space: 再生 / Esc: 選択を解除\nCtrl+Z: 元に戻す / Ctrl+Y: やり直し / Ctrl+S: 設定を保存 / Ctrl+O: 設定を開く / Ctrl+A: 一覧を全選択 / Delete: 選択を削除 / Ctrl++ Ctrl+- Ctrl+0: 拡大・縮小・全体表示")
        def _toggle_keys(btn):
            if self.keys_lbl.winfo_ismapped():
                self.keys_lbl.pack_forget()
                btn.config(text="操作のしかた ▼")
            else:
                self.keys_lbl.pack(fill="x", padx=6, after=hr)
                btn.config(text="操作のしかた ▲")
        kb = ttk.Button(hr, text="操作のしかた ▼")
        kb.config(command=lambda: _toggle_keys(kb))
        kb.pack(side="left")
        ttk.Button(hr, text="全体 (Home)", width=11, command=self.fit_all).pack(side="right")
        ttk.Button(hr, text="縮小 ↓", width=7, command=lambda: self.zoom_key(1 / 0.7)).pack(side="right", padx=2)
        ttk.Button(hr, text="拡大 ↑", width=7, command=lambda: self.zoom_key(0.7)).pack(side="right")

        self.tr = {"en": Track(self.manual, self, "en", "英語版 (基準の映像)"),
                   "jp": Track(self.manual, self, "jp", "日本語の録画")}
        self.tr["en"].pack(fill="x", padx=6, pady=(2, 2))
        self.tr["jp"].pack(fill="x", padx=6, pady=(2, 2))

        mid = ttk.Frame(self.manual, padding=4)
        mid.pack(fill="x")
        self.pv, self.ent, self.fr_lbl = {}, {}, {}
        for k, name in (("en", "英語版 (カーソル位置の画面)"), ("jp", "日本語の録画 (カーソル位置の画面)")):
            fr = ttk.LabelFrame(mid, text=name)
            fr.grid(row=0, column=0 if k == "en" else 2, padx=4, sticky="n")
            lab = tk.Label(fr, width=20, height=3, bg="black", fg="#888", text="(プレビュー)")
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
        g1 = ttk.LabelFrame(ctr, text=" ① 同じ場面を登録する ")
        g1.pack(fill="x", pady=(0, 6))
        r1 = ttk.Frame(g1)
        r1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(r1, text="A 音で探して合わせる", width=20, command=self.auto_snap).pack(side="left")
        ttk.Button(r1, text="S この2つは『同じ場面』", width=22, command=self.add_point).pack(side="left", padx=6, ipady=3)
        self.refine_v = tk.BooleanVar(value=True)
        ttk.Checkbutton(g1, text="登録するとき、音で微調整する",
                        variable=self.refine_v).pack(anchor="w", padx=8, pady=(0, 4))
        g2 = ttk.LabelFrame(ctr, text=" ② 特別な部分 (先に波形をドラッグして範囲を選ぶ) ")
        g2.pack(fill="x")
        rw = ttk.Frame(g2)
        rw.pack(fill="x", padx=8, pady=(6, 2))
        for text, cmd in (("CM", self.add_cm), ("暗転・長さ違い", self.add_dim), ("カット", self.add_cut)):
            ttk.Button(rw, text=text, width=14, command=cmd).pack(side="left", padx=(0, 6))
        tk.Label(g2, text="CM=下の範囲は使わない / 暗転・長さ違い=同じ場面だが長さが違う / カット=上の範囲は日本語版でカット済み",
                 fg="#555", font=("", 8), anchor="w", justify="left", wraplength=560).pack(fill="x", padx=8)
        r3 = ttk.Frame(g2)
        r3.pack(fill="x", padx=8, pady=(2, 4))
        self.sel_lbl = tk.Label(r3, text="選択中の範囲: なし", fg="#2f6fd0", anchor="w", font=("", 9))
        self.sel_lbl.pack(side="left")
        ttk.Button(r3, text="選択を解除", command=self.clear_ranges).pack(side="right")
        wf = ttk.Frame(r1)
        wf.pack(side="left", padx=8)
        ttk.Label(wf, text="照合 秒/±秒").pack(side="left")
        self.win_v, self.srch_v = tk.StringVar(value="8"), tk.StringVar(value="60")
        ttk.Entry(wf, textvariable=self.win_v, width=4).pack(side="left", padx=(6, 2))
        ttk.Entry(wf, textvariable=self.srch_v, width=4).pack(side="left")

        # 指定の一覧
        lf = ttk.LabelFrame(self.manual, text=" 指定の一覧 (行をクリックすると、その場所に移動します) ")
        lf.pack(side="top", fill="both", expand=True, padx=6, pady=(4, 0))
        self.warn_lbl = tk.Label(lf, text="", anchor="w", justify="left", wraplength=1100, fg="#8a5a00")
        self.warn_lbl.pack(side="bottom", fill="x", padx=8, pady=(0, 4))  # 先に確保して、窓が低くても隠れないようにする
        lf.bind("<Configure>", lambda e: self.warn_lbl.config(wraplength=max(300, e.width - 40)))
        mid2 = ttk.Frame(lf)
        mid2.pack(fill="both", expand=True, padx=4, pady=4)
        cols = ("kind", "en", "jp", "note")
        self.itree = ttk.Treeview(mid2, columns=cols, show="headings", height=5, selectmode="extended")
        for c, h, w in zip(cols, ("種類", "英語版", "日本語の録画", "内容"), (110, 190, 190, 560)):
            self.itree.heading(c, text=h)
            self.itree.column(c, width=w, anchor="w")
        for kind, bg in (("pt", "#e6f6ec"), ("cm", "#e6e8ec"), ("dim", "#efe8ff"), ("cut", "#ffecd2")):
            self.itree.tag_configure(kind, background=bg)
        self.itree.pack(side="left", fill="both", expand=True)
        vscroll(self.itree)
        RangeSelect(self.itree)
        self.itree.bind("<<TreeviewSelect>>", self.on_item_select)
        self.itree.bind("<Double-1>", lambda e: self.edit_item())
        self.itree.bind("<Delete>", lambda e: self.del_item())
        bt = ttk.Frame(mid2)
        bt.pack(side="left", padx=6, anchor="n")
        ttk.Button(bt, text="編集", width=8, command=self.edit_item).pack(pady=2)
        ttk.Button(bt, text="削除", width=8, command=self.del_item).pack(pady=2)
        ttk.Button(bt, text="全部削除", width=8, command=self.clear_items).pack(pady=2)

        self.log = tk.Text(self.manual, height=1, bg="#111", fg="#cfc", font=("Consolas", 9))
        self.log.pack(side="bottom", fill="x", padx=6, pady=(0, 2))
        bot = ttk.Frame(self.manual, padding=4)
        bot.pack(side="bottom", fill="x")
        ttk.Label(bot, text="保存先").pack(side="left")
        ttk.Entry(bot, textvariable=self.out_path).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(bot, text="変更", command=self.pick_out).pack(side="left")
        self.render_btn = ttk.Button(bot, text="合成スタート", command=self.render)
        self.render_btn.pack(side="left", padx=6)
        lf.pack_forget()  # 一覧を最後に詰め直す = 窓が低いときは一覧のほうが縮み、保存先・合成ボタンは隠れない
        lf.pack(side="top", fill="both", expand=True, padx=6, pady=(4, 0))
        self._build_easy()
        self._build_clip()
        self._build_find()
        attach_tips(self, extra=[
            (self.clip_tree, "切り出したい日本語のファイルの一覧です。ここにファイルをドロップして追加できます。"),
            (self.clip_en_ent, "元になる、長い(まとまった)英語版の映像ファイルです。"),
            (self.clip_codec_cb, "「自動」は、使えれば HEVC(NVIDIA → x265)、使えなければ H.264 で書き出します。"
                                 "古い機器やプレーヤーで再生したいときは H.264 を選びます。"),
            (self.en_ent, "英語版(きれいな映像)のファイルの場所です。"),
            (self.jp_ent, "日本語吹き替えの音声が入った録画ファイルの場所です。"),
            (self.itree, "登録した指定の一覧です。行をクリックすると、その場所に移動します。"),
            (self.etree, "自動で見つけた区間の一覧です。「要確認」の行は特に確かめてください。"),
        ] + [(w, t) for k, t in (("en", "きれいな英語版の映像ファイルをここにドロップ、またはクリックして選びます。"),
                                  ("jp", "日本語吹き替えが入った録画ファイルをここにドロップ、またはクリックして選びます。"))
             for w in (self.zone[k]["frame"], self.zone[k]["title"], self.zone[k]["name"])])

    # ---------- ヘッダー(更新) / かんたんタブ ----------
    def _build_header(self):
        self.status = ttk.Label(self, text="", anchor="w", foreground="#555")
        self.status.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        h = ttk.Frame(self)
        h.place(relx=1.0, x=-6, y=2, anchor="ne")  # タブと同じ行の右端に重ねる (縦の余白を作らない)
        self._hdr = h
        ttk.Label(h, text=ver_label(), foreground="#c0392b" if BETA_REVIEW else "#888").pack(side="right", padx=(8, 0))
        self.auto_upd = tk.BooleanVar(value=self._setting("auto_update", True))
        mb = ttk.Menubutton(h, text="更新・設定 ▾")
        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="更新を確認", command=lambda: self.check_update(True))
        m.add_command(label="更新前の版に戻す", command=self.rollback)
        m.add_separator()
        m.add_checkbutton(label="起動時に更新を確認", variable=self.auto_upd, command=self._save_auto)
        mb["menu"] = m
        mb.pack(side="right", padx=2)
        self.upd_menu = mb
        Tooltip(mb, "新しい版の確認、更新前の版に戻す、起動時の自動確認のオン/オフを選べます。")

    def _build_easy(self):
        f = self.easy
        pad = {"padx": 12, "pady": 5}
        tk.Label(f, justify="left", anchor="w", fg="#333", wraplength=1050, font=("", 10),
                 text="日本語吹き替えの音声を、きれいな英語版の映像に合わせて1本の動画にします。\n"
                      "手順:  ① ファイルを選ぶ  →  ② 自動で合わせる  →  ③ 結果を確認  →  ④ 合成する\n"
                      "(英語版が長い1本で、日本語がCMや番組の一部など短いときは「切り出し」タブ、日本語の録画が大量にあって探したいときは「録画から探す」タブ、\n うまく合わないときは「手動調整」タブを使います)"
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
        ast = ttk.Frame(o)
        def _tg():
            if ast.winfo_ismapped():
                ast.pack_forget(); tgb.config(text="詳細設定 ▶")
            else:
                ast.pack(side="left", padx=8); tgb.config(text="詳細設定 ▼")
        tgb = ttk.Button(o, text="詳細設定 ▶", command=_tg)
        tgb.pack(side="left")
        Tooltip(tgb, "ふだんは触らなくて大丈夫です。日本語の録画に音声が複数あるときの番号指定を開きます。")
        ttk.Label(ast, text="日本語の録画に音声が複数あるときだけ、使う音声の番号を変えます (通常は 0):").pack(side="left")
        ttk.Spinbox(ast, from_=0, to=9, width=3, textvariable=self.jp_ast).pack(side="left", padx=6)
        self.easy_video = tk.BooleanVar(value=False)
        cbv = ttk.Checkbutton(ast, text="映像でも合わせる(時間はかかるが、より確実)", variable=self.easy_video)
        cbv.pack(side="left", padx=(16, 0))
        Tooltip(cbv, "音で合わせたあと、画面の似かたでも合わせて、場所ごとに確かなほうを採ります。\n"
                     "音が違いすぎてうまく合わない動画や、しっかり確かめたいときに入れてください。時間は長めにかかります。\n"
                     "入れたあと、もう一度「自動で合わせる」を押してください。")
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
        self.auto_msg = tk.Label(c2, text="ファイルを2つ選んだら、「自動で合わせる」を押してください。", justify="left", anchor="w",
                                 wraplength=1050, fg="#333")
        self.auto_msg.pack(fill="x", padx=12, pady=(2, 8))

        # ③ 結果を確認
        c3 = ttk.LabelFrame(f, text=" ③ 結果を確認 ")
        c3.pack(fill="both", expand=True, **pad)
        r3 = ttk.Frame(c3)
        r3.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("n", "en", "jp", "st")
        self.etree = ttk.Treeview(r3, columns=cols, show="headings", height=5, selectmode="browse")
        for c, h, w in zip(cols, ("区間", "英語版の時間", "日本語録画の時間", "状態"), (50, 220, 220, 330)):
            self.etree.heading(c, text=h)
            self.etree.column(c, width=w, anchor="w")
        self.etree.tag_configure("warn", background="#ffe9c7")
        self.etree.pack(side="left", fill="both", expand=True)
        vscroll(self.etree)
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
        self.rbar = ttk.Progressbar(r5, mode="determinate", length=160)
        self.rbar.pack(side="left")
        self.easy_done = tk.Label(r5, text="", anchor="w", fg="#2a7")
        self.easy_done.pack(side="left", padx=10)
        self.open_btn = ttk.Button(r5, text="フォルダを開く", command=self.open_folder)
        self.beta_btn = ttk.Button(r5, text="自動チェックの結果", command=self._beta_show)   # ベータ版だけ。合成のあとに出す

    # ---------- 切り出し (一致部分だけ) ----------
    def _build_clip(self):
        f = self.clipt
        self.clip_en = self.en_var  # 英語版の映像は、どのタブでも共通
        self.clip_out = tk.StringVar()
        self.clip_crf = tk.IntVar(value=18)
        self.clip_codec = tk.StringVar(value="自動 (HEVC優先)")
        self.clip_files = []
        self.en_var.trace_add("write", lambda *a: (not self.clip_out.get().strip() and self.en_var.get().strip()
                                                    and self.clip_out.set(str(Path(self.en_var.get().strip()).parent))))
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)
        pad = {"padx": 12, "pady": 5}
        tk.Label(f, justify="left", anchor="w", fg="#333", wraplength=1050, font=("", 10),
                 text="英語版が長い1本で、日本語の音声が短いファイル(CMや番組の一部など)のときに使います。\n"
                      "日本語のファイルを1本ずつ自動で合わせ、一致した部分だけを英語版から切り出して、日本語の音声をつけて書き出します。\n"
                      "この切り出しだけは映像を再エンコードします(フレーム単位で切れます)。"
                      "「かんたん」タブの、映像をそのままコピーする合成は今までどおりです。").pack(fill="x", **pad)
        c1 = ttk.LabelFrame(f, text=" ① 英語版の映像 (元になる、長いもの) ")
        c1.pack(fill="x", **pad)
        r1 = ttk.Frame(c1)
        r1.pack(fill="x", padx=8, pady=8)
        self.clip_en_ent = ttk.Entry(r1, textvariable=self.clip_en)
        self.clip_en_ent.pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="参照", command=self._clip_pick_en).pack(side="left", padx=6)
        c2 = ttk.LabelFrame(f, text=" ② 日本語のファイル (短いもの。複数OK。ここにドロップ、または追加) ")
        r2 = ttk.Frame(c2)
        r2.pack(fill="both", expand=True, padx=8, pady=6)
        self.clip_tree = ttk.Treeview(r2, columns=("f", "st"), show="headings", height=6, selectmode="extended")
        for c, h, w in (("f", "ファイル", 520), ("st", "状態", 520)):
            self.clip_tree.heading(c, text=h)
            self.clip_tree.column(c, width=w, anchor="w")
        self.clip_tree.tag_configure("ok", background="#e6f6ec")
        self.clip_tree.tag_configure("warn", background="#ffe9c7")
        self.clip_tree.tag_configure("ng", background="#ffe0e0")
        self.clip_tree.pack(side="left", fill="both", expand=True)
        vscroll(self.clip_tree)
        enable_multiselect(self.clip_tree)
        bt = ttk.Frame(r2)
        bt.pack(side="left", padx=8, anchor="n")
        ttk.Button(bt, text="追加...", width=10, command=self._clip_add).pack(pady=2)
        ttk.Button(bt, text="外す", width=10, command=self._clip_remove).pack(pady=2)
        ttk.Button(bt, text="全部外す", width=10, command=self._clip_clear).pack(pady=2)
        c3 = ttk.LabelFrame(f, text=" ③ 書き出し ")
        r3 = ttk.Frame(c3)
        r3.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(r3, text="保存先フォルダ:").pack(side="left")
        ttk.Entry(r3, textvariable=self.clip_out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r3, text="変更", command=self._clip_pick_out).pack(side="left")
        r4 = ttk.Frame(c3)
        r4.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(r4, text="画質 (数字が小さいほど高画質・大きいファイル。通常は 18):").pack(side="left")
        ttk.Spinbox(r4, from_=12, to=28, width=4, textvariable=self.clip_crf).pack(side="left", padx=6)
        ttk.Label(r4, text="映像の形式:").pack(side="left", padx=(18, 4))
        self.clip_codec_cb = ttk.Combobox(r4, textvariable=self.clip_codec, state="readonly", width=20,
                                          values=("自動 (HEVC優先)", "H.264 (どの機器でも再生しやすい)"))
        self.clip_codec_cb.pack(side="left")
        ttk.Label(r4, text="ファイル名は「日本語ファイル名_jpdub.mkv」になります。", foreground="#667").pack(side="left", padx=12)
        r5 = ttk.Frame(f)
        self.clip_btn = ttk.Button(r5, text="切り出しスタート", command=self.run_clip)
        self.clip_btn.pack(side="left", ipadx=14, ipady=4)
        self.clip_bar = ttk.Progressbar(r5, mode="determinate", maximum=100)
        self.clip_bar.pack(side="left", fill="x", expand=True, padx=12)
        self.clip_msg = tk.Label(f, text="", anchor="w", justify="left", fg="#333", wraplength=1050)
        self.clip_open = ttk.Button(r5, text="フォルダを開く", command=lambda: self._open_dir(self.clip_out.get()))
        # 下の部品を先に確保してから、一覧を残りの場所に広げる(窓が低くても、スタートボタンが隠れない)
        self.clip_msg.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        r5.pack(side="bottom", fill="x", **pad)
        c3.pack(side="bottom", fill="x", **pad)
        c2.pack(side="top", fill="both", expand=True, **pad)

    # ---------- 録画から探す ----------
    VIDEO_EXT = (".ts", ".m2ts", ".mts", ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".mpg", ".mpeg", ".m4v", ".flv", ".webm")

    def _build_find(self):
        f = self.findt
        self.find_out = tk.StringVar()
        self.find_crf = tk.IntVar(value=18)
        self.find_codec = tk.StringVar(value="自動 (HEVC優先)")
        self.find_vscan = tk.BooleanVar(value=True)
        self.find_group = tk.BooleanVar(value=True)
        self.find_files = []
        self.find_en_extra = []   # 2本目以降の英語版(1本目は、ほかのタブと共通の en_var)
        self.cm_map = {}          # 英語版のファイル → {"mt": 更新時刻, "cms": 切り分けたCMの一覧}
        self._cm_load()           # 前回までに切り分けた一覧(アプリを閉じても残す)
        self._cm_view = ""        # CMの一覧に出している英語版
        self.en_var.trace_add("write", lambda *a: self._find_en_changed())
        self._find_stop_ev = threading.Event()
        self.en_var.trace_add("write", lambda *a: (not self.find_out.get().strip() and self.en_var.get().strip()
                                                    and self.find_out.set(str(Path(self.en_var.get().strip()).parent))))
        pad = {"padx": 12, "pady": 5}
        tk.Label(f, justify="left", anchor="w", fg="#333", wraplength=1050, font=("", 10),
                 text="英語版のCM(1本につながったものでもOK)と同じ音を、日本語のテレビ録画の中から探して、日本語音声つきで書き出します。\n"
                      "同じCMが何回流れていても全部書き出します。ファイル名は「CM01_30秒_英語版名(録画名).mkv」の形です(秒数は5秒きざみ)。\n"
                      "書き出しは映像を再エンコードします(CMの切れ目をフレーム単位で合わせるため)。").pack(fill="x", **pad)
        c1 = ttk.LabelFrame(f, text=" ① 英語版の映像 (複数まとめて探せます) → CMを切り分ける ")
        c1.pack(fill="x", **pad)
        self.find_en_frame = c1   # ここへのドロップは、英語版として受ける
        r1 = ttk.Frame(c1)
        r1.pack(fill="x", padx=8, pady=(8, 4))
        self.en2_tree = ttk.Treeview(r1, columns=("f",), show="headings", height=3, selectmode="extended")
        self.en2_tree.heading("f", text="英語版の映像 (複数OK。ここにドロップ、または追加)")
        self.en2_tree.column("f", anchor="w")
        self.en2_tree.pack(side="left", fill="x", expand=True)
        vscroll(self.en2_tree)
        self.en2_tree.bind("<<TreeviewSelect>>", lambda e: self._en2_select())
        enable_multiselect(self.en2_tree, lambda: self._en2_remove())
        bx = ttk.Frame(r1)
        bx.pack(side="left", padx=8, anchor="n")
        ttk.Button(bx, text="追加...", width=14, command=self._en2_add).pack(pady=1)
        ttk.Button(bx, text="外す", width=14, command=self._en2_remove).pack(pady=1)
        self.find_split_btn = ttk.Button(bx, text="CMを切り分ける", width=14, command=self.run_split)
        self.find_split_btn.pack(pady=1)
        r1b = ttk.Frame(c1)
        r1b.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.cm_tree = ttk.Treeview(r1b, columns=("on", "n", "t", "len", "sec"), show="headings", height=6, selectmode="extended")
        self.cm_tree.tag_configure("off", foreground="#aaa")
        self.cm_tree.bind("<Button-1>", self._cm_click)
        for k_ in ("<space>", "<Return>", "<KP_Enter>"):   # スペース / Enter で、選んでいるCMのチェックを切り替え
            self.cm_tree.bind(k_, lambda e: (self._cm_toggle(), "break")[1])
        for c, h, w in (("on", "探す", 50), ("n", "CM", 70), ("t", "英語版の時間", 190), ("len", "長さ", 90), ("sec", "名前に付く秒数", 120)):
            self.cm_tree.heading(c, text=h)
            self.cm_tree.column(c, width=w, anchor="center")
        self.cm_tree.pack(side="left", fill="both", expand=True)
        vscroll(self.cm_tree)
        enable_multiselect(self.cm_tree, lambda: self._cm_toggle())
        self.cm_thumb_frame = ttk.Frame(r1b)   # 選んだCMの代表的な画面(3枚)
        self.cm_thumb_lbl = []
        for _i in range(3):
            fr_ = ttk.Frame(self.cm_thumb_frame)
            fr_.pack(side="left", padx=(0, 6))
            im_ = ttk.Label(fr_, text="", anchor="center")
            im_.pack()
            tx_ = ttk.Label(fr_, text="", foreground="#667")
            tx_.pack()
            self.cm_thumb_lbl.append((im_, tx_))
        b1 = ttk.Frame(r1b)
        b1.pack(side="right", padx=8, anchor="n", before=self.cm_tree)
        self.cm_thumb_frame.pack(side="right", padx=(8, 0), anchor="n", before=self.cm_tree)
        ttk.Button(b1, text="外す/戻す", width=12, command=self._cm_toggle).pack(pady=2)
        ttk.Button(b1, text="全部戻す", width=12, command=lambda: self._cm_all(True)).pack(pady=2)
        ttk.Button(b1, text="全部外す", width=12, command=lambda: self._cm_all(False)).pack(pady=2)
        ttk.Button(b1, text="一覧を保存...", width=12, command=self._cm_export).pack(pady=2)
        ttk.Button(b1, text="一覧を読み込む...", width=12, command=self._cm_import).pack(pady=2)
        self._cm_thumb_tok = 0
        self._cm_thumb_cache = {}
        self.cm_tree.bind("<<TreeviewSelect>>", lambda e: self._cm_thumbs_show(), add="+")
        self.cm_note = ttk.Label(c1, text=self.CM_NOTE0, foreground="#667")
        self.cm_note.pack(anchor="w", padx=10, pady=(0, 6))

        c2 = ttk.LabelFrame(f, text=" ② 日本語の録画 (複数OK。ファイルやフォルダをここにドロップ、または追加) ")
        r2 = ttk.Frame(c2)
        r2.pack(fill="both", expand=True, padx=8, pady=6)
        self.find_tree = ttk.Treeview(r2, columns=("f", "st"), show="headings", height=5, selectmode="extended")
        for c, h, w in (("f", "録画ファイル", 520), ("st", "状態", 520)):
            self.find_tree.heading(c, text=h)
            self.find_tree.column(c, width=w, anchor="w")
        for tg, bg in (("ok", "#e6f6ec"), ("warn", "#ffe9c7"), ("ng", "#ffe0e0")):
            self.find_tree.tag_configure(tg, background=bg)
        self.find_tree.pack(side="left", fill="both", expand=True)
        vscroll(self.find_tree)
        enable_multiselect(self.find_tree, lambda: self._find_remove())
        bt = ttk.Frame(r2)
        bt.pack(side="left", padx=8, anchor="n")
        ttk.Button(bt, text="追加...", width=10, command=self._find_add).pack(pady=2)
        ttk.Button(bt, text="フォルダ...", width=10, command=self._find_add_dir).pack(pady=2)
        ttk.Button(bt, text="外す", width=10, command=self._find_remove).pack(pady=2)
        ttk.Button(bt, text="全部外す", width=10, command=self._find_clear).pack(pady=2)

        c3 = ttk.LabelFrame(f, text=" ③ 書き出し ")
        r3 = ttk.Frame(c3)
        r3.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(r3, text="保存先フォルダ:").pack(side="left")
        ttk.Entry(r3, textvariable=self.find_out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r3, text="変更", command=lambda: (lambda d: d and self.find_out.set(d))(filedialog.askdirectory(title="保存先フォルダを選択"))).pack(side="left")
        r4 = ttk.Frame(c3)
        r4.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(r4, text="画質 (通常は 18):").pack(side="left")
        ttk.Spinbox(r4, from_=12, to=28, width=4, textvariable=self.find_crf).pack(side="left", padx=6)
        ttk.Label(r4, text="映像の形式:").pack(side="left", padx=(18, 4))
        self.find_codec_cb = ttk.Combobox(r4, textvariable=self.find_codec, state="readonly", width=20,
                                          values=("自動 (HEVC優先)", "H.264 (どの機器でも再生しやすい)"))
        self.find_codec_cb.pack(side="left")
        vs = ttk.Checkbutton(r4, text="音が合わない短縮版も映像で探す(遅い)", variable=self.find_vscan)
        vs.pack(side="left", padx=(18, 0))
        gp = ttk.Checkbutton(r4, text="同じCMはCMごとのフォルダにまとめる", variable=self.find_group)
        gp.pack(side="left", padx=(18, 0))
        Tooltip(gp, "書き出し先の中に「30秒_英語版名_CM05」のようなフォルダを作って、同じCMが見つかった分(別の録画・短縮版・何回目か)を、その中に入れます。")
        Tooltip(vs, "日本語版の音が英語版と違っても、画面が同じなら短縮版・途中で切れた版として見つけます。録画1本あたり数分長くかかります。")
        r5 = ttk.Frame(f)
        self.find_btn = ttk.Button(r5, text="探してスタート", command=self.run_find)
        self.find_btn.pack(side="left", ipadx=14, ipady=4)
        self.find_stop = ttk.Button(r5, text="中止", command=self._find_cancel)
        self.find_stop.pack(side="left", padx=(8, 0), ipady=4)
        self.find_stop.state(["disabled"])
        self.find_bar = ttk.Progressbar(r5, mode="determinate", maximum=100)
        self.find_bar.pack(side="left", fill="x", expand=True, padx=12)
        self.find_msg = tk.Label(f, text="", anchor="w", justify="left", fg="#333", wraplength=1050)
        self.find_open = ttk.Button(r5, text="フォルダを開く", command=lambda: self._open_dir(self.find_out.get()))
        self.find_results = []
        self.find_review_btn = ttk.Button(r5, text="確認ウインドウ", command=lambda: self._open_review())
        self.find_msg.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        r5.pack(side="bottom", fill="x", **pad)
        c3.pack(side="bottom", fill="x", **pad)
        c1.pack_forget()
        c1.pack(side="top", fill="x", **pad)
        c2.pack(side="top", fill="both", expand=True, **pad)

    def _en_all(self):
        """英語版の一覧(1本目は、ほかのタブと共通の欄)。"""
        p = self.en_var.get().strip()
        return ([p] if p else []) + [x for x in self.find_en_extra if x and x != p]

    def _cms_of(self, p):
        """切り分け済みのCM一覧。英語版のファイルが変わっていたら、古い一覧は使わない。"""
        e = self.cm_map.get(p)
        try:
            if e and e["mt"] == int(Path(p).stat().st_mtime):
                return e["cms"]
        except OSError:
            pass
        return []

    CM_FILE = Path(__file__).resolve().parent / "syncdub_CMリスト.json"

    def _cm_load(self):
        try:
            d = json.loads(self.CM_FILE.read_text(encoding="utf-8"))
            for p, e in d.items():
                self.cm_map[p] = {"mt": int(e["mt"]), "cms": [(float(a), float(b)) for a, b in e["cms"]], "off": set(e.get("off", []))}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _cm_save(self):
        try:
            self.CM_FILE.write_text(json.dumps({p: {"mt": e["mt"], "cms": e["cms"], "off": sorted(e["off"])}
                                                for p, e in self.cm_map.items()}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _cm_store(self, p, cms, off=None):
        """切り分けた一覧を覚える。off: 使わない(チェックを外した)CMの番号の集合。番号は、外しても振り直さない。"""
        try:
            self.cm_map[p] = {"mt": int(Path(p).stat().st_mtime), "cms": list(cms), "off": set(off or ())}
        except OSError:
            return
        self._cm_save()

    def _cm_off(self, p):
        self._cms_of(p)
        e = self.cm_map.get(p)
        return set(e.get("off", ())) if e and self._cms_of(p) else set()

    def _en2_fill(self):
        al = self._en_all()
        if self._cm_view not in al:
            self._cm_view = al[0] if al else ""
        self.en2_tree.delete(*self.en2_tree.get_children())
        for i, x in enumerate(al):
            self.en2_tree.insert("", "end", iid=str(i), values=(x,))
            if x == self._cm_view:
                self.en2_tree.selection_set(str(i))
        self._cm_fill()

    def _en2_select(self):
        al = self._en_all()
        sel = [int(i) for i in self.en2_tree.selection()]
        if sel and sel[0] < len(al) and al[sel[0]] != self._cm_view:
            self._cm_view = al[sel[0]]
            self._cm_fill()
            self.cm_note.config(text=f"選んだ英語版のCMの一覧です。{self.CM_NOTE0}" if not self._cms_of(self._cm_view) else
                                f"選んだ英語版のCMの一覧です({len(self._cms_of(self._cm_view))} 本)。探さないCMは ☑ を外します。", foreground="#667")

    def _en2_add(self):
        for x in filedialog.askopenfilenames(title="英語版の映像を選択 (複数選べます)", filetypes=MEDIA_TYPES):
            self._en2_put(x)
        self._en2_fill()

    def _en2_put(self, x):
        if x in self._en_all():
            return
        if not self.en_var.get().strip():
            self.en_var.set(x)
        else:
            self.find_en_extra.append(x)

    def _en2_remove(self):
        sel = {int(i) for i in self.en2_tree.selection()}
        keep = [x for i, x in enumerate(self._en_all()) if i not in sel]
        self.en_var.set(keep[0] if keep else "")
        self.find_en_extra = keep[1:]
        self._en2_fill()

    def _open_review(self):
        """ベータ版用: 書き出したものの確認ウインドウを開く。"""
        if not self.find_results:
            return
        w = getattr(self, "_review_win", None)
        try:
            if w is not None and w.winfo_exists():
                w.destroy()
        except tk.TclError:
            pass
        self._review_win = ReviewWindow(self, self.find_results, self.find_out.get())

    def _find_cancel(self):
        self._find_stop_ev.set()
        self.find_stop.state(["disabled"])
        self.find_msg.config(text="中止しています…(ffmpegを止めて、書きかけのファイルを消しています)", fg="#8a5a00")
        threading.Thread(target=cancel_all, daemon=True).start()

    def _cm_fill(self):
        sel0, foc0, y0 = self.cm_tree.selection(), self.cm_tree.focus(), self.cm_tree.yview()[0]   # 作り直しても、選択・位置・スクロールを保つ
        self.cm_tree.delete(*self.cm_tree.get_children())
        off = self._cm_off(self._cm_view)
        for i, (a, b) in enumerate(self._cms_of(self._cm_view)):
            self.cm_tree.insert("", "end", iid=str(i), tags=("off",) if i in off else (),
                                values=("☐" if i in off else "☑", f"CM{i + 1:0{max(2, len(str(len(self._cms_of(self._cm_view)))))}d}", f"{self.short(a)} 〜 {self.short(b)}",
                                        f"{b - a:.1f} 秒", f"{round5(b - a)} 秒"))
        keep_ = [i for i in sel0 if self.cm_tree.exists(i)]
        if keep_:
            self.cm_tree.selection_set(keep_)
        if foc0 and self.cm_tree.exists(foc0):
            self.cm_tree.focus(foc0)
        self.cm_tree.yview_moveto(y0)

    CM_THUMB_W = 130

    def _cm_thumbs_show(self):
        """選んだCMの画面を3枚並べる: 頭から3秒後(固定)と、そのあと終わりまで均等に。"""
        sel = [int(i) for i in self.cm_tree.selection()]
        p, cms = self._cm_view, self._cms_of(self._cm_view)
        self._cm_thumb_tok += 1
        tok = self._cm_thumb_tok
        if not sel or not p or sel[0] >= len(cms):
            for im_, tx_ in self.cm_thumb_lbl:
                im_.config(image="", text="")
                im_._img = None
                tx_.config(text="")
            return
        a, b = cms[sel[0]]
        L = b - a
        t0 = min(3.0, max(0.0, L * 0.25))
        t1 = max(t0, L - 0.5)
        ts = [a + t0 + (t1 - t0) * i / 2 for i in range(3)]
        for im_, tx_ in self.cm_thumb_lbl:
            im_.config(image="", text="…")
            im_._img = None
        W = self.CM_THUMB_W

        def work():
            try:
                voff = video_start_offset(probe(p))
            except Exception:  # noqa: BLE001
                voff = 0.0
            for i, t in enumerate(ts):
                if tok != self._cm_thumb_tok:
                    return
                key = (p, round(t, 2), W)
                png = self._cm_thumb_cache.get(key)
                if png is None:
                    try:
                        r = _subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t + voff):.3f}", "-i", p, "-frames:v", "1",
                                             "-vf", f"scale={W}:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                                            capture_output=True, **NOWIN)
                        png = r.stdout or b""
                    except Exception:  # noqa: BLE001
                        png = b""
                    if png:
                        if len(self._cm_thumb_cache) > 300:
                            self._cm_thumb_cache.clear()
                        self._cm_thumb_cache[key] = png

                def show(i=i, t=t, png=png):
                    if tok != self._cm_thumb_tok:
                        return
                    im_, tx_ = self.cm_thumb_lbl[i]
                    try:
                        img = tk.PhotoImage(data=base64.b64encode(png)) if png else None
                    except tk.TclError:
                        img = None
                    im_._img = img
                    im_.config(image=img if img else "", text="" if img else "(画面を取れませんでした)")
                    tx_.config(text=f"{self.short(t)} (+{t - a:.1f}秒)")
                self.q.put(show)
        threading.Thread(target=work, daemon=True).start()

    def _cm_click(self, ev):
        """「使う」の列をクリックしたら、その行のチェックを切り替える。"""
        if self.cm_tree.identify_region(ev.x, ev.y) == "cell" and self.cm_tree.identify_column(ev.x) == "#1":
            row = self.cm_tree.identify_row(ev.y)
            if row:
                self._cm_toggle([int(row)])
                return "break"

    def _cm_toggle(self, idx=None):
        """選んだCMのチェックを切り替える(1つでもチェックがあれば全部外し、全部外れていれば全部入れる)。"""
        if idx is None:
            idx = [int(i) for i in self.cm_tree.selection()]
        p = self._cm_view
        if not idx or not p:
            return
        off = self._cm_off(p)
        if all(i in off for i in idx):
            off -= set(idx)
        else:
            off |= set(idx)
        self._cm_store(p, self._cms_of(p), off)
        self._cm_fill()
        self._cm_note_count()

    def _cm_all(self, on):
        p = self._cm_view
        if p and self._cms_of(p):
            self._cm_store(p, self._cms_of(p), set() if on else set(range(len(self._cms_of(p)))))
            self._cm_fill()
            self._cm_note_count()

    def _cm_note_count(self):
        n = len(self._cms_of(self._cm_view))
        if n:
            u = n - len(self._cm_off(self._cm_view))
            self.cm_note.config(text=f"{n} 本に分けました。探すのは ☑ の {u} 本です(番号は変わりません)。違う所は ☑ をクリックして外せます。", foreground="#1f6f43")

    @staticmethod
    def _parse_t(x):
        x = x.strip()
        if ":" in x:
            v = 0.0
            for q in x.split(":"):
                v = v * 60 + float(q)
            return v
        return float(x)

    def _cm_export(self):
        p, cms = self._cm_view, self._cms_of(self._cm_view)
        if not p or not cms:
            messagebox.showinfo("CMの一覧", "書き出せるCMの一覧がありません。先に「CMを切り分ける」を押してください。")
            return
        f = filedialog.asksaveasfilename(title="CMの一覧を保存", defaultextension=".txt", initialfile=f"{Path(p).stem}_CMリスト.txt",
                                         filetypes=[("テキスト", "*.txt")])
        if not f:
            return
        off = self._cm_off(p)
        lines = ["# syncdub CMリスト (タブ区切り: 番号 / 開始秒 / 終了秒 / 使う=1・使わない=0)", f"# 英語版: {Path(p).name}"]
        lines += [f"{i + 1}\t{a:.3f}\t{b:.3f}\t{0 if i in off else 1}" for i, (a, b) in enumerate(cms)]
        Path(f).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.cm_note.config(text=f"一覧を保存しました: {f}", foreground="#1f6f43")

    def _cm_import(self):
        p = self._cm_view
        if not p:
            messagebox.showinfo("CMの一覧", "先に英語版の映像を追加して、選んでください。")
            return
        f = filedialog.askopenfilename(title="CMの一覧を読み込む", filetypes=[("テキスト", "*.txt *.tsv *.csv"), ("すべて", "*.*")])
        if not f:
            return
        cms, off = [], set()
        try:
            for ln in Path(f).read_text(encoding="utf-8-sig").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                c = [x for x in re.split(r"[\t,]", ln)]
                a, b = self._parse_t(c[1]), self._parse_t(c[2])
                if b <= a:
                    raise ValueError(f"終わりが開始より前です: {ln}")
                if len(c) > 3 and c[3].strip() in ("0", "×", "☐"):
                    off.add(len(cms))
                cms.append((a, b))
        except Exception as ex:  # noqa: BLE001
            messagebox.showwarning("CMの一覧", f"読み込めませんでした: {ex}")
            return
        if not cms:
            messagebox.showwarning("CMの一覧", "CMが1本も読み込めませんでした。")
            return
        self._cm_store(p, cms, off)
        self._cm_fill()
        self._cm_note_count()

    CM_NOTE0 = "「CMを切り分ける」を押すと一覧が出ます(押さずにスタートしても自動で切り分けます)。探さないCMは ☑ を外します。"

    def _find_en_changed(self):
        try:
            self._en2_fill()
        except (AttributeError, tk.TclError):   # 画面を作っている途中は何もしない
            pass

    def run_split(self):
        al = self._en_all()
        en_p = self._cm_view if self._cm_view in al else (al[0] if al else "")
        if not en_p or not Path(en_p).exists():
            messagebox.showinfo("英語版の映像", "英語版の映像を追加して、切り分けたいものを選んでください。")
            return
        self._cm_view = en_p
        self.find_split_btn.state(["disabled"])
        self.cm_note.config(text="CMを探しています…", foreground="#333")

        def work():
            try:
                cms = split_commercials(en_p)
            except Exception as ex:  # noqa: BLE001
                self.q.put(lambda ex=ex: (self.cm_note.config(text=f"⚠ 調べられませんでした: {ex}", foreground="#b33"),
                                    self.find_split_btn.state(["!disabled"])))
                return

            def done():
                self._cm_store(en_p, cms)
                self._cm_view = en_p
                self._cm_fill()
                self.find_split_btn.state(["!disabled"])
                if cms:
                    self._cm_note_count()
                else:
                    self.cm_note.config(text="黒い画面で区切られた部分が見つかりませんでした。映像全体を1本として探します。", foreground="#8a5a00")
            self.q.put(done)
        threading.Thread(target=work, daemon=True).start()

    def _find_add_files(self, files):
        for p in files:
            pp = Path(p)
            cand = sorted(x for x in pp.rglob("*") if x.suffix.lower() in self.VIDEO_EXT) if pp.is_dir() else [pp]
            for x in cand:
                x = str(x)
                if x not in self.find_files and not x.lower().endswith(".json"):
                    self.find_files.append(x)
                    self.find_tree.insert("", "end", iid=str(len(self.find_files) - 1), values=(Path(x).name, "待機中"))

    def _find_add(self):
        self._find_add_files(list(filedialog.askopenfilenames(title="日本語の録画を選択 (複数選べます)", filetypes=MEDIA_TYPES)))

    def _find_add_dir(self):
        d = filedialog.askdirectory(title="録画が入っているフォルダを選択 (中のフォルダも探します)")
        if d:
            self._find_add_files([d])

    def _find_remove(self):
        sel = set(self.find_tree.selection())
        if not sel:
            return
        keep = [p for i, p in enumerate(self.find_files) if str(i) not in sel]
        self._find_clear()
        self._find_add_files(keep)

    def _find_clear(self):
        self.find_files = []
        self.find_tree.delete(*self.find_tree.get_children())

    def _find_set(self, i, text, tag=""):
        iid = str(i)
        if self.find_tree.exists(iid):
            self.find_tree.set(iid, "st", text)
            self.find_tree.item(iid, tags=(tag,) if tag else ())

    def run_find(self):
        en_all = [x for x in self._en_all() if Path(x).exists()]
        en_p, files = (en_all[0] if en_all else ""), list(self.find_files)
        if not en_p:
            messagebox.showinfo("英語版の映像", "英語版の映像を選んでください。")
            return
        if not files:
            messagebox.showinfo("日本語の録画", "日本語の録画を追加してください。")
            return
        outd = Path(self.find_out.get().strip() or Path(en_p).parent)
        self.find_out.set(str(outd))
        try:
            crf = max(0, min(51, int(self.find_crf.get())))
        except (tk.TclError, ValueError):
            crf = 18
        codec = "h264" if self.find_codec.get().startswith("H.264") else "auto"
        ast = self.jp_ast.get()
        vscan = bool(self.find_vscan.get())
        group = bool(self.find_group.get())
        extras = en_all[1:]   # 2本目以降の英語版
        cms_by = {x: self._cms_of(x) for x in en_all}   # 「CMを切り分ける」で出した一覧
        off_by = {x: self._cm_off(x) for x in en_all}   # チェックを外した(探さない)CM
        self.find_btn.state(["disabled"])
        _CANCEL.clear()
        self._find_stop_ev.clear()
        self.find_stop.state(["!disabled"])
        self.find_open.pack_forget()
        self.find_review_btn.pack_forget()
        self.find_results = results_l = []
        self.find_msg.config(text="英語版を読み込んでいます…", fg="#333")
        self.find_bar.config(value=0)
        n = len(files)

        def safe(t):
            return re.sub(r'[\\/:*?"<>|]', "_", t)

        def work_():
            outd.mkdir(parents=True, exist_ok=True)

            def setup_en(p, cl_pre, primary, skip=()):
                cl = cl_pre
                if not cl:  # 前回の探索記録に、同じ英語版の切り分け結果が残っていれば、それを使う(切り分けの時間を省く)
                    try:
                        st0 = Path(p).stat()
                        old0 = json.loads((outd / f"syncdub_探索記録_{safe(Path(p).stem)}.json").read_text(encoding="utf-8"))
                        sg0 = old0.get("sig")
                        if sg0 and sg0[1] == st0.st_size and sg0[2] == int(st0.st_mtime) and sg0[3]:
                            cl = [(float(x[0]), float(x[1])) for x in sg0[3]]
                            skip = ()
                    except Exception:  # noqa: BLE001
                        pass
                # チェックを外したCMがあるときは、☑ のCMの区間だけ音声を読む(全部 ☑ のときは、頭から終わりまで全部読む)
                en = Media(p, 0, parts=[c_ for k_, c_ in enumerate(cl) if k_ not in skip]) if (cl and skip) else Media(p, 0)
                if not cl:  # 「CMを切り分ける」を押していなければ、ここで自動で切り分ける
                    self.q.put(lambda: self.find_msg.config(text=f"英語版のCMを切り分けています…({Path(p).name})", fg="#333"))
                    try:
                        cl = split_commercials(p)
                    except Exception:  # noqa: BLE001
                        cl = []
                    if cl:
                        def show(c=list(cl)):
                            self._cm_store(p, c)
                            self._cm_fill()
                            if p == self._cm_view:
                                self.cm_note.config(text=f"{len(c)} 本に分けました(スタート時に自動で)。", foreground="#1f6f43")
                        self.q.put(show)
                    else:
                        cl = [(0.0, en.end)]
                cms_ = [en.slice(a_, b_) for a_, b_ in cl]
                st_en = Path(p).stat()
                sig = [p, st_en.st_size, int(st_en.st_mtime), [[round(a_, 2), round(b_, 2)] for a_, b_ in cl]]
                rec_p = outd / f"syncdub_探索記録_{safe(Path(p).stem)}.json"
                rec = {"sig": sig, "algo": FIND_ALGO, "files": {}}
                try:
                    old = json.loads(rec_p.read_text(encoding="utf-8"))
                    if old.get("sig") == sig and old.get("algo") == FIND_ALGO:
                        rec = old
                except Exception:  # noqa: BLE001
                    pass
                probes = {}
                if vscan:
                    try:
                        probes = en_probes(en, cl, skip)
                    except Exception:  # noqa: BLE001
                        probes = {}
                return {"en_p": p, "en": en, "cl": cl, "cms": cms_, "rec": rec, "rec_p": rec_p, "found": [0] * len(cl),
                        "en_stem": safe(Path(p).stem), "probes": probes, "skip": set(skip) if cl_pre else set()}

            ctxs, skipped_en = [], []
            for ei_, p_ in enumerate([en_p] + extras):
                self.q.put(lambda p_=p_: self.find_msg.config(text=f"英語版を読み込んでいます…({Path(p_).name})", fg="#333"))
                try:
                    ctxs.append(setup_en(p_, cms_by.get(p_, []), ei_ == 0, off_by.get(p_, set())))
                except Exception as ex:  # noqa: BLE001
                    if ei_ == 0:
                        self.q.put(lambda ex=ex: (self.find_msg.config(text=f"⚠ 英語版を読み込めませんでした: {ex}", fg="#b33"),
                                                  self.find_btn.state(["!disabled"])))
                        return
                    skipped_en.append(Path(p_).name)
            stop = self._find_stop_ev

            def save_rec(cx):
                try:
                    cx["rec_p"].write_text(json.dumps(cx["rec"], ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass

            class _Stop(Exception):
                pass
            rejected = []
            res = {"ok": 0, "warn": 0, "ng": 0, "skip": 0}
            stopped = False
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            pre = {}
            prev = {}
            thc = {}   # 映像の下調べ(1秒1コマ)の結果。英語版が何本あっても、録画ごとに1回だけ
            M = len(ctxs)
            nn = n * M

            def needs(p):
                try:
                    sx = Path(p).stat()
                    for c_ in ctxs:
                        e = c_["rec"]["files"].get(p)
                        if not (e and e.get("key") == [sx.st_size, int(sx.st_mtime), int(vscan)]):
                            return True
                    return False
                except OSError:
                    return False
            for i, jp_p in enumerate(files):
                if stop.is_set():
                    stopped = True
                    break
                jp_loaded = {}   # 録画の読み込みは、英語版が何本あっても1回だけ
                for ei, cx in enumerate(ctxs):
                    en, cl, cms, rec, found, en_stem, probes = cx["en"], cx["cl"], cx["cms"], cx["rec"], cx["found"], cx["en_stem"], cx["probes"]
                    skip = cx["skip"]
                    cw = max(2, len(str(len(cl))))   # CM番号の桁数(100本以上なら3桁にして、並べたとき順番どおりになるように)
                    ii = i * M + ei
                    lab = f"[{en_stem}] " if M > 1 else ""
                    t_rec = time.time()

                    def el(t_rec=t_rec):
                        d = int(time.time() - t_rec)
                        return f" [{d // 60}分{d % 60:02d}秒]" if d >= 60 else f" [{d}秒]"

                    def st(text, tag="", i=i, lab=lab):
                        self.q.put(lambda: self._find_set(i, lab + text, tag))

                    def prog(fr, i=i, ii=ii, lab=lab):
                        if stop.is_set():
                            raise _Stop()
                        self.q.put(lambda: (self.find_bar.config(value=(ii + fr * 0.5) / nn * 100),
                                            self.find_msg.config(text=f"({i + 1}/{n}) {lab}{Path(jp_p).name}: 音を探しています…", fg="#333")))
                    try:
                        try:
                            fst = Path(jp_p).stat()
                            fkey = [fst.st_size, int(fst.st_mtime), int(vscan)]
                        except OSError:
                            fkey = None
                        ent = rec["files"].get(jp_p)
                        if ent and not set(ent.get("skip", ())) <= skip:   # 前回は探さなかったCMを、今回は探す → 調べ直す
                            ent = None
                        if ent and ent.get("key") == fkey and ent.get("done"):
                            ent["keep"] = [d for d in ent["keep"] if d["k"] not in skip]
                            st(f"前回調べ済み ({len(ent['keep'])} 件)", "ok" if ent["keep"] else "")
                            for d in ent["keep"]:
                                if d.get("v") is not None and d["v"] < VID_MIN:
                                    tt = int(d["t"])
                                    rejected.append(f"{lab}CM{d['k'] + 1:02d}\t{Path(jp_p).name}\t{tt // 3600}:{tt % 3600 // 60:02d}:{tt % 60:02d}\t映像{d['v']:.2f}\t音{d['z']:.1f}")
                                else:
                                    found[d["k"]] += 1
                            continue
                        if ent and ent.get("key") == fkey:
                            keep = [d for d in ent["keep"] if d["k"] not in skip]
                        else:
                            st("音を探しています…")
                            fut = pre.pop(jp_p, None)
                            jp = jp_loaded.get(jp_p) or (fut.result() if fut else LightMedia(jp_p, ast))
                            jp_loaded[jp_p] = jp
                            if probes and jp_p not in prev and jp_p not in thc:
                                prev[jp_p] = pool.submit(scan_jp_thumbs, jp_p, stop)   # 映像の下調べ(1秒1コマ)も、音を探している間に進めておく
                            if i + 1 < n and files[i + 1] not in pre and needs(files[i + 1]):
                                pre[files[i + 1]] = pool.submit(LightMedia, files[i + 1], ast)  # 次の録画を、探している間に先に読み込んでおく
                                if probes:
                                    prev[files[i + 1]] = pool.submit(scan_jp_thumbs, files[i + 1], stop)
                            dropped = []
                            hits = find_in_recording(cms, jp, prog, dropped=dropped, skip=skip)
                            jo = []
                            for k, hl in enumerate(hits):
                                L = cl[k][1] - cl[k][0]
                                for h in hl:
                                    jo.append({"k": k, "t": h["t"], "z": h["z"], "ok": h["ok"], "L": L,
                                               "zz": h.get("zz"), "np": h.get("np"), "n": h.get("n")})
                            # 同じ場所に複数のCMが当てはまったら、はっきりしているほうだけ残す
                            wk = sorted((d for d in jo if d.get("weak")), key=lambda d: -d["z"])
                            for d in wk[12:]:  # 弱い候補が多すぎるときは、強いほうから12件だけ映像で確かめる
                                jo.remove(d)
                            vcache = {}
                            for d in jo:
                                a_, b_ = cl[d["k"]]
                                try:
                                    d["vf"] = []
                                    d["v"] = video_similarity(en, a_, b_ - a_, jp, d["t"], vcache, detail=d["vf"])
                                except Exception:  # noqa: BLE001
                                    d["v"] = None
                            # 音が弱くしか合わなかった候補は、映像が合うときだけ採用する(音が似ているだけの別CMを避ける)
                            for d in list(jo):
                                if d.get("weak") and not (d["v"] is not None and d["v"] >= VID_OK):
                                    dropped.append({"k": d["k"], "t": round(d["t"], 2), "z": round(d["z"], 1), "zz": d["zz"], "np": d["np"], "n": d["n"], "v": d["v"], "weak": True})
                                    jo.remove(d)
                            # 映像が合わないもの(声やBGMだけ同じ別CM)は先に外す。音が強い偽物が、本物を押しのけないように
                            bad = [d for d in jo if d["v"] is not None and d["v"] < VID_MIN]
                            good = [d for d in jo if not (d["v"] is not None and d["v"] < VID_MIN)]

                            def over(d, e):
                                return min(d["t"] + d["L"], e["t"] + e["L"]) - max(d["t"], e["t"]) > 0.5 * min(d["L"], e["L"])
                            # 映像まで合うものを先に、次に映像が似ているもの、最後に音がはっきりしているものの順で残す
                            good.sort(key=lambda d: (not vid_good(d), -(d["v"] or 0), -d["z"]))
                            keep = []
                            for d in good:
                                clash = [e for e in keep if over(d, e)]
                                if clash:
                                    for e in clash:
                                        e["clash"] = True
                                        e.setdefault("alts", []).append({"k": d["k"], "z": round(d["z"], 1), "v": d["v"]})
                                    continue
                                keep.append(d)
                            for d in bad:  # 映像が合わず外したもの(重なる本物がなければ、記録として残す)
                                clash = [e for e in keep if over(d, e)]
                                if clash:
                                    for e in clash:
                                        e.setdefault("alts", []).append({"k": d["k"], "z": round(d["z"], 1), "v": d["v"], "rej": True})
                                else:
                                    keep.append(d)
                            # 長いCMの短縮版・途中で切れた版(部分ごとに探して、順番に並んでいるものをまとめる)
                            pcs = make_pieces(cl, skip)
                            if pcs:
                                try:
                                    phits = find_in_recording([en.slice(a_, b_) for _k, a_, b_ in pcs], jp, prog, loose=True)
                                    plog = []
                                    extra = {}
                                    fv_ = prev.pop(jp_p, None)
                                    if fv_ is not None or jp_p in thc:
                                        st("映像を調べています…")
                                        if fv_ is not None:
                                            thc[jp_p] = fv_.result()
                                        th_ = thc[jp_p]
                                        if stop.is_set():
                                            raise _Stop()
                                        if th_ is not None:
                                            vd_ = {}
                                            extra = video_scan_hits(en, jp, probes, th_, diag=vd_)
                                            vd_["best"] = sorted(vd_.get("best", []), key=lambda x: -x[1])[:8]
                                            dropped.append({"video_scan_diag": vd_})
                                            dropped.append({"video_scan": {str(k_): [[round(h_["e"], 1), round(h_["t"], 1), round(h_["l"], 1)] for h_ in v_] for k_, v_ in extra.items()}})
                                    sv_ = find_short_versions(en, jp, cl, pcs, phits, vcache, keep, dropped, plog=plog, extra=extra)
                                    dropped.append({"pieces_found": sum(len(x) for x in phits), "log": plog[:60]})
                                    for e_ in sv_:
                                        # 短縮版と重なる「映像が合わず除外」の記録は、短縮版だったという説明がつくので消す
                                        keep[:] = [x for x in keep if not (x["k"] == e_["k"] and x.get("v") is not None and x["v"] < VID_MIN
                                                                           and min(x["t"] + x["L"], e_["t"] + e_["L"]) - max(x["t"], e_["t"]) > 0)]
                                        keep.append(e_)
                                except _Stop:
                                    raise
                                except Exception as ex_:  # noqa: BLE001
                                    dropped.append({"short_error": repr(ex_)})
                            keep.sort(key=lambda d: d["t"])
                            jp = None
                            rec["files"][jp_p] = {"key": fkey, "skip": sorted(skip), "keep": keep, "dropped": sorted([x for x in dropped if "z" in x], key=lambda x: -x["z"])[:40] + [x for x in dropped if "z" not in x], "done": False}
                            save_rec(cx)
                        if not keep:
                            rec["files"][jp_p]["done"] = True
                            save_rec(cx)
                            st("見つかりませんでした" + el(), "")
                            continue
                        nth = {}
                        nw = nwarn = nsk = nrej = nfix = 0
                        ex_cache = {}

                        def existing_in(dp):   # 書き出し済みの名前(【…】は無視)。まとめ先のフォルダと、保存先の直下(以前の書き出し)の両方を見る
                            if dp not in ex_cache:
                                names = set()
                                for dd in {dp, outd}:
                                    try:
                                        names |= {re.sub(r"【[^】]*】", "", f.name) for f in dd.iterdir()}
                                    except OSError:
                                        pass
                                ex_cache[dp] = names
                            return ex_cache[dp]
                        for j, d in enumerate(keep):
                            if stop.is_set():
                                raise _Stop()
                            k = d["k"]
                            a, b = cl[k]
                            nth[k] = nth.get(k, 0) + 1
                            tail = f"_{nth[k]}回目" if sum(1 for e in keep if e["k"] == k) > 1 else ""
                            v = d.get("v")
                            why = []
                            if not vid_good(d):  # 映像まで合っていれば、音の曖昧さは気にしない
                                if not d["ok"]:
                                    why.append("一部だけ一致")
                                if d.get("clash"):
                                    why.append("別のCMとも似ている")
                            if v is not None and VID_MIN <= v and not vid_good(d):
                                why.append("映像が少し違う")
                            if v is not None and v < VID_MIN:  # 音は合っても映像が合わない=BGMや声が同じだけの別のCM。書き出さず記録だけ残す
                                tt = int(d["t"])
                                rejected.append(f"{lab}CM{k + 1:0{cw}d}\t{Path(jp_p).name}\t{tt // 3600}:{tt % 3600 // 60:02d}:{tt % 60:02d}\t映像{v:.2f}\t音{d['z']:.1f}")
                                nrej += 1
                                continue
                            base = f"CM{k + 1:0{cw}d}_{round5(d.get('out_len', b - a))}秒_{en_stem}{tail}" + ("【短縮版】" if d.get("short") else "")
                            name = base + ("【要確認】" if why else "") + (f"【映像{v:.2f}】" if v is not None else "") \
                                + f"({safe(Path(jp_p).stem)}).mkv"
                            outd_k = outd / f"CM{k + 1:0{cw}d}_{round5(b - a)}秒_{en_stem}" if group else outd
                            out = outd_k / name
                            found[k] += 1
                            if re.sub(r"【[^】]*】", "", base) + f"({safe(Path(jp_p).stem)}).mkv" in existing_in(outd_k):
                                res["skip"] += 1
                                nsk += 1
                                continue
                            outd_k.mkdir(parents=True, exist_ok=True)
                            st(f"書き出しています… ({j + 1}/{len(keep)})")
                            self.q.put(lambda ii=ii, j=j, m=len(keep): self.find_bar.config(value=(ii + 0.5 + 0.5 * j / m) / nn * 100))
                            proj = {"en_video": cx["en_p"], "jp_media": jp_p, "jp_audio_stream": ast, "output": str(out),
                                    "keep_en_audio": True, "audio_codec": "aac", "audio_bitrate": "256k",
                                    "segments": ([{"en_start": r_[0], "en_end": r_[1], "jp_start": r_[2], "fit": "stretch"} for r_ in d["runs"]]
                                                 if d.get("runs") and not d.get("fine") else
                                                 [{"en_start": r_[0], "en_end": r_[1], "jp_start": r_[2], "jp_end": (d["runs"][q_ + 1][2] if q_ + 1 < len(d["runs"]) else d["jp_span"][1]), "fit": "stretch"}
                                                  for q_, r_ in enumerate(d["runs"])] if d.get("fine") else
                                                 [{"en_start": round(a, 4), "en_end": round(b, 4), "jp_start": round(d["t"], 4), "fit": "stretch"}]),
                                    "clip": ({"start": d["runs"][0][0], "end": d["runs"][-1][1], "crf": crf, "preset": "medium", "codec": codec}
                                             if d.get("runs") else
                                             {"start": round(a, 4), "end": round(b, 4), "crf": crf, "preset": "medium", "codec": codec})}
                            tmpd = Path(tempfile.mkdtemp(prefix="syncdub_"))
                            fixed_lag = None
                            try:
                                pj = tmpd / "project.json"
                                pj.write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
                                buf = io.StringIO()
                                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                                    main([str(pj)])
                                if not d.get("fine"):
                                    # 音の一括ずれの自動補正: 日本語・英語の音(BGM・効果音)を突き合わせて、全体が一定量ずれていたら
                                    # その分だけ日本語側の位置を動かして書き出し直す (録画側の音声の時刻が途中で飛んでいる場合など)
                                    lag_ = audio_lag(str(out))
                                    if lag_ is not None:
                                        orig_ = json.loads(pj.read_text(encoding="utf-8"))
                                        for sg_ in proj["segments"]:
                                            sg_["jp_start"] = round(sg_["jp_start"] + lag_, 4)
                                        pj.write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
                                        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                                            main([str(pj)])
                                        lag2_ = audio_lag(str(out))
                                        if lag2_ is not None and abs(lag2_) > abs(lag_) * 0.5:  # 直らなかったので元に戻す
                                            pj.write_text(json.dumps(orig_, ensure_ascii=False), encoding="utf-8")
                                            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                                                main([str(pj)])
                                        else:
                                            nfix += 1
                                            fixed_lag = lag_
                                if d.get("fine"):
                                    # 細かく編集し直された版は、日本語音声が連続しているので、区間ごとに切らず元の音をそのまま使う
                                    # (区間ごとに切ると、つなぎ目で数ミリ秒の音の欠けやずれが出る)
                                    tmp_o = out.with_name(out.stem + ".tmp.mkv")
                                    j0_, j1_ = d["jp_span"]
                                    cmd_ = ["ffmpeg", "-y", "-v", "error", "-i", str(out), "-ss", f"{j0_ + d.get('voff', 0.0):.6f}", "-t", f"{j1_ - j0_:.6f}",
                                            "-i", jp_p, "-map", "0:v:0", "-map", f"1:a:{int(ast or 0)}", "-map", "0:a:1?", "-c:v", "copy",
                                            "-c:a:0", "aac", "-b:a:0", "256k", "-c:a:1", "copy",
                                            "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese dub (synced)", "-disposition:a:0", "default",
                                            "-metadata:s:a:1", "language=eng", "-disposition:a:1", "0", str(tmp_o)]
                                    subprocess.run(cmd_, check=True, capture_output=True, **NOWIN)
                                    os.replace(tmp_o, out)
                            except BaseException:   # 中止・失敗したときは、書きかけのファイルを消す
                                for f_ in (out, out.with_name(out.stem + ".tmp.mkv")):
                                    rm_retry(f_)
                                try:
                                    outd_k.rmdir()   # 空になったCMごとのフォルダも片づける(中身があれば消えない)
                                except OSError:
                                    pass
                                raise
                            finally:
                                shutil.rmtree(tmpd, ignore_errors=True)
                            nw += 1
                            results_l.append({"cm": f"CM{k + 1:0{cw}d}", "out": str(out), "name": out.name, "jp": jp_p, "jp_name": Path(jp_p).name,
                                              "en": cx["en_p"], "t": d["t"], "v": v, "z": d["z"], "short": bool(d.get("short")), "fine": bool(d.get("fine")),
                                              "why": list(why), "lagfix": fixed_lag, "segs": json.loads(json.dumps(proj["segments"]))})
                            if why:
                                nwarn += 1
                                res["warn"] += 1
                            else:
                                res["ok"] += 1
                        rec["files"][jp_p]["done"] = True
                        save_rec(cx)
                        msg = f"✔ {nw + nsk} 件" + (f" (うち {nsk} 件は書き出し済み)" if nsk else " 書き出しました")
                        if nwarn:
                            msg += f" (うち要確認 {nwarn} 件: 一部だけ一致 / 別のCMとも似ている / 映像が少し違う)"
                        if nrej:
                            msg += f" ・映像が合わず除外 {nrej} 件"
                        if nfix:
                            msg += f" ・音のずれを自動で直したもの {nfix} 件"
                        st(msg + el(), "warn" if nwarn else "ok")
                    except (_Stop, Cancelled):
                        st("中止しました (書きかけのファイルは消しました。次回は続きから)", "warn")
                        stopped = True
                        break
                    except subprocess.CalledProcessError as ex:
                        st("× ffmpegでエラー: " + (ex.stderr or "")[-120:].replace("\n", " "), "ng")
                        res["ng"] += 1
                    except Exception as ex:  # noqa: BLE001
                        st(f"× エラー: {ex}", "ng")
                        res["ng"] += 1
                    if stopped:
                        break
                if stopped:
                    break
                thc.pop(jp_p, None)
                self.q.put(lambda i=i: self.find_bar.config(value=(i + 1) / n * 100))


            pool.shutdown(wait=False, cancel_futures=True)

            def fin():
                _CANCEL.clear()
                self.find_btn.state(["!disabled"])
                self.find_stop.state(["disabled"])
                bits = [f"書き出し {res['ok'] + res['warn']} 本"]
                if res["warn"]:
                    bits.append(f"うち要確認 {res['warn']} 本(名前に【要確認】が付いています)")
                if rejected:
                    bits.append(f"音は合っても映像が合わず除外 {len(rejected)} 件(保存先の「syncdub_除外した候補.txt」に記録)")
                    try:
                        (outd / "syncdub_除外した候補.txt").write_text(
                            "CM\t録画\t録画の時刻\t映像の似かた\t音の強さ\n" + "\n".join(rejected) + "\n", encoding="utf-8")
                    except OSError:
                        pass
                if res["ng"]:
                    bits.append(f"失敗 {res['ng']} 録画")
                if res["skip"]:
                    bits.append(f"同じ名前があって飛ばした {res['skip']} 本")
                if not stopped:
                    for c_ in ctxs:
                        miss = [f"CM{k + 1:0{max(2, len(str(len(c_['cl']))))}d}" for k, c in enumerate(c_["found"]) if c == 0 and k not in c_["skip"]]
                        if miss and len(c_["cl"]) > 1:
                            bits.append((f"[{c_['en_stem']}] " if len(ctxs) > 1 else "") + "どの録画にも見つからなかったCM: " + "、".join(miss[:20]) + ("…" if len(miss) > 20 else ""))
                if skipped_en:
                    bits.append("読み込めなかった英語版: " + "、".join(skipped_en))
                if stopped:
                    bits.insert(0, "中止しました(もう一度スタートすると、調べ済みの録画は飛ばして続きから進みます)")
                self.find_msg.config(text=("" if stopped else "✔ ") + "。".join(bits), fg="#1f6f43" if not (res["ng"] or res["warn"]) else "#8a5a00")
                if res["ok"] + res["warn"]:
                    self.find_open.pack(side="left", padx=8)
                if BETA_REVIEW and results_l:
                    self.find_review_btn.pack(side="left", padx=8)
                    if not stopped:
                        self._open_review()
                self.say("探索完了")
            self.q.put(fin)
        def work():
            try:
                work_()
            except Cancelled:   # 準備(英語版の読み込みなど)の途中で止めたとき
                def stopped_():
                    _CANCEL.clear()
                    self.find_btn.state(["!disabled"])
                    self.find_stop.state(["disabled"])
                    self.find_msg.config(text="中止しました (次回は続きから)", fg="#8a5a00")
                self.q.put(stopped_)

        threading.Thread(target=work, daemon=True).start()

    def _tab_changed(self, _e=None):
        """切り出しタブを開いたとき、「かんたん」タブで選んだ日本語ファイルが一覧になければ入れる(ファイル指定をタブ間で共通に)。"""
        try:
            if self.nb.select() == str(self.clipt) and not self.clip_files:
                jp = self.jp_var.get().strip()
                if jp and Path(jp).exists():
                    self._clip_add_files([jp])
        except Exception:  # noqa: BLE001
            pass

    def _open_dir(self, d):
        try:
            if sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception:  # noqa: BLE001
            pass

    def _clip_pick_en(self):
        p = filedialog.askopenfilename(title="英語版の映像を選択", filetypes=MEDIA_TYPES)
        if p:
            self.clip_en.set(p)
            if not self.clip_out.get():
                self.clip_out.set(str(Path(p).parent))

    def _clip_pick_out(self):
        d = filedialog.askdirectory(title="保存先フォルダを選択")
        if d:
            self.clip_out.set(d)

    def _clip_add_files(self, files):
        for p in files:
            if p and p not in self.clip_files and not p.lower().endswith(".json"):
                self.clip_files.append(p)
                self.clip_tree.insert("", "end", iid=str(len(self.clip_files) - 1), values=(Path(p).name, "待機中"))

    def _clip_add(self):
        self._clip_add_files(list(filedialog.askopenfilenames(title="日本語のファイルを選択 (複数選べます)", filetypes=MEDIA_TYPES)))

    def _clip_drop(self, ev):
        self._clip_add_files(list(self.tk.splitlist(ev.data)))

    def _clip_remove(self):
        sel = set(self.clip_tree.selection())
        if not sel:
            return
        keep = [p for i, p in enumerate(self.clip_files) if str(i) not in sel]
        st = {p: self.clip_tree.item(str(i), "values")[1] for i, p in enumerate(self.clip_files)}
        self._clip_clear()
        self._clip_add_files(keep)
        for i, p in enumerate(self.clip_files):
            self.clip_tree.set(str(i), "st", st[p])

    def _clip_clear(self):
        self.clip_files = []
        self.clip_tree.delete(*self.clip_tree.get_children())

    def _clip_set(self, i, text, tag=""):
        iid = str(i)
        if self.clip_tree.exists(iid):
            self.clip_tree.set(iid, "st", text)
            self.clip_tree.item(iid, tags=(tag,) if tag else ())

    def run_clip(self):
        en_p, files = self.clip_en.get().strip(), list(self.clip_files)
        if not en_p or not Path(en_p).exists():
            messagebox.showinfo("英語版の映像", "英語版の映像を選んでください。")
            return
        if not files:
            messagebox.showinfo("日本語のファイル", "日本語のファイルを追加してください。")
            return
        outd = Path(self.clip_out.get().strip() or Path(en_p).parent)
        self.clip_out.set(str(outd))
        outs = [outd / (Path(p).stem + "_jpdub.mkv") for p in files]
        over = False
        if any(o.exists() for o in outs):
            over = messagebox.askyesno("上書き確認", "同じ名前のファイルが既にあります。上書きしますか?\n(「いいえ」だと、その分は飛ばします)")
        try:
            crf = int(self.clip_crf.get())
        except (tk.TclError, ValueError):
            crf = 18
        crf = max(0, min(51, crf))
        codec = "h264" if self.clip_codec.get().startswith("H.264") else "auto"
        ast = self.jp_ast.get()
        self.clip_btn.state(["disabled"])
        self.clip_open.pack_forget()
        self.clip_msg.config(text="英語版を読み込んでいます…", fg="#333")
        self.clip_bar.config(value=0)
        n = len(files)

        def work():
            try:
                en = Media(en_p, 0)
            except Exception as ex:  # noqa: BLE001
                self.q.put(lambda ex=ex: (self.clip_msg.config(text=f"⚠ 英語版を読み込めませんでした: {ex}", fg="#b33"),
                                    self.clip_btn.state(["!disabled"])))
                return
            done = 0
            res = {"ok": 0, "warn": 0, "ng": 0, "skip": 0}
            for i, (jp_p, out) in enumerate(zip(files, outs)):
                def st(text, tag="", i=i):
                    self.q.put(lambda: self._clip_set(i, text, tag))

                def prog(fr, msg, i=i):
                    self.q.put(lambda: (self.clip_bar.config(value=(i + fr * 0.5) / n * 100),
                                        self.clip_msg.config(text=f"({i + 1}/{n}) {Path(jp_p).name}: {msg}", fg="#333")))
                if out.exists() and not over:
                    st("飛ばしました (同じ名前のファイルがあります)", "warn")
                    res["skip"] += 1
                    continue
                try:
                    st("音を照合しています…")
                    jp = Media(jp_p, ast)
                    segs, info = auto_align(en, jp, prog)
                    if not info["ok"] or not segs:
                        st("× " + (info.get("msg") or "一致する場所が見つかりませんでした"), "ng")
                        res["ng"] += 1
                        continue
                    c0, c1 = segs[0]["en_start"], segs[-1]["en_end"]
                    flag = "・".join(sorted({s["flag"] for s in segs if s.get("flag")}))
                    st("切り出して書き出しています…")
                    self.q.put(lambda i=i: self.clip_bar.config(value=(i + 0.6) / n * 100))
                    sg = []
                    for s in segs:
                        d = {"en_start": round(s["en_start"], 4), "en_end": round(s["en_end"], 4),
                             "jp_start": round(s["jp_start"], 4), "fit": s.get("fit", "stretch")}
                        if s["jp_end"] is not None:
                            d["jp_end"] = round(s["jp_end"], 4)
                        sg.append(d)
                    proj = {"en_video": en_p, "jp_media": jp_p, "jp_audio_stream": ast, "output": str(out),
                            "keep_en_audio": True, "audio_codec": "aac", "audio_bitrate": "256k", "segments": sg,
                            "clip": {"start": round(c0, 4), "end": round(c1, 4), "crf": crf, "preset": "medium", "codec": codec}}
                    tmpd = Path(tempfile.mkdtemp(prefix="syncdub_"))
                    try:
                        pj = tmpd / "project.json"
                        pj.write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                            main([str(pj)])
                    finally:
                        shutil.rmtree(tmpd, ignore_errors=True)
                    span = f"英語版 {self.short(c0)}〜{self.short(c1)} を切り出し"
                    m_enc = re.search(r"映像エンコーダ: (\S+)", buf.getvalue())
                    if m_enc:
                        span += f" [{ENC_LABEL.get(m_enc.group(1), m_enc.group(1))}]"
                    if "カット部分を飛ばして" in buf.getvalue():
                        span += " (日本語版でカットされた部分は飛ばしました)"
                    if flag:
                        st(f"✔ 完了 (要確認: {flag}) {span}", "warn")
                        res["warn"] += 1
                    else:
                        st(f"✔ 完了 {span}", "ok")
                        res["ok"] += 1
                except subprocess.CalledProcessError as ex:
                    st("× ffmpegでエラー: " + (ex.stderr or "")[-120:].replace("\n", " "), "ng")
                    res["ng"] += 1
                except Exception as ex:  # noqa: BLE001
                    st(f"× エラー: {ex}", "ng")
                    res["ng"] += 1
                done += 1
                self.q.put(lambda i=i: self.clip_bar.config(value=(i + 1) / n * 100))

            def fin():
                self.clip_btn.state(["!disabled"])
                bits = [f"完了 {res['ok'] + res['warn']} 本"]
                if res["warn"]:
                    bits.append(f"うち要確認 {res['warn']} 本(聞いて確かめてください)")
                if res["ng"]:
                    bits.append(f"失敗 {res['ng']} 本")
                if res["skip"]:
                    bits.append(f"飛ばした {res['skip']} 本")
                self.clip_msg.config(text="✔ " + "、".join(bits), fg="#1f6f43" if not (res["ng"] or res["warn"]) else "#8a5a00")
                if res["ok"] + res["warn"]:
                    self.clip_open.pack(side="left", padx=8)
                self.say("切り出し完了")
            self.q.put(fin)
        threading.Thread(target=work, daemon=True).start()

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
        p = filedialog.askopenfilename(title="英語版の映像を選択" if k == "en" else "日本語の録画を選択", filetypes=MEDIA_TYPES)
        if p:
            (self.en_var if k == "en" else self.jp_var).set(p)
            self._maybe_load()

    def _maybe_load(self):
        if self.en_var.get().strip() and self.jp_var.get().strip():
            self._auto_after_load = False  # 自動検出は「自動で合わせる」ボタンを押したときだけ
            self.load_media()
        elif self.nb.select() not in (str(self.findt), str(self.clipt)):   # 「録画から探す」「切り出し」では、この案内は無関係
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
        by_video = bool(self.easy_video.get())
        self._align_mode = "音+映像" if by_video else "音"
        self.auto_msg.config(text=("音と画面の両方で、対応する場所を探しています…(長い動画は10分以上かかることがあります)" if by_video
                                   else "音を照合して、対応する場所を探しています…(長い動画は数分かかります)"), fg="#333")
        self.easy_done.config(text="")

        def prog(fr, msg):
            self.q.put(lambda: (self.pbar.config(value=fr * 100), self.auto_msg.config(text=msg)))

        def work():
            try:
                segs, info = (combined_align if by_video else auto_align)(en, jp, prog)
            except Exception as ex:  # noqa: BLE001
                segs, info = [], {"ok": False, "unused_jp": [], "msg": f"解析中にエラーが起きました: {ex}"}
            self.q.put(lambda: self._auto_done(segs, info))
        threading.Thread(target=work, daemon=True).start()

    def _auto_done(self, segs, info):
        if not info["ok"]:
            self.items, self.auto_flags = [], None
            self.rebuild()
            self.pbar.config(value=0)
            self.auto_msg.config(text="⚠ " + info["msg"], fg="#b33")
            self._busy(False)
            return
        try:
            en_len = float(self.media["en"].end)
        except Exception:  # noqa: BLE001
            en_len = None
        self.items = items_from_segs(segs, en_len)
        self._align_unused = list(info.get("unused_jp") or [])
        self._align_segs = [dict(x) for x in segs]
        self.auto_flags = [(s["en_start"], s["en_end"], s["flag"]) for s in segs if s.get("flag")]
        self.rebuild()
        self.pbar.config(value=100)
        warn = len(self.auto_flags)
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
            if self.auto_flags is None:
                flag, st = "", "手動で設定"
            else:
                flag = "・".join(f for a, b, f in self.auto_flags if a < s["en_end"] - 1e-6 and b > s["en_start"] + 1e-6)
                st = "要確認: " + flag if flag else "OK"
            self.etree.insert("", "end", iid=str(i), tags=("warn",) if flag else (), values=(
                seg_letter(i), f"{self.short(s['en_start'])} 〜 {self.short(s['en_end'])}",
                f"{self.short(s['jp_start'])} 〜 {self.short(je)}", st))
        self.easy_render.state(["disabled"] if (not self.segs) else ["!disabled"])

    def on_easy_select(self, _):
        s = self.etree.selection()
        if s and self.media["en"]:
            seg = self.segs[int(s[0])]
            for t in self.tr.values():
                t.sel = int(s[0])
                t.redraw()
            self.set_cursor("en", seg["en_start"], scroll=True)
            self.set_cursor("jp", seg["jp_start"], scroll=True)

    def _listen(self, k):
        if not self.etree.selection():
            self.say("先に、表の行を選んでください")
            return
        self.play(k)

    # ---- ベータ版: 書き出した動画の自動チェックと、貼り付け用の報告文
    def _beta_report(self, out, issues, notes, log=""):
        def med(path):
            try:
                inf = probe(path)
                st = []
                for x in inf.get("streams", []):
                    if x.get("codec_type") == "video":
                        st.append(" ".join(str(v) for v in ("映像", x.get("codec_name"), f"{x.get('width')}x{x.get('height')}" if x.get("width") else None,
                                                            x.get("r_frame_rate")) if v))
                    elif x.get("codec_type") == "audio":
                        st.append(" ".join(str(v) for v in ("音声", x.get("codec_name"), f"{x.get('channels')}ch" if x.get("channels") else None,
                                                            f"{x.get('sample_rate')}Hz" if x.get("sample_rate") else None) if v))
                return f"{Path(path).name} / 長さ {_mmss(fmt_duration(inf))} / " + ", ".join(st)
            except Exception as ex:  # noqa: BLE001
                return f"{Path(path).name} (情報を読めず: {ex})"
        L = [f"syncdub 自動チェック報告  {ver_label()}  {datetime.now():%Y-%m-%d %H:%M}",
             f"合わせ方: かんたん / {getattr(self, '_align_mode', '?')}" + (" (自動合わせのあと、手で直しています)" if self.auto_flags is None and self.items else ""),
             "英語版: " + med(self.en_var.get()),
             "日本語: " + med(self.jp_var.get()) + f" / 使った音声 {self.jp_ast.get()}番",
             "出力: " + med(out), ""]
        if issues is None:
            L.append("■ 自動チェックはできませんでした")
        elif issues:
            L.append(f"■ おかしそうな所 {len(issues)} 件 (時刻は出力した動画の時刻)")
            L += ["  " + x for x in issues[:60]]
        else:
            L.append("■ おかしそうな所は見つかりませんでした")
        L += ["  (" + x + ")" for x in notes]
        L += ["", "■ 自動合わせの結果 (英語版の時間 → 日本語録画の時間 / 状態)"]
        for x in getattr(self, "_align_segs", []):
            je = x["jp_end"] if x.get("jp_end") is not None else x["jp_start"] + (x["en_end"] - x["en_start"])
            L.append(f"  {x['en_start']:.2f}〜{x['en_end']:.2f} → {x['jp_start']:.2f}〜{je:.2f}"
                     + (" 伸縮" if x.get("jp_end") is not None else "") + (" 頭詰め" if x.get("trim_s") else "")
                     + (" 末尾詰め" if x.get("trim_e") else "") + (f" / {x['flag']}" if x.get("flag") else ""))
        if getattr(self, "_align_unused", None):
            L.append("  使わなかった日本語: " + ", ".join(f"{a:.1f}〜{b:.1f}" for a, b in self._align_unused))
        L += ["", "■ 合成に使った区間 (英語版 → 日本語録画)"]
        for x in self.segs:
            je = x.get("jp_end")
            L.append(f"  {x['en_start']:.2f}〜{x['en_end']:.2f} → {x['jp_start']:.2f}" + (f"〜{je:.2f}" if je is not None else ""))
        if log:
            L += ["", "■ 合成のログ(末尾)", log[-700:].strip()]
        return "\n".join(L)

    def _beta_check(self, out, log=""):
        name = Path(out).name
        self.say("書き出した動画を自動チェックしています…(ベータ版)")
        self.easy_done.config(text=f"✔ 完成しました: {name}　／　自動チェック中…(1分ほど)", fg="#1f6f43")
        self.rbar.config(mode="indeterminate")
        self.rbar.start(12)
        cuts = sorted({round(float(v), 3) for x in self.segs for v in (x["en_start"], x["en_end"])})

        def work():
            try:
                issues, notes = output_check(out, cuts=cuts)
            except Exception as ex:  # noqa: BLE001
                issues, notes = None, [f"チェック中にエラー: {ex}"]
            self.q.put(lambda: done(issues, notes))

        def done(issues, notes):
            self.rbar.stop()
            self.rbar.config(mode="determinate", value=0)
            res = ("自動チェックはできませんでした" if issues is None
                   else f"自動チェック: おかしそうな所 {len(issues)} 件" if issues else "自動チェック: 問題は見つかりませんでした")
            self.easy_done.config(text=f"✔ 完成しました: {name}　／　{res}", fg="#b36b00" if issues else "#1f6f43")
            rep = self._beta_report(out, issues, notes, log)
            self._beta_rep = rep
            if not self.beta_btn.winfo_ismapped():
                self.beta_btn.pack(side="left", padx=4)
            if issues:
                self.say(f"合成完了 (自動チェック: おかしそうな所 {len(issues)} 件)")
                self._beta_show(rep, f"書き出した動画に、おかしそうな所が {len(issues)} 件ありました。\n"
                                     "下の文面をコピーして、そのまま Claude に貼ると原因を調べやすくなります。")
            else:
                self.say("合成完了 (自動チェック: 問題は見つかりませんでした)" if issues is not None else "合成完了 (自動チェックはできませんでした)")
        threading.Thread(target=work, daemon=True).start()

    def _beta_show(self, rep=None, head="書き出した動画の自動チェックの結果です。Claude に貼るときは「コピー」を押してください。"):
        rep = rep or getattr(self, "_beta_rep", "")
        w = tk.Toplevel(self)
        w.title("自動チェック (ベータ版)")
        w.geometry("760x520")
        ttk.Label(w, text=head, justify="left").pack(anchor="w", padx=10, pady=(10, 4))
        bf = ttk.Frame(w)
        bf.pack(side="bottom", fill="x", padx=10, pady=8)
        fr = ttk.Frame(w)
        fr.pack(fill="both", expand=True, padx=10)
        tx = tk.Text(fr, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(fr, orient="vertical", command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tx.pack(side="left", fill="both", expand=True)
        tx.insert("1.0", rep)
        note = ttk.Label(bf, text="")

        def copy():
            self.clipboard_clear()
            self.clipboard_append(tx.get("1.0", "end-1c"))
            note.config(text="コピーしました")
        ttk.Button(bf, text="コピー", command=copy).pack(side="left")
        note.pack(side="left", padx=8)
        ttk.Button(bf, text="閉じる", command=w.destroy).pack(side="right")

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
                if not update_is_newer(info):
                    if manual:
                        self.q.put(lambda: messagebox.showinfo("更新", f"最新版です ({ver_label()})"))
                    return
                code = download_update_source(info, ref)
                self.q.put(lambda: self._update_found(info, code))
            except Exception as ex:  # noqa: BLE001
                if manual:
                    self.q.put(lambda ex=ex: messagebox.showwarning("更新を確認できません", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def _restart(self, me):
        subprocess.Popen([sys.executable, str(me)], **NOWIN)
        self.destroy()

    def _update_found(self, info, code):
        notes = f"\n\n{info['notes']}" if info.get("notes") else ""
        if not messagebox.askyesno("更新", f"新しい版 {ver_label(info)} があります (現在 {ver_label()})。{notes}\n更新して再起動しますか?"):
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
        targets = {self: None, self.clip_en_ent: "clipen", self.find_en_frame: "findens", self.en2_tree: "findens", self.cm_tree: "findens", self.en_ent: "en", self.tr["en"]: "en", self.pv["en"]: "en",
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
        if k == "clipen":  # 切り出しタブの、英語版の入力欄
            self.clip_en.set(files[0])
            if not self.clip_out.get():
                self.clip_out.set(str(Path(files[0]).parent))
            return
        if k == "findens":  # 録画から探すタブの、英語版の欄: 英語版の一覧に追加(複数OK)
            for x in files:
                if Path(x).is_file() and not x.lower().endswith(".json"):
                    self._en2_put(x)
            self._en2_fill()
            return
        if self.nb.select() == str(self.findt):  # 録画から探すタブ: ファイルもフォルダも日本語の録画として追加
            self._find_add_files(files)
            return
        if self.nb.select() == str(self.clipt):  # 切り出しタブが開いているときは、どこにドロップしても日本語ファイルの追加
            self._clip_add_files(files)
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
        p = filedialog.askopenfilename(title="ファイルを選択", filetypes=MEDIA_TYPES)
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
        # 保存先の名前は日本語の録画に合わせる(自分で決めた保存先は、そのまま残す)
        cur = self.out_path.get()
        if not cur or cur == getattr(self, "_auto_out", None):
            pe, pj = Path(self.en_var.get()), Path(self.jp_var.get())
            out = pe.with_name(pj.stem + ".mkv")
            if out == pj or out == pe:   # 元の録画を上書きしないように
                out = pe.with_name(pj.stem + "_jpdub.mkv")
            self._auto_out = str(out)
            self.out_path.set(self._auto_out)
        self._busy(False)
        self.rebuild()
        self.say(f"読み込み完了  EN {m['en'].fps:.3f}fps / JP {m['jp'].fps:.3f}fps")
        self.set_active("en")
        self.refresh()
        for k in ("en", "jp"):
            self.update_preview(k)
        if getattr(self, "_auto_after_load", False):
            self._auto_after_load = False
            self.run_auto()
        else:
            self.pbar.config(value=0)
            self.auto_msg.config(text="読み込みました。「自動で合わせる」を押すと、自動で合わせます。\n(手動で合わせる場合は「手動調整」タブへ)", fg="#333")

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
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox)):
            return
        ks = ev.keysym
        k = self.active
        if isinstance(w, ttk.Treeview) and ks in ("Up", "Down", "Left", "Right"):
            return  # 一覧の操作中は矢印キーを一覧に任せる
        if ks in ("Left", "Right"):
            d = -1 if ks == "Left" else 1
            if ev.state & 0x1:
                self.step(k, seconds=d * 1.0)
            elif ev.state & 0x4:
                self.step(k, frames=d * 10)
            else:
                self.step(k, frames=d)
        elif ks == "Up":
            self.zoom_key(0.7)
        elif ks == "Down":
            self.zoom_key(1 / 0.7)
        elif ks == "Home":
            self.fit_all()
        elif ks == "s":
            self.add_point()
        elif ks == "a":
            self.auto_snap()
        elif ks == "Escape":
            self.clear_ranges()
        elif ks == "space":
            self.play(k)
        elif ks == "Tab":
            self.set_active("jp" if k == "en" else "en")
        return "break"

    # ---------- 拡大縮小 (Reaper風) ----------
    def zoom(self, k, f, anchor):
        """k のトラックを anchor の時刻を中心に拡大縮小。もう片方も同じ縮尺にして、カーソルの位置をそろえる。"""
        tr = self.tr[k]
        tr.zoom_by(f, anchor)
        o = self.tr["jp" if k == "en" else "en"]
        if o.media:
            frac = min(max((o.cursor - o.vs) / o.vspan, 0.1), 0.9) if o.vspan else 0.5
            o.vspan = tr.vspan
            o.vs = o.cursor - frac * o.vspan
            o.redraw()
        tr.redraw()

    def zoom_key(self, f):
        k = self.active
        self.zoom(k, f, self.tr[k].cursor)

    def fit_all(self):
        for k, t in self.tr.items():
            m = self.media[k]
            if m:
                t.vs = min(0.0, m.a_origin)
                t.vspan = max(m.end - t.vs, 1.0)
                t.redraw()

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
        """カーソル位置から約20秒の音を再生 (もう一度押すと停止)。"""
        if os.name == "nt" and time.time() < getattr(self, "_sound_until", 0):
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
            self._sound_until = 0
            self.say("再生を止めました")
            return
        if self.play_proc and self.play_proc.poll() is None:
            self.play_proc.terminate()
            self.play_proc = None
            self.say("再生を止めました")
            return
        if getattr(self, "_playing", False):
            self._playing = False
            self.say("再生を止めました")
            return
        m = self.media[k]
        if not m:
            return
        t = max(0.0, self.tr[k].cursor + m.voff)
        self.say("再生中… (もう一度押すと止まります)")
        wav = str(Path(tempfile.gettempdir()) / f"syncdub_play_{k}.wav")
        self._playing = True

        def work():
            try:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", "20", "-i", m.path,
                     "-map", f"0:a:{m.astream}", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", wav],
                    timeout=60, stdin=subprocess.DEVNULL)
                ok, err = True, ""
            except Exception as ex:  # noqa: BLE001
                ok, err = False, str(getattr(ex, "stderr", "") or ex)[-80:]
            self.q.put(lambda: self._play_ready(ok, wav, err))
        threading.Thread(target=work, daemon=True).start()

    def _play_ready(self, ok, wav, err):
        """再生用の音ができたあと、メインスレッドで再生を始める (別スレッドからの再生で固まるのを避ける)。"""
        playing, self._playing = self._playing, False
        if not playing:
            return  # 準備中にもう一度押された = 取り消し
        if not ok:
            self.say(f"再生用の音を作れませんでした: {err}")
            return
        try:
            if os.name == "nt":
                import winsound
                self._sound_until = time.time() + 20
                winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                self.play_proc = subprocess.Popen(["ffplay", "-v", "quiet", "-nodisp", "-autoexit", wav], **NOWIN)
        except Exception as ex:  # noqa: BLE001
            self.say(f"再生できませんでした: {ex}")

    # ---------- 指定 (同じ場面 / CM / 暗転・長さ違い / カット) ----------
    KIND_JA = {"pt": "同じ場面", "cm": "CM", "dim": "暗転・長さ違い", "cut": "カット"}

    def on_range(self, k):
        """波形をドラッグして範囲を選んだとき。"""
        self.set_active(k)
        self.update_sel_label()

    def update_sel_label(self):
        parts = []
        for k, name in (("en", "英語"), ("jp", "日本語")):
            r = self.tr[k].selr
            if r:
                parts.append(f"{name} {tc(r[0])[3:]} 〜 {tc(r[1])[3:]} ({r[1] - r[0]:.2f}秒)")
            self.tr[k].redraw()
        self.sel_lbl.config(text="選択中の範囲: " + (" / ".join(parts) if parts else "なし"))

    def clear_ranges(self):
        for t in self.tr.values():
            t.selr = None
            t.redraw()
        self.update_sel_label()

    def _unfocus(self):
        try:
            self.tr[self.active].focus_set()
        except Exception:  # noqa: BLE001
            pass

    def add_point(self):
        if not all(self.media.values()):
            self.say("先に映像と録画を読み込んでください")
            return
        te, tj = self.tr["en"].cursor, self.tr["jp"].cursor
        if self.refine_v.get():
            r = xcorr_find(self.media["en"], te, self.media["jp"], tj, 2.0, 0.3)
            if r and r[2] > 8:
                tj = r[0]
                self.set_cursor("jp", tj)
        if any(i["k"] == "pt" and abs(i["en"] - te) < 0.02 and abs(i["jp"] - tj) < 0.02 for i in self.items):
            self.say("同じ場所はすでに登録されています")
            return
        self.items.append({"k": "pt", "en": te, "jp": tj, "note": ""})
        self.auto_flags = None
        self.rebuild()
        self.say(f"『同じ場面』を登録: 英語 {tc(te)[3:]} = 日本語 {tc(tj)[3:]}")

    def _need(self, k, what):
        r = self.tr[k].selr
        if not r:
            self.say(f"先に{what}の波形をドラッグして範囲を選んでください")
        return r

    def add_cm(self):
        r = self._need("jp", "日本語(下)")
        if not r:
            return
        self.items.append({"k": "cm", "jp0": r[0], "jp1": r[1]})
        self.after_add("日本語の選択範囲を CM にしました")

    def add_cut(self):
        r = self._need("en", "英語(上)")
        if not r:
            return
        ans = self.ask_choice("カットされた場面の音声",
                              "この範囲は日本語版でカットされています。\nこの間の音声はどうしますか?",
                              [("silence", "無音にする"), ("english", "英語版の音声をそのまま使う")])
        if not ans:
            return
        self.items.append({"k": "cut", "en0": r[0], "en1": r[1], "audio": ans})
        self.after_add("英語の選択範囲を カット にしました")

    def add_dim(self):
        re_, rj = self.tr["en"].selr, self.tr["jp"].selr
        if not (re_ and rj):
            self.say("英語(上)と日本語(下)の両方で、同じ場面の範囲をドラッグして選んでください")
            return
        ans = self.ask_choice("長さの違いの扱い",
                              f"英語 {re_[1] - re_[0]:.2f}秒 ↔ 日本語 {rj[1] - rj[0]:.2f}秒\n長さが違う分はどうしますか?",
                              [("stretch", "音の速さを少し変えて合わせる"), ("pad", "足りない所は無音で埋める"),
                               ("trim", "余った所は切る")])
        if not ans:
            return
        self.items.append({"k": "dim", "en0": re_[0], "en1": re_[1], "jp0": rj[0], "jp1": rj[1], "fit": ans})
        self.after_add("長さが違う場面を登録しました")

    def after_add(self, msg):
        self.auto_flags = None
        self.clear_ranges()
        self.rebuild()
        self.say(msg)

    def ask_choice(self, title, text, choices):
        d = tk.Toplevel(self)
        d.title(title)
        d.transient(self)
        d.grab_set()
        tk.Label(d, text=text, justify="left").pack(padx=12, pady=8)
        res = {"v": None}
        for val, lab in choices:
            ttk.Button(d, text=lab, width=34, command=lambda v=val: (res.__setitem__("v", v), d.destroy())).pack(padx=12, pady=2)
        ttk.Button(d, text="キャンセル", command=d.destroy).pack(pady=8)
        self.wait_window(d)
        return res["v"]

    # ---------- 指定 → 区間 ----------
    def rebuild(self, track=True):
        import copy
        if track and self.items != self._last:
            self.undo_st.append(self._last)
            del self.undo_st[:-100]
            self.redo_st.clear()
        self._last = copy.deepcopy(self.items)
        m = self.media
        if all(m.values()):
            en_rng = (min(0.0, m["en"].a_origin), m["en"].end)
            jp_rng = (min(0.0, m["jp"].a_origin), m["jp"].end)
        else:
            en_rng = jp_rng = (0.0, 1e6)
        if self.items and all(m.values()):
            self.segs, self.extra_segs, self.warns = build_from_items(self.items, en_rng, jp_rng)
        else:
            self.segs, self.extra_segs, self.warns = [], [], []
        self.refresh()

    def refresh(self):
        # 一覧
        self.itree.delete(*self.itree.get_children())
        for i, it in enumerate(self.items):
            k = it["k"]
            if k == "pt":
                en, jp, note = tc(it["en"]), tc(it["jp"]), "この2つは同じ場面"
            elif k == "cm":
                en, jp, note = "—", f"{tc(it['jp0'])[3:]} 〜 {tc(it['jp1'])[3:]}", f"CM ({it['jp1'] - it['jp0']:.1f}秒) は使わない"
            elif k == "cut":
                au = "英語版の音声を使う" if it.get("audio") == "english" else "無音"
                en, jp, note = f"{tc(it['en0'])[3:]} 〜 {tc(it['en1'])[3:]}", "—", f"日本語版でカット ({it['en1'] - it['en0']:.1f}秒) / 音声: {au}"
            else:
                en = f"{tc(it['en0'])[3:]} 〜 {tc(it['en1'])[3:]}"
                jp = f"{tc(it['jp0'])[3:]} 〜 {tc(it['jp1'])[3:]}"
                note = (f"長さ違い: 英語 {it['en1'] - it['en0']:.2f}秒 ↔ 日本語 {it['jp1'] - it['jp0']:.2f}秒 "
                        f"({FIT_JA.get(it.get('fit'), '')})")
            self.itree.insert("", "end", iid=str(i), tags=(k,), values=(self.KIND_JA[k], en, jp, note))
        more = f"  (ほか {len(self.warns) - 1} 件)" if len(self.warns) > 1 else ""
        self.warn_lbl.config(text=("⚠ " + self.warns[0] + more) if self.warns else "")
        # 波形上の表示
        en_pts, jp_pts, n = [], [], 0
        for it in self.items:
            if it["k"] == "pt":
                n += 1
                en_pts.append((it["en"], str(n)))
                jp_pts.append((it["jp"], str(n)))
            elif it["k"] == "dim":
                n += 1
                en_pts += [(it["en0"], f"{n}a"), (it["en1"], f"{n}b")]
                jp_pts += [(it["jp0"], f"{n}a"), (it["jp1"], f"{n}b")]
        te, tj = self.tr["en"], self.tr["jp"]
        te.pts, tj.pts = en_pts, jp_pts
        te.marks = [(min(i["en0"], i["en1"]), max(i["en0"], i["en1"]), i["k"]) for i in self.items if i["k"] in ("cut", "dim")]
        tj.marks = [(min(i["jp0"], i["jp1"]), max(i["jp0"], i["jp1"]), i["k"]) for i in self.items if i["k"] in ("cm", "dim")]
        te.bands = [(s["en_start"], s["en_end"], i) for i, s in enumerate(self.segs)]
        tj.bands = [(s["jp_start"], s["jp_end"] if s["jp_end"] is not None else s["jp_start"] + s["en_end"] - s["en_start"], i)
                    for i, s in enumerate(self.segs)]
        for t in self.tr.values():
            t.sel = None
            t.redraw()
        self.refresh_easy()

    def undo(self):
        if not self.undo_st:
            self.say("これ以上、元に戻せません")
            return
        self.redo_st.append(self._last)
        self.items = self.undo_st.pop()
        self.auto_flags = None
        self.rebuild(track=False)
        self.say("元に戻しました (Ctrl+Z) / やり直し: Ctrl+Y")

    def redo(self):
        if not self.redo_st:
            self.say("やり直す操作がありません")
            return
        self.undo_st.append(self._last)
        self.items = self.redo_st.pop()
        self.auto_flags = None
        self.rebuild(track=False)
        self.say("やり直しました (Ctrl+Y)")

    def item_idx(self):
        s = self.itree.selection()
        return int(s[0]) if s else None

    def on_item_select(self, _):
        if len(self.itree.selection()) != 1:
            return
        i = self.item_idx()
        if i is None or not self.media["en"]:
            return
        it = self.items[i]
        if it["k"] == "pt":
            self.set_cursor("en", it["en"], scroll=True)
            self.set_cursor("jp", it["jp"], scroll=True)
        elif it["k"] == "cm":
            self.set_cursor("jp", it["jp0"], scroll=True)
        elif it["k"] == "cut":
            self.set_cursor("en", it["en0"], scroll=True)
        else:
            self.set_cursor("en", it["en0"], scroll=True)
            self.set_cursor("jp", it["jp0"], scroll=True)

    def del_item(self):
        sel = sorted((int(x) for x in self.itree.selection()), reverse=True)
        if not sel:
            return
        for i in sel:
            del self.items[i]
        self.auto_flags = None
        self.rebuild()
        self.say(f"{len(sel)}件を削除しました (Ctrl+Z で元に戻せます)")

    def clear_items(self):
        if not self.items:
            return
        if not messagebox.askyesno("全部削除", f"指定 {len(self.items)} 件をすべて削除しますか?\n(Ctrl+Z で元に戻せます)"):
            return
        self.items = []
        self.auto_flags = None
        self.rebuild()

    def edit_item(self):
        i = self.item_idx()
        if i is None:
            return
        it = self.items[i]
        keys = {"pt": [("英語の時刻", "en", "en"), ("日本語の時刻", "jp", "jp")],
                "cm": [("CM 開始 (日本語)", "jp0", "jp"), ("CM 終了 (日本語)", "jp1", "jp")],
                "cut": [("カット 開始 (英語)", "en0", "en"), ("カット 終了 (英語)", "en1", "en")],
                "dim": [("英語 開始", "en0", "en"), ("英語 終了", "en1", "en"),
                        ("日本語 開始", "jp0", "jp"), ("日本語 終了", "jp1", "jp")]}[it["k"]]
        d = tk.Toplevel(self)
        d.title(f"{self.KIND_JA[it['k']]} を編集")
        d.transient(self)
        vs = {}
        for r, (lab, key, tk_) in enumerate(keys):
            ttk.Label(d, text=lab).grid(row=r, column=0, sticky="w", padx=6, pady=3)
            v = tk.StringVar(value=tc(it[key]))
            ttk.Entry(d, textvariable=v, width=18).grid(row=r, column=1, padx=6)
            vs[key] = (v, tk_)

        def ok():
            try:
                new = dict(it)
                for key, (v, tk_) in vs.items():
                    new[key] = parse_tc(v.get().strip(), self.fps(tk_))
            except ValueError as ex:
                messagebox.showerror("入力エラー", str(ex), parent=d)
                return
            self.items[i] = new
            d.destroy()
            self.auto_flags = None
            self.rebuild()
        ttk.Button(d, text="OK", command=ok).grid(row=len(keys), column=0, columnspan=2, pady=8)

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
        off = 0.0
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

    # ---------- プロジェクト入出力 ----------
    def project_dict(self):
        segs = []
        for sg in self.segs:
            d = {"en_start": round(sg["en_start"], 4), "en_end": round(sg["en_end"], 4),
                 "jp_start": round(sg["jp_start"], 4), "fit": sg["fit"]}
            if sg["jp_end"] is not None:
                d["jp_end"] = round(sg["jp_end"], 4)
            segs.append(d)
        for e in self.extra_segs:
            segs.append({"type": "english", "en_start": round(e["en_start"], 4), "en_end": round(e["en_end"], 4)})
        segs.sort(key=lambda x: x["en_start"])
        return {"en_video": self.en_var.get(), "jp_media": self.jp_var.get(),
                "jp_audio_stream": self.jp_ast.get(), "output": self.out_path.get(),
                "keep_en_audio": True, "audio_codec": "aac", "audio_bitrate": "256k",
                "segments": segs, "items": self.items}

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
        self.items = d.get("items", [])
        self.auto_flags = None
        self.rebuild()
        if Path(self.en_var.get()).exists() and Path(self.jp_var.get()).exists():
            self.say("設定を読み込みました。素材の波形を読み込んでいます…")
            self._auto_after_load = False
            self.load_media()
        else:
            self.say("設定を読み込みました。素材のファイルが見つかりません。パスを確かめて「読み込む」を押してください")

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
        if out.exists():      # 前に作った動画をプレーヤーなどで開いたままだと、上書きできずに失敗する → 始める前に知らせる
            try:
                with open(out, "r+b"):
                    pass
            except OSError:
                messagebox.showwarning("保存先を使えません",
                                       f"{out.name} は、ほかのアプリ(動画プレーヤーなど)で開かれているため、上書きできません。\n\n"
                                       "そのアプリを閉じてからもう一度「合成スタート」を押すか、「変更」で別の名前にしてください。")
                return
        # 設定ファイルは一時フォルダに作り、合成が終わったら消す(出力の隣にゴミを残さない)
        tmpd = Path(tempfile.mkdtemp(prefix="syncdub_"))
        proj = str(tmpd / "project.json")
        Path(proj).write_text(json.dumps(self.project_dict(), ensure_ascii=False), encoding="utf-8")
        self._busy(True)
        self.rbar.config(mode="indeterminate")
        self.rbar.start(12)
        self.open_btn.pack_forget()
        self.beta_btn.pack_forget()
        self.easy_done.config(text="合成しています…(動画の長さによって数分かかります)", fg="#333")
        self.say("合成中…")
        self.logw(f"--- 合成開始: {out.name}")

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
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)
            self.q.put(lambda: self._render_done(buf.getvalue(), ok))
        threading.Thread(target=work, daemon=True).start()

    def _render_done(self, text, ok):
        self.logw(text)
        self.rbar.stop()
        self.rbar.config(mode="determinate", value=0)  # 止まっているときは空にする (緑のブロックを残さない)
        self._busy(False)
        if ok:
            self.easy_done.config(text=f"✔ 完成しました: {Path(self.out_path.get()).name}", fg="#1f6f43")
            self.open_btn.pack(side="left", padx=8)
            if BETA_REVIEW:
                self._beta_check(self.out_path.get(), text)
        else:
            self.easy_done.config(text="⚠ 合成に失敗しました (詳細は「手動調整」タブ下のログ)", fg="#b33")
            if "Permission denied" in text:
                messagebox.showerror("合成に失敗", "保存先に書き込めませんでした。\n\n"
                                     "同じ名前の動画をほかのアプリ(動画プレーヤーなど)で開いていないか、"
                                     "保存先のフォルダに書き込めるかを確かめてください。「変更」で別の名前や場所にもできます。")
            else:
                messagebox.showerror("合成に失敗", text[-600:])
        self.say("合成完了" if ok else "合成に失敗しました")


if __name__ == "__main__":
    set_taskbar_identity()
    app = App()
    app.after(500, lambda: set_window_taskbar_identity(app))
    if len(sys.argv) > 1:
        app.load_project(sys.argv[1])
    app.mainloop()

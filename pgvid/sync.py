"""Synchronise clips to IGC time.

Action cameras without GPS often have an unset or drifting clock, so file
timestamps can't be trusted. Two complementary signals are used:

1. **Instrument clock OCR** (pgvid.ocr, optional): the flight instrument screen
   in frame shows the local time. Consistent readings give the clip start
   time-of-day exactly.
2. **Gyro vs. IGC turn-rate cross-correlation**: camera yaw rate (gyro projected
   on the low-passed accelerometer "up" vector) against the IGC turn rate.
   Used to pick the day for OCR-anchored clips and to place clips without a
   readable clock (or when OCR wasn't run at all).

A global dynamic programme picks one candidate (day, start) per clip in
recording order so that clips never overlap and time always moves forward.
GoPro chapters (GX02xxxx after GX01xxxx) are kept contiguous. Clips that cannot
be matched are placed between their neighbours and flagged low confidence.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import fftconvolve

from .config import Project
from .igc import Track, load_tracks
from .index import load_index, load_telemetry

CLIP_MAX = 45.0   # deg/s clamp to limit head-turn spikes
TOPK = 6
MIN_SEP = 40      # s between candidate peaks
OCR_BONUS = 25.0  # score for an OCR-anchored placement (dominates xcorr)


@dataclass
class ClipSync:
    clip: str
    day: int | None          # index into tracks (0-based) or None
    date: str | None
    start_epoch: float | None
    duration: float
    score: float
    corr: float              # Pearson r of gyro/IGC at the chosen placement
    method: str              # "ocr" | "xcorr" | "chapter" | "interp" | "none"
    confidence: str          # high | medium | low
    ocr_n: int = 0
    ocr_spread: float = 0.0


def clip_yaw_1hz(tel: dict) -> np.ndarray:
    if "yaw" not in tel:
        return np.zeros(0)
    y = np.degrees(tel["yaw"])
    n = len(y) // 10
    y = y[: n * 10].reshape(n, 10).mean(1)
    return np.clip(uniform_filter1d(y, 5), -CLIP_MAX, CLIP_MAX)


def track_turn(tr: Track) -> np.ndarray:
    turn = np.clip(tr.turn, -CLIP_MAX, CLIP_MAX).copy()
    turn[: tr.takeoff_idx] = 0
    turn[tr.landing_idx:] = 0
    return turn


def ncc(clip: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Pearson correlation of `clip` against every window of `ref` (valid mode)."""
    L = len(clip)
    if L < 5 or len(ref) < L:
        return np.zeros(0)
    c = clip - clip.mean()
    cn = np.linalg.norm(c)
    if cn < 1e-6:
        return np.zeros(len(ref) - L + 1)
    num = fftconvolve(ref, c[::-1], mode="valid")
    cs = np.concatenate([[0], np.cumsum(ref)])
    cs2 = np.concatenate([[0], np.cumsum(ref * ref)])
    s = cs[L:] - cs[:-L]
    s2 = cs2[L:] - cs2[:-L]
    var = np.maximum(s2 - s * s / L, 1e-6)
    r = num / (cn * np.sqrt(var))
    r[var < L * 4.0] = 0  # ignore windows with essentially no turning
    return r


def _peaks(r: np.ndarray, k: int, sep: int) -> list[int]:
    r = r.copy()
    out = []
    for _ in range(k):
        i = int(np.argmax(r))
        if r[i] <= 0:
            break
        out.append(i)
        r[max(0, i - sep): i + sep] = -np.inf
    return out


def _z(r: float, L: int) -> float:
    return float(r * np.sqrt(max(L / 8.0, 1.0)))


def ocr_anchor(readings: list[dict], tol: float = 3.0) -> tuple[float, int, float] | None:
    """Return (start time-of-day seconds, n consistent readings, spread) or None."""
    if not readings:
        return None
    starts = np.array([r["clock"] - r["t"] for r in readings])
    best = None
    for s0 in starts:
        m = np.abs(starts - s0) <= tol
        if best is None or m.sum() > best[1].sum():
            best = (s0, m)
    s0, m = best
    sel = starts[m]
    n = int(m.sum())
    # need 2+ agreeing readings, or 1 reading if it is the only one (validated later by xcorr)
    if n < 2 and len(starts) > 1:
        return None
    return float(np.median(sel)), n, float(sel.max() - sel.min())


def _groups(clips: list[str]) -> list[list[str]]:
    """Group GoPro chapters of the same recording: GX01nnnn, GX02nnnn, ..."""
    groups: dict[str, list[str]] = {}
    order = []
    for c in clips:
        key = c[4:8]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    return [sorted(groups[k], key=lambda c: c[2:4]) for k in order]


def compute(prj: Project) -> list[ClipSync]:
    index = load_index(prj)
    tracks = load_tracks(prj.igc_dir)
    refs = [track_turn(tr) for tr in tracks]
    tz = dt.timezone(dt.timedelta(hours=prj["utc_offset_hours"]))
    midnights = [dt.datetime(tr.date.year, tr.date.month, tr.date.day, tzinfo=tz).timestamp() for tr in tracks]
    ocr_path = prj.work("ocr.json")
    ocr = json.loads(ocr_path.read_text()) if ocr_path.exists() else {}

    clips = [n for n, e in sorted(index.items(), key=lambda kv: kv[1]["order"]) if not e["error"]]
    groups = _groups(clips)
    durs = {n: index[n]["meta"]["duration"] for n in clips}

    # per group: concatenated yaw, total duration, OCR anchor (in group time)
    G = []
    for g in groups:
        yaw = np.concatenate([clip_yaw_1hz(load_telemetry(prj, c)) for c in g])
        offs = np.cumsum([0.0] + [durs[c] for c in g[:-1]])
        readings = [dict(r, t=r["t"] + off) for c, off in zip(g, offs) for r in ocr.get(c, [])]
        G.append({"clips": g, "yaw": yaw, "dur": float(sum(durs[c] for c in g)), "offs": offs,
                  "anchor": ocr_anchor(readings)})

    best_total, best = -np.inf, None
    for sign in (1.0, -1.0):
        cands = []
        for gi, g in enumerate(G):
            y = sign * g["yaw"]
            lst = []
            rs = [ncc(y, ref) for ref in refs]
            if g["anchor"]:
                tod, n, spread = g["anchor"]
                for d, tr in enumerate(tracks):
                    start = midnights[d] + tod
                    k = int(round(start - tr.t[0]))
                    r = float(rs[d][k]) if rs[d].size and 0 <= k < rs[d].size else 0.0
                    lst.append((start, OCR_BONUS + _z(r, len(y)), d, r, "ocr"))
            else:
                for d, tr in enumerate(tracks):
                    if rs[d].size == 0:
                        continue
                    for i in _peaks(rs[d], TOPK, MIN_SEP):
                        z = _z(rs[d][i], len(y))
                        if z >= 2.0:
                            lst.append((float(tr.t[0] + i), z, d, float(rs[d][i]), "xcorr"))
            cands.append(lst)
        # DP with Pareto pruning on (end time, score)
        states = [(0.0, -np.inf, [])]
        for gi, g in enumerate(G):
            new = []
            for tot, end, path in states:
                new.append((tot, end, path + [None]))
                for c in cands[gi]:
                    if c[0] >= end - 2:
                        new.append((tot + c[1], c[0] + g["dur"], path + [c]))
            new.sort(key=lambda x: (x[1], -x[0]))
            pr, bs = [], -np.inf
            for st in new:
                if st[0] > bs + 1e-9:
                    pr.append(st)
                    bs = st[0]
            states = pr
        top = max(states, key=lambda x: x[0])
        print(f"  sign {sign:+.0f}: total {top[0]:.1f}, placed {sum(p is not None for p in top[2])}/{len(G)} recordings")
        if top[0] > best_total:
            best_total, best = top[0], (sign, top[2])

    sign, path = best
    out: list[ClipSync] = []
    for g, c in zip(G, path):
        for j, name in enumerate(g["clips"]):
            if c is None:
                out.append(ClipSync(name, None, None, None, durs[name], 0.0, 0.0, "none", "low"))
                continue
            start, s, d, r, method = c
            if method == "ocr":
                n = g["anchor"][1]
                conf = "high" if n >= 2 else "medium"
            else:
                conf = "medium" if s > 5 else "low"
            out.append(ClipSync(name, d, str(tracks[d].date), start + float(g["offs"][j]), durs[name],
                                round(s, 2), round(r, 3), method if j == 0 else "chapter", conf,
                                g["anchor"][1] if g["anchor"] else 0,
                                round(g["anchor"][2], 2) if g["anchor"] else 0.0))
    _interpolate(out, tracks)
    prj.work("sync.json").write_text(json.dumps({"sign": sign, "clips": [asdict(c) for c in out]}, indent=1))
    return out


def _interpolate(out: list[ClipSync], tracks: list[Track]) -> None:
    """Place unmatched clips between matched neighbours."""
    placed = lambda c: c.method not in ("none", "interp")  # noqa: E731
    for i, c in enumerate(out):
        if c.method != "none":
            continue
        prev = next((out[j] for j in range(i - 1, -1, -1) if placed(out[j])), None)
        nxt = next((out[j] for j in range(i + 1, len(out)) if placed(out[j])), None)
        if prev and nxt and prev.day == nxt.day:
            c.day, c.date = prev.day, prev.date
            lo = prev.start_epoch + prev.duration
            c.start_epoch = lo + max(0.0, (nxt.start_epoch - lo - c.duration) / 2)
        elif nxt and (prev is None or prev.day != nxt.day):
            tr = tracks[nxt.day]
            c.day, c.date = nxt.day, nxt.date
            c.start_epoch = min(nxt.start_epoch, tr.takeoff) - c.duration - 30
        elif prev:
            c.day, c.date = prev.day, prev.date
            c.start_epoch = prev.start_epoch + prev.duration + 30
        else:
            continue
        c.method, c.confidence = "interp", "low"


def load_sync(prj: Project) -> dict[str, dict]:
    j = json.loads(prj.work("sync.json").read_text())
    return {c["clip"]: c for c in j["clips"]}


def run(prj: Project, force: bool = False) -> None:
    tz = dt.timezone(dt.timedelta(hours=prj["utc_offset_hours"]))
    tracks = load_tracks(prj.igc_dir)
    res = compute(prj)
    for c in res:
        ts = dt.datetime.fromtimestamp(c.start_epoch, tz).strftime("%m-%d %H:%M:%S") if c.start_epoch else "-"
        phase = ""
        if c.start_epoch is not None and c.day is not None:
            tr = tracks[c.day]
            mid = c.start_epoch + c.duration / 2
            phase = "ground-pre" if mid < tr.takeoff else "ground-post" if mid > tr.landing else "flight"
        print(f"{c.clip} {c.duration:7.1f}s  {ts}  {c.method:7s} {c.confidence:6s} z={c.score:5.1f} "
              f"r={c.corr:+.2f} ocr={c.ocr_n} {phase}")

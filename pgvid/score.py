"""Highlight scoring.

Builds a 1 Hz feature timeline per clip (telemetry + visual + audio + YOLO
gliders + synced IGC flight data), computes per-category scores, and extracts
the best non-overlapping, non-duplicate segments per day and category.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

from .analyze import load_gliders, load_visual
from .config import Project
from .igc import load_tracks
from .index import load_index, load_telemetry
from .sync import load_sync

CATEGORIES = ("launch", "gaggle", "close_pass", "strong_thermal", "fast_glide",
              "clouds", "scenery", "goal_landing")


def _to_1hz(t: np.ndarray, v: np.ndarray, n: int) -> np.ndarray:
    if len(t) == 0:
        return np.zeros(n)
    idx = np.clip(t.astype(int), 0, n - 1)
    s = np.bincount(idx, weights=v.astype(float), minlength=n)
    c = np.bincount(idx, minlength=n)
    out = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    m = ~np.isnan(out)
    return np.interp(np.arange(n), np.flatnonzero(m), out[m]) if m.any() else np.zeros(n)


def build_timeline(prj: Project, clip: str, sync: dict, tracks) -> dict:
    idx = load_index(prj)[clip]
    n = int(idx["meta"]["duration"])
    tl: dict[str, np.ndarray] = {"t": np.arange(n, dtype=float)}
    tel = load_telemetry(prj, clip)
    for k in ("yaw", "shake", "jerk", "gyro_mag"):
        if k in tel:
            tl[k] = _to_1hz(tel["t"], np.abs(tel[k]) if k == "yaw" else tel[k], n)
    vis = load_visual(prj, clip)
    for k in ("bright", "contrast", "sat", "sky", "cloud", "dark", "green", "flow", "blobs", "sharp",
              "rms", "beep", "wind"):
        if k in vis:
            tl[k] = _to_1hz(vis["t"], vis[k], n)
    gl = load_gliders(prj, clip)
    if gl is not None:
        tl["gliders"] = _to_1hz(gl["t"], gl["n"], n)
        tl["glider_area"] = _to_1hz(gl["t"], gl["area"], n)
    else:
        tl["gliders"] = tl.get("blobs", np.zeros(n)) * 0.5
        tl["glider_area"] = np.zeros(n)
    # synced IGC data
    s = sync.get(clip)
    tl["epoch"] = np.full(n, np.nan)
    for k in ("alt", "vario", "speed", "turn", "since_takeoff", "to_landing", "alt_rel"):
        tl[k] = np.full(n, np.nan)
    if s and s["start_epoch"] is not None and s["day"] is not None:
        tr = tracks[s["day"]]
        ep = s["start_epoch"] + tl["t"]
        tl["epoch"] = ep
        inside = (ep >= tr.t[0]) & (ep <= tr.t[-1])
        for k in ("alt", "vario", "speed", "turn"):
            v = tr.at(ep, k)
            tl[k] = np.where(inside, v, np.nan)
        tl["since_takeoff"] = ep - tr.takeoff
        tl["to_landing"] = tr.landing - ep
        fl = slice(tr.takeoff_idx, tr.landing_idx)
        amin, amax = np.percentile(tr.alt[fl], 5), np.max(tr.alt[fl])
        tl["alt_rel"] = np.where(inside, (tl["alt"] - amin) / max(amax - amin, 1), np.nan)
        tl["day"] = np.full(n, s["day"], dtype=float)
        tl["sync_ok"] = np.full(n, 1.0 if s["confidence"] in ("high", "medium") else 0.0)
    else:
        tl["day"] = np.full(n, -1.0)
        tl["sync_ok"] = np.zeros(n)
    return tl


class Norm:
    """Corpus-wide robust 0..1 normalisation per feature."""

    def __init__(self, tls: list[dict]):
        self.p = {}
        for k in tls[0]:
            vals = np.concatenate([tl[k] for tl in tls if k in tl])
            vals = vals[np.isfinite(vals)]
            if vals.size:
                self.p[k] = (np.percentile(vals, 5), np.percentile(vals, 95))

    def __call__(self, tl: dict, k: str) -> np.ndarray:
        if k not in tl or k not in self.p:
            return np.zeros(len(tl["t"]))
        lo, hi = self.p[k]
        v = (tl[k] - lo) / max(hi - lo, 1e-9)
        return np.clip(np.nan_to_num(v, nan=0.0), 0, 1)


def category_scores(tl: dict, N: Norm) -> dict[str, np.ndarray]:
    n = len(tl["t"])
    nz = lambda k: np.nan_to_num(tl[k], nan=0.0) if k in tl else np.zeros(n)  # noqa: E731
    flying = np.isfinite(tl["alt"]) & (nz("since_takeoff") > 0) & (nz("to_landing") > 0)
    vario, speed, turn = nz("vario"), nz("speed"), np.abs(nz("turn"))
    quality = (1 - np.clip((nz("dark") - 0.25) / 0.35, 0, 1)) * (0.5 + 0.5 * N(tl, "sharp")) \
        * (1 - 0.6 * N(tl, "shake"))
    sc = {}
    # launch: last ~60 s before takeoff to 40 s after, or ground clips with lots of motion
    st = tl["since_takeoff"]
    near_to = np.isfinite(st) & (st > -45) & (st < 40) & (tl.get("sync_ok", np.ones(n)) > 0)
    sc["launch"] = near_to * (0.6 + 0.4 * N(tl, "jerk"))
    sc["gaggle"] = flying * (0.65 * np.clip(nz("gliders") / 8, 0, 1) + 0.2 * np.clip(nz("blobs") / 10, 0, 1)
                             + 0.15 * (turn > 8))
    sc["close_pass"] = flying * np.clip(nz("glider_area") / 0.02, 0, 1) * (nz("gliders") > 0)
    sc["strong_thermal"] = flying * np.clip((vario - 1.5) / 3.0, 0, 1) * (0.6 + 0.4 * (turn > 8)) \
        * (0.7 + 0.3 * np.clip(nz("gliders") / 5, 0, 1))
    sc["fast_glide"] = flying * np.clip((speed - 35) / 25, 0, 1) * (turn < 6) * (0.5 + 0.5 * N(tl, "flow"))
    sc["clouds"] = flying * (0.55 * N(tl, "cloud") + 0.25 * np.nan_to_num(tl["alt_rel"]) + 0.2 * N(tl, "contrast"))
    sc["scenery"] = flying * (0.35 * N(tl, "contrast") + 0.25 * N(tl, "sat") + 0.2 * N(tl, "green")
                              + 0.2 * np.clip(1 - np.abs(nz("sky") - 0.35) / 0.35, 0, 1))
    tlnd = tl["to_landing"]
    sc["goal_landing"] = np.isfinite(tlnd) * ((tlnd > -20) & (tlnd < 100)) * (0.6 + 0.4 * N(tl, "jerk"))
    for k in sc:
        sc[k] = uniform_filter1d(sc[k].astype(float) * quality, 3)
    sc["quality"] = quality
    return sc


def _hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def extract_segments(prj: Project, tls: dict[str, dict], scs: dict[str, dict], win: int = 5,
                     per_cat_day: int = 8, sep: int = 25) -> list[dict]:
    segs = []
    for clip, sc in scs.items():
        tl = tls[clip]
        n = len(tl["t"])
        if n < win:
            continue
        vis = load_visual(prj, clip)
        for cat in CATEGORIES:
            s = np.convolve(sc[cat], np.ones(win) / win, mode="valid")
            used = np.zeros_like(s, dtype=bool)
            for _ in range(4):
                i = int(np.argmax(np.where(used, -1, s)))
                if s[i] <= 0.15:
                    break
                used[max(0, i - sep): i + sep] = True
                mid = i + win / 2
                ph = int(vis["phash"][min(int(mid * 2), len(vis["phash"]) - 1)])
                segs.append({
                    "clip": clip, "start": float(i), "dur": float(win), "category": cat,
                    "score": round(float(s[i]), 3), "day": int(tl["day"][0]),
                    "epoch": float(tl["epoch"][i]) if np.isfinite(tl["epoch"][i]) else None,
                    "alt": _f(tl["alt"][i:i + win]), "vario": _f(tl["vario"][i:i + win]),
                    "speed": _f(tl["speed"][i:i + win]), "gliders": _f(tl["gliders"][i:i + win]),
                    "phash": str(ph),
                })
    # rank per (day, category) with dedup
    out = []
    for day in sorted({s["day"] for s in segs}):
        for cat in CATEGORIES:
            cand = sorted([s for s in segs if s["day"] == day and s["category"] == cat],
                          key=lambda s: -s["score"])
            chosen = []
            for s in cand:
                if any(_hamming(s["phash"], c["phash"]) < 8 for c in chosen):
                    continue
                chosen.append(s)
                if len(chosen) >= per_cat_day:
                    break
            out.extend(chosen)
    return out


def _f(a: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    return round(float(a.mean()), 2) if a.size else None


def apply_pins(prj: Project, segs: list[dict], tls: dict[str, dict]) -> list[dict]:
    """Project YAML `pins:` force segments in; `exclude:` drops clip ranges.

    pins:    [{clip: GX010262.MP4, start: 4, dur: 30, category: launch, score: 1.0}]
    exclude: [{clip: GX010212.MP4, start: 0, end: 61}]
    """
    for ex in prj.get("exclude", []) or []:
        segs = [s for s in segs if not (s["clip"] == ex["clip"] and s["start"] < ex.get("end", 1e9)
                                        and s["start"] + s["dur"] > ex.get("start", 0))]
    for p in prj.get("pins", []) or []:
        tl = tls[p["clip"]]
        i, d = int(p["start"]), int(p["dur"])
        vis = load_visual(prj, p["clip"])
        ph = int(vis["phash"][min(int((i + d / 2) * 2), len(vis["phash"]) - 1)])
        segs.append({"clip": p["clip"], "start": float(i), "dur": float(d), "category": p["category"],
                     "score": float(p.get("score", 1.0)), "day": int(tl["day"][0]),
                     "epoch": float(tl["epoch"][i]) if np.isfinite(tl["epoch"][i]) else None,
                     "alt": _f(tl["alt"][i:i + d]), "vario": _f(tl["vario"][i:i + d]),
                     "speed": _f(tl["speed"][i:i + d]), "gliders": _f(tl["gliders"][i:i + d]),
                     "phash": str(ph), "pinned": True, "note": p.get("note", "")})
    return segs


def run(prj: Project, force: bool = False) -> None:
    index = load_index(prj)
    sync = load_sync(prj)
    tracks = load_tracks(prj.igc_dir)
    clips = [n for n, e in sorted(index.items(), key=lambda kv: kv[1]["order"]) if not e["error"]]
    tls = {c: build_timeline(prj, c, sync, tracks) for c in clips}
    N = Norm(list(tls.values()))
    scs = {c: category_scores(tls[c], N) for c in clips}
    for c in clips:
        np.savez_compressed(prj.work("timeline", Path(c).stem + ".npz"), **tls[c],
                            **{f"score_{k}": v for k, v in scs[c].items()})
    segs = extract_segments(prj, tls, scs)
    segs = apply_pins(prj, segs, tls)
    prj.work("segments.json").write_text(json.dumps(segs, indent=1))
    print(f"{len(segs)} candidate segments")
    for cat in CATEGORIES:
        cs = [s for s in segs if s["category"] == cat]
        print(f"  {cat:15s} {len(cs):3d}  best {max([s['score'] for s in cs], default=0):.2f}")

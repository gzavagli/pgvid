"""Footage indexing: ffprobe metadata + GPMF telemetry reduced to 10 Hz features."""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

from . import gpmf
from .config import Project

FS = 10.0  # Hz for reduced telemetry


def probe(path: Path) -> dict | None:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        return None
    num, den = map(int, v["r_frame_rate"].split("/"))
    return {"duration": float(j["format"]["duration"]), "width": v["width"], "height": v["height"],
            "fps": num / den, "codec": v["codec_name"]}


def list_clips(prj: Project) -> list[Path]:
    excl = set(prj.get("exclude_clips", []))
    clips = [p for p in prj.footage_dir.glob("G*.MP4") if p.name not in excl]
    # GoPro chaptering: GX01nnnn, GX02nnnn ... -> sort by file number, then chapter
    clips.sort(key=lambda p: (p.stem[4:8], p.stem[2:4]))
    return clips


def proxy_for(clip: Path) -> Path | None:
    lrv = clip.with_name("GL" + clip.stem[2:] + ".LRV")
    return lrv if lrv.exists() else None


def reduce_telemetry(tel: dict, duration: float) -> dict:
    """Reduce raw GPMF streams to a regular 10 Hz grid of useful signals."""
    n = int(np.floor(duration * FS))
    grid = np.arange(n) / FS
    out = {"t": grid}
    g, a = tel.get("GYRO"), tel.get("ACCL")
    if g is not None and a is not None and len(g["t"]) > 10:
        win = max(int(len(a["t"]) / max(a["t"][-1] - a["t"][0], 1) * 2), 1)  # ~2 s low-pass
        al = uniform_filter1d(a["v"], win, axis=0)
        up = al / np.linalg.norm(al, axis=1, keepdims=True)
        ai = np.stack([np.interp(g["t"], a["t"], up[:, i]) for i in range(3)], 1)
        yaw = (g["v"] * ai).sum(1)                         # rad/s about apparent vertical
        gmag = np.linalg.norm(g["v"], axis=1)
        hf = g["v"] - uniform_filter1d(g["v"], 20, axis=0)  # high-frequency jitter
        shake = np.linalg.norm(hf, axis=1)
        amag = np.linalg.norm(a["v"], axis=1)
        jerk = np.abs(np.gradient(amag))
        for name, sig, t in (("yaw", yaw, g["t"]), ("gyro_mag", gmag, g["t"]),
                             ("shake", shake, g["t"]), ("acc_mag", amag, a["t"]),
                             ("jerk", jerk, a["t"])):
            # average into 10 Hz bins
            idx = np.clip((t * FS).astype(int), 0, n - 1)
            cnt = np.bincount(idx, minlength=n)
            s = np.bincount(idx, weights=sig, minlength=n)
            with np.errstate(invalid="ignore", divide="ignore"):
                v = s / cnt
            mask = cnt > 0
            out[name] = np.interp(grid, grid[mask], v[mask]) if mask.any() else np.zeros(n)
    for k in ("YAVG", "ISOE"):
        if k in tel and len(tel[k]["t"]):
            out[k.lower()] = np.interp(grid, tel[k]["t"], tel[k]["v"][:, 0])
    return out


def _index_one(args) -> tuple[str, dict | None, str | None]:
    clip, cache = args
    meta = probe(clip)
    rec = cache.parent.parent / "recovered" / clip.name
    src = clip
    if meta is None and rec.exists():
        src, meta = rec, probe(rec)
    if meta is None:
        return clip.name, None, "unreadable (missing moov?)"
    try:
        if src == rec:
            tel = gpmf.parse((rec.parent / (clip.stem + ".gpmf.bin")).read_bytes())
        else:
            tel = gpmf.load_clip_telemetry(clip)
        red = reduce_telemetry(tel, meta["duration"])
        np.savez_compressed(cache, **red)
    except Exception as e:  # keep going, report
        return clip.name, meta, f"telemetry error: {e}"
    meta["source"] = str(src)
    return clip.name, meta, None


def run(prj: Project, force: bool = False, workers: int = 6) -> dict:
    clips = list_clips(prj)
    jobs = []
    index_path = prj.work("index.json")
    index = json.loads(index_path.read_text()) if index_path.exists() and not force else {}
    for c in clips:
        cache = prj.work("telemetry", c.stem + ".npz")
        if force or c.name not in index or not cache.exists():
            jobs.append((c, cache))
    with ProcessPoolExecutor(workers) as ex:
        for name, meta, err in ex.map(_index_one, jobs):
            print(f"  {name}: {'OK' if not err else err}")
            index[name] = {"meta": meta, "error": err}
    order = [c.name for c in clips]
    out = {}
    for i, name in enumerate(order):
        e = index[name]
        e["order"] = i
        e["path"] = (e["meta"] or {}).get("source") or str(prj.footage_dir / name)
        px = proxy_for(prj.footage_dir / name)
        e["proxy"] = str(px) if px and probe(px) else None
        out[name] = e
    index_path.write_text(json.dumps(out, indent=1))
    ok = [n for n, e in out.items() if not e["error"]]
    tot = sum(out[n]["meta"]["duration"] for n in ok)
    print(f"indexed {len(ok)}/{len(out)} clips, {tot/60:.1f} min usable")
    return out


def load_index(prj: Project) -> dict:
    return json.loads(prj.work("index.json").read_text())


def load_telemetry(prj: Project, clip_name: str) -> dict:
    return dict(np.load(prj.work("telemetry", Path(clip_name).stem + ".npz")))

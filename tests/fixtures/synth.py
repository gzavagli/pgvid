"""Synthetic test data: fake IGC flights, GPMF payloads, tiny video clips, a whole project.

Everything here is generated: made-up pilot, a launch in the middle of the
Atlantic, test-pattern video. Nothing comes from real flights.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import struct
import subprocess
from pathlib import Path

import numpy as np
import yaml

LAT0, LON0 = 0.5, -30.0   # open ocean: clearly fictional


# ----------------------------------------------------------------------------- IGC

def _igc_lat(lat: float) -> str:
    h = "N" if lat >= 0 else "S"
    lat = abs(lat)
    d = int(lat)
    m = (lat - d) * 60
    return f"{d:02d}{int(m):02d}{int((m % 1) * 1000):03d}{h}"


def _igc_lon(lon: float) -> str:
    h = "E" if lon >= 0 else "W"
    lon = abs(lon)
    d = int(lon)
    m = (lon - d) * 60
    return f"{d:03d}{int(m):02d}{int((m % 1) * 1000):03d}{h}"


def flight_profile(seed: int = 0, ground: int = 120, n_cycles: int = 6):
    """1 Hz (x, y, alt, turn_rate) arrays: ground, thermal/glide cycles, ground."""
    rng = np.random.default_rng(seed)
    xs, ys, alts, turns = [], [], [], []
    x = y = 0.0
    alt = 1500.0
    heading = 0.0
    for _ in range(ground):
        xs.append(x), ys.append(y), alts.append(alt), turns.append(0.0)
    for _ in range(n_cycles):
        rate = float(rng.choice([-18.0, 18.0]))      # deg/s, ~20 s per circle
        for _ in range(int(rng.integers(120, 240))):  # thermal: circle at 10 m/s, climb 2 m/s
            heading += rate
            x += 10 * math.sin(math.radians(heading))
            y += 10 * math.cos(math.radians(heading))
            alt += 2.0
            xs.append(x), ys.append(y), alts.append(alt), turns.append(rate)
        heading = float(rng.uniform(0, 360))
        for _ in range(int(rng.integers(150, 300))):  # glide: straight at 11 m/s, sink 1.2 m/s
            x += 11 * math.sin(math.radians(heading))
            y += 11 * math.cos(math.radians(heading))
            alt -= 1.2
            xs.append(x), ys.append(y), alts.append(alt), turns.append(0.0)
    for _ in range(ground):
        xs.append(x), ys.append(y), alts.append(alt), turns.append(0.0)
    return np.array(xs), np.array(ys), np.array(alts), np.array(turns)


def write_igc(path: Path, date: dt.date, start_utc: dt.time = dt.time(12, 0, 0), seed: int = 0,
              pilot: str = "Jane Pilot", lat0: float = LAT0, lon0: float = LON0) -> dict:
    x, y, alt, turn = flight_profile(seed)
    lat = lat0 + y / 111_320.0
    lon = lon0 + x / (111_320.0 * math.cos(math.radians(lat0)))
    t0 = dt.datetime.combine(date, start_utc)
    lines = ["AXXX001 synthetic", f"HFDTE{date:%d%m%y}", f"HFPLTPILOTINCHARGE:{pilot}",
             "HFGTYGLIDERTYPE:Example Wing"]
    for i in range(len(x)):
        t = t0 + dt.timedelta(seconds=i)
        a = int(round(alt[i]))
        lines.append(f"B{t:%H%M%S}{_igc_lat(lat[i])}{_igc_lon(lon[i])}A{a:05d}{a:05d}")
    path.write_text("\r\n".join(lines) + "\r\n")
    epoch0 = t0.replace(tzinfo=dt.timezone.utc).timestamp()
    return {"path": path, "epoch0": epoch0, "turn": turn, "n": len(x)}


# ----------------------------------------------------------------------------- GPMF

def klv(key: str, typ: str, size: int, data: bytes) -> bytes:
    rep = len(data) // size if size else 0
    pad = (-len(data)) % 4
    return key.encode() + typ.encode() + bytes([size]) + struct.pack(">H", rep) + data + b"\0" * pad


def gpmf_payload(gyro: np.ndarray, rate: float = 200.0, chunk_s: float = 1.0, scal: int = 100) -> bytes:
    """GPMF DEVC blocks with STMP/SCAL/GYRO, `gyro` (N, 3) in rad/s."""
    out = b""
    per = int(rate * chunk_s)
    for k in range(0, len(gyro), per):
        g = np.round(gyro[k:k + per] * scal).astype(">i2")
        strm = (klv("STMP", "J", 8, struct.pack(">Q", int(k / rate * 1e6)))
                + klv("SCAL", "s", 2, struct.pack(">h", scal)) + klv("GYRO", "s", 6, g.tobytes()))
        out += klv("DEVC", "\0", 1, klv("STRM", "\0", 1, strm))
    return out


# ----------------------------------------------------------------------------- media

def make_clip(path: Path, seconds: float = 3.0, size: str = "640x360", fps: str = "30000/1001") -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", f"{seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return path


def make_tone(path: Path, seconds: float = 40.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
                    "-t", f"{seconds}", str(path)], check=True, stdin=subprocess.DEVNULL)
    return path


# ----------------------------------------------------------------------------- whole project

def make_project(root: Path, n_days: int = 2, competition: bool = False, extra: dict | None = None) -> Path:
    """A project directory with synthetic IGC days and the YAML; returns the YAML path."""
    (root / "footage").mkdir(parents=True, exist_ok=True)
    (root / "igc").mkdir(exist_ok=True)
    (root / "music").mkdir(exist_ok=True)
    base = dt.date(2026, 7, 14)
    for d in range(n_days):
        write_igc(root / "igc" / f"day{d + 1}.igc", base + dt.timedelta(days=d), seed=d)
    cfg = {"name": "synthetic", "event": "Example Open 2026", "event_short": "Example Open",
           "location": "Nowhere Valley", "dates": "14-15 July 2026", "utc_offset_hours": 2,
           "pilot": "Jane Pilot", "glider": "Example Wing", "channel": "@example",
           "footage_dir": "footage", "igc_dir": "igc", "music_dir": "music"}
    if competition:
        cfg["competition"] = {"provider": "manual"}
        tdir = root / "work" / "synthetic" / "tasks"
        tdir.mkdir(parents=True, exist_ok=True)
        for d in range(n_days):
            (tdir / f"task{d + 1}.yaml").write_text(yaml.safe_dump({
                "num": d + 1, "date": str(base + dt.timedelta(days=d)), "type": "Race to goal",
                "distance_km": 42.0 + d, "turnpoints": [
                    {"type": "TAKEOFF", "lat": LAT0, "lon": LON0, "radius_m": 400},
                    {"type": "SSS", "lat": LAT0 + 0.05, "lon": LON0, "radius_m": 3000},
                    {"type": "GOAL", "lat": LAT0 + 0.2, "lon": LON0 + 0.1, "radius_m": 400}],
                "stats": {"no_of_pilots_reaching_goal": 30}, "pilots": 80,
                "result": {"rank": 12, "time": "1:45:00", "distance_km": 42.0}}))
    cfg.update(extra or {})
    p = root / "project.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return p


def fake_pipeline_state(prj, clips: dict[str, float], fps: float = 2.0) -> None:
    """Write index/sync/visual/segments/music caches as if the analysis steps had run.

    clips: {name: duration}. Clip i is placed during day i % n_days, mid-flight.
    """
    from pgvid.igc import load_tracks
    tracks = load_tracks(prj.igc_dir)
    index, sync, segs = {}, [], []
    cats = ["launch", "gaggle", "close_pass", "strong_thermal", "fast_glide", "clouds", "scenery", "goal_landing"]
    rng = np.random.default_rng(1)
    for i, (name, dur) in enumerate(clips.items()):
        path = prj.footage_dir / name
        index[name] = {"meta": {"duration": dur, "width": 640, "height": 360, "fps": 29.97, "codec": "h264"},
                       "error": None, "order": i, "path": str(path), "proxy": None}
        day = i % len(tracks)
        tr = tracks[day]
        start = tr.takeoff + 600 + 60 * (i // len(tracks))
        sync.append({"clip": name, "day": day, "date": str(tr.date), "start_epoch": start, "duration": dur,
                     "score": 30.0, "corr": 0.8, "method": "ocr", "confidence": "high", "ocr_n": 3, "ocr_spread": 0.0})
        n = int(dur * fps)
        np.savez_compressed(prj.work("visual", Path(name).stem + ".npz"), t=np.arange(n) / fps,
                            contrast=np.full(n, 50.0), bright=np.full(n, 120.0), sat=np.full(n, 90.0),
                            phash=np.zeros(n, np.uint64))
        for k, cat in enumerate(cats):
            for j in range(2):
                st = float(rng.uniform(0, max(dur - 5, 0.1)))
                segs.append({"clip": name, "start": st, "dur": min(5.0, dur), "category": cat,
                             "score": round(float(rng.uniform(0.2, 1.0)), 3), "day": day, "epoch": start + st,
                             "alt": 1800.0, "vario": 1.5, "speed": 35.0, "gliders": 3.0,
                             "phash": str(int(rng.integers(0, 2**62)))})
    prj.work("index.json").write_text(json.dumps(index))
    prj.work("sync.json").write_text(json.dumps({"sign": 1.0, "clips": sync}))
    prj.work("segments.json").write_text(json.dumps(segs))
    beats = [round(0.5 * k, 3) for k in range(1, 400)]
    music = {}
    for name in ("Track One - Artist A", "Track Two - Artist B"):
        music[name] = {"path": str(prj.music_dir / f"{name}.wav"), "name": name, "duration": 200.0, "tempo": 120.0,
                       "beats": beats, "downbeats": beats[::4], "energy": [0.5] * 400, "energy_hop": 0.5,
                       "drops": [16.0, 48.0], "mean_energy": 0.5}
    prj.work("music.json").write_text(json.dumps(music))

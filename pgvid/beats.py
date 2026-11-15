"""Music analysis: tempo, beat and downbeat grid, energy curve, drop detection."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import Project


def analyze_track(path: Path) -> dict:
    import librosa

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    dur = len(y) / sr
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time", tightness=120)
    tempo = float(np.atleast_1d(tempo)[0])
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    # downbeats: choose the beat phase (0..3) with the strongest onset energy
    bt_frames = librosa.time_to_frames(beats, sr=sr)
    bt_frames = bt_frames[bt_frames < len(onset)]
    phase = int(np.argmax([onset[bt_frames[p::4]].mean() if len(bt_frames[p::4]) else 0 for p in range(4)]))
    downbeats = beats[phase::4]
    # energy (RMS) in 0.5 s hops, smoothed
    hop = int(sr * 0.5)
    rms = np.array([np.sqrt(np.mean(y[i:i + hop] ** 2)) for i in range(0, len(y), hop)])
    rms_s = np.convolve(rms, np.ones(8) / 8, mode="same")
    e = (rms_s - rms_s.min()) / max(np.ptp(rms_s), 1e-9)
    # drops: biggest positive energy jumps aligned to downbeats
    jumps = []
    for db in downbeats:
        i = int(db / 0.5)
        if 8 <= i < len(e) - 8:
            jumps.append((float(e[i:i + 8].mean() - e[i - 8:i].mean()), float(db)))
    jumps.sort(reverse=True)
    drops = sorted(t for _, t in jumps[:4])
    return {"path": str(path), "name": path.stem, "duration": round(dur, 2), "tempo": round(tempo, 1),
            "beats": [round(float(b), 3) for b in beats], "downbeats": [round(float(b), 3) for b in downbeats],
            "energy": [round(float(x), 3) for x in e], "energy_hop": 0.5, "drops": drops,
            "mean_energy": round(float(e.mean()), 3)}


def load_beats(prj: Project) -> dict[str, dict]:
    return json.loads(prj.work("music.json").read_text())


def run(prj: Project, force: bool = False) -> None:
    out = prj.work("music.json")
    res = {} if force or not out.exists() else json.loads(out.read_text())
    for p in sorted(prj.music_dir.glob("*.mp3")) + sorted(prj.music_dir.glob("*.wav")):
        if p.stem in res:
            continue
        res[p.stem] = analyze_track(p)
        r = res[p.stem]
        print(f"{p.stem}: {r['duration']}s, {r['tempo']} BPM, {len(r['beats'])} beats, drops at {r['drops']}, "
              f"mean energy {r['mean_energy']}")
    out.write_text(json.dumps(res))

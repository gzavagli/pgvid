"""pgvid tests. All data is synthetic (see tests/fixtures/synth.py). Run: pytest -q"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import struct
import subprocess
import warnings

import numpy as np
import pytest
import yaml

from pgvid import cards, gpmf
from pgvid.config import load_project, render_template
from pgvid.edl import BeatGrid, build_long, build_short
from pgvid.igc import load_tracks, parse_igc
from pgvid.ocr import parse_clock
from pgvid.providers.xctsk import parse_xctsk
from pgvid.sync import ncc, ocr_anchor
from pgvid.tasks import chapters

from .fixtures import synth

HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ----------------------------------------------------------------------------- telemetry / tracks

def test_gpmf_parse_synthetic():
    gyro = struct.pack(">6h", 100, 200, 300, -100, -200, -300)
    strm = synth.klv("STMP", "J", 8, struct.pack(">Q", 0)) + synth.klv("SCAL", "s", 2, struct.pack(">h", 100)) \
        + synth.klv("GYRO", "s", 6, gyro)
    strm2 = synth.klv("STMP", "J", 8, struct.pack(">Q", 1_000_000)) + synth.klv("SCAL", "s", 2, struct.pack(">h", 100)) \
        + synth.klv("GYRO", "s", 6, gyro)
    buf = synth.klv("DEVC", "\0", 1, synth.klv("STRM", "\0", 1, strm)) \
        + synth.klv("DEVC", "\0", 1, synth.klv("STRM", "\0", 1, strm2))
    out = gpmf.parse(buf)
    g = out["GYRO"]
    assert g["v"].shape == (4, 3)
    np.testing.assert_allclose(g["v"][0], [1, 2, 3])
    np.testing.assert_allclose(g["t"], [0, 0.5, 1.0, 1.5])


def test_gpmf_payload_roundtrip():
    gyro = np.zeros((600, 3))
    gyro[:, 2] = np.linspace(-1, 1, 600)
    out = gpmf.parse(synth.gpmf_payload(gyro, rate=200.0))
    assert out["GYRO"]["v"].shape == (600, 3)
    assert abs(out["GYRO"]["t"][-1] - 2.995) < 0.01
    np.testing.assert_allclose(out["GYRO"]["v"][:, 2], np.round(gyro[:, 2] * 100) / 100)


def test_igc_parse(tmp_path):
    info = synth.write_igc(tmp_path / "f.igc", dt.date(2026, 7, 14))
    tr = parse_igc(info["path"])
    assert tr.pilot == "Jane Pilot"
    assert tr.date == dt.date(2026, 7, 14)
    assert np.all(np.diff(tr.t) == 1)
    assert 0 < tr.takeoff_idx < tr.landing_idx < len(tr.t) - 1
    assert 1000 < np.nanmax(tr.alt) < 5000
    assert np.abs(tr.vario).max() < 15
    assert tr.flown_km() > 5
    # circling segments show up as turn rate of the right magnitude
    circ = np.abs(info["turn"]) > 0
    assert 10 < np.median(np.abs(tr.turn[circ])) < 25


def test_sync_recovers_known_offset():
    rng = np.random.default_rng(0)
    ref = np.zeros(6000)
    t = 0
    while t < 5800:
        g = rng.integers(60, 300)
        th = rng.integers(60, 400)
        ref[t + g:t + g + th] = rng.choice([-20, 20]) + rng.normal(0, 3, size=min(th, 6000 - t - g))
        t += g + th
    true = 2345
    clip = ref[true:true + 600] + rng.normal(0, 6, 600)  # head-movement noise
    r = ncc(clip, ref)
    assert abs(int(np.argmax(r)) - true) <= 1


def test_sync_against_synthetic_igc(tmp_path):
    """Camera yaw cut from the IGC turn rate (plus noise) is placed at the right second."""
    from pgvid.sync import track_turn
    info = synth.write_igc(tmp_path / "f.igc", dt.date(2026, 7, 14), seed=3)
    tr = parse_igc(info["path"])
    ref = track_turn(tr)
    true = tr.takeoff_idx + 400
    clip = ref[true:true + 300] + np.random.default_rng(2).normal(0, 5, 300)
    assert abs(int(np.argmax(ncc(clip, ref))) - true) <= 2


def test_ocr_anchor_consensus():
    readings = [{"t": 10.0, "clock": 50000}, {"t": 40.0, "clock": 50030}, {"t": 70.0, "clock": 51234}]
    tod, n, spread = ocr_anchor(readings)
    assert n == 2 and abs(tod - 49990) < 0.01


def test_parse_clock_window():
    got = parse_clock(["12:34:56", "battery 07:00:00", "x 1:2:3", "23:59:59"], t=5.0, hours=(8, 20))
    assert [g["clock"] for g in got] == [12 * 3600 + 34 * 60 + 56]


def test_beat_snap():
    g = BeatGrid([0.5, 1.0, 1.5, 2.0], [0.5, 2.5])
    assert g.snap(1.1, 0.8, 1.4) == 1.0
    assert g.snap(1.1, 1.2, 1.4) == 1.1  # nothing in range -> unchanged


# ----------------------------------------------------------------------------- config / templates

def test_template_safety():
    assert render_template("{event} | {missing}", {"event": "X"}) == "X | "
    with pytest.raises(ValueError):
        render_template("{event.__class__}", {"event": "X"})
    with pytest.raises(ValueError):
        render_template("{event[0]}", {"event": "X"})


def test_config_paths_and_defaults(tmp_path):
    p = synth.make_project(tmp_path, n_days=1, extra={"base_dir": "."})
    prj = load_project(p)
    assert prj.igc_dir == tmp_path / "igc"
    assert prj.work_dir == tmp_path / "work" / "synthetic"
    assert prj.tasks_dir == prj.work_dir / "tasks"
    assert prj.section("youtube") == {}
    assert prj.fmt("{event_short} in {location}") == "Example Open in Nowhere Valley"


def test_config_requires_keys(tmp_path):
    (tmp_path / "p.yaml").write_text("name: x\n")
    with pytest.raises(ValueError, match="missing required keys"):
        load_project(tmp_path / "p.yaml")


def test_example_project_loads(tmp_path):
    from pathlib import Path
    from pgvid.metadata import DEFAULTS
    src = Path(__file__).resolve().parent.parent / "examples" / "example_project.yaml"
    shutil.copy(src, tmp_path / "example.yaml")
    prj = load_project(tmp_path / "example.yaml")
    assert prj["competition"]["provider"] == "xctsk"
    for key, tpl in {**DEFAULTS, **prj.section("youtube")}.items():
        for t in ([tpl] if isinstance(tpl, str) else tpl):
            prj.fmt(t, n_chapters=6, n_tasks=6, n_pilots=100, results_url="")


def test_font_override_falls_back(tmp_path):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cards.configure_fonts({"heavy": str(tmp_path / "nope.ttf")})
    assert any("not found" in str(x.message) for x in w)
    assert cards.FONT_FILES["heavy"] == cards.BUNDLED_FONTS["heavy"]
    cards.configure_fonts({})


# ----------------------------------------------------------------------------- tasks / chapters

def test_xctsk_parse():
    task = {"taskType": "CLASSIC", "version": 1, "turnpoints": [
        {"type": "TAKEOFF", "radius": 400, "waypoint": {"name": "TO", "lat": 0.5, "lon": -30.0, "altSmoothed": 900}},
        {"type": "SSS", "radius": 3000, "waypoint": {"name": "S", "lat": 0.55, "lon": -30.0}},
        {"radius": 1000, "waypoint": {"name": "T1", "lat": 0.7, "lon": -29.9}},
        {"type": "ESS", "radius": 1000, "waypoint": {"name": "E", "lat": 0.6, "lon": -29.7}},
        {"radius": 400, "waypoint": {"name": "G", "lat": 0.6, "lon": -29.7}}],
        "sss": {"type": "RACE", "direction": "EXIT", "timeGates": ["11:00:00Z"]}}
    t = parse_xctsk(json.dumps(task), 1, "2026-07-14", utc_offset=2)
    assert [tp["type"] for tp in t["turnpoints"]] == ["TAKEOFF", "SSS", "TP", "ESS", "GOAL"]
    assert t["start_time"] == "13:00"
    assert 20 < t["distance_km"] < 60


def test_chapters_free_flight(tmp_path):
    prj = load_project(synth.make_project(tmp_path, n_days=2))
    ch = chapters(prj, load_tracks(prj.igc_dir))
    assert [c["label"] for c in ch] == ["FLIGHT 1", "FLIGHT 2"]
    assert all(c["task"] is None and c["distance_km"] > 0 and c["airtime_s"] > 0 for c in ch)


def test_chapters_match_tasks_by_date(tmp_path):
    prj = load_project(synth.make_project(tmp_path, n_days=2, competition=True))
    ch = chapters(prj, load_tracks(prj.igc_dir))
    assert [c["label"] for c in ch] == ["TASK 1", "TASK 2"]
    assert ch[1]["task"]["distance_km"] == 43.0


# ----------------------------------------------------------------------------- graphics

def test_graphics_frames():
    W, H = 1280, 720
    img, pos = cards.hud_frame(W, H, 1834.0, 2.4, 38.0, "13:05:10", "TASK 1")
    assert img.mode == "RGBA" and 0 <= pos[0] < W and img.getbbox() is not None
    chap = {"label": "FLIGHT 1", "date": "2026-07-14", "distance_km": 61.3, "type": "Free flight",
            "task": None, "airtime_s": 9000, "max_alt_m": 2800}
    from PIL import Image
    card = cards.chapter_card_frame(W, H, 3.0, 7.0, chap, lambda p: Image.new("RGBA", (int(W * 0.625), H)))
    assert card.size == (W, H)
    end = cards.end_screen_frame(W, H, 2.0, 5.0, "", ["Example Open"])
    assert end.getbbox() is not None
    assert cards.title_frame(W, H, 1.0, 5.0, "Example Open", "July", "Nowhere").getbbox() is not None


# ----------------------------------------------------------------------------- EDL

@pytest.mark.parametrize("competition", [False, True])
def test_edl_durations_and_contiguity(tmp_path, competition):
    prj = load_project(synth.make_project(tmp_path, n_days=2, competition=competition))
    synth.fake_pipeline_state(prj, {f"GX01{i:04d}.MP4": 120.0 for i in range(1, 9)})
    for edl, lo, hi in ((build_short(prj), 29.9, 30.1), (build_long(prj, target=240), 150, 400)):
        assert lo <= edl["duration"] <= hi
        t = 0.0
        for s in edl["shots"]:
            assert abs(s["start"] - t) < 0.02, s
            assert s["dur"] > 0.8
            t = s["start"] + s["dur"]
        assert abs(t - edl["duration"]) < 0.05
    long = build_long(prj, target=240)
    assert [c["title"] for c in long["chapters"]] == (["Task 1", "Task 2"] if competition else ["Flight 1", "Flight 2"])
    assert sum(1 for s in long["shots"] if s["type"] == "card") == 2


# ----------------------------------------------------------------------------- render smoke test

@pytest.mark.slow
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_render_preview_with_hud(tmp_path):
    from pgvid.render import render
    prj = load_project(synth.make_project(tmp_path, n_days=1))
    synth.make_clip(prj.footage_dir / "GX010001.MP4", seconds=4.0)
    synth.make_tone(prj.music_dir / "Track One - Artist A.wav", seconds=10.0)
    synth.fake_pipeline_state(prj, {"GX010001.MP4": 4.0})
    edl = {"kind": "long", "width": 1280, "height": 720, "fps": "30000/1001", "duration": 3.0,
           "music": [{"track": "Track One - Artist A", "file": str(prj.music_dir / "Track One - Artist A.wav"),
                      "in": 0.0, "start": 0.0, "end": 3.0, "fade_in": 0.0, "fade_out": 0.5}],
           "chapters": [],
           "shots": [{"type": "clip", "clip": "GX010001.MP4", "in": 0.2, "speed": 1.0, "start": 0.0, "dur": 1.5,
                      "overlay": "hud", "audio": "low", "section": "day1"},
                     {"type": "clip", "clip": "GX010001.MP4", "in": 2.0, "speed": 1.0, "start": 1.5, "dur": 1.5,
                      "overlay": "title+caption", "caption": "HELLO", "audio": "up", "section": "title"}]}
    prj.work("edl", "long.yaml").write_text(yaml.safe_dump(edl))
    out = render(prj, "long", "preview", workers=2)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True, check=True).stdout)
    assert abs(dur - 3.0) < 0.15

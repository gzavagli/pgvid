"""OCR of the flight instrument clock visible in the footage (optional, macOS only).

If your flight instrument (phone/vario screen) is in frame and shows the local
time as HH:MM:SS, reading it from a few sampled frames gives an absolute time
reference per clip. That is far more reliable than gyro correlation for short
or thermal-only clips. Skip this step if the screen isn't visible: `sync`
then relies on gyro/turn-rate correlation alone.

Uses Apple's Vision framework through pyobjc: `pip install "pgvid[ocr]"`.

    ocr:
      crop_bottom: 0.45      # fraction of the frame height (from the bottom) to read
      hours: [8, 20]         # ignore clock readings outside this local-time window
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .config import Project
from .index import load_index

_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)[:.]([0-5]\d)(?!\d)")


def _check_backend() -> None:
    if sys.platform != "darwin":
        raise SystemExit("pgvid ocr needs macOS (Apple Vision). Skip it: `sync` works without OCR.")
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        raise SystemExit('pgvid ocr needs pyobjc: pip install "pgvid[ocr]"') from None


def _ocr_image(path: Path) -> list[str]:
    import Quartz
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return []
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    ok, _ = handler.performRequests_error_([req], None)
    if not ok:
        return []
    out = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if cand:
            out.append(str(cand[0].string()))
    return out


def _grab(clip: Path, t: float, dst: Path, crop_bottom: float) -> bool:
    # bottom part of the frame, where the instrument usually sits
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(clip), "-frames:v", "1",
         "-vf", f"crop=iw:ih*{crop_bottom:.3f}:0:ih*{1 - crop_bottom:.3f}", str(dst)], capture_output=True)
    return r.returncode == 0 and dst.exists()


def sample_times(duration: float, step: float = 15.0, max_n: int = 14) -> list[float]:
    if duration < 3:
        return [duration / 2]
    n = int(min(max_n, max(3, duration // step)))
    return [duration * (i + 0.5) / n for i in range(n)]


def parse_clock(texts: list[str], t: float, hours: tuple[int, int] = (8, 20)) -> list[dict]:
    res = []
    for s in texts:
        for m in _TIME.finditer(s):
            h, mi, se = map(int, m.groups())
            if hours[0] <= h <= hours[1]:
                res.append({"t": round(t, 2), "clock": h * 3600 + mi * 60 + se, "text": s})
    return res


def read_clip(clip: Path, duration: float, tmp: Path, crop_bottom: float = 0.45,
              hours: tuple[int, int] = (8, 20)) -> list[dict]:
    res = []
    for t in sample_times(duration):
        img = tmp / f"{clip.stem}_{t:08.2f}.png"
        if not _grab(clip, t, img, crop_bottom):
            continue
        texts = _ocr_image(img)
        img.unlink(missing_ok=True)
        res += parse_clock(texts, t, hours)
    return res


def run(prj: Project, force: bool = False) -> dict:
    _check_backend()
    cfg = prj.section("ocr")
    crop = float(cfg.get("crop_bottom", 0.45))
    hours = tuple(cfg.get("hours", (8, 20)))
    index = load_index(prj)
    path = prj.work("ocr.json")
    cache = json.loads(path.read_text()) if path.exists() and not force else {}
    todo = [(n, e) for n, e in sorted(index.items(), key=lambda kv: kv[1]["order"])
            if not e["error"] and n not in cache]
    with tempfile.TemporaryDirectory() as td, ProcessPoolExecutor(4) as ex:
        futs = {ex.submit(read_clip, Path(e["path"]), e["meta"]["duration"], Path(td), crop, hours): n
                for n, e in todo}
        for f in as_completed(futs):
            n = futs[f]
            cache[n] = f.result()
            print(f"  {n}: {len(cache[n])} clock readings", flush=True)
            path.write_text(json.dumps(cache, indent=1))
    path.write_text(json.dumps(cache, indent=1))
    return cache

"""Per-clip visual + audio features from the LRV proxies.

Visual (2 fps, 384x216):
  bright, contrast, sat, sky (blue sky fraction), cloud (white/grey fraction in
  the upper half), dark (obstruction: glove / harness filling the frame),
  green (vegetation/ground fill), flow (median optical flow outside the
  cockpit area), blobs (small colourful non-blue blobs = probable gliders),
  sharp (Laplacian variance), phash (64-bit perceptual hash for dedup).
Audio (2 Hz): rms (dB), beep (1-4 kHz band ratio, vario tones), wind (<300 Hz).
Gliders (1 fps, YOLO, separate pass): n_gliders, max_glider_area.
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from .config import Project
from .index import load_index

W, H = 384, 216
FPS = 2.0


def _frames(path: Path, fps: float, w: int, h: int):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.wait()


def _phash(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(small)[:8, :8]
    bits = (d > np.median(d)).flatten()
    return int(sum(1 << i for i, b in enumerate(bits) if b))


def _blobs(hsv: np.ndarray) -> int:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    colorful = (s > 110) & (v > 70) & ((h < 85) | (h > 140))
    colorful[int(H * 0.62):, :] = False  # cockpit / own risers area
    n, lab, stats, _ = cv2.connectedComponentsWithStats(colorful.astype(np.uint8), 8)
    cnt = 0
    for i in range(1, n):
        x, y, w, hh, a = stats[i]
        if not (3 <= a <= 600):
            continue
        if x <= 1 or y <= 1 or x + w >= W - 1:
            continue
        if max(w, hh) / max(min(w, hh), 1) > 5:
            continue  # lines / risers
        # must be surrounded by sky-ish pixels (bright or blue), not ground
        y0, y1, x0, x1 = max(0, y - 4), min(H, y + hh + 4), max(0, x - 4), min(W, x + w + 4)
        ring = hsv[y0:y1, x0:x1]
        skyish = ((ring[..., 2] > 140) & (ring[..., 1] < 90)) | ((ring[..., 0] > 90) & (ring[..., 0] < 130) & (ring[..., 2] > 110))
        if skyish.mean() > 0.45:
            cnt += 1
    return cnt


def _audio(path: Path, duration: float) -> dict:
    sr = 16000
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    x = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0
    hop = int(sr / FPS)
    n = int(np.ceil(duration * FPS))
    rms = np.full(n, -90.0)
    beep = np.zeros(n)
    wind = np.zeros(n)
    for i in range(min(n, len(x) // hop)):
        seg = x[i * hop:(i + 1) * hop]
        if seg.size < 256:
            continue
        rms[i] = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-6)
        spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2
        f = np.fft.rfftfreq(seg.size, 1 / sr)
        tot = spec.sum() + 1e-9
        beep[i] = spec[(f > 900) & (f < 4000)].sum() / tot
        wind[i] = spec[f < 300].sum() / tot
    return {"rms": rms, "beep": beep, "wind": wind}


def analyze_clip(args) -> tuple[str, str | None]:
    name, src, duration, out = args
    feats: dict[str, list] = {k: [] for k in ("bright", "contrast", "sat", "sky", "cloud", "dark", "green",
                                              "flow", "blobs", "sharp", "phash")}
    prev = None
    try:
        for fr in _frames(Path(src), FPS, W, H):
            hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            hh, ss, vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
            top = slice(0, H // 2)
            feats["bright"].append(float(vv.mean()))
            feats["contrast"].append(float(gray.std()))
            feats["sat"].append(float(ss.mean()))
            feats["sky"].append(float(((hh > 90) & (hh < 130) & (ss > 40) & (vv > 100)).mean()))
            feats["cloud"].append(float(((ss[top] < 40) & (vv[top] > 165)).mean()))
            feats["dark"].append(float((vv < 45).mean()))
            feats["green"].append(float(((hh > 30) & (hh < 85) & (ss > 60) & (vv > 50)).mean()))
            feats["sharp"].append(float(cv2.Laplacian(gray, cv2.CV_32F).var()))
            feats["phash"].append(_phash(gray))
            feats["blobs"].append(_blobs(hsv))
            small = cv2.resize(gray, (W // 2, H // 2))
            if prev is not None:
                fl = cv2.calcOpticalFlowFarneback(prev, small, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                mag = np.linalg.norm(fl[: int(H / 2 * 0.62)], axis=2)
                feats["flow"].append(float(np.median(mag)))
            else:
                feats["flow"].append(0.0)
            prev = small
        arr = {k: np.array(v, dtype=np.uint64 if k == "phash" else np.float32) for k, v in feats.items()}
        arr["t"] = np.arange(len(arr["bright"])) / FPS
        aud = _audio(Path(src), duration)
        n = len(arr["t"])
        for k, v in aud.items():
            arr[k] = np.resize(v, n) if len(v) >= n else np.pad(v, (0, n - len(v)), mode="edge")
        np.savez_compressed(out, **arr)
    except Exception as e:
        return name, str(e)
    return name, None


def _device(pref: str) -> str:
    if pref and pref != "auto":
        return pref
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def detect_gliders(prj: Project, index: dict, force: bool = False) -> None:
    """Optional object-detection pass (`pip install "pgvid[detect]"`, AGPL-3.0 licensed).

    COCO 'kite' (and 'bird'/'airplane') reliably fire on paragliders.

        detect:
          weights: yolo11s.pt   # file path or a model name the library can download
          device: auto          # auto | mps | cuda | cpu
    """
    try:
        from ultralytics import YOLO
    except Exception:  # optional dependency
        print('  glider detection skipped (optional): pip install "pgvid[detect]"')
        return
    cfg = prj.section("detect")
    weights = str(cfg.get("weights", "yolo11s.pt"))
    wp = prj.resolve(weights)
    model = YOLO(str(wp) if wp.exists() else weights)
    device = _device(cfg.get("device", "auto"))
    for name, e in sorted(index.items(), key=lambda kv: kv[1]["order"]):
        if e["error"]:
            continue
        out = prj.work("gliders", Path(name).stem + ".npz")
        if out.exists() and not force:
            continue
        counts, areas, boxes_all = [], [], []
        frames = list(_frames(Path(e["proxy"] or e["path"]), 1.0, 768, 432))
        for i in range(0, len(frames), 16):
            batch = [f[..., ::-1].copy() for f in frames[i:i + 16]]  # BGR->RGB
            res = model.predict(batch, imgsz=768, conf=0.12, classes=[4, 14, 33], device=device, verbose=False)
            for r in res:
                b = r.boxes
                xyxy = b.xyxy.cpu().numpy() if len(b) else np.zeros((0, 4))
                # discard detections in cockpit zone (own wing tips / risers at frame edges low)
                keep = [k for k, bb in enumerate(xyxy) if bb[3] < 432 * 0.7]
                xyxy = xyxy[keep]
                a = ((xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1]) / (768 * 432)) if len(xyxy) else np.zeros(0)
                counts.append(len(xyxy))
                areas.append(float(a.max()) if a.size else 0.0)
                cx = ((xyxy[:, 0] + xyxy[:, 2]) / 2 / 768) if len(xyxy) else np.zeros(0)
                cy = ((xyxy[:, 1] + xyxy[:, 3]) / 2 / 432) if len(xyxy) else np.zeros(0)
                # area-weighted centroid of detections (for smart vertical crop)
                if a.size:
                    boxes_all.append((float((cx * a).sum() / a.sum()), float((cy * a).sum() / a.sum())))
                else:
                    boxes_all.append((np.nan, np.nan))
        np.savez_compressed(out, t=np.arange(len(counts), dtype=np.float32), n=np.array(counts, np.float32),
                            area=np.array(areas, np.float32), centroid=np.array(boxes_all, np.float32).reshape(-1, 2))
        print(f"  gliders {name}: max {max(counts) if counts else 0}, mean {np.mean(counts) if counts else 0:.1f}")


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    index = load_index(prj)
    jobs = []
    for name, e in sorted(index.items(), key=lambda kv: kv[1]["order"]):
        if e["error"]:
            continue
        out = prj.work("visual", Path(name).stem + ".npz")
        if out.exists() and not force:
            continue
        jobs.append((name, e["proxy"] or e["path"], e["meta"]["duration"], out))
    if not args or "visual" in args:
        with ProcessPoolExecutor(6) as ex:
            for name, err in ex.map(analyze_clip, jobs):
                print(f"  visual {name}: {'OK' if not err else err}")
    if not args or "gliders" in args:
        detect_gliders(prj, index, force)


def load_visual(prj: Project, clip: str) -> dict:
    return dict(np.load(prj.work("visual", Path(clip).stem + ".npz")))


def load_gliders(prj: Project, clip: str) -> dict | None:
    p = prj.work("gliders", Path(clip).stem + ".npz")
    return dict(np.load(p)) if p.exists() else None

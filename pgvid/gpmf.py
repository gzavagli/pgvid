"""Minimal GoPro GPMF (telemetry) parser.

Extracts the `gpmd` data track with ffmpeg and decodes the numeric sensor
streams (ACCL, GYRO, GRAV, CORI, YAVG, ISOE, SHUT ...) into numpy arrays with
per-sample timestamps (seconds from the start of the clip), using the STMP
(microsecond) timestamps carried by each stream payload.
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import numpy as np

# GPMF type char -> numpy big-endian dtype
_DT = {
    "b": ">i1", "B": ">u1", "s": ">i2", "S": ">u2", "l": ">i4", "L": ">u4",
    "f": ">f4", "d": ">f8", "j": ">i8", "J": ">u8", "q": ">u4", "Q": ">u8",
}

NUMERIC_STREAMS = ("ACCL", "GYRO", "GRAV", "CORI", "IORI", "YAVG", "ISOE", "SHUT", "WBAL")


def _iter_klv(buf: bytes, start: int, end: int):
    o = start
    while o + 8 <= end:
        key = buf[o:o + 4].decode("latin1")
        typ = chr(buf[o + 4]) if buf[o + 4] else "\0"
        size = buf[o + 5]
        rep = struct.unpack(">H", buf[o + 6:o + 8])[0]
        n = size * rep
        yield key, typ, size, rep, o + 8, o + 8 + n
        o += 8 + ((n + 3) & ~3)


def extract_gpmd(mp4: Path, stream_index: int | None = None) -> bytes:
    """Return the concatenated raw gpmd payloads of a GoPro MP4."""
    if stream_index is None:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "d",
             "-show_entries", "stream=index,codec_tag_string",
             "-of", "csv=p=0", str(mp4)],
            capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            idx, _, tag = line.partition(",")
            if tag.strip() == "gpmd":
                stream_index = int(idx)
                break
        if stream_index is None:
            raise RuntimeError(f"no GPMF track in {mp4}")
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mp4), "-map", f"0:{stream_index}",
         "-c", "copy", "-f", "data", "-"],
        capture_output=True, check=True)
    return res.stdout


def parse(buf: bytes) -> dict:
    """Parse GPMF payloads.

    Returns {stream_key: {"t": (N,) seconds, "v": (N, k) float array}} plus
    "_meta" with device name and axis order (ORIN).
    """
    chunks: dict[str, list[tuple[float, np.ndarray]]] = {}
    meta: dict[str, str] = {}
    for key, typ, size, rep, a, b in _iter_klv(buf, 0, len(buf)):
        if key != "DEVC":
            continue
        for k2, t2, s2, r2, a2, b2 in _iter_klv(buf, a, b):
            if k2 == "DVNM":
                meta["device"] = buf[a2:b2].decode("latin1").strip("\0")
            if k2 != "STRM":
                continue
            scal = None
            stmp = None
            orin = None
            for k3, t3, s3, r3, a3, b3 in _iter_klv(buf, a2, b2):
                if k3 == "SCAL":
                    scal = np.frombuffer(buf[a3:b3], dtype=_DT[t3]).astype(np.float64)
                elif k3 == "STMP":
                    stmp = int.from_bytes(buf[a3:b3], "big") / 1e6
                elif k3 == "ORIN":
                    orin = buf[a3:b3].decode("latin1")
                elif k3 in NUMERIC_STREAMS and t3 in _DT and r3 > 0:
                    dt = np.dtype(_DT[t3])
                    ncol = s3 // dt.itemsize
                    v = np.frombuffer(buf[a3:b3], dtype=dt).astype(np.float64).reshape(r3, ncol)
                    if scal is not None:
                        v = v / (scal if scal.size in (1, ncol) else scal[:1])
                    chunks.setdefault(k3, []).append((stmp, v))
                    if orin:
                        meta[f"ORIN_{k3}"] = orin
    out: dict = {"_meta": meta}
    for k, lst in chunks.items():
        ts, vs = [], []
        for i, (t0, v) in enumerate(lst):
            n = len(v)
            if t0 is None:
                t0 = float(i)
            if i + 1 < len(lst) and lst[i + 1][0] is not None:
                t1 = lst[i + 1][0]
            else:  # extrapolate with previous rate
                t1 = t0 + (ts[-1][-1] - ts[-1][0]) * n / max(len(ts[-1]) - 1, 1) if ts else t0 + 1.0
            ts.append(t0 + (t1 - t0) * np.arange(n) / n)
            vs.append(v)
        out[k] = {"t": np.concatenate(ts), "v": np.concatenate(vs)}
    return out


def load_clip_telemetry(mp4: Path) -> dict:
    return parse(extract_gpmd(mp4))

"""Recover a GoPro MP4 whose `moov` atom is missing (camera powered off while recording).

The `mdat` payload is still intact: interleaved HEVC samples (4-byte length
prefixed NAL units), AAC audio and GPMF telemetry. We walk it, keep every run of
valid HEVC NAL units (converted to Annex-B), collect GPMF `DEVC` blocks, skip the
rest, prepend the VPS/SPS/PPS taken from a healthy reference clip recorded with
the same settings, and remux the elementary stream into a new MP4 (no audio).
"""
from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

from .config import Project

MAX_NAL = 20_000_000


def _valid_nal(buf: memoryview, o: int, end: int) -> int:
    """Return NAL length if a plausible length-prefixed HEVC NAL starts at o, else 0."""
    if o + 6 > end:
        return 0
    n = struct.unpack_from(">I", buf, o)[0]
    if n < 2 or n > MAX_NAL or o + 4 + n > end:
        return 0
    b0, b1 = buf[o + 4], buf[o + 5]
    if b0 & 0x80:
        return 0
    typ = (b0 >> 1) & 0x3F
    layer = ((b0 & 1) << 5) | (b1 >> 3)
    tid = b1 & 7
    if layer != 0 or tid == 0 or typ > 40 or 10 <= typ <= 15 or 22 <= typ <= 31:
        return 0
    return n


def _chain_ok(buf: memoryview, o: int, end: int, k: int = 2) -> bool:
    for _ in range(k):
        n = _valid_nal(buf, o, end)
        if not n:
            return False
        o += 4 + n
        if o >= end:
            return True
    return True


def param_sets(ref: Path) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ref), "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb", "-frames:v", "1", "-f", "hevc", "-"],
                       capture_output=True, check=True, stdin=subprocess.DEVNULL)
    data = r.stdout
    # keep only VPS(32) SPS(33) PPS(34)
    out = bytearray()
    parts = data.split(b"\x00\x00\x00\x01")
    for p in parts[1:]:
        if p and ((p[0] >> 1) & 0x3F) in (32, 33, 34):
            out += b"\x00\x00\x00\x01" + p.rstrip(b"\x00")
    return bytes(out)


def recover(src: Path, ref: Path, out_mp4: Path, work: Path) -> dict:
    size = os.path.getsize(src)
    with open(src, "rb") as f:
        data = f.read()
    buf = memoryview(data)
    # locate mdat
    o = 0
    mdat_start = None
    while o + 8 <= size:
        n, t = struct.unpack_from(">I4s", buf, o)
        hdr = 8
        if n == 1:
            n = struct.unpack_from(">Q", buf, o + 8)[0]
            hdr = 16
        if t == b"mdat":
            mdat_start = o + hdr
            break
        if n == 0:
            break
        o += n
    if mdat_start is None:
        raise RuntimeError("no mdat")
    hevc = work / (src.stem + ".hevc")
    gpmf_bin = work / (src.stem + ".gpmf.bin")
    end = size
    o = mdat_start
    nal_count = skipped = 0
    with open(hevc, "wb") as fv, open(gpmf_bin, "wb") as fg:
        fv.write(param_sets(ref))
        while o < end - 8:
            n = _valid_nal(buf, o, end)
            if n and _chain_ok(buf, o, end, 1):
                fv.write(b"\x00\x00\x00\x01")
                fv.write(buf[o + 4:o + 4 + n])
                o += 4 + n
                nal_count += 1
                continue
            if bytes(buf[o:o + 4]) == b"DEVC":
                klen = buf[o + 5] * struct.unpack_from(">H", buf, o + 6)[0]
                blk = 8 + ((klen + 3) & ~3)
                if o + blk <= end:
                    fg.write(buf[o:o + blk])
                    o += blk
                    continue
            # resync: scan forward for the next plausible NAL chain or DEVC
            o += 1
            skipped += 1
            while o < end - 8:
                if buf[o] == 0 and _valid_nal(buf, o, end) and _chain_ok(buf, o, end, 3):
                    break
                if buf[o] == 0x44 and bytes(buf[o:o + 4]) == b"DEVC":
                    break
                o += 1
                skipped += 1
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-r", "30000/1001", "-f", "hevc",
                    "-i", str(hevc), "-c", "copy", "-tag:v", "hvc1", str(out_mp4)], check=True, stdin=subprocess.DEVNULL)
    hevc.unlink(missing_ok=True)
    return {"nals": nal_count, "skipped_bytes": skipped, "gpmf": str(gpmf_bin), "out": str(out_mp4)}


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    """pgvid recover --project p.yaml GX010123.MP4 [GX010124.MP4 ...] [ref=GX010122.MP4]

    The reference must be a healthy clip recorded with the same resolution/fps
    settings; by default the closest lower-numbered readable clip is used.
    """
    from .index import probe
    args = args or []
    ref_arg = next((a.split("=", 1)[1] for a in args if a.startswith("ref=")), None)
    names = [a for a in args if not a.startswith("ref=")]
    if not names:
        raise SystemExit("usage: pgvid recover --project p.yaml CLIP.MP4 [CLIP2.MP4 ...] [ref=HEALTHY.MP4]")
    for name in names:
        src = prj.footage_dir / Path(name).name
        out = prj.work("recovered", src.name)
        if out.exists() and not force:
            print(f"{name}: already recovered -> {out}")
            continue
        if ref_arg:
            ref = prj.footage_dir / Path(ref_arg).name
        else:  # closest lower-numbered healthy clip (GoPro naming GXccnnnn)
            num = int(src.stem[4:8])
            ref = next((p for k in range(1, 50) if (p := prj.footage_dir / f"GX01{num - k:04d}.MP4").exists()
                        and probe(p)), None)
        if ref is None or not ref.exists():
            raise SystemExit(f"{name}: no healthy reference clip found, pass ref=CLIP.MP4")
        info = recover(src, ref, out, prj.work("recovered", "x").parent)
        meta = probe(out)
        print(f"{name}: recovered {info['nals']} NALs, skipped {info['skipped_bytes']/1e6:.1f} MB, "
              f"duration {meta['duration'] if meta else '?'} s -> {out}")

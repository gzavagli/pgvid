"""Render an EDL to a finished video.

Modes:
  preview  - LRV proxies, 720p (long) / 720x1280 (short), fast x264
  final    - 4K HEVC sources, 3840x2160 / 1080x1920, x264 CRF 17 preset slow

Each EDL shot becomes an intermediate segment (video + camera-audio WAV). Overlays
(HUD, title, lower third, end screen, captions) and task cards are generated with
Pillow and streamed to ffmpeg as raw RGBA. Segments are concatenated without
re-encoding, then muxed with the music/camera audio mix normalised to -14 LUFS.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from . import cards, maps
from .analyze import load_gliders, load_visual
from .config import Project
from .igc import load_tracks
from .index import load_index
from .sync import load_sync
from .tasks import chapters

FPS = "30000/1001"
FPSF = 30000 / 1001
SR = 48000


class Ctx:
    def __init__(self, prj: Project, edl: dict, mode: str):
        self.prj, self.edl, self.mode = prj, edl, mode
        self.index = load_index(prj)
        self.sync = load_sync(prj)
        self.tracks = load_tracks(prj.igc_dir)
        self.chapters = chapters(prj, self.tracks)
        self.tz = dt.timezone(dt.timedelta(hours=prj["utc_offset_hours"]))
        fw, fh = edl["width"], edl["height"]
        if mode == "preview":
            scale = 720 / min(fw, fh)
            self.W, self.H = int(round(fw * scale / 2) * 2), int(round(fh * scale / 2) * 2)
        else:
            self.W, self.H = fw, fh
        self.vertical = self.H > self.W
        self.dir = prj.work("render", f"{edl['kind']}_{mode}")
        self.dir.mkdir(parents=True, exist_ok=True)

    def venc(self) -> list[str]:
        if self.mode == "preview":
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-g", "60", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        return ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-g", "60", "-bf", "2", "-x264-params", "keyint=60:min-keyint=60:scenecut=0", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


# ----------------------------------------------------------------------------- per-shot helpers

def _source(ctx: Ctx, clip: str) -> str:
    e = ctx.index[clip]
    if ctx.mode == "preview" and e.get("proxy"):
        return e["proxy"]
    return e["path"]


def _has_audio(path: str) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _window_stats(ctx: Ctx, shot: dict) -> dict:
    vis = load_visual(ctx.prj, shot["clip"])
    a, b = shot["in"], shot["in"] + shot["dur"] * shot.get("speed", 1.0)
    m = (vis["t"] >= a) & (vis["t"] <= b)
    if not m.any():
        m = np.ones_like(vis["t"], bool)
    return {"contrast": float(vis["contrast"][m].mean()), "bright": float(vis["bright"][m].mean()),
            "sat": float(vis["sat"][m].mean())}


def _crop_path(ctx: Ctx, shot: dict) -> tuple[float, float]:
    """Horizontal centre (fraction of width) at shot start/end for the vertical crop."""
    gl = load_gliders(ctx.prj, shot["clip"])
    if gl is None:
        return 0.5, 0.5
    a, b = shot["in"], shot["in"] + shot["dur"] * shot.get("speed", 1.0)
    cen = gl["centroid"]
    t = gl["t"]

    def med(lo, hi):
        m = (t >= lo) & (t <= hi) & np.isfinite(cen[:, 0])
        if not m.any():
            return None
        return float(np.median(cen[m, 0]))

    mid = (a + b) / 2
    xs, xe = med(a - 1, mid), med(mid, b + 1)
    xs = xs if xs is not None else (xe if xe is not None else 0.5)
    xe = xe if xe is not None else xs
    # pull toward the centre (keeps risers/hands framing natural) and limit pan speed
    xs, xe = 0.5 + 0.75 * (xs - 0.5), 0.5 + 0.75 * (xe - 0.5)
    max_pan = 0.06 * shot["dur"]
    xe = xs + float(np.clip(xe - xs, -max_pan, max_pan))
    return xs, xe


def _grade(stats: dict) -> str:
    g = "eq=contrast=1.06:saturation=1.16:gamma=0.98,curves=all='0/0 0.25/0.22 0.75/0.80 1/1'"
    if stats["contrast"] < 42:   # hazy / overcast: extra local contrast and a touch of warmth
        g += ",cas=strength=0.6,colorbalance=rs=0.03:bs=-0.03:rm=0.02:bm=-0.02"
    return g


def _join_nonempty(text: str, sep: str = "  ·  ") -> str:
    """Overlay templates use '|' between parts; empty parts (unset fields) are dropped."""
    return sep.join(p.strip() for p in text.split("|") if p.strip())


# ----------------------------------------------------------------------------- overlay frame generators

def _overlay_gen(ctx: Ctx, shot: dict):
    """Return (generator of RGBA frames, (w, h), (x, y)) or None."""
    ov = shot.get("overlay")
    if not ov:
        return None
    W, H = ctx.W, ctx.H
    n = int(round(shot["dur"] * FPSF))
    prj = ctx.prj
    parts = ov.split("+")
    hud_fn = None
    if "hud" in parts:
        s = ctx.sync.get(shot["clip"])
        if s and s["start_epoch"] is not None and s["day"] is not None:
            tr = ctx.tracks[s["day"]]
            label = ctx.chapters[s["day"]]["label"] if s["day"] < len(ctx.chapters) else ""

            def hud_fn(i):
                ep = s["start_epoch"] + shot["in"] + i / FPSF * shot.get("speed", 1.0)
                clock = dt.datetime.fromtimestamp(ep, ctx.tz).strftime("%H:%M:%S")
                fade = min(1.0, i / 12, (n - i) / 12)
                return cards.hud_frame(W, H, float(tr.at(ep, "alt")), float(tr.at(ep, "vario")),
                                       float(tr.at(ep, "speed")), clock, label, alpha=max(fade, 0))
    if parts == ["hud"]:
        if hud_fn is None:
            return None
        img0, pos = hud_fn(0)

        def gen():
            for i in range(n):
                yield hud_fn(i)[0]
        return gen(), img0.size, pos

    es = prj.section("end_screen")
    long_cfg = prj.section("long")

    def full(i):
        t = i / FPSF
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for p in parts:
            if p == "title":
                img.alpha_composite(cards.title_frame(W, H, t, shot["dur"], prj["event_short"], prj["dates"],
                                                      prj["location"]))
            elif p == "lower_third" and prj["pilot"]:
                detail = _join_nonempty(prj.fmt(long_cfg.get("lower_third", "{glider}|{event_short}")))
                img.alpha_composite(cards.lower_third_frame(W, H, t, min(shot["dur"], 5.0), prj["pilot"], detail))
            elif p == "endscreen":
                lines = [_join_nonempty(prj.fmt(x)) for x in es.get("lines", ["{event_short}|{location}"])]
                if prj.get("overall_text"):
                    lines.append(prj["overall_text"])
                cta = prj.fmt(es.get("cta", "SUBSCRIBE  {channel}")) if prj["channel"] else ""
                img.alpha_composite(cards.end_screen_frame(W, H, t, shot["dur"], cta, [x for x in lines if x],
                                                           heading=es.get("heading", "THANKS FOR FLYING ALONG")))
            elif p == "caption":
                big = 0.145 if ctx.vertical else 0.07
                img.alpha_composite(cards.caption_frame(W, H, t, shot["dur"], shot.get("caption", ""),
                                                        y_frac=0.14 if ctx.vertical else 0.12, size_frac=big))
            elif p == "hud" and hud_fn is not None:
                panel, pos = hud_fn(i)
                img.alpha_composite(panel, pos)
        return img

    def gen():
        for i in range(n):
            yield full(i)
    return gen(), (W, H), (0, 0)


def _feed(proc: subprocess.Popen, frames) -> None:
    try:
        for fr in frames:
            proc.stdin.write(fr.tobytes())
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------- segment renderers

def render_clip(ctx: Ctx, i: int, shot: dict) -> tuple[Path, Path]:
    out_v = ctx.dir / f"seg{i:03d}.mp4"
    out_a = ctx.dir / f"seg{i:03d}.wav"
    src = _source(ctx, shot["clip"])
    speed = shot.get("speed", 1.0)
    dur = shot["dur"]
    src_dur = dur * speed + 0.2
    W, H = ctx.W, ctx.H
    stats = _window_stats(ctx, shot)
    vf = [f"setpts=(PTS-STARTPTS)/{speed}"]
    if ctx.vertical:
        xs, xe = _crop_path(ctx, shot)
        vf.append(f"crop=w=trunc(ih*9/16/2)*2:h=ih:x='clip(({xs}+({xe}-{xs})*t/{dur})*iw-ow/2,0,iw-ow)':y=0")
    vf.append(f"scale={W}:{H}:flags=lanczos:in_range=auto:out_range=tv,setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709")
    if shot.get("stabilize") and ctx.mode == "final":
        vf.append("deshake")
    vf.append(_grade(stats))
    vf.append(f"fps={FPS}")
    if shot.get("fade_in"):
        vf.append(f"fade=t=in:st=0:d={shot['fade_in']}")
    if shot.get("fade_out"):
        vf.append(f"fade=t=out:st={dur - shot['fade_out']:.3f}:d={shot['fade_out']}")
    vf.append(f"trim=duration={dur:.4f},setpts=PTS-STARTPTS,format=yuv420p")
    hw = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" and src.upper().endswith(".MP4") else []
    cmd = ["ffmpeg", "-v", "error", "-y", *hw, "-ss", f"{shot['in']:.3f}", "-t", f"{src_dur:.3f}", "-i", src]
    ov = _overlay_gen(ctx, shot)
    if ov:
        frames, (ow, oh), (ox, oy) = ov
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{ow}x{oh}", "-r", FPS, "-i", "-"]
        fc = f"[0:v]{','.join(vf[:-1])}[base];[1:v]format=rgba[ov];[base][ov]overlay={ox}:{oy}:eof_action=pass:format=auto,{vf[-1]}[v]"
        cmd += ["-filter_complex", fc, "-map", "[v]"]
    else:
        cmd += ["-vf", ",".join(vf), "-map", "0:v:0"]
    cmd += ["-an", *ctx.venc(), "-r", FPS, str(out_v)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if ov else subprocess.DEVNULL)
    if ov:
        th = threading.Thread(target=_feed, args=(p, frames), daemon=True)
        th.start()
        th.join()
    if p.wait() != 0:
        raise RuntimeError(f"segment {i} failed: {' '.join(cmd)}")
    # camera audio
    gain = {"up": -3.0, "low": -15.0, "mute": -90.0}.get(shot.get("audio", "low"), -15.0)
    af = []
    if speed != 1.0:
        af.append(f"atempo={speed}")
    af += [f"volume={gain}dB", "afade=t=in:d=0.08", f"afade=t=out:st={max(dur - 0.12, 0):.3f}:d=0.12",
           f"apad,atrim=0:{dur:.4f}"]
    if _has_audio(src):
        acmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{shot['in']:.3f}", "-t", f"{src_dur:.3f}", "-i", src,
                "-vn", "-af", ",".join(af), "-ar", str(SR), "-ac", "2", str(out_a)]
    else:
        acmd = _silence_cmd(dur, out_a)
    subprocess.run(acmd, check=True, stdin=subprocess.DEVNULL)
    return out_v, out_a


def _silence_cmd(dur: float, out: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo", "-t", f"{dur:.4f}",
            str(out)]


_MAP_LOCK = threading.Lock()


def render_card(ctx: Ctx, i: int, shot: dict) -> tuple[Path, Path]:
    out_v = ctx.dir / f"seg{i:03d}.mp4"
    out_a = ctx.dir / f"seg{i:03d}.wav"
    day = shot["day"] if "day" in shot else shot["task"] - 1   # older EDLs only carry `task`
    chapter = ctx.chapters[day]
    track = ctx.tracks[day]
    task = chapter["task"] or {"num": f"day{day}", "turnpoints": []}
    W, H = ctx.W, ctx.H
    map_size = (int(W * 0.625), H)
    cache = ctx.prj.work("tiles", "x").parent
    with _MAP_LOCK:  # tile fetch + base render once per task (cached on the function)
        maps.render_task_map(task, track, size=map_size, cache=cache, progress=0.0)
        base_cache = dict(maps.render_task_map._cache)

    def map_fn(p):
        maps.render_task_map._cache = base_cache
        return maps.render_task_map(task, track, size=map_size, cache=cache, progress=p)

    rate = 15  # generate at 15 fps, ffmpeg upsamples to 29.97
    n = int(round(shot["dur"] * rate))
    frames = (cards.chapter_card_frame(W, H, k / rate, shot["dur"], chapter, map_fn, ctx.prj["pilot"]).convert("RGB")
              for k in range(n))
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(rate),
           "-i", "-", "-vf", f"fps={FPS},trim=duration={shot['dur']:.4f},setpts=PTS-STARTPTS,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           *ctx.venc(), "-r", FPS, str(out_v)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    _feed(p, frames)
    if p.wait() != 0:
        raise RuntimeError(f"card {i} failed")
    subprocess.run(_silence_cmd(shot["dur"], out_a), check=True, stdin=subprocess.DEVNULL)
    return out_v, out_a


# ----------------------------------------------------------------------------- audio mix

def build_music(ctx: Ctx, total: float, out: Path) -> None:
    plan = ctx.edl["music"]
    inputs, fl = [], []
    for k, m in enumerate(plan):
        d = m["end"] - m["start"]
        inputs += ["-ss", f"{m['in']:.3f}", "-t", f"{d + 0.1:.3f}", "-i", m["file"]]
        f = f"[{k}:a]aresample={SR},aformat=channel_layouts=stereo,atrim=0:{d:.3f}"
        if m.get("fade_in"):
            f += f",afade=t=in:d={m['fade_in']}"
        if m.get("fade_out"):
            f += f",afade=t=out:st={max(d - m['fade_out'], 0):.3f}:d={m['fade_out']}"
        ms = int(m["start"] * 1000)
        f += f",adelay={ms}|{ms}[m{k}]"
        fl.append(f)
    mix = "".join(f"[m{k}]" for k in range(len(plan)))
    fl.append(f"{mix}amix=inputs={len(plan)}:normalize=0:dropout_transition=0,apad,atrim=0:{total:.3f}[out]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fl), "-map", "[out]",
                    "-ar", str(SR), str(out)], check=True, stdin=subprocess.DEVNULL)


def mix_audio(ctx: Ctx, music: Path, camera: Path, total: float, out: Path) -> None:
    # duck music during "up" shots (camera audio featured)
    ducks = [(s["start"], s["start"] + s["dur"]) for s in ctx.edl["shots"] if s.get("audio") == "up"]
    vol = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in ducks) or "0"
    pre = out.with_name("premix.wav")
    fc = (f"[0:a]volume='if({vol},0.55,1)':eval=frame[mu];"
          f"[1:a]volume=1.0[ca];[mu][ca]amix=inputs=2:normalize=0,atrim=0:{total:.3f}[out]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(music), "-i", str(camera), "-filter_complex", fc,
                    "-map", "[out]", "-ar", str(SR), str(pre)], check=True, stdin=subprocess.DEVNULL)
    # pass 1: measure integrated loudness
    r = subprocess.run(["ffmpeg", "-nostats", "-i", str(pre), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    integrated = float(m[-1]) if m else -14.0
    gain = -14.0 - integrated
    # pass 2: exact gain + true-peak limiter (-1.5 dBTP)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(pre), "-af",
                    f"volume={gain:.2f}dB,alimiter=limit=0.75:attack=1:release=60:level=false",
                    "-ar", str(SR), str(out)], check=True, stdin=subprocess.DEVNULL)


# ----------------------------------------------------------------------------- main

def render(prj: Project, kind: str, mode: str, only: list[int] | None = None, workers: int | None = None) -> Path:
    edl = yaml.safe_load(prj.work("edl", f"{kind}.yaml").read_text())
    ctx = Ctx(prj, edl, mode)
    shots = edl["shots"]
    workers = workers or (4 if mode == "preview" else 3)

    def job(i):
        s = shots[i]
        v = ctx.dir / f"seg{i:03d}.mp4"
        sig = json.dumps(s, sort_keys=True)
        sigf = ctx.dir / f"seg{i:03d}.json"
        if v.exists() and sigf.exists() and sigf.read_text() == sig and (only is None or i not in only):
            return i
        (render_card if s["type"] == "card" else render_clip)(ctx, i, s)
        sigf.write_text(sig)
        print(f"  [{kind}/{mode}] seg {i + 1}/{len(shots)} {s['type']} {s.get('clip', s.get('task', ''))}", flush=True)
        return i

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(job, range(len(shots))))
    # concat video + camera audio
    lst = ctx.dir / "list.txt"
    lst.write_text("".join(f"file '{ctx.dir / f'seg{i:03d}.mp4'}'\n" for i in range(len(shots))))
    video = ctx.dir / "video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    str(video)], check=True, stdin=subprocess.DEVNULL)
    alst = ctx.dir / "alist.txt"
    alst.write_text("".join(f"file '{ctx.dir / f'seg{i:03d}.wav'}'\n" for i in range(len(shots))))
    camera = ctx.dir / "camera.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(alst), "-c", "copy",
                    str(camera)], check=True, stdin=subprocess.DEVNULL)
    total = edl["duration"]
    music = ctx.dir / "music.wav"
    build_music(ctx, total, music)
    mixed = ctx.dir / "mix.wav"
    mix_audio(ctx, music, camera, total, mixed)
    name = f"{prj['name']}_{kind}{'_preview' if mode == 'preview' else ''}.mp4"
    out = prj.out(name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(mixed), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "320k" if mode == "final" else "160k",
                    "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)], check=True, stdin=subprocess.DEVNULL)
    print(f"-> {out}")
    return out


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    args = args or ["short", "preview"]
    kinds = [a for a in args if a in ("long", "short")] or ["long", "short"]
    mode = "final" if "final" in args else "preview"
    if force:
        for k in kinds:
            shutil.rmtree(prj.work("render", f"{k}_{mode}"), ignore_errors=True)
    for k in kinds:
        render(prj, k, mode)

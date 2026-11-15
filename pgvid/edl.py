"""Automatic edit decision lists (EDL) for the long video and the Short.

The EDL is plain YAML so it can be reviewed and hand-edited before rendering:

  music:   [{track, in, start, end, gain}]        # timeline placement of tracks
  shots:   [{type: clip|card, start, dur, ...}]   # contiguous timeline

Cut points are snapped to the composite music beat grid.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import yaml

from .beats import load_beats
from .config import Project
from .igc import load_tracks
from .index import load_index
from .sync import load_sync
from .tasks import chapters

# nominal shot lengths (s) per category, long video
NOMINAL = {"launch": 5.0, "gaggle": 4.5, "close_pass": 3.5, "strong_thermal": 4.0, "fast_glide": 3.5,
           "clouds": 4.5, "scenery": 4.5, "goal_landing": 5.0}
CAT_CAP = {"launch": 2, "gaggle": 4, "close_pass": 3, "strong_thermal": 3, "fast_glide": 3, "clouds": 3,
           "scenery": 3, "goal_landing": 2}
DUCK_UP = {"launch", "strong_thermal", "goal_landing"}


# ----------------------------------------------------------------------------- helpers

def _hamming(a, b) -> int:
    return bin(int(a) ^ int(b)).count("1")


def _overlaps(a: dict, b: dict, pad: float = 8.0) -> bool:
    return a["clip"] == b["clip"] and not (a["start"] + a["dur"] + pad <= b["start"] or b["start"] + b["dur"] + pad <= a["start"])


def pick(segs: list[dict], n: int, used: list[dict], caps: dict | None = None, must: tuple = (),
         clip_cap: int = 4) -> list[dict]:
    caps = caps or CAT_CAP
    cnt: dict[str, int] = {}
    per_clip: dict[str, int] = {}
    out: list[dict] = []

    def ok(s):
        if cnt.get(s["category"], 0) >= caps.get(s["category"], 99):
            return False
        if per_clip.get(s["clip"], 0) >= clip_cap:
            return False
        for o in out + used:
            if _overlaps(s, o) or _hamming(s["phash"], o["phash"]) < 10:
                return False
        return True

    def take(s):
        out.append(s)
        cnt[s["category"]] = cnt.get(s["category"], 0) + 1
        per_clip[s["clip"]] = per_clip.get(s["clip"], 0) + 1

    ranked = sorted(segs, key=lambda s: -s["score"])
    for cat in must:
        s = next((s for s in ranked if s["category"] == cat and ok(s)), None)
        if s:
            take(s)
    for s in ranked:
        if len(out) >= n:
            break
        if ok(s):
            take(s)
    return out


class BeatGrid:
    def __init__(self, beats: list[float], downbeats: list[float]):
        self.b = np.array(sorted(beats))
        self.db = np.array(sorted(downbeats))

    def snap(self, t: float, lo: float, hi: float, down: bool = False) -> float:
        arr = self.db if down and len(self.db) else self.b
        c = arr[(arr >= lo) & (arr <= hi)]
        if c.size == 0:
            return t
        return float(c[np.argmin(np.abs(c - t))])


def music_plan(prj: Project, tracks: list[tuple[str, float, float]], total: float, xfade: float = 2.0):
    """tracks: [(name, track_in, timeline_start)] -> plan list + composite beat grid."""
    mb = load_beats(prj)
    plan, beats, downs = [], [], []
    for i, (name, tin, tstart) in enumerate(tracks):
        m = mb[name]
        tend = tracks[i + 1][2] + xfade if i + 1 < len(tracks) else total
        tend = min(tend, tstart + m["duration"] - tin)
        plan.append({"track": name, "file": m["path"], "in": round(tin, 3), "start": round(tstart, 3),
                     "end": round(tend, 3), "fade_in": xfade if i else 0.0,
                     "fade_out": xfade if i + 1 < len(tracks) else 3.0})
        lo = tstart + (xfade if i else 0)
        hi = tracks[i + 1][2] if i + 1 < len(tracks) else total
        beats += [b - tin + tstart for b in m["beats"] if lo <= b - tin + tstart <= hi]
        downs += [b - tin + tstart for b in m["downbeats"] if lo <= b - tin + tstart <= hi]
    return plan, BeatGrid(beats, downs)


def clip_shot(seg: dict, out_start: float, out_dur: float, index: dict, speed: float = 1.0, **kw) -> dict:
    src_dur = out_dur * speed
    clip_len = index[seg["clip"]]["meta"]["duration"]
    center = seg["start"] + seg["dur"] / 2
    s_in = min(max(center - src_dur / 2, 0.2), max(clip_len - src_dur - 0.2, 0.0))
    shot = {"type": "clip", "clip": seg["clip"], "in": round(s_in, 3), "speed": speed,
            "start": round(out_start, 3), "dur": round(out_dur, 3), "category": seg["category"],
            "score": seg["score"], "day": seg["day"]}
    shot.update(kw)
    return shot


def _chrono_key(s: dict, index: dict):
    return (s["epoch"] if s.get("epoch") else 1e12, index[s["clip"]]["order"], s["start"])


def default_music(prj: Project, n_chapters: int, max_tracks: int = 3) -> list[list]:
    """No `long.music` configured: use the analysed tracks, switching on chapter cards."""
    names = sorted(load_beats(prj))
    if not names:
        raise ValueError(f"no music analysed: put tracks in {prj.music_dir} and run `pgvid beats`")
    names = names[:max(1, min(max_tracks, n_chapters))]
    return [[n, 0.0, round(i * n_chapters / len(names))] for i, n in enumerate(names)]


# ----------------------------------------------------------------------------- long video

def build_long(prj: Project, target: float | None = None) -> dict:
    index = load_index(prj)
    sync = load_sync(prj)
    tracks = load_tracks(prj.igc_dir)
    chaps = chapters(prj, tracks)
    if not chaps:
        raise ValueError(f"no IGC tracks in {prj.igc_dir}")
    segs = json.loads(prj.work("segments.json").read_text())
    style = prj.section("long")
    target = target or float(style.get("target_seconds", 420))
    teaser_n, title_d, card_d, outro_d = 7, 5.0, 7.0, 18.0
    teaser_d = 15.0
    n_tasks = len(chaps)
    per_task = (target - teaser_d - title_d - outro_d) / n_tasks - card_d

    # tentative chapter starts to place the music switches on chapter cards
    chap_starts = [teaser_d + title_d + i * (card_d + per_task) for i in range(n_tasks)]
    tracks_cfg = style.get("music") or default_music(prj, n_tasks)
    mtracks = [(name, tin, 0.0 if ch == 0 else chap_starts[min(ch, n_tasks - 1)] - 1.0) for name, tin, ch in tracks_cfg]
    plan, grid = music_plan(prj, mtracks, target + 5)

    used: list[dict] = []
    shots: list[dict] = []
    t = 0.0
    # --- teaser: best action across all days
    flying = [s for s in segs if s["category"] in ("gaggle", "close_pass", "strong_thermal", "clouds")]
    teaser = pick(flying, teaser_n, used, caps={"gaggle": 3, "close_pass": 2, "strong_thermal": 1, "clouds": 2})
    used += teaser
    for i, s in enumerate(teaser):
        nominal = teaser_d / teaser_n
        end = grid.snap(t + nominal, t + nominal * 0.7, t + nominal * 1.3, down=(i == len(teaser) - 1))
        shots.append(clip_shot(s, t, end - t, index, overlay=None, audio="low", section="teaser"))
        t = end
    # --- title over a scenic shot
    scen = pick([s for s in segs if s["category"] in ("scenery", "clouds")], 1, used)
    used += scen
    end = grid.snap(t + title_d, t + title_d - 1, t + title_d + 1, down=True)
    shots.append(clip_shot(scen[0], t, end - t, index, overlay="title", audio="low", section="title"))
    t = end
    # --- chapters (one per flight day)
    chapters_out = []
    for ti, chap in enumerate(chaps):
        day = chap["day"]
        sec = f"task{chap['num']}" if chap["task"] else f"day{day + 1}"
        chapters_out.append({"day": day, "task": chap["num"], "title": chap["label"].title(), "start": round(t, 3)})
        end = grid.snap(t + card_d, t + card_d - 1, t + card_d + 1, down=True)
        shots.append({"type": "card", "card": "chapter", "day": day, "task": chap["num"], "start": round(t, 3),
                      "dur": round(end - t, 3), "section": sec})
        t = end
        chap_end_target = chap_starts[ti] + card_d + per_task if ti + 1 < n_tasks else target - outro_d
        dsegs = [s for s in segs if s["day"] == day]
        n_shots = max(6, int(round((chap_end_target - t) / 4.0)))
        sel = pick(dsegs, n_shots, used, must=("launch", "goal_landing", "gaggle", "strong_thermal"))
        used += sel
        sel.sort(key=lambda s: _chrono_key(s, index))
        remaining = chap_end_target - t
        weights = np.array([NOMINAL[s["category"]] for s in sel])
        lens = weights / weights.sum() * remaining
        for k, (s, L) in enumerate(zip(sel, lens)):
            last = k == len(sel) - 1
            tgt = chap_end_target if last else t + L
            end = grid.snap(tgt, t + L * 0.7, t + L * 1.35, down=last)
            speed = 1.5 if s["category"] == "fast_glide" else 1.0
            sync_c = sync.get(s["clip"], {})
            hud = sync_c.get("confidence") in ("high", "medium") and s.get("alt") is not None
            ov = "hud" if hud else None
            if ti == 0 and k == 1 and prj["pilot"]:
                ov = "lower_third+hud" if hud else "lower_third"
            shots.append(clip_shot(s, t, end - t, index, speed=speed, overlay=ov,
                                   audio="up" if s["category"] in DUCK_UP else "low",
                                   section=sec, fade_in=0.35 if k == 0 else 0.0))
            t = end
    # --- outro with end screen
    outro = pick([s for s in segs if s["category"] in ("scenery", "clouds", "goal_landing")], 2, used)
    used += outro
    for i, s in enumerate(outro):
        d = outro_d / len(outro)
        shots.append(clip_shot(s, t, d, index, overlay="endscreen" if i == len(outro) - 1 else None,
                               audio="low", section="outro", fade_out=1.5 if i == len(outro) - 1 else 0.0))
        t += d
    total = t
    plan[-1]["end"] = round(min(plan[-1]["end"], total), 3)
    return {"kind": "long", "width": 3840, "height": 2160, "fps": "30000/1001", "duration": round(total, 3),
            "music": plan, "chapters": chapters_out, "shots": shots}


# ----------------------------------------------------------------------------- Short

def build_short(prj: Project, total: float = 30.0) -> dict:
    index = load_index(prj)
    segs = json.loads(prj.work("segments.json").read_text())
    mb = load_beats(prj)
    sty = prj.section("short")
    if not mb:
        raise ValueError(f"no music analysed: put tracks in {prj.music_dir} and run `pgvid beats`")
    name = sty.get("music") or sorted(mb)[0]
    m = mb[name]
    drop = sty.get("drop") or m["drops"][0]
    hook = 2.0
    db = np.array(m["downbeats"])
    tin = float(db[np.argmin(np.abs(db - (drop - hook)))])
    plan = [{"track": name, "file": m["path"], "in": round(tin, 3), "start": 0.0, "end": total,
             "fade_in": 0.0, "fade_out": 1.0}]
    grid = BeatGrid([b - tin for b in m["beats"] if tin <= b <= tin + total],
                    [b - tin for b in m["downbeats"] if tin <= b <= tin + total])
    beat = 60.0 / m["tempo"]
    caps = {"launch": 1, "gaggle": 4, "close_pass": 3, "strong_thermal": 2, "fast_glide": 1, "clouds": 2,
            "scenery": 1, "goal_landing": 1}
    # prefer shots with detections (better vertical crop) and high quality
    sel = pick(segs, 13, [], caps=caps, must=("gaggle", "close_pass", "launch", "clouds", "goal_landing"))
    hook_pool = [s for s in sel if s["category"] in ("gaggle", "close_pass")] or sel
    hook_seg = max(hook_pool, key=lambda s: s["score"])
    rest = [s for s in sel if s is not hook_seg]
    order_pref = ["launch", "close_pass", "gaggle", "clouds", "strong_thermal", "fast_glide", "gaggle",
                  "close_pass", "scenery", "clouds", "gaggle", "goal_landing"]
    ordered = []
    for cat in order_pref:
        s = next((x for x in rest if x["category"] == cat and x not in ordered), None)
        if s:
            ordered.append(s)
    ordered += [x for x in rest if x not in ordered]
    shots = []
    t = 0.0
    end = grid.snap(hook, hook - 0.4, hook + 0.4, down=True)
    hook_text = prj.fmt(sty["hook"]) if sty.get("hook") else (prj["event_short"].upper() or "CROSS COUNTRY")
    shots.append(clip_shot(hook_seg, 0.0, end, index, overlay="caption", caption=hook_text,
                           crop="auto", audio="low", section="hook"))
    t = end
    body_end = total - 2.6
    for s in ordered:
        if t >= body_end - 1.3:
            break
        n_beats = 4 if len(shots) % 3 == 1 else 6
        tgt = t + n_beats * beat
        end = grid.snap(tgt, tgt - beat * 0.6, tgt + beat * 0.6)
        if end > body_end - 1.3:   # don't leave a sliver: run this shot to the end of the body
            end = body_end
        shots.append(clip_shot(s, t, end - t, index, speed=1.3 if s["category"] == "fast_glide" else 1.0,
                               crop="auto", audio="low", section="body"))
        t = end
    # loop ending: continuation of the hook clip just before the hook point
    loop_seg = dict(hook_seg)
    loop_seg["start"] = max(0.0, hook_seg["start"] - 3.5)
    shots.append(clip_shot(loop_seg, t, total - t, index, crop="auto", overlay="caption",
                           caption=prj.fmt(sty.get("outro") or "FULL VIDEO\nON MY CHANNEL"), audio="low",
                           section="loop"))
    return {"kind": "short", "width": 1080, "height": 1920, "fps": "30000/1001", "duration": total,
            "music": plan, "shots": shots}


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    which = args or ["long", "short"]
    for w in which:
        out = prj.work("edl", f"{w}.yaml")
        if out.exists() and not force:
            print(f"{out} exists (use --force to rebuild; hand edits would be lost)")
            continue
        edl = build_long(prj) if w == "long" else build_short(prj)
        edl = json.loads(json.dumps(edl, default=float))
        out.write_text(yaml.safe_dump(edl, sort_keys=False, allow_unicode=True, width=140))
        n = sum(1 for s in edl["shots"] if s["type"] == "clip")
        print(f"{w}: {edl['duration']:.1f}s, {n} clips, {len(edl['shots']) - n} cards -> {out}")

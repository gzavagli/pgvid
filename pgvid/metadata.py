"""YouTube package: titles, descriptions (with chapters + credits), tags, thumbnails.

All wording comes from the `youtube:` section of the project YAML. Templates use
`{placeholders}`: the project text fields (event, event_short, location, dates,
pilot, glider, channel) plus n_chapters, n_tasks, n_pilots and results_url.
Anything not set falls back to the neutral defaults below.
"""
from __future__ import annotations

import subprocess
from io import BytesIO

import yaml
from PIL import Image, ImageDraw, ImageEnhance

from . import cards
from .config import Project
from .igc import load_tracks
from .index import load_index
from .maps import ATTRIBUTION
from .tasks import chapters

DEFAULTS = {
    "long_title": "{event_short} | Paragliding in {location} (Cockpit POV)",
    "long_intro": "Cross-country paragliding at the {event} in {location} ({dates}), filmed from the cockpit.",
    "gear": "",
    "notes": ["In-video flight data (altitude, climb rate, ground speed) comes from the IGC tracklogs."],
    "result_prefix": "my result",
    "hashtags": "#paragliding #xcflying #freeflight",
    "tags": ["paragliding", "cross country flying", "thermal flying", "free flight", "paraglider POV"],
    "short_title": "{event_short} #Shorts",
    "short_description": "{event_short} in {location}.",
    "short_hashtags": "#Shorts #paragliding #flying",
    "thumbnail_long": ["{event_short}", "{n_chapters} DAYS IN THE AIR"],
    "thumbnail_short": ["{event_short}", ""],
}


def _ts(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def music_credits(edl: dict) -> list[str]:
    """Track file names are expected as 'Title - Artist' ('_' stands for '&')."""
    out = []
    for m in edl["music"]:
        title, _, artist = m["track"].partition(" - ")
        out.append(f"♪ \"{title.strip()}\" by {artist.replace('_', '&').strip()}" if artist else f"♪ {title.strip()}")
    return list(dict.fromkeys(out))


class Copy:
    """Resolved `youtube:` templates for one project."""

    def __init__(self, prj: Project, chaps: list[dict]):
        self.prj = prj
        self.cfg = {**DEFAULTS, **prj.section("youtube")}
        tasks = [c["task"] for c in chaps if c["task"]]
        comp = prj.section("competition")
        self.values = prj.text_values(
            n_chapters=len(chaps), n_tasks=len(tasks),
            n_pilots=max([t.get("pilots") or 0 for t in tasks], default=0) or "",
            results_url=comp.get("results_url", ""))

    def __call__(self, key: str) -> str:
        from .config import render_template
        return render_template(self.cfg.get(key) or "", self.values)

    def list(self, key: str) -> list[str]:
        from .config import render_template
        v = self.cfg.get(key) or []
        return [render_template(x, self.values) for x in ([v] if isinstance(v, str) else v)]


def long_description(prj: Project, edl: dict, chaps: list[dict], copy: Copy) -> str:
    ch = [("0:00", "Intro")]
    title_shot = next(s for s in edl["shots"] if s.get("section") == "title")
    ch.append((_ts(title_shot["start"]), f"{prj['event_short']}"))
    for c, chap in zip(edl["chapters"], chaps):
        kind = (chap.get("type") or "").lower()
        ch.append((_ts(c["start"]), f"{chap['label'].title()}: {chap.get('distance_km')} km {kind}".rstrip()))
    outro = next(s for s in edl["shots"] if s.get("section") == "outro")
    ch.append((_ts(outro["start"]), "Outro"))
    has_tasks = any(c["task"] for c in chaps)
    lines = [copy("long_intro"), "", "Chapters", *[f"{a} {b}" for a, b in ch], "",
             "Tasks" if has_tasks else "Flights"]
    for chap in chaps:
        t = chap["task"]
        if t:
            r = t.get("result") or {}
            res = (f" · {copy('result_prefix')}: #{r['rank']}"
                   + (f", goal in {r['time']}" if r.get("time") else f", {r.get('distance_km')} km") if r else "")
            goal = (t.get("stats") or {}).get("no_of_pilots_reaching_goal")
            lines.append(f"• Task {t['num']} ({t.get('date')}): {t.get('distance_km')} km {t.get('type', '').lower()}"
                         + (f", {goal} pilots in goal" if goal else "") + res)
        else:
            a = int(chap.get("airtime_s") or 0)
            lines.append(f"• {chap['label'].title()} ({chap['date']}): {chap['distance_km']} km flown, "
                         f"{a // 3600}:{a % 3600 // 60:02d} airtime")
    if prj.get("overall_text"):
        lines.append(prj["overall_text"])
    if copy.values.get("results_url"):
        lines += ["", f"Results & tracks: {copy.values['results_url']}"]
    extra = [x for x in [copy("gear"), *copy.list("notes")] if x]
    if extra:
        lines += ["", *extra]
    lines += ["", "Music", *music_credits(edl), "", f"Task maps: {ATTRIBUTION}" if has_tasks else f"Maps: {ATTRIBUTION}",
              "", copy("hashtags")]
    return "\n".join(lines)


def short_description(prj: Project, edl: dict, copy: Copy) -> str:
    return "\n".join([copy("short_description"), "", "Music", *music_credits(edl), "", copy("short_hashtags")])


def best_frame(prj: Project, edl: dict, index: dict, section_pref=("teaser",)) -> Image.Image:
    shot = next(s for s in edl["shots"] if s["type"] == "clip" and s.get("section") in section_pref)
    src = index[shot["clip"]]["path"]
    t = shot["in"] + shot["dur"] * shot.get("speed", 1.0) / 2
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1", "-f", "image2pipe",
                        "-vcodec", "png", "-"], capture_output=True, check=True, stdin=subprocess.DEVNULL)
    return Image.open(BytesIO(r.stdout)).convert("RGB")


def thumbnail(frame: Image.Image, size: tuple[int, int], top: str, bottom: str) -> Image.Image:
    W, H = size
    fr = frame
    if W / H < fr.width / fr.height:  # crop to aspect
        nw = int(fr.height * W / H)
        fr = fr.crop(((fr.width - nw) // 2, 0, (fr.width + nw) // 2, fr.height))
    img = fr.resize(size, Image.LANCZOS)
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.25)).enhance(1.1).convert("RGBA")
    grad = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(H):
        a = int(170 * max(0.0, (y / H - 0.45) / 0.55) ** 1.4)
        gd.line((0, y, W, y), fill=(0, 0, 0, a))
    img.alpha_composite(grad)
    s = W / 1280
    f1 = cards.font("heavy", int(150 * s))
    f2 = cards.font("heavy", int(84 * s))
    d = ImageDraw.Draw(img)
    y = H - int(300 * s) if W > H else int(H * 0.62)
    for txt, f, col in ((top, f1, cards.WHITE), (bottom, f2, cards.ACCENT2)):
        if not txt:
            continue
        w = d.textlength(txt, font=f)
        x = (W - w) / 2 if W < H else int(50 * s)
        cards._shadow_text(img, (x, y), txt, f, fill=col, shadow=int(10 * s))
        y += int(f.size * 1.05)
    return img.convert("RGB")


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    index = load_index(prj)
    chaps = chapters(prj, load_tracks(prj.igc_dir))
    copy = Copy(prj, chaps)
    out = prj.out("youtube")
    out.mkdir(parents=True, exist_ok=True)
    long_edl = yaml.safe_load(prj.work("edl", "long.yaml").read_text())
    short_edl = yaml.safe_load(prj.work("edl", "short.yaml").read_text())
    tags = copy.list("tags")
    (out / "long_title.txt").write_text(copy("long_title") + "\n")
    (out / "long_description.txt").write_text(long_description(prj, long_edl, chaps, copy))
    (out / "long_tags.txt").write_text(", ".join(tags))
    (out / "short_title.txt").write_text(copy("short_title") + "\n")
    (out / "short_description.txt").write_text(short_description(prj, short_edl, copy))
    (out / "short_tags.txt").write_text(", ".join(tags[:10]))
    tl, tb = (copy.list("thumbnail_long") + ["", ""])[:2]
    sl, sb = (copy.list("thumbnail_short") + ["", ""])[:2]
    fr = best_frame(prj, long_edl, index)
    thumbnail(fr, (1280, 720), tl.upper(), tb.upper()).save(out / "long_thumbnail.jpg", quality=92)
    fr2 = best_frame(prj, short_edl, index, ("hook",))
    thumbnail(fr2, (1080, 1920), sl.upper(), sb.upper()).save(out / "short_cover.jpg", quality=92)
    print("youtube package ->", out)

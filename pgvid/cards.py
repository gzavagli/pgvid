"""On-screen graphics (Pillow): title, chapter cards, lower third, end screen, HUD.

All graphics are rendered at the output resolution. Animated graphics are
streamed as raw RGBA frames to ffmpeg (see render.py) to avoid huge PNG dumps.

Fonts: the bundled Barlow Condensed (SIL OFL, see assets/fonts/OFL.txt) is used
unless the project YAML overrides a role:

    fonts:
      heavy: ["/System/Library/Fonts/Avenir Next Condensed.ttc", "Heavy"]  # [file, style in collection]
      num: "/path/to/SomeFont-Bold.ttf"
"""
from __future__ import annotations

import datetime as dt
import functools
import math
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

_ASSETS = Path(__file__).resolve().parent / "assets" / "fonts"
BUNDLED_FONTS: dict[str, tuple[str, str | None]] = {
    "heavy": (str(_ASSETS / "BarlowCondensed-ExtraBold.ttf"), None),
    "bold": (str(_ASSETS / "BarlowCondensed-Bold.ttf"), None),
    "demi": (str(_ASSETS / "BarlowCondensed-SemiBold.ttf"), None),
    "regular": (str(_ASSETS / "BarlowCondensed-Medium.ttf"), None),
    "num": (str(_ASSETS / "BarlowCondensed-SemiBold.ttf"), None),
    "din": (str(_ASSETS / "BarlowCondensed-Bold.ttf"), None),
}
FONT_FILES: dict[str, tuple[str, str | None]] = dict(BUNDLED_FONTS)
ACCENT = (255, 122, 26)      # paraglider orange
ACCENT2 = (255, 196, 0)
WHITE = (255, 255, 255)


def configure_fonts(cfg: dict | None) -> None:
    """Apply `fonts:` overrides from the project YAML; missing files fall back to the bundled font."""
    FONT_FILES.clear()
    FONT_FILES.update(BUNDLED_FONTS)
    for kind, spec in (cfg or {}).items():
        if kind not in BUNDLED_FONTS:
            warnings.warn(f"fonts: unknown role '{kind}' (roles: {', '.join(BUNDLED_FONTS)})")
            continue
        path, style = (spec, None) if isinstance(spec, str) else (spec[0], spec[1] if len(spec) > 1 else None)
        path = str(Path(path).expanduser())
        if not Path(path).is_file():
            warnings.warn(f"fonts: {path} not found, using the bundled font for '{kind}'")
            continue
        FONT_FILES[kind] = (path, style)
    font.cache_clear()


@functools.lru_cache(maxsize=128)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path, style = FONT_FILES[kind]
    if style is None:
        return ImageFont.truetype(path, size)
    for i in range(24):
        try:
            f = ImageFont.truetype(path, size, index=i)
        except OSError:
            break
        if f.getname()[1].lower() == style.lower():
            return f
    return ImageFont.truetype(path, size)


def ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def _shadow_text(img: Image.Image, xy, text, f, fill=WHITE, shadow=8, alpha=255):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((xy[0] + shadow * 0.4, xy[1] + shadow * 0.6), text, font=f, fill=(0, 0, 0, int(160 * alpha / 255)))
    layer = layer.filter(ImageFilter.GaussianBlur(shadow))
    d = ImageDraw.Draw(layer)
    d.text(xy, text, font=f, fill=fill + (alpha,) if len(fill) == 3 else fill)
    img.alpha_composite(layer)


# --------------------------------------------------------------------------- title

def title_frame(W: int, H: int, t: float, dur: float, event: str, sub: str, place: str) -> Image.Image:
    """Transparent overlay: big event title with animated accent bar."""
    s = W / 3840
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    a_in = ease(t / 0.6)
    a_out = 1 - ease((t - (dur - 0.5)) / 0.5)
    a = a_in * a_out
    # vignette band
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    db = ImageDraw.Draw(band)
    db.rectangle((0, H * 0.34, W, H * 0.70), fill=(0, 0, 0, int(110 * a)))
    band = band.filter(ImageFilter.GaussianBlur(60 * s))
    img.alpha_composite(band)
    f1 = font("heavy", int(250 * s))
    f2 = font("demi", int(110 * s))
    f3 = font("regular", int(80 * s))
    d = ImageDraw.Draw(img)
    w1 = d.textlength(event.upper(), font=f1)
    x = (W - w1) / 2
    y = H * 0.38 + (1 - a_in) * 60 * s
    _shadow_text(img, (x, y), event.upper(), f1, alpha=int(255 * a))
    bw = w1 * ease((t - 0.2) / 0.7)
    d.rectangle(((W - bw) / 2, y + 290 * s, (W + bw) / 2, y + 306 * s), fill=ACCENT + (int(255 * a),))
    w2 = d.textlength(sub, font=f2)
    _shadow_text(img, ((W - w2) / 2, y + 335 * s), sub, f2, fill=ACCENT2, alpha=int(255 * ease((t - 0.4) / 0.6) * a_out))
    w3 = d.textlength(place, font=f3)
    _shadow_text(img, ((W - w3) / 2, y + 470 * s), place, f3, alpha=int(230 * ease((t - 0.6) / 0.6) * a_out))
    return img


# --------------------------------------------------------------------------- chapter (task / flight) card

def chapter_card_frame(W: int, H: int, t: float, dur: float, chapter: dict, map_img_fn,
                       pilot: str = "") -> Image.Image:
    """Opaque frame: left info panel + animated map (map_img_fn(progress) -> RGBA).

    `chapter` comes from `pgvid.tasks.chapters()`: label, date, distance_km, type
    and, for competition days, the task dict (start time, turnpoints, stats, result).
    """
    task = chapter.get("task") or {}
    s = W / 3840
    img = Image.new("RGBA", (W, H), (14, 17, 22, 255))
    map_w = int(W * 0.625)
    prog = ease((t - 0.8) / max(dur - 2.0, 1.0))
    m = map_img_fn(prog)
    if m.size != (map_w, H):
        m = m.resize((map_w, H), Image.LANCZOS)
    slide = int((1 - ease(t / 0.7)) * 200 * s)
    img.alpha_composite(m, (W - map_w + slide, 0))
    # gradient joining panel and map
    grad = Image.new("RGBA", (int(260 * s), H))
    gd = ImageDraw.Draw(grad)
    for i in range(grad.width):
        gd.line((i, 0, i, H), fill=(14, 17, 22, int(255 * (1 - i / grad.width))))
    img.alpha_composite(grad, (W - map_w, 0))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    x0 = int(150 * s)
    y = int(260 * s)

    def line(txt, f, fill=WHITE, dy=0, delay=0.0):
        nonlocal y
        a = ease((t - delay) / 0.5)
        off = (1 - a) * 80 * s
        _shadow_text(img, (x0 - off, y), txt, f, fill=fill, alpha=int(255 * a), shadow=4)
        y += dy

    date = dt.date.fromisoformat(chapter["date"]).strftime("%A %d %B") if chapter.get("date") else ""
    line(chapter["label"], font("heavy", int(260 * s)), WHITE, int(290 * s), 0.0)
    d.rectangle((x0, y - 10 * s, x0 + 420 * s * ease((t - 0.2) / 0.6), y + 6 * s), fill=ACCENT)
    y += int(60 * s)
    line(date, font("demi", int(92 * s)), ACCENT2, int(170 * s), 0.25)
    dist = chapter.get("distance_km")
    line(f"{dist:.1f} km" if dist else "", font("num", int(150 * s)), WHITE, int(165 * s), 0.4)
    line(chapter.get("type", ""), font("regular", int(80 * s)), (210, 210, 210), int(150 * s), 0.5)
    stats = []
    if task:
        if task.get("start_time"):
            stats.append(("START", task["start_time"]))
        ntp = sum(1 for tp in task.get("turnpoints", []) if tp["type"] not in ("TAKEOFF",))
        if ntp:
            stats.append(("TURNPOINTS", str(ntp)))
        st = task.get("stats") or {}
        if st.get("no_of_pilots_reaching_goal"):
            stats.append(("IN GOAL", f"{st['no_of_pilots_reaching_goal']} / {task.get('pilots', '?')}"))
    else:
        if chapter.get("airtime_s"):
            a_s = int(chapter["airtime_s"])
            stats.append(("AIRTIME", f"{a_s // 3600}:{a_s % 3600 // 60:02d}"))
        if chapter.get("max_alt_m"):
            stats.append(("MAX ALT", f"{chapter['max_alt_m']:,.0f} m"))
    fx, fy = font("regular", int(54 * s)), font("num", int(92 * s))
    for i, (k, v) in enumerate(stats):
        a = ease((t - 0.65 - 0.12 * i) / 0.5)
        cx = x0 + i * int(430 * s)
        d.text((cx, y), k, font=fx, fill=(170, 170, 170, int(255 * a)))
        d.text((cx, y + 62 * s), v, font=fy, fill=WHITE + (int(255 * a),))
    y += int(260 * s)
    r = task.get("result")
    if r:
        a = ease((t - 1.2) / 0.6)
        d.rounded_rectangle((x0 - 30 * s, y - 20 * s, x0 + 1250 * s, y + 210 * s), radius=int(28 * s),
                            fill=(255, 255, 255, int(22 * a)), outline=ACCENT + (int(200 * a),), width=int(4 * s))
        d.text((x0, y), (pilot or "result").upper(), font=font("demi", int(58 * s)), fill=(200, 200, 200, int(255 * a)))
        goal = bool(r.get("time"))
        res = f"#{r['rank']}   |   " + (f"GOAL {r['time']}" if goal else f"{r['distance_km']} km")
        d.text((x0, y + 75 * s), res, font=font("num", int(100 * s)), fill=WHITE + (int(255 * a),))
    img.alpha_composite(ov)
    # fade out
    fo = ease((t - (dur - 0.4)) / 0.4)
    if fo > 0:
        img.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, int(255 * fo))))
    return img


# --------------------------------------------------------------------------- lower third

def lower_third_frame(W: int, H: int, t: float, dur: float, name: str, detail: str) -> Image.Image:
    s = W / 3840
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    a = ease(t / 0.5) * (1 - ease((t - (dur - 0.5)) / 0.5))
    d = ImageDraw.Draw(img)
    x, y = int(160 * s), int(H - 430 * s)
    bar_w = int(16 * s)
    d.rectangle((x, y, x + bar_w, y + 210 * s), fill=ACCENT + (int(255 * a),))
    off = (1 - ease(t / 0.6)) * 120 * s
    _shadow_text(img, (x + 50 * s - off, y - 10 * s), name.upper(), font("heavy", int(120 * s)), alpha=int(255 * a), shadow=5)
    _shadow_text(img, (x + 54 * s - off, y + 130 * s), detail, font("demi", int(66 * s)), fill=ACCENT2, alpha=int(255 * a), shadow=4)
    return img


# --------------------------------------------------------------------------- end screen

def end_screen_frame(W: int, H: int, t: float, dur: float, cta: str, lines: list[str],
                     heading: str = "THANKS FOR FLYING ALONG") -> Image.Image:
    """Leaves the right-hand YouTube end-screen element area empty (video + subscribe)."""
    s = W / 3840
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    a = ease(t / 0.8)
    dim = Image.new("RGBA", (W, H), (8, 10, 14, int(170 * a)))
    img.alpha_composite(dim)
    d = ImageDraw.Draw(img)
    x = int(220 * s)
    y = int(H * 0.30)
    _shadow_text(img, (x, y), heading, font("heavy", int(150 * s)), alpha=int(255 * a))
    d.rectangle((x, y + 185 * s, x + 900 * s * ease((t - 0.3) / 0.6), y + 199 * s), fill=ACCENT + (int(255 * a),))
    yy = y + 260 * s
    for i, ln in enumerate(lines):
        aa = ease((t - 0.5 - 0.15 * i) / 0.5)
        _shadow_text(img, (x, yy), ln, font("demi", int(80 * s)), fill=(225, 225, 225), alpha=int(255 * aa), shadow=4)
        yy += 110 * s
    if cta:
        aa = ease((t - 1.0) / 0.5)
        _shadow_text(img, (x, yy + 60 * s), cta, font("heavy", int(110 * s)), fill=ACCENT2,
                     alpha=int(255 * aa), shadow=5)
    return img


# --------------------------------------------------------------------------- HUD

def hud_frame(W: int, H: int, alt: float, vario: float, speed: float, clock: str, task_label: str,
              alpha: float = 1.0) -> tuple[Image.Image, tuple[int, int]]:
    """Compact telemetry HUD panel (transparent RGBA) and its top-left position in a WxH frame."""
    s = W / 3840
    pw, ph = int(820 * s), int(360 * s)
    pos = (W - pw - int(110 * s), int(100 * s))
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0 = 0, 0
    a = int(255 * alpha)
    d.rounded_rectangle((x0, y0, x0 + pw - 1, y0 + ph - 1), radius=int(36 * s), fill=(0, 0, 0, int(115 * alpha)))
    # vario bar (left)
    bx, by, bw, bh = x0 + int(40 * s), y0 + int(40 * s), int(34 * s), ph - int(80 * s)
    mid = by + bh / 2
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=int(14 * s), fill=(255, 255, 255, int(40 * alpha)))
    v = max(-5.0, min(5.0, vario))
    col = (60, 220, 100) if v >= 0 else (240, 70, 60)
    hgt = abs(v) / 5 * bh / 2
    if v >= 0:
        d.rounded_rectangle((bx, mid - hgt, bx + bw, mid), radius=int(10 * s), fill=col + (a,))
    else:
        d.rounded_rectangle((bx, mid, bx + bw, mid + hgt), radius=int(10 * s), fill=col + (a,))
    d.line((bx - 8 * s, mid, bx + bw + 8 * s, mid), fill=(255, 255, 255, a), width=int(4 * s))
    tx = bx + bw + int(45 * s)
    fn, fu, fl = font("num", int(112 * s)), font("demi", int(46 * s)), font("regular", int(40 * s))
    sign = "+" if vario >= 0 else "−"
    d.text((tx, y0 + 30 * s), f"{sign}{abs(vario):.1f}", font=fn, fill=col + (a,))
    d.text((tx + d.textlength(f"{sign}{abs(vario):.1f}", font=fn) + 12 * s, y0 + 88 * s), "m/s", font=fu, fill=(220, 220, 220, a))
    d.text((tx, y0 + 175 * s), f"{alt:,.0f}", font=font("num", int(84 * s)), fill=WHITE + (a,))
    d.text((tx + d.textlength(f"{alt:,.0f}", font=font("num", int(84 * s))) + 10 * s, y0 + 213 * s), "m", font=fu, fill=(220, 220, 220, a))
    gx = x0 + int(500 * s)
    d.text((gx, y0 + 175 * s), f"{speed:.0f}", font=font("num", int(84 * s)), fill=WHITE + (a,))
    d.text((gx + d.textlength(f"{speed:.0f}", font=font("num", int(84 * s))) + 10 * s, y0 + 213 * s), "km/h", font=fu, fill=(220, 220, 220, a))
    d.text((tx, y0 + 285 * s), f"{task_label}   {clock}", font=fl, fill=(200, 200, 200, a))
    return img, pos


# --------------------------------------------------------------------------- generic text overlay (Short hooks)

def caption_frame(W: int, H: int, t: float, dur: float, text: str, y_frac: float = 0.16,
                  size_frac: float = 0.085) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    a = ease(t / 0.25) * (1 - ease((t - (dur - 0.25)) / 0.25))
    sc = 0.85 + 0.15 * ease(t / 0.3)
    f = font("heavy", int(W * size_frac * sc))
    d = ImageDraw.Draw(img)
    lines = text.split("\n")
    y = H * y_frac
    for ln in lines:
        w = d.textlength(ln, font=f)
        _shadow_text(img, ((W - w) / 2, y), ln, f, alpha=int(255 * a), shadow=int(W * 0.008))
        y += f.size * 1.05
    return img

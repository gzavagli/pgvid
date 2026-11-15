"""Task map rendering: terrain tiles + cylinders + optimised route + pilot track.

Tiles: OpenTopoMap (CC-BY-SA). Attribution is drawn on the map and must be
repeated in the video description:
  "Map data © OpenStreetMap contributors, SRTM | Map style © OpenTopoMap (CC-BY-SA)"
Tiles are cached on disk; only a few dozen are fetched per task.
"""
from __future__ import annotations

import math
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

TILE_URL = "https://tile.opentopomap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "Map data © OpenStreetMap contributors, SRTM | Map style © OpenTopoMap (CC-BY-SA)"
UA = {"User-Agent": "pgvid video tool"}
TS = 256


def configure(contact: str = "") -> None:
    """Set the tile User-Agent. Tile usage policies ask for a way to contact you."""
    UA["User-Agent"] = "pgvid video tool" + (f" (contact: {contact})" if contact else "")


def lonlat_to_px(lon: float | np.ndarray, lat: float | np.ndarray, z: int):
    n = TS * 2 ** z
    x = (np.asarray(lon) + 180.0) / 360.0 * n
    lr = np.radians(np.asarray(lat))
    y = (1 - np.log(np.tan(lr) + 1 / np.cos(lr)) / math.pi) / 2 * n
    return x, y


def meters_per_px(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / 2 ** z


def get_tile(z: int, x: int, y: int, cache: Path) -> Image.Image:
    p = cache / f"{z}_{x}_{y}.png"
    if not p.exists():
        cache.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            r = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=UA, timeout=30)
            if r.ok:
                p.write_bytes(r.content)
                break
            time.sleep(1.5 * (attempt + 1))
        else:
            return Image.new("RGB", (TS, TS), (200, 200, 200))
        time.sleep(0.2)  # be polite
    return Image.open(p).convert("RGB")


def optimized_route(tps: list[dict], iters: int = 60) -> list[tuple[float, float]]:
    """Approximate shortest route touching every cylinder (lat/lon list)."""
    lat0 = np.mean([t["lat"] for t in tps])
    kx = 111320 * math.cos(math.radians(lat0))
    ky = 110540
    C = np.array([[(t["lon"]) * kx, t["lat"] * ky] for t in tps])
    R = np.array([t["radius_m"] for t in tps], float)
    P = C.copy()
    for _ in range(iters):
        for i in range(1, len(P)):
            a = P[i - 1]
            b = P[i + 1] if i + 1 < len(P) else None
            c, r = C[i], R[i]
            if b is None:
                target = a
            else:
                ab = b - a
                tt = np.clip(np.dot(c - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1)
                target = a + tt * ab
            d = target - c
            dist = np.linalg.norm(d)
            P[i] = target if dist <= r and b is not None else c + d / max(dist, 1e-9) * r
    return [(p[1] / ky, p[0] / kx) for p in P]


class MapCanvas:
    """A tile mosaic covering a lat/lon bbox at a fixed output size."""

    def __init__(self, bbox: tuple[float, float, float, float], size: tuple[int, int], cache: Path,
                 max_zoom: int = 12):
        lat_min, lon_min, lat_max, lon_max = bbox
        W, H = size
        # pick zoom so bbox fits (tiles rendered at 2x for crispness then downscaled)
        z = max_zoom
        while z > 3:
            x0, y1 = lonlat_to_px(lon_min, lat_min, z)
            x1, y0 = lonlat_to_px(lon_max, lat_max, z)
            if (x1 - x0) <= W and (y1 - y0) <= H:
                break
            z -= 1
        self.z = z
        cx, cy = lonlat_to_px((lon_min + lon_max) / 2, (lat_min + lat_max) / 2, z)
        # scale factor to fill the output size
        x0, y1 = lonlat_to_px(lon_min, lat_min, z)
        x1, y0 = lonlat_to_px(lon_max, lat_max, z)
        self.scale = min(W / max(x1 - x0, 1), H / max(y1 - y0, 1))
        self.W, self.H = W, H
        self.ox = cx - W / 2 / self.scale
        self.oy = cy - H / 2 / self.scale
        tx0, ty0 = int(self.ox // TS), int(self.oy // TS)
        tx1, ty1 = int((self.ox + W / self.scale) // TS), int((self.oy + H / self.scale) // TS)
        mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * TS, (ty1 - ty0 + 1) * TS))
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                mosaic.paste(get_tile(z, tx, ty, cache), ((tx - tx0) * TS, (ty - ty0) * TS))
        left = self.ox - tx0 * TS
        top = self.oy - ty0 * TS
        crop = mosaic.crop((int(left), int(top), int(left + W / self.scale), int(top + H / self.scale)))
        self.base = crop.resize((W, H), Image.LANCZOS)

    def xy(self, lat, lon):
        x, y = lonlat_to_px(lon, lat, self.z)
        return (x - self.ox) * self.scale, (y - self.oy) * self.scale

    def radius_px(self, lat: float, r_m: float) -> float:
        return r_m / meters_per_px(lat, self.z) * self.scale


TYPE_COL = {"TAKEOFF": (255, 255, 255), "SSS": (90, 200, 90), "ESS": (255, 190, 40), "GOAL": (255, 190, 40),
            "TP": (240, 240, 240)}


def render_task_map(task: dict, track=None, size=(2400, 2160), cache: Path = Path("."),
                    progress: float = 1.0, style: dict | None = None) -> Image.Image:
    """Return an RGBA image of the task map. `progress` 0..1 draws a partial track.

    With no turnpoints (a free flight) only the track is drawn.
    """
    tps = task.get("turnpoints") or []
    if not tps and track is None:
        raise ValueError("need turnpoints or a track to draw a map")
    route = optimized_route(tps) if tps else []
    lats = [t["lat"] for t in tps] + [p[0] for p in route]
    lons = [t["lon"] for t in tps] + [p[1] for p in route]
    if track is not None:
        fl = slice(track.takeoff_idx, track.landing_idx)
        lats += [float(track.lat[fl].min()), float(track.lat[fl].max())]
        lons += [float(track.lon[fl].min()), float(track.lon[fl].max())]
    pad = 0.06
    la0, la1, lo0, lo1 = min(lats), max(lats), min(lons), max(lons)
    dla, dlo = (la1 - la0) * pad + 0.01, (lo1 - lo0) * pad + 0.01
    cv = getattr(render_task_map, "_cache", {}).get((task["num"], size))
    if cv is None:
        cv = MapCanvas((la0 - dla, lo0 - dlo, la1 + dla, lo1 + dlo), size, cache)
        img = ImageEnhance.Color(cv.base).enhance(0.55)
        img = ImageEnhance.Brightness(img).enhance(0.78)
        cv.styled = img.convert("RGBA")
        render_task_map._cache = {(task["num"], size): cv}
    img = cv.styled.copy()
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    s = size[0] / 2400
    # cylinders (draw big SSS faintly)
    for t in tps:
        x, y = cv.xy(t["lat"], t["lon"])
        r = cv.radius_px(t["lat"], t["radius_m"])
        col = TYPE_COL.get(t["type"], (240, 240, 240))
        d.ellipse((x - r, y - r, x + r, y + r), fill=col + (38,), outline=col + (220,), width=int(5 * s))
        d.ellipse((x - 7 * s, y - 7 * s, x + 7 * s, y + 7 * s), fill=col + (255,))
    # optimised route
    pts = [cv.xy(la, lo) for la, lo in route]
    if len(pts) >= 2:
        d.line(pts, fill=(255, 255, 255, 235), width=int(7 * s), joint="curve")
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):  # arrows at mid legs
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        ang = math.atan2(y1 - y0, x1 - x0)
        a = 26 * s
        tri = [(mx + a * math.cos(ang), my + a * math.sin(ang)),
               (mx + a * math.cos(ang + 2.5), my + a * math.sin(ang + 2.5)),
               (mx + a * math.cos(ang - 2.5), my + a * math.sin(ang - 2.5))]
        d.polygon(tri, fill=(255, 255, 255, 255))
    # labels
    for i, t in enumerate(tps):
        x, y = cv.xy(t["lat"], t["lon"])
        label = {"TAKEOFF": "TO", "SSS": "SSS", "ESS": "ESS", "GOAL": "GOAL"}.get(t["type"], f"TP{i}")
        _label(d, (x + 14 * s, y - 50 * s), label, s)
    img = Image.alpha_composite(img, ov)
    # pilot track
    if track is not None and progress > 0:
        fl = np.arange(track.takeoff_idx, track.landing_idx)
        k = max(2, int(len(fl) * progress))
        sel = fl[:k:3]
        xs, ys = cv.xy(track.lat[sel], track.lon[sel])
        tr_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        dt = ImageDraw.Draw(tr_layer)
        pts = list(zip(xs.tolist(), ys.tolist()))
        dt.line(pts, fill=(0, 0, 0, 120), width=int(11 * s), joint="curve")
        dt.line(pts, fill=(255, 70, 40, 255), width=int(6 * s), joint="curve")
        x, y = pts[-1]
        dt.ellipse((x - 16 * s, y - 16 * s, x + 16 * s, y + 16 * s), fill=(255, 70, 40, 255),
                   outline=(255, 255, 255, 255), width=int(4 * s))
        img = Image.alpha_composite(img, tr_layer)
    # attribution
    from .cards import font
    al = Image.new("RGBA", img.size, (0, 0, 0, 0))
    da = ImageDraw.Draw(al)
    f = font("regular", int(26 * s))
    tw = da.textlength(ATTRIBUTION, font=f)
    da.rectangle((size[0] - tw - 30 * s, size[1] - 48 * s, size[0], size[1]), fill=(0, 0, 0, 150))
    da.text((size[0] - tw - 15 * s, size[1] - 42 * s), ATTRIBUTION, font=f, fill=(230, 230, 230, 255))
    img = Image.alpha_composite(img, al)
    return img


def _label(d: ImageDraw.ImageDraw, xy, text: str, s: float) -> None:
    from .cards import font
    f = font("bold", int(40 * s))
    x, y = xy
    w = d.textlength(text, font=f)
    d.rounded_rectangle((x - 10 * s, y - 6 * s, x + w + 10 * s, y + 50 * s), radius=int(10 * s),
                        fill=(0, 0, 0, 170))
    d.text((x, y), text, font=f, fill=(255, 255, 255, 255))

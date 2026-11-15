"""IGC tracklog parsing and derived flight metrics (1 Hz)."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

_B = re.compile(r"^B(\d{2})(\d{2})(\d{2})(\d{2})(\d{5})([NS])(\d{3})(\d{5})([EW])([AV])([-\d]\d{4})([-\d]\d{4})")


@dataclass
class Track:
    path: Path
    date: dt.date
    pilot: str
    glider: str
    t: np.ndarray        # UTC epoch seconds, 1 Hz regular grid
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray      # GPS altitude (m)
    vario: np.ndarray    # m/s, smoothed
    speed: np.ndarray    # ground speed km/h, smoothed
    heading: np.ndarray  # deg
    turn: np.ndarray     # turn rate deg/s (signed, + = right/clockwise)
    takeoff_idx: int
    landing_idx: int

    @property
    def takeoff(self) -> float:
        return float(self.t[self.takeoff_idx])

    @property
    def landing(self) -> float:
        return float(self.t[self.landing_idx])

    def at(self, epoch: float | np.ndarray, field: str) -> np.ndarray:
        return np.interp(epoch, self.t, getattr(self, field))

    def flown_km(self, step: int = 10) -> float:
        """Track length between takeoff and landing, sampled every `step` seconds."""
        sl = slice(self.takeoff_idx, self.landing_idx + 1, step)
        la, lo = np.radians(self.lat[sl]), np.radians(self.lon[sl])
        if la.size < 2:
            return 0.0
        h = np.sin(np.diff(la) / 2) ** 2 + np.cos(la[:-1]) * np.cos(la[1:]) * np.sin(np.diff(lo) / 2) ** 2
        return float((2 * 6371.0 * np.arcsin(np.sqrt(h))).sum())


def _hdr(lines: list[str], key: str) -> str:
    for ln in lines:
        if ln.startswith(key):
            return ln.split(":", 1)[-1].strip()
    return ""


def parse_igc(path: Path) -> Track:
    lines = path.read_text(errors="replace").splitlines()
    m = re.search(r"(\d{2})(\d{2})(\d{2})", _hdr(lines, "HFDTE"))
    if not m:
        raise ValueError(f"no date in {path}")
    date = dt.date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    base = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc).timestamp()
    ts, la, lo, al = [], [], [], []
    prev = None
    day_add = 0
    for ln in lines:
        b = _B.match(ln)
        if not b:
            continue
        hh, mm, ss = int(b.group(1)), int(b.group(2)), int(b.group(3))
        sec = hh * 3600 + mm * 60 + ss
        if prev is not None and sec + day_add < prev - 3600:
            day_add += 86400  # midnight rollover
        sec += day_add
        prev = sec
        lat = int(b.group(4)) + int(b.group(5)) / 60000.0
        lon = int(b.group(7)) + int(b.group(8)) / 60000.0
        if b.group(6) == "S":
            lat = -lat
        if b.group(9) == "W":
            lon = -lon
        ts.append(base + sec)
        la.append(lat)
        lo.append(lon)
        al.append(float(b.group(12)) if b.group(12) != "00000" else float(b.group(11)))
    t = np.array(ts)
    order = np.argsort(t, kind="stable")
    t, la, lo, al = t[order], np.array(la)[order], np.array(lo)[order], np.array(al)[order]
    keep = np.concatenate([[True], np.diff(t) > 0])
    t, la, lo, al = t[keep], la[keep], lo[keep], al[keep]
    grid = np.arange(np.ceil(t[0]), np.floor(t[-1]) + 1)
    lat = np.interp(grid, t, la)
    lon = np.interp(grid, t, lo)
    alt = np.interp(grid, t, al)

    # local metric coordinates
    r = 6371000.0
    x = np.radians(lon - lon[0]) * r * np.cos(np.radians(lat[0]))
    y = np.radians(lat - lat[0]) * r
    xs = uniform_filter1d(x, 5)
    ys = uniform_filter1d(y, 5)
    vx, vy = np.gradient(xs), np.gradient(ys)
    speed = np.hypot(vx, vy) * 3.6
    heading = (np.degrees(np.arctan2(vx, vy)) + 360) % 360
    hu = np.unwrap(np.radians(heading))
    turn = np.degrees(np.gradient(uniform_filter1d(hu, 5)))
    vario = np.gradient(uniform_filter1d(alt, 9))
    vario = uniform_filter1d(vario, 5)

    # takeoff / landing: sustained speed > 10 km/h or vertical movement
    moving = uniform_filter1d((speed > 12).astype(float), 30) > 0.6
    idx = np.flatnonzero(moving)
    takeoff_idx = int(idx[0]) if idx.size else 0
    landing_idx = int(idx[-1]) if idx.size else len(grid) - 1

    return Track(path=path, date=date, pilot=_hdr(lines, "HFPLT"), glider=_hdr(lines, "HFGTY"),
                 t=grid, lat=lat, lon=lon, alt=alt, vario=vario, speed=uniform_filter1d(speed, 5),
                 heading=heading, turn=turn, takeoff_idx=takeoff_idx, landing_idx=landing_idx)


def load_tracks(igc_dir: Path) -> list[Track]:
    tracks = [parse_igc(p) for p in sorted(igc_dir.glob("*.igc")) + sorted(igc_dir.glob("*.IGC"))]
    tracks.sort(key=lambda tr: tr.t[0])
    return tracks

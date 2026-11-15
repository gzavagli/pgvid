"""Tasks from XCTrack task files (.xctsk, JSON format version 1).

    competition:
      provider: xctsk
      tasks:
        - {file: tasks/day1.xctsk, date: 2026-07-14}
        - {file: tasks/day2.xctsk, date: 2026-07-15, type: Elapsed time}

Each file holds one task. The optimised distance is computed here (no results
or pilot statistics are available from a task file). Compressed QR-code
("XCTSK:" v2) strings are not supported: save the task as a file first.
"""
from __future__ import annotations

import datetime as dt
import json
import math

from .base import Provider


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _local(hhmmss_z: str, utc_offset: float) -> str:
    try:
        t = dt.datetime.strptime(hhmmss_z.rstrip("Z"), "%H:%M:%S")
    except ValueError:
        return hhmmss_z
    return (t + dt.timedelta(hours=utc_offset)).strftime("%H:%M")


def parse_xctsk(text: str, num: int, date: str, utc_offset: float = 0.0) -> dict:
    j = json.loads(text)
    if j.get("version", 1) != 1 or "turnpoints" not in j:
        raise ValueError("only XCTrack task format version 1 (JSON with 'turnpoints') is supported")
    tps = []
    for i, tp in enumerate(j["turnpoints"]):
        w = tp["waypoint"]
        tps.append({"n": i, "type": tp.get("type") or "TP", "id": w.get("name", f"TP{i}"),
                    "radius_m": int(tp.get("radius", 400)), "lat": float(w["lat"]), "lon": float(w["lon"]),
                    "alt_m": int(w.get("altSmoothed", 0) or 0)})
    if tps:
        if tps[0]["type"] == "TP":
            tps[0]["type"] = "TAKEOFF"
        if tps[-1]["type"] in ("TP", "ESS") and len(tps) > 1:
            tps[-1]["type"] = "GOAL" if tps[-1]["type"] == "TP" else "ESS"
    task = {"num": num, "date": date, "title": f"Task {num}", "turnpoints": tps,
            "type": {"RACE": "Race to goal", "ELAPSED-TIME": "Elapsed time"}.get(
                (j.get("sss") or {}).get("type", "RACE"), "Race to goal")}
    gates = (j.get("sss") or {}).get("timeGates") or []
    if gates:
        task["start_time"] = _local(gates[0], utc_offset)
    if len(tps) >= 2:
        from ..maps import optimized_route
        route = optimized_route(tps)
        task["distance_km"] = round(sum(_haversine_km(a, b) for a, b in zip(route[:-1], route[1:])), 1)
        task["optimized_km"] = task["distance_km"]
    return task


class XctskProvider(Provider):
    name = "xctsk"

    def fetch(self, force: bool = False) -> list[dict]:
        entries = self.cfg.get("tasks") or []
        if not entries:
            raise ValueError("competition.tasks is empty: list {file, date} entries")
        out = []
        for i, e in enumerate(entries, 1):
            path = self.prj.resolve(e["file"])
            t = parse_xctsk(path.read_text(), i, str(e.get("date", "")), float(self.prj["utc_offset_hours"]))
            if e.get("type"):
                t["type"] = e["type"]
            out.append(t)
        return out

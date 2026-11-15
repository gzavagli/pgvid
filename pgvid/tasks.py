"""Competition tasks (optional) and per-day chapters.

`pgvid tasks` asks the configured provider (see `pgvid.providers`) for the task
definitions and writes one YAML per task to `<tasks_dir>/taskN.yaml`.

Task schema (every key except `num` is optional):

    num: 1
    date: "2026-07-14"              # matches the IGC date of that day's flight
    title: "Task 1"
    type: "Race to goal"
    distance_km: 84.2
    start_time: "13:00"
    turnpoints: [{type: TAKEOFF|SSS|TP|ESS|GOAL, lat, lon, radius_m, id}]
    pilots: 120
    stats: {no_of_pilots_reaching_goal: 64}
    result: {rank: 42, time: "2:31:10", distance_km: 84.2}

Without a `competition:` section the long video gets one chapter per flight
(one IGC file = one day), with a track-only map card.
"""
from __future__ import annotations

import yaml

from .config import Project
from .providers import get_provider


def load_tasks(prj: Project) -> list[dict]:
    if not prj.section("competition"):
        return []
    files = sorted(prj.tasks_dir.glob("task*.yaml"),
                   key=lambda q: int("".join(c for c in q.stem if c.isdigit()) or 0))
    tasks = [yaml.safe_load(p.read_text()) or {} for p in files]
    for i, t in enumerate(tasks, 1):
        t.setdefault("num", i)
        t.setdefault("turnpoints", [])
        t["date"] = str(t["date"]) if t.get("date") else ""
    return tasks


def chapters(prj: Project, tracks: list, tasks: list[dict] | None = None) -> list[dict]:
    """One chapter per flight day (IGC file), with its task if there is one.

    Tasks are matched to days by date; if any task lacks a date, by position.
    """
    tasks = load_tasks(prj) if tasks is None else tasks
    by_date = {t["date"]: t for t in tasks if t.get("date")}
    use_dates = bool(tasks) and len(by_date) == len(tasks)
    if use_dates and sum(str(tr.date) in by_date for tr in tracks) < min(len(tasks), len(tracks)):
        use_dates = False  # dates don't line up with the IGC days: fall back to flying order
    label = (prj.section("long").get("chapter_label") or ("TASK" if tasks else "FLIGHT")).upper()
    out = []
    for d, tr in enumerate(tracks):
        task = by_date.get(str(tr.date)) if use_dates else (tasks[d] if d < len(tasks) else None)
        num = task["num"] if task else d + 1
        out.append({
            "day": d, "num": num, "date": str(tr.date), "task": task,
            "label": f"{label} {num}",
            "distance_km": (task or {}).get("distance_km") or round(tr.flown_km(), 1),
            "type": (task or {}).get("type") or ("" if task else "Free flight"),
            "airtime_s": float(tr.landing - tr.takeoff),
            "max_alt_m": float(tr.alt[tr.takeoff_idx:tr.landing_idx + 1].max()),
        })
    return out


def run(prj: Project, force: bool = False) -> None:
    prov = get_provider(prj)
    if prov is None:
        print("no `competition:` section: chapters will follow the IGC flights, nothing to fetch")
        return
    if prov.name == "manual":
        tasks = prov.fetch(force)
        print(f"manual tasks: {len(tasks)} found in {prj.tasks_dir}")
        for t in tasks:
            print(f"  Task {t['num']} {t.get('date') or '(no date)'} {len(t['turnpoints'])} turnpoints")
        return
    existing = sorted(prj.tasks_dir.glob("task*.yaml"))
    if existing and not force:
        print(f"{len(existing)} task files already in {prj.tasks_dir} (use --force to refetch)")
        return
    tasks = prov.fetch(force)
    for t in tasks:
        (prj.tasks_dir / f"task{t['num']}.yaml").write_text(yaml.safe_dump(t, sort_keys=False, allow_unicode=True))
        r = t.get("result", {})
        print(f"Task {t['num']} {t.get('date')} {t.get('distance_km')} km, {len(t['turnpoints'])} TPs, "
              f"start {t.get('start_time')}, pilots {t.get('pilots', '-')}"
              + (f", rank {r.get('rank')} time {r.get('time')}" if r else ""))
    ov = prov.overall()
    (prj.tasks_dir / "overall.yaml").write_text(yaml.safe_dump(ov or {}, sort_keys=False))
    if ov:
        print("overall:", ov)

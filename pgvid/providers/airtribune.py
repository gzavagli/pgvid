"""Task definitions and results scraped from Airtribune results pages.

    competition:
      provider: airtribune
      results_url: https://airtribune.com/<comp-slug>/results
      task_ids: [1234, 1236, ...]   # in flying order (from the task result URLs)
      class: open                     # results class shown on the site

Scraping is inherently fragile: it depends on the page layout at the time of
writing. Every page is cached in `<work_dir>/airtribune`, so the site is hit
only once per page. Please keep it that way and be considerate.
If this breaks, export the tasks as .xctsk files or write them by hand
(providers `xctsk` / `manual`).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

from .base import Provider

BASE = "https://airtribune.com"


class _Tables(HTMLParser):
    """Collects every <table> as a list of rows (list of cell texts) and all text chunks."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.texts: list[str] = []
        self._stack: list[list[list[str]]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._stack and self._stack[-1]:
            self._stack[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        s = data.strip()
        if s:
            self.texts.append(s)


def _parse_tp_table(rows: list[list[str]]) -> list[dict]:
    tps = []
    for r in rows:
        if len(r) < 8 or not re.match(r"^\d+", r[0]):
            continue
        m = re.search(r"Lat:\s*([-\d.]+)\s*Lon:\s*([-\d.]+)", r[6])
        if not m:
            continue
        kind = r[0].split()[1] if len(r[0].split()) > 1 else ""
        tps.append({
            "n": int(r[0].split()[0]),
            "type": {"SS": "SSS", "ES": "ESS"}.get(kind, kind or "TP"),
            "dist_km": float(r[1].replace("km", "").strip() or 0),
            "id": r[2],
            "radius_m": int(re.sub(r"[^\d]", "", r[3]) or 0),
            "open": r[4], "close": r[5],
            "lat": float(m.group(1)), "lon": float(m.group(2)),
            "alt_m": int(re.sub(r"[^\d]", "", r[7]) or 0),
        })
    if tps:
        tps[0]["type"] = "TAKEOFF" if tps[0]["type"] == "TP" else tps[0]["type"]
        tps[-1]["type"] = "GOAL" if tps[-1]["type"] in ("TP", "") else tps[-1]["type"]
    return tps


def _kv(rows: list[list[str]]) -> dict:
    return {r[0]: r[1] for r in rows if len(r) == 2}


class AirtribuneProvider(Provider):
    name = "airtribune"

    def __init__(self, prj, cfg):
        super().__init__(prj, cfg)
        url = cfg.get("results_url", "")
        u = urlparse(url)
        if u.scheme != "https" or not u.netloc.endswith("airtribune.com"):
            raise ValueError("competition.results_url must be an https://airtribune.com/... results URL")
        self.slug = u.path.strip("/").split("/")[0]
        self.cls = cfg.get("class", "open")
        self.task_ids = [int(t) for t in cfg.get("task_ids", [])]
        contact = prj.get("contact")
        self.ua = {"User-Agent": "pgvid video tool" + (f" (contact: {contact})" if contact else "")}
        self.cache = prj.work("airtribune", "x").parent

    def _get(self, url: str, cache: Path) -> str:
        if cache.exists():
            return cache.read_text()
        r = requests.get(url, headers=self.ua, timeout=60)
        r.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(r.text)
        return r.text

    def _me(self, rows: list[list[str]], cols: slice) -> list[str] | None:
        pilot = (self.prj.get("pilot") or "").strip().lower()
        if not pilot:
            return None
        return next((r for r in rows if pilot in [c.lower() for c in r[cols]]), None)

    def scrape_task(self, num: int, task_id: int) -> dict:
        html = self._get(f"{BASE}/{self.slug}/results/task{task_id}/day/{self.cls}",
                         self.cache / f"task{task_id}_day.html")
        p = _Tables()
        p.feed(html)
        tp_rows = next((t for t in p.tables if any(len(r) >= 8 and "Lat:" in " ".join(r) for r in t)), [])
        stats = {}
        for t in p.tables:
            kv = _kv(t)
            if "best_dist" in kv or "no_of_pilots_reaching_goal" in kv:
                stats.update(kv)
        res_rows = [r for t in p.tables for r in t if len(r) >= 15 and r[0].isdigit()]
        me = self._me(res_rows, slice(2, 3))
        dm = re.search(r"Task \d+:.*?\b(20\d{2}-\d{2}-\d{2})\b", " ".join(p.texts[:400]))
        play = self._get(f"{BASE}/play/{task_id}/2d", self.cache / f"play{task_id}.html")
        title = re.search(r'og:title" content="([^"|]+)', play)
        preview = re.search(r'og:image" content="([^"?]+)', play)
        task = {
            "num": num, "source_id": task_id, "date": dm.group(1) if dm else "",
            "title": title.group(1).strip() if title else f"Task {num}",
            "turnpoints": _parse_tp_table(tp_rows),
            "pilots": len(res_rows),
            "stats": {k: stats.get(k) for k in ("best_dist", "best_time", "first_start_time", "first_finish_time",
                                                  "no_of_pilots_reaching_es", "no_of_pilots_reaching_goal",
                                                  "no_of_pilots_present", "no_of_pilots_flying", "goalratio")
                      if stats.get(k) is not None},
        }
        m = re.search(r"([\d.]+)\s*km\s*-\s*(.+)$", task["title"])
        if m:
            task["distance_km"] = float(m.group(1))
            task["type"] = m.group(2).strip()
        if task["turnpoints"]:
            task["optimized_km"] = task["turnpoints"][-1]["dist_km"]
            sss = next((t for t in task["turnpoints"] if t["type"] == "SSS"), None)
            if sss:
                task["start_time"] = sss["open"]
        if me:
            task["result"] = {"rank": int(me[0]), "ss": me[7], "es": me[8], "time": me[9],
                              "speed_kmh": me[10], "distance_km": me[11], "points": me[-1]}
        if preview and urlparse(preview.group(1)).scheme == "https":
            img = self.prj.tasks_dir / f"task{num}_preview.png"
            if not img.exists():
                r = requests.get(preview.group(1), headers=self.ua, timeout=60)
                if r.ok and r.headers.get("content-type", "").startswith("image/"):
                    img.write_bytes(r.content)
            if img.exists():
                task["preview_image"] = img.name
        return task

    def fetch(self, force: bool = False) -> list[dict]:
        if not self.task_ids:
            raise ValueError("competition.task_ids is empty")
        return [self.scrape_task(i, tid) for i, tid in enumerate(self.task_ids, 1)]

    def overall(self) -> dict | None:
        last = self.task_ids[-1]
        html = self._get(f"{BASE}/{self.slug}/results/task{last}/comp/{self.cls}",
                         self.cache / f"task{last}_comp.html")
        p = _Tables()
        p.feed(html)
        rows = [r for t in p.tables for r in t if len(r) >= 6 and r[0].isdigit()]
        me = self._me(rows, slice(1, 4))
        return {"rank": int(me[0]), "of": len(rows), "points": me[-1]} if me else None

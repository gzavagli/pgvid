"""Hand-written task files.

Put one YAML per task in `tasks_dir` (task1.yaml, task2.yaml, ...). Only `num`
and `date` are really needed; add `turnpoints` to get a task map, and
`distance_km`, `type`, `start_time`, `stats`, `result` for the task card.
See docs/pipeline.md for the full schema.
"""
from __future__ import annotations

import yaml

from .base import Provider


class ManualProvider(Provider):
    name = "manual"

    def fetch(self, force: bool = False) -> list[dict]:
        tasks = []
        for i, p in enumerate(sorted(self.prj.tasks_dir.glob("task*.yaml"),
                                     key=lambda q: int("".join(c for c in q.stem if c.isdigit()) or 0)), 1):
            t = yaml.safe_load(p.read_text()) or {}
            t.setdefault("num", i)
            t.setdefault("turnpoints", [])
            t["date"] = str(t["date"]) if t.get("date") else ""
            tasks.append(t)
        return tasks

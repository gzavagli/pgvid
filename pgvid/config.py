"""Project configuration loading.

A project is a single YAML file (see examples/example_project.yaml). Relative
paths in it are resolved against `base_dir` (itself relative to the YAML file)
or, if `base_dir` is not set, against the directory containing the YAML file.
"""
from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REQUIRED = ("name", "footage_dir", "igc_dir", "utc_offset_hours")

# Defaults for optional keys. Text keys are used in on-screen graphics and the
# YouTube package; anything left empty is simply not shown.
DEFAULTS: dict = {
    "event": "",
    "event_short": "",
    "location": "",
    "dates": "",
    "pilot": "",
    "glider": "",
    "channel": "",
    "contact": "",
    "music_dir": "music",
    "work_dir": "work/{name}",
    "output_dir": "output/{name}",
    "tasks_dir": "{work_dir}/tasks",
    "exclude_clips": [],
    "pins": [],
    "exclude": [],
    "competition": None,
    "fonts": {},
    "ocr": {},
    "detect": {},
    "end_screen": {},
    "youtube": {},
    "long": {},
    "short": {},
}


class _SafeFormatter(string.Formatter):
    """str.format for user templates: plain `{name}` fields only.

    Attribute/index access (`{x.attr}`, `{x[0]}`) is rejected and unknown
    fields render as empty strings, so a typo never crashes a render.
    """

    def get_field(self, field_name, args, kwargs):
        if not field_name.isidentifier():
            raise ValueError(f"unsupported template field {{{field_name}}}: use plain names only")
        return kwargs.get(field_name, ""), field_name


_FMT = _SafeFormatter()


def render_template(template: str, values: dict) -> str:
    return _FMT.vformat(template or "", (), values)


@dataclass
class Project:
    raw: dict
    path: Path
    base_dir: Path = field(init=False)
    footage_dir: Path = field(init=False)
    igc_dir: Path = field(init=False)
    tasks_dir: Path = field(init=False)
    music_dir: Path = field(init=False)
    work_dir: Path = field(init=False)
    output_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        missing = [k for k in REQUIRED if k not in self.raw]
        if missing:
            raise ValueError(f"{self.path}: missing required keys: {', '.join(missing)}")
        for k, v in DEFAULTS.items():
            if self.raw.get(k) is None and v is not None:
                self.raw[k] = v.copy() if isinstance(v, (dict, list)) else v
        bd = self.raw.get("base_dir")
        self.base_dir = (self.path.parent / os.path.expanduser(bd)).resolve() if bd else self.path.parent.resolve()
        self.raw["work_dir"] = str(self.raw["work_dir"]).format(name=self.raw["name"])

        def p(key: str) -> Path:
            v = str(self.raw[key]).format(name=self.raw["name"], work_dir=self.raw["work_dir"])
            q = Path(os.path.expanduser(v))
            return q if q.is_absolute() else self.base_dir / q

        self.footage_dir = p("footage_dir")
        self.igc_dir = p("igc_dir")
        self.music_dir = p("music_dir")
        self.work_dir = p("work_dir")
        self.output_dir = p("output_dir")
        self.tasks_dir = p("tasks_dir")
        for d in (self.work_dir, self.output_dir, self.tasks_dir):
            d.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, key: str):
        return self.raw[key]

    def get(self, key: str, default=None):
        v = self.raw.get(key)
        return default if v is None else v

    def section(self, key: str) -> dict:
        """An optional mapping section (e.g. `ocr`, `detect`), always a dict."""
        return self.raw.get(key) or {}

    def work(self, *parts: str) -> Path:
        q = self.work_dir.joinpath(*parts)
        q.parent.mkdir(parents=True, exist_ok=True)
        return q

    def out(self, *parts: str) -> Path:
        q = self.output_dir.joinpath(*parts)
        q.parent.mkdir(parents=True, exist_ok=True)
        return q

    def resolve(self, value: str) -> Path:
        q = Path(os.path.expanduser(value))
        return q if q.is_absolute() else self.base_dir / q

    def text_values(self, **extra) -> dict:
        """Values available to `{placeholder}` templates."""
        vals = {k: self.raw.get(k) or "" for k in
                ("name", "event", "event_short", "location", "dates", "pilot", "glider", "channel")}
        vals.update({k: v for k, v in extra.items() if v is not None})
        return vals

    def fmt(self, template: str, **extra) -> str:
        return render_template(template, self.text_values(**extra))


def load_project(path: str | Path) -> Project:
    path = Path(path).expanduser().resolve()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    prj = Project(raw=raw, path=path)
    from . import cards, maps
    cards.configure_fonts(prj.section("fonts"))
    maps.configure(prj.get("contact", ""))
    return prj

"""pgvid command line: pgvid <step> --project path/to/project.yaml [args...]"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

STEPS = {
    "index": "pgvid.index",
    "tasks": "pgvid.tasks",
    "ocr": "pgvid.ocr",
    "sync": "pgvid.sync",
    "analyze": "pgvid.analyze",
    "score": "pgvid.score",
    "report": "pgvid.report",
    "beats": "pgvid.beats",
    "edl": "pgvid.edl",
    "render": "pgvid.render",
    "metadata": "pgvid.metadata",
    "recover": "pgvid.recover",
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pgvid", description="Paragliding footage + IGC -> YouTube videos")
    ap.add_argument("step", choices=sorted(STEPS))
    ap.add_argument("--project", default=os.environ.get("PGVID_PROJECT"),
                    help="project YAML (or set PGVID_PROJECT)")
    ap.add_argument("--force", action="store_true", help="recompute even if cached outputs exist")
    ap.add_argument("args", nargs="*", help="step-specific arguments")
    a = ap.parse_args(argv)
    if not a.project:
        ap.error("--project is required (or set PGVID_PROJECT)")
    from .config import load_project
    prj = load_project(a.project)
    mod = importlib.import_module(STEPS[a.step])
    kwargs = {"force": a.force} if "force" in mod.run.__code__.co_varnames else {}
    if a.args:
        if "args" not in mod.run.__code__.co_varnames:
            ap.error(f"step '{a.step}' takes no extra arguments")
        kwargs["args"] = a.args
    try:
        mod.run(prj, **kwargs)
    except FileNotFoundError as e:
        sys.exit(f"pgvid {a.step}: missing input ({e.filename}). Did you run the previous steps?")


if __name__ == "__main__":
    main()

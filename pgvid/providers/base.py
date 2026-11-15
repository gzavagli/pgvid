"""Provider interface and registry."""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Project

_REGISTRY = {
    "airtribune": "pgvid.providers.airtribune:AirtribuneProvider",
    "xctsk": "pgvid.providers.xctsk:XctskProvider",
    "manual": "pgvid.providers.manual:ManualProvider",
}


class Provider:
    """Fetch task definitions (and optionally results) for a competition."""

    name = "base"

    def __init__(self, prj: "Project", cfg: dict):
        self.prj = prj
        self.cfg = cfg

    def fetch(self, force: bool = False) -> list[dict]:
        """Return task dicts numbered 1..N in flying order."""
        raise NotImplementedError

    def overall(self) -> dict | None:
        """Optional overall result for the configured pilot: {rank, of, points}."""
        return None


def get_provider(prj: "Project") -> Provider | None:
    cfg = prj.section("competition")
    if not cfg:
        return None
    name = cfg.get("provider", "manual")
    if name not in _REGISTRY:
        raise ValueError(f"unknown competition provider '{name}' (choose from {', '.join(sorted(_REGISTRY))})")
    mod, cls = _REGISTRY[name].split(":")
    return getattr(importlib.import_module(mod), cls)(prj, cfg)

"""Competition data providers.

A provider turns some source of task definitions into a list of task dicts
(see `pgvid.tasks` for the schema). Pick one in the project YAML:

    competition:
      provider: airtribune | xctsk | manual
      ...provider-specific keys...

Omit `competition` entirely for plain (non-competition) flights.
"""
from __future__ import annotations

from .base import Provider, get_provider

__all__ = ["Provider", "get_provider"]

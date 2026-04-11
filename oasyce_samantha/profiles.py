"""Connection profiles for the Oasyce App backend.

Samantha's ``AppChannel`` needs a Go App backend to post replies to.
This module is the catalogue of known backends plus the "custom"
escape hatch. Keeping it separate from ``cli.py`` means

  (a) no magic URLs buried in CLI code — the hardcoded production
      URL that used to live in ``samantha.cli`` is now a named
      ``Profile`` with a docstring,
  (b) easy to add more profiles without touching interactive prompts,
  (c) the list is its own documentation of where Samantha can run.

0.2.0 will add a ``StdoutChannel`` and a "standalone" profile that
needs no backend at all. Until then, every Samantha deployment needs
one of these.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """A named Oasyce App backend Samantha can deliver chat replies to."""

    name: str
    api_base: str
    description: str


PUBLIC = Profile(
    name="public",
    api_base="http://39.107.153.12:39275/api/v1",
    description="Oasyce public backend (Aliyun-hosted).",
)

LOCAL = Profile(
    name="local",
    api_base="http://127.0.0.1:39277/api/v1",
    description="Local development — you run the App backend yourself.",
)

PROFILES: dict[str, Profile] = {p.name: p for p in (PUBLIC, LOCAL)}


def env_override() -> Profile | None:
    """Honour ``OASYCE_APP_API_BASE`` for non-interactive / CI setups.

    Returns an anonymous ``Profile`` wrapping the environment value so
    callers can present it in the same UI pattern as ``PUBLIC`` / ``LOCAL``.
    Returns ``None`` when the variable is unset — caller falls back to
    interactive selection.
    """
    url = os.getenv("OASYCE_APP_API_BASE")
    if url:
        return Profile(
            name="env",
            api_base=url,
            description="from OASYCE_APP_API_BASE environment variable",
        )
    return None

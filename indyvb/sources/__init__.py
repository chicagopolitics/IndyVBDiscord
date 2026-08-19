"""Registry of every configured source."""
from __future__ import annotations

from .base import Source
from .groupme import GroupMeEvents
from .leaguelab import CCALeagues, CCATournaments, IBeachLeagues
from .volleyballlife import VolleyballLifeTournaments

ALL_SOURCES: list[type[Source]] = [
    CCALeagues,
    CCATournaments,
    IBeachLeagues,
    VolleyballLifeTournaments,
    GroupMeEvents,
]

SOURCES_BY_SLUG: dict[str, type[Source]] = {s.slug: s for s in ALL_SOURCES}


def build(slugs: list[str] | None = None) -> list[Source]:
    """Instantiate the requested sources, or all of them when none are named."""
    if not slugs:
        return [cls() for cls in ALL_SOURCES]
    unknown = [s for s in slugs if s not in SOURCES_BY_SLUG]
    if unknown:
        known = ", ".join(sorted(SOURCES_BY_SLUG))
        raise ValueError(f"unknown source(s): {', '.join(unknown)}. Known: {known}")
    return [SOURCES_BY_SLUG[s]() for s in slugs]


__all__ = [
    "ALL_SOURCES", "SOURCES_BY_SLUG", "Source", "build",
    "CCALeagues", "CCATournaments", "IBeachLeagues", "VolleyballLifeTournaments",
    "GroupMeEvents",
]

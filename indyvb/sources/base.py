"""Source adapter interface."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..http import Fetcher
from ..models import Event

log = logging.getLogger(__name__)


class Source(ABC):
    """One upstream site or endpoint that yields Events."""

    slug: str = ""
    name: str = ""
    homepage: str = ""

    @abstractmethod
    def fetch(self, fetcher: Fetcher) -> list[Event]:
        """Return every listing this source currently advertises."""

    def safe_fetch(self, fetcher: Fetcher) -> tuple[list[Event], str | None]:
        """Fetch, converting any failure into an error string.

        One dead site must never take down the whole run, so the CLI always
        goes through this rather than calling fetch() directly.
        """
        try:
            events = self.fetch(fetcher)
            log.info("%s: %d events", self.slug, len(events))
            return events, None
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            log.error("%s failed: %s", self.slug, exc, exc_info=True)
            return [], f"{type(exc).__name__}: {exc}"

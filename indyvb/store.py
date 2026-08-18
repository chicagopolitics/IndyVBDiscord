"""Tracks what has already been posted, so Discord only sees what is new.

State is a single JSON file mapping each event uid to the fingerprint that was
last announced. Comparing fingerprints separates brand-new listings from ones
whose details (price, status, date) changed since the last run.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Event

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("data/seen.json")

# How long to keep a uid after its event date passes. Keeps the file from
# growing without bound while still preventing a re-post if a source briefly
# drops and restores a listing.
RETENTION_DAYS = 90


@dataclass
class Diff:
    """What changed since the previous run."""

    new: list[Event] = field(default_factory=list)
    updated: list[Event] = field(default_factory=list)
    unchanged: list[Event] = field(default_factory=list)

    @property
    def notable(self) -> list[Event]:
        return sorted(self.new + self.updated, key=lambda e: e.sort_key())

    def __bool__(self) -> bool:
        return bool(self.new or self.updated)


class SeenStore:
    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._entries = raw.get("events", {})
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop the run; worst case we
            # re-announce, which is far better than crashing the job.
            log.warning("could not read state at %s (%s); starting fresh",
                        self.path, exc)
            self._entries = {}

    def diff(self, events: list[Event]) -> Diff:
        """Classify events against what was previously recorded."""
        result = Diff()
        for event in events:
            previous = self._entries.get(event.uid)
            if previous is None:
                result.new.append(event)
            elif previous.get("fingerprint") != event.fingerprint:
                result.updated.append(event)
            else:
                result.unchanged.append(event)
        return result

    def record(self, events: list[Event]) -> None:
        """Mark events as announced. Call only after a successful publish."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for event in events:
            self._entries[event.uid] = {
                "fingerprint": event.fingerprint,
                "name": event.name,
                "start_date": event.start_date.isoformat() if event.start_date else None,
                "last_seen": now,
            }

    def prune(self, today: date | None = None) -> int:
        """Drop entries whose event finished long enough ago to be irrelevant."""
        today = today or date.today()
        stale = []
        for uid, entry in self._entries.items():
            start = entry.get("start_date")
            if not start:
                continue
            try:
                event_date = datetime.fromisoformat(start).date()
            except ValueError:
                continue
            if (today - event_date).days > RETENTION_DAYS:
                stale.append(uid)
        for uid in stale:
            del self._entries[uid]
        return len(stale)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "events": self._entries,
        }
        # Write via a temp file so an interrupted run cannot truncate the state.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._entries)

"""Maps events onto the forum tag vocabulary used in the Discord server.

The production channel is a forum whose tags are required, so every post must
carry at least one. Tags are applied by snowflake id rather than by name, so a
name-to-id mapping is loaded from a config file that `indyvb forum-tags`
generates by reading the channel.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .models import Event
from .utils import detect_format

log = logging.getLogger(__name__)

DEFAULT_TAG_CONFIG = Path("data/forum_tags.json")

# Discord allows at most 5 tags on a forum thread.
MAX_TAGS = 5

# Canonical vocabulary. Order is the priority used if a post would ever exceed
# MAX_TAGS: the kind is never dropped, because the forum requires a tag and
# every event has one.
# "Hard Court" is the forum's name for what is played on a gym floor; sand
# includes indoor sand, so the tag cannot be called "Indoor".
HARD_COURT = "Hard Court"

SURFACE_TAGS = ("Sand", HARD_COURT, "Grass")
# Open Play is a kind, not a modifier: a drop-in session is not a league, even
# when the organiser files it as one.
KIND_TAGS = ("League", "Tournament", "Open Play")
FORMAT_TAGS = ("Doubles", "Quads")
MODIFIER_TAGS = ("Reverse Co-Ed",)

TAG_PRIORITY = KIND_TAGS + SURFACE_TAGS + FORMAT_TAGS + MODIFIER_TAGS

# Earlier names for tags, so a rename in the forum does not silently drop a
# tag. Looked up only when the canonical name is not present.
TAG_ALIASES = {
    "hard court": ("Indoor",),
    "indoor": (HARD_COURT,),
}

# Sources that only ever play one surface. CCA is hard court; iBeach is sand,
# including at its indoor sand facility.
_HARD_COURT_SOURCES = {"cca", "cca-tournaments"}
_SAND_SOURCES = {"ibeach-leagues", "ibeach-tournaments"}

_GRASS_RE = r"\bgrass\b"
_SAND_RE = r"\b(sand|beach)\b"
# Hard-court signals. Deliberately excludes the word "indoor", which says where
# an event is, not what it is played on - iBeach's indoor courts are sand.
_HARD_COURT_RE = (
    r"\b(hard ?court|hardwood|gym|gymnasium|field ?house|ymca|jcc"
    r"|rec(reation)? cent(er|re)|high school|middle school)\b"
)

# Local venues whose surface cannot be read from the name. Substrings, matched
# case-insensitively against the listing text and venue names. Add to this as
# new venues turn up - it is the intended place for local knowledge.
VENUE_SURFACE = {
    "ibeach": "Sand",                    # includes "iBeach Indoor Courts"
    "academy volleyball club": HARD_COURT,  # hard court, despite the name
    "the academy": HARD_COURT,
}


def _text_of(event: Event) -> str:
    """All the free text worth pattern-matching against, lowercased."""
    parts = [event.name, event.location or "", event.description or ""]
    parts.extend(event.tags or [])
    parts.extend(v.name for v in event.venues)
    return " ".join(parts).lower()


def surface_tags(event: Event) -> set[str]:
    """What the event is played ON - not whether it has a roof.

    This distinction matters locally: iBeach has an indoor *sand* facility, so
    "iBeach Indoor Courts" is Sand. The Indoor tag means hard court, which is
    why the word "indoor" on its own is not evidence for it.

    Returns at most one surface: an event is not both sand and hard court.
    """
    text = _text_of(event)

    if re.search(_GRASS_RE, text):
        return {"Grass"}

    # Venues whose surface the name alone does not reveal.
    for needle, surface in VENUE_SURFACE.items():
        if needle in text:
            return {surface}

    # Sources that only ever play one surface.
    if event.source in _SAND_SOURCES:
        return {"Sand"}
    if event.source in _HARD_COURT_SOURCES:
        return {HARD_COURT}

    # Otherwise read it from the listing. Sand is checked first so that
    # "indoor sand" resolves to Sand rather than to hard court.
    if re.search(_SAND_RE, text):
        return {"Sand"}
    if re.search(_HARD_COURT_RE, text):
        return {HARD_COURT}
    return set()


def kind_tags(event: Event) -> set[str]:
    """Exactly one of League / Tournament / Open Play.

    The title wins over how the source files the listing: iBeach sells its
    Friday drop-in sessions through the league system, but "Friday Adult Open
    Play" is open play, not a league. Only the title is used, because the
    description and group name are too noisy to classify on.
    """
    title = (event.name or "").lower()

    # The only title that overrides the source: a drop-in session sold through
    # a league system is still open play.
    if "open play" in title or "open gym" in title:
        return {"Open Play"}

    if event.kind == "tournament":
        return {"Tournament"}
    if event.kind == "league":
        return {"League"}

    # Generic events (GroupMe) have no reliable kind, so read the title.
    if "tournament" in title or "tourney" in title:
        return {"Tournament"}
    if "league" in title:
        return {"League"}
    return {"Open Play"}


def derive_tags(event: Event) -> list[str]:
    """Tag names for an event, most important first, capped at MAX_TAGS."""
    found: set[str] = set()

    found |= kind_tags(event)
    found |= surface_tags(event)

    # Sources normally set play_format, but fall back to reading the name so a
    # listing is still tagged if one ever does not.
    play_format = event.play_format or detect_format(event.name)
    if play_format == "2s":
        found.add("Doubles")
    elif play_format == "4s":
        found.add("Quads")

    if "reverse" in _text_of(event):
        found.add("Reverse Co-Ed")

    ordered = [t for t in TAG_PRIORITY if t in found]
    if len(ordered) > MAX_TAGS:
        log.debug("trimming tags for %s: %s", event.name, ordered[MAX_TAGS:])
    return ordered[:MAX_TAGS]


class TagMap:
    """Maps tag names to the forum's snowflake ids."""

    def __init__(self, mapping: dict[str, str] | None = None):
        # Compare case-insensitively; Discord tag names are free text.
        self._by_name = {k.strip().lower(): str(v)
                         for k, v in (mapping or {}).items()}

    @classmethod
    def load(cls, path: Path | str = DEFAULT_TAG_CONFIG) -> "TagMap":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"No forum tag mapping at {path}. Run "
                f"`python -m indyvb.cli forum-tags --save` first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("tags", data))

    @staticmethod
    def save(mapping: dict[str, str], path: Path | str = DEFAULT_TAG_CONFIG) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tags": {name: str(tid) for name, tid in mapping.items()}}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _lookup(self, name: str) -> str | None:
        """Find a tag id, falling back to known earlier names."""
        key = name.strip().lower()
        tag_id = self._by_name.get(key)
        if tag_id:
            return tag_id
        for alias in TAG_ALIASES.get(key, ()):
            tag_id = self._by_name.get(alias.strip().lower())
            if tag_id:
                log.info("forum tag %r resolved via alias %r", name, alias)
                return tag_id
        return None

    def ids_for(self, names: list[str]) -> list[str]:
        """Resolve names to ids, skipping any the forum does not define."""
        ids = []
        for name in names:
            tag_id = self._lookup(name)
            if tag_id:
                ids.append(tag_id)
            else:
                log.warning("forum has no tag named %r; skipping it", name)
        return ids

    def missing(self, names: list[str]) -> list[str]:
        return [n for n in names if self._lookup(n) is None]

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return self._lookup(name) is not None


def tags_for(event: Event, tag_map: TagMap) -> list[str]:
    """The snowflake ids to apply to this event's forum post."""
    return tag_map.ids_for(derive_tags(event))


class TagLookupError(RuntimeError):
    pass


def fetch_available_tags(bot_token: str, webhook_url: str,
                         session=None) -> dict[str, str]:
    """Read the forum's tag names and ids.

    Discord exposes tag ids only through the channel object, which needs a bot
    token. The channel id is read from the webhook itself, so the only thing
    that has to be configured by hand is the token.
    """
    import requests

    if not bot_token:
        raise TagLookupError(
            "Reading forum tag ids needs a bot token. Set DISCORD_BOT_TOKEN in "
            "your .env (the bot must be in the server and able to view the "
            "forum channel)."
        )
    if not webhook_url:
        raise TagLookupError("Set DISCORD_WEBHOOK_URL so the channel can be found.")

    session = session or requests.Session()

    # The webhook object is readable with the webhook token alone.
    resp = session.get(webhook_url, timeout=30)
    if resp.status_code != 200:
        raise TagLookupError(
            f"Could not read the webhook ({resp.status_code}). Check "
            f"DISCORD_WEBHOOK_URL is correct and still exists."
        )
    channel_id = resp.json().get("channel_id")
    if not channel_id:
        raise TagLookupError("The webhook did not report a channel id.")

    resp = session.get(
        f"https://discord.com/api/v10/channels/{channel_id}",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=30,
    )
    if resp.status_code == 401:
        raise TagLookupError("Discord rejected the bot token (401).")
    if resp.status_code == 403:
        raise TagLookupError(
            "The bot cannot see that channel (403). Invite it to the server and "
            "give it View Channel on the forum."
        )
    if resp.status_code != 200:
        raise TagLookupError(
            f"Could not read the channel ({resp.status_code}): {resp.text[:200]}")

    channel = resp.json()
    # 15 = GUILD_FORUM, 16 = GUILD_MEDIA
    if channel.get("type") not in (15, 16):
        raise TagLookupError(
            f"Channel #{channel.get('name')} is not a forum channel, so it has "
            f"no tags. Point the webhook at the forum channel instead."
        )
    available = channel.get("available_tags") or []
    if not available:
        raise TagLookupError(
            f"Forum #{channel.get('name')} has no tags defined yet."
        )
    return {tag["name"]: str(tag["id"]) for tag in available}

"""Maps events onto the forum tag vocabulary used in the Discord server.

The production channel is a forum whose tags are required, so every post must
carry at least one. Tags are applied by snowflake id rather than by name, so a
name-to-id mapping is loaded from a config file that `indyvb forum-tags`
generates by reading the channel.
"""
from __future__ import annotations

import json
import logging
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
SURFACE_TAGS = ("Sand", "Indoor", "Grass")
KIND_TAGS = ("League", "Tournament")
FORMAT_TAGS = ("Doubles", "Quads")
MODIFIER_TAGS = ("Reverse Co-Ed", "Open Play")

TAG_PRIORITY = KIND_TAGS + SURFACE_TAGS + FORMAT_TAGS + MODIFIER_TAGS

# Sources whose events are always played indoors.
_INDOOR_SOURCES = {"cca", "cca-tournaments"}
# Sources played on sand unless a venue says otherwise.
_SAND_SOURCES = {"ibeach-leagues", "ibeach-tournaments"}


def _text_of(event: Event) -> str:
    """All the free text worth pattern-matching against, lowercased."""
    parts = [event.name, event.location or "", event.description or ""]
    parts.extend(event.tags or [])
    parts.extend(v.name for v in event.venues)
    return " ".join(parts).lower()


def surface_tags(event: Event) -> set[str]:
    """Playing surface. An event can legitimately have more than one.

    Several iBeach leagues list both the sand courts and the indoor courts,
    because play moves inside later in the season.
    """
    text = _text_of(event)
    found: set[str] = set()

    if "grass" in text:
        found.add("Grass")

    if event.source in _INDOOR_SOURCES:
        found.add("Indoor")
    if event.source in _SAND_SOURCES:
        # "iBeach Indoor Courts" is a distinct venue from the sand courts.
        indoor_venue = any("indoor" in v.name.lower() for v in event.venues)
        sand_venue = any("indoor" not in v.name.lower() for v in event.venues)
        if indoor_venue:
            found.add("Indoor")
        if sand_venue or not event.venues:
            found.add("Sand")

    # Explicit wording in the listing beats any assumption about the source.
    if "indoor" in text:
        found.add("Indoor")

    return found


def derive_tags(event: Event) -> list[str]:
    """Tag names for an event, most important first, capped at MAX_TAGS."""
    found: set[str] = set()

    found.add("Tournament" if event.kind == "tournament" else "League")
    found |= surface_tags(event)

    # Sources normally set play_format, but fall back to reading the name so a
    # listing is still tagged if one ever does not.
    play_format = event.play_format or detect_format(event.name)
    if play_format == "2s":
        found.add("Doubles")
    elif play_format == "4s":
        found.add("Quads")

    text = _text_of(event)
    if "reverse" in text:
        found.add("Reverse Co-Ed")
    if "open play" in text:
        found.add("Open Play")

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

    def ids_for(self, names: list[str]) -> list[str]:
        """Resolve names to ids, skipping any the forum does not define."""
        ids = []
        for name in names:
            tag_id = self._by_name.get(name.strip().lower())
            if tag_id:
                ids.append(tag_id)
            else:
                log.warning("forum has no tag named %r; skipping it", name)
        return ids

    def missing(self, names: list[str]) -> list[str]:
        return [n for n in names if n.strip().lower() not in self._by_name]

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name.strip().lower() in self._by_name


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

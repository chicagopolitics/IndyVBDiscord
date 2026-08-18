"""Delivery to Discord.

The webhook path needs no bot token, no gateway connection and no hosting, so
it is the default. Publishers share one interface, which is what lets a bot be
added later without touching the scrape or render layers.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

# Discord returns 429 with a retry_after when a webhook is hit too fast.
RATE_LIMIT_PAUSE = 1.0
MAX_RETRIES = 5


class PublishError(RuntimeError):
    pass


class Publisher:
    """Sends batches of embeds somewhere."""

    def send(self, embeds: list[dict], content: str | None = None) -> None:
        raise NotImplementedError


class ConsolePublisher(Publisher):
    """Dry-run target: prints what would be sent instead of sending it."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.messages: list[dict] = []

    def send(self, embeds: list[dict], content: str | None = None) -> None:
        payload = {"content": content, "embeds": embeds}
        self.messages.append(payload)
        titles = [e.get("title", "(untitled)") for e in embeds]
        print(f"[dry-run] message with {len(embeds)} embed(s)"
              + (f", content={content!r}" if content else ""))
        for title in titles:
            print(f"          - {title}")
        if self.verbose:
            import json
            print(json.dumps(payload, indent=2, ensure_ascii=False))


class WebhookPublisher(Publisher):
    """Posts to a Discord incoming webhook URL."""

    def __init__(self, webhook_url: str, username: str | None = None,
                 avatar_url: str | None = None, session: requests.Session | None = None):
        if not webhook_url:
            raise PublishError(
                "No Discord webhook URL configured. Set DISCORD_WEBHOOK_URL "
                "in your environment or .env file."
            )
        if "discord.com/api/webhooks/" not in webhook_url:
            raise PublishError(
                "That does not look like a Discord webhook URL "
                "(expected https://discord.com/api/webhooks/...)."
            )
        self.webhook_url = webhook_url
        self.username = username
        self.avatar_url = avatar_url
        self.session = session or requests.Session()

    def send(self, embeds: list[dict], content: str | None = None) -> None:
        payload: dict = {"embeds": embeds}
        if content:
            payload["content"] = content
        if self.username:
            payload["username"] = self.username
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        # Never let a scraped listing ping the whole server.
        payload["allowed_mentions"] = {"parse": []}

        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.post(self.webhook_url, json=payload, timeout=30)

            if resp.status_code == 429:
                retry_after = _retry_after(resp)
                log.warning("rate limited, sleeping %.2fs", retry_after)
                time.sleep(retry_after)
                continue

            if resp.status_code in (200, 201, 204):
                time.sleep(RATE_LIMIT_PAUSE)
                return

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                backoff = 2 ** attempt
                log.warning("discord %s, retrying in %ss", resp.status_code, backoff)
                time.sleep(backoff)
                continue

            raise PublishError(
                f"Discord rejected the message ({resp.status_code}): "
                f"{resp.text[:400]}"
            )
        raise PublishError(f"Giving up after {MAX_RETRIES} attempts (rate limited).")


def _retry_after(resp: requests.Response) -> float:
    try:
        return float(resp.json().get("retry_after", RATE_LIMIT_PAUSE))
    except Exception:  # noqa: BLE001 - malformed body, fall back to a safe pause
        return RATE_LIMIT_PAUSE


def publish_all(publisher: Publisher, batches: list[list[dict]],
                header: str | None = None) -> int:
    """Send every batch, putting the header on the first message only."""
    sent = 0
    for index, batch in enumerate(batches):
        publisher.send(batch, content=header if index == 0 else None)
        sent += len(batch)
    return sent

"""Shared HTTP client: real user-agent, retries, polite delays, optional disk cache."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ibeachvolleyball.com serves 403 to default library user-agents. A normal
# browser UA is required to get any of these pages at all.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30
MIN_DELAY = 1.0  # seconds between requests to the same host


class Fetcher:
    """Fetches URLs with retries and per-host throttling.

    Set ``cache_dir`` to persist responses to disk; repeat runs then work
    offline, which keeps development and tests off the live sites.
    """

    def __init__(self, cache_dir: Path | None = None, min_delay: float = MIN_DELAY):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_delay = min_delay
        self._last_hit: dict[str, float] = {}

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        return self.cache_dir / f"{key}.cache"

    def _throttle(self, url: str) -> None:
        host = url.split("/")[2] if "://" in url else url
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get_text(self, url: str, *, headers: dict | None = None,
                 use_cache: bool = True, log_url: str | None = None) -> str:
        """Fetch a URL as text.

        ``log_url`` replaces the URL in log output, for endpoints that require
        a credential in the query string.
        """
        shown = log_url or url
        path = self._cache_path(url)
        if use_cache and path and path.exists():
            log.debug("cache hit %s", shown)
            return path.read_text(encoding="utf-8")

        self._throttle(url)
        log.info("GET %s", shown)
        resp = self.session.get(url, headers=headers or {}, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        if path:
            path.write_text(text, encoding="utf-8")
        return text

    def get_json(self, url: str, *, headers: dict | None = None,
                 use_cache: bool = True, log_url: str | None = None):
        import json
        merged = {"Accept": "application/json"}
        merged.update(headers or {})
        return json.loads(self.get_text(url, headers=merged, use_cache=use_cache,
                                        log_url=log_url))

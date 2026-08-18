"""Command line interface for the Indy volleyball scraper."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from . import sources as source_registry
from .discord_events import DiscordEventError, ScheduledEventSync
from .http import Fetcher
from .ics import DEFAULT_CALENDAR_NAME, to_ics
from .models import Event
from .publish import ConsolePublisher, PublishError, WebhookPublisher, publish_all
from .render import chunk_embeds, digest_embeds, event_embed, to_text
from .store import DEFAULT_STATE_PATH, SeenStore

log = logging.getLogger("indyvb")


def collect(slugs: list[str] | None, fetcher: Fetcher) -> tuple[list[Event], dict[str, str]]:
    """Fetch every requested source, collecting failures instead of raising."""
    events: list[Event] = []
    errors: dict[str, str] = {}
    for source in source_registry.build(slugs):
        found, error = source.safe_fetch(fetcher)
        if error:
            errors[source.slug] = error
        events.extend(found)
    return events, errors


def apply_filters(events: list[Event], args, today: date | None = None) -> list[Event]:
    """Narrow the raw scrape down to what the user asked for."""
    today = today or date.today()
    result = events

    if not args.include_past:
        result = [e for e in result if e.is_upcoming(today)]
    if args.kind:
        result = [e for e in result if e.kind == args.kind]
    if args.open_only:
        result = [e for e in result if e.is_open]
    if args.within:
        horizon = today + timedelta(days=args.within)
        # Undated listings are kept: dropping them would hide real events.
        result = [e for e in result
                  if e.start_date is None or e.start_date <= horizon]
    if args.search:
        needle = args.search.lower()
        result = [e for e in result
                  if needle in e.name.lower()
                  or needle in (e.location or "").lower()]
    return sorted(result, key=lambda e: e.sort_key())


def _report_errors(errors: dict[str, str]) -> None:
    for slug, message in errors.items():
        print(f"  ! {slug}: {message}", file=sys.stderr)


def _make_fetcher(args) -> Fetcher:
    cache_dir = Path(args.cache) if args.cache else None
    return Fetcher(cache_dir=cache_dir)


def cmd_sources(args) -> int:
    print("Configured sources:\n")
    for cls in source_registry.ALL_SOURCES:
        instance = cls()
        url = getattr(instance, "url", "")
        print(f"  {cls.slug:22} {cls.name}")
        print(f"  {'':22} {url}")
    return 0


def cmd_health(args) -> int:
    """Check each source still returns parseable data.

    A scraper's worst failure is silent: the site is redesigned, the selectors
    match nothing, and the run reports zero new events forever. Treating an
    empty result as a failure is what makes that visible.
    """
    import time

    fetcher = _make_fetcher(args)
    rows, failures = [], 0
    for source in source_registry.build(args.source):
        started = time.monotonic()
        events, error = source.safe_fetch(fetcher)
        elapsed = time.monotonic() - started

        if error:
            state, detail = "FAIL", error
            failures += 1
        elif not events:
            state, detail = "EMPTY", "parsed 0 events - the page markup may have changed"
            failures += 1
        else:
            dated = sum(1 for e in events if e.start_date)
            upcoming = sum(1 for e in events if e.is_upcoming())
            state = "OK"
            detail = (f"{len(events)} events, {dated} dated, {upcoming} upcoming")
            if dated < len(events):
                state = "WARN"
                detail += "  <- some events have no date"
        rows.append((state, source.slug, f"{elapsed:.1f}s", detail))

    width = max(len(r[1]) for r in rows)
    for state, slug, elapsed, detail in rows:
        print(f"  [{state:5}] {slug:{width}}  {elapsed:>6}  {detail}")

    print(f"\n{len(rows) - failures}/{len(rows)} sources healthy.")
    return 1 if failures else 0


def cmd_fetch(args) -> int:
    fetcher = _make_fetcher(args)
    events, errors = collect(args.source, fetcher)
    events = apply_filters(events, args)
    payload = [e.to_dict() for e in events]

    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {len(payload)} events to {args.output}")
    else:
        print(text)
    _report_errors(errors)
    return 1 if errors and not events else 0


def cmd_list(args) -> int:
    fetcher = _make_fetcher(args)
    events, errors = collect(args.source, fetcher)
    events = apply_filters(events, args)
    print(to_text(events))
    print(f"\n{len(events)} event(s).")
    _report_errors(errors)
    return 1 if errors and not events else 0


def cmd_calendar(args) -> int:
    fetcher = _make_fetcher(args)
    events, errors = collect(args.source, fetcher)
    events = apply_filters(events, args)

    # Publishing an empty calendar would delete every subscriber's entries, so
    # a bad scrape must fail loudly and leave the previous file in place.
    if len(events) < args.min_events:
        print(f"Refusing to write: got {len(events)} events, expected at least "
              f"{args.min_events}. The previous calendar is left untouched.",
              file=sys.stderr)
        _report_errors(errors)
        return 3

    text = to_ics(events, calendar_name=args.name,
                  include_deadlines=not args.no_deadlines)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the CRLF line endings the format requires.
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        entries = text.count("BEGIN:VEVENT")
        print(f"Wrote {entries} calendar entries ({len(events)} listings) to {path}")
    else:
        sys.stdout.write(text)

    _report_errors(errors)
    return 1 if errors and not events else 0


def cmd_discord_events(args) -> int:
    """Mirror listings into the server's Events tab."""
    fetcher = _make_fetcher(args)
    events, errors = collect(args.source, fetcher)
    _report_errors(errors)
    events = apply_filters(events, args)

    if not events:
        print("Nothing to sync.")
        return 0

    try:
        sync = ScheduledEventSync(
            os.getenv("DISCORD_BOT_TOKEN", ""),
            os.getenv("DISCORD_GUILD_ID", ""),
        )
        result = sync.sync(events, dry_run=args.dry_run)
    except DiscordEventError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}{result.summary()}")
    for name in result.created:
        print(f"  + {name}")
    for name in result.updated:
        print(f"  ~ {name}")
    if args.verbose:
        for name, reason in result.skipped:
            print(f"  - {name}  ({reason})")
    elif result.skipped:
        print(f"  ({len(result.skipped)} skipped; -v to list them)")
    return 0


def cmd_post(args) -> int:
    fetcher = _make_fetcher(args)
    events, errors = collect(args.source, fetcher)
    _report_errors(errors)

    # A partial scrape would look like listings disappeared. Never let that
    # state be recorded, or the missing events are silently never announced.
    if errors and args.new_only and not args.allow_partial:
        print("Aborting: at least one source failed and --new-only is set.\n"
              "Re-run when the source recovers, or pass --allow-partial.",
              file=sys.stderr)
        return 2

    events = apply_filters(events, args)

    store = SeenStore(args.state)
    if args.new_only:
        diff = store.diff(events)
        to_post = diff.notable
        print(f"{len(diff.new)} new, {len(diff.updated)} updated, "
              f"{len(diff.unchanged)} unchanged.")
    else:
        to_post = events

    if not to_post:
        print("Nothing to post.")
        return 0

    if args.style == "digest":
        embeds = digest_embeds(to_post)
    else:
        embeds = [event_embed(e) for e in to_post]
    batches = chunk_embeds(embeds)

    header = args.message
    if header is None and args.new_only:
        header = f"**New volleyball listings** ({len(to_post)})"

    if args.dry_run:
        publisher = ConsolePublisher(verbose=args.verbose)
    else:
        try:
            publisher = WebhookPublisher(
                os.getenv("DISCORD_WEBHOOK_URL", ""),
                username=os.getenv("DISCORD_USERNAME") or None,
                avatar_url=os.getenv("DISCORD_AVATAR_URL") or None,
            )
        except PublishError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    try:
        sent = publish_all(publisher, batches, header=header)
    except PublishError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"{'Would send' if args.dry_run else 'Sent'} {sent} embed(s) "
          f"in {len(batches)} message(s).")

    # Only remember what actually reached Discord.
    if args.new_only and not args.dry_run:
        store.record(to_post)
        pruned = store.prune()
        store.save()
        print(f"State updated ({len(store)} tracked"
              + (f", {pruned} pruned" if pruned else "") + ").")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indyvb",
        description="Scrape Indianapolis volleyball leagues and tournaments, "
                    "and publish them to Discord.",
    )
    # Defined on a shared parent rather than the top level so it works in the
    # natural position, after the subcommand.
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    common = argparse.ArgumentParser(add_help=False, parents=[base])
    common.add_argument("-s", "--source", action="append",
                        help="limit to a source slug (repeatable); default is all")
    common.add_argument("--kind", choices=["league", "tournament"],
                        help="only leagues or only tournaments")
    common.add_argument("--within", type=int, metavar="DAYS",
                        help="only events starting within DAYS days")
    common.add_argument("--open-only", action="store_true",
                        help="only listings still open for signup")
    common.add_argument("--include-past", action="store_true",
                        help="keep events that have already finished")
    common.add_argument("--search", metavar="TEXT",
                        help="filter by text in the name or location")
    common.add_argument("--cache", metavar="DIR",
                        help="cache HTTP responses in DIR (useful offline)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", parents=[base], help="list configured sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("health", parents=[common],
                       help="verify every source still parses (exit 1 if not)")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("list", parents=[common], help="print events as text")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("fetch", parents=[common], help="print events as JSON")
    p.add_argument("-o", "--output", metavar="FILE", help="write JSON to FILE")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("calendar", parents=[common],
                       help="export events as a subscribable .ics calendar")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write the .ics file here (default: stdout)")
    p.add_argument("--name", default=DEFAULT_CALENDAR_NAME,
                   help="calendar display name shown to subscribers")
    p.add_argument("--no-deadlines", action="store_true",
                   help="omit the signup-deadline entries")
    p.add_argument("--min-events", type=int, default=0, metavar="N",
                   help="refuse to write if fewer than N events were found; "
                        "guards against publishing an empty calendar")
    p.set_defaults(func=cmd_calendar)

    p = sub.add_parser("discord-events", parents=[common],
                       help="mirror listings into the server Events tab")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would change without touching the server")
    p.set_defaults(func=cmd_discord_events)

    p = sub.add_parser("post", parents=[common], help="publish events to Discord")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent instead of sending")
    p.add_argument("--new-only", action="store_true",
                   help="only post listings that are new or changed since last run")
    p.add_argument("--style", choices=["detailed", "digest"], default="detailed",
                   help="one embed per event, or a compact per-source summary")
    p.add_argument("--message", help="text to post above the embeds")
    p.add_argument("--state", default=str(DEFAULT_STATE_PATH),
                   help=f"path to the seen-state file (default {DEFAULT_STATE_PATH})")
    p.add_argument("--allow-partial", action="store_true",
                   help="post with --new-only even if a source failed")
    p.set_defaults(func=cmd_post)

    return parser


def _force_utf8_output() -> None:
    """Keep emoji and separators from crashing the default Windows console.

    The Windows terminal defaults to cp1252, which cannot encode the emoji used
    in the Discord embeds, and printing one raises UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

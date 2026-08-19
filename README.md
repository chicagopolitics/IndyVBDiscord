# IndyVB Discord

Scrapes Indianapolis-area volleyball leagues and tournaments from local sites and
publishes them to a Discord channel.

## Sources

| Slug | What it covers | Endpoint used |
|---|---|---|
| `cca` | CCA Sports indoor leagues | `ccasports.com/leagues?v=upcoming&sport=Volleyball` |
| `cca-tournaments` | CCA one-night tournaments | `ccasports.com/page/Indoor-Volleyball-Tournaments` |
| `ibeach-leagues` | iBeach leagues (2s / 4s / 6s) | `widget.leaguelab.com/v1/ibeachvolleyball/league-listing` |
| `ibeach-tournaments` | iBeach beach tournaments | `api-v8.volleyballlife.com/tournament/summaries` |

Three things worth knowing about why these particular endpoints:

- **CCA and iBeach both run on LeagueLab.** CCA is a white-label deployment, so
  its leagues page and its tournaments page use the same `div.league-listing`
  markup and share one parser. iBeach uses LeagueLab's hosted widget instead.
- **`ibeachvolleyball.com` is not scraped directly.** It returns HTTP 403 to
  non-browser clients, and the page is only an iframe wrapper around the
  LeagueLab widget. Requesting the widget is both simpler and more stable.
- **VolleyballLife is a single-page app** with no useful server-rendered HTML.
  It reads from a public JSON API, which this talks to directly, so no browser
  automation or HTML parsing is involved for tournaments.

## Venues

League listings only carry venue *ids* (`data-locationids="6648"`), which is why
the iBeach widget renders the unhelpful "Multiple Locations" whenever a league
spans two courts. Every LeagueLab site publishes a `/locations` page with one
block per venue, so a single extra request per site resolves ids to real names
and street addresses.

The result is that a league that used to read "Multiple Locations" now reads
"iBeach, iBeach Indoor Courts" with an address and a Google Maps link, and CCA's
"Fishers Locations" resolves to *Fishers Community Center* and *Fishers YMCA*.

Each event carries a `venues` list of structured `Venue` records (id, name,
address, latitude, longitude) plus a `map_url` on each one. VolleyballLife is
the only source that supplies exact coordinates, which are preferred over the
address string when building the map link.

The directory is loaded lazily, once per run, and a failure to load it is
logged and ignored - venue detail is an enhancement, so losing it degrades the
output rather than breaking the scrape.

## Setup

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Then copy `.env.example` to `.env` and paste in your webhook URL:

```bash
cp .env.example .env
```

To create the webhook: **Server Settings → Integrations → Webhooks → New
Webhook**, pick the channel, then **Copy Webhook URL**. Anyone holding that URL
can post to the channel, so it stays in `.env` (gitignored) and never in the code.

## Usage

Everything runs through one CLI. `list` and `health` touch no Discord state, so
they are safe to run any time.

See what is out there right now:

```bash
.venv/Scripts/python.exe -m indyvb.cli list --within 30
```

Check every source still parses (exits non-zero if one breaks):

```bash
.venv/Scripts/python.exe -m indyvb.cli health
```

Preview exactly what would be posted, without sending anything:

```bash
.venv/Scripts/python.exe -m indyvb.cli post --dry-run --within 30 --open-only
```

Post only listings that are new or changed since the last run:

```bash
.venv/Scripts/python.exe -m indyvb.cli post --new-only
```

Dump structured JSON for use elsewhere:

```bash
.venv/Scripts/python.exe -m indyvb.cli fetch -o events.json
```

Export a subscribable calendar:

```bash
.venv/Scripts/python.exe -m indyvb.cli calendar -o public/indyvb.ics
```

Mirror listings into the server's Events tab (preview first):

```bash
.venv/Scripts/python.exe -m indyvb.cli discord-events --dry-run --within 60
```

### Useful flags

| Flag | Effect |
|---|---|
| `--source cca` | Limit to one source (repeatable) |
| `--kind tournament` | Only tournaments, or only leagues |
| `--within 30` | Only events starting in the next 30 days |
| `--open-only` | Only listings still open for signup |
| `--search fishers` | Filter by name or location text |
| `--style digest` | One compact embed per source instead of one per event |
| `--dry-run` | Print what would be sent, send nothing |
| `--cache DIR` | Cache HTTP responses; lets you iterate offline |

## Posting to a forum channel

A forum channel is not a text channel with extras - a webhook message without a
thread name is rejected outright, and tags are applied by snowflake id rather
than by name. Pass `--forum` (or set `DISCORD_FORUM=1`) and each listing becomes
its own forum thread with tags applied.

### One-time setup

Tag ids are only exposed on the channel object, which needs a bot token. The
channel is found from the webhook itself, so the token is the only extra config:

```bash
.venv/Scripts/python.exe -m indyvb.cli forum-tags --save
```

That writes `data/forum_tags.json` and reports drift in both directions - tags
this tool would apply that the forum lacks, and forum tags nothing will ever
set. Re-run it whenever the forum's tags change.

### How tags are derived

| Tag | Comes from |
|---|---|
| `League` / `Tournament` | the listing kind (always exactly one) |
| `Doubles` / `Quads` | play format 2s / 4s (6s has no tag in the vocabulary) |
| `Sand` / `Indoor` | the source and the venues on the listing |
| `Grass` | the word "grass" in the listing |
| `Reverse Co-Ed` | the word "reverse" in the listing |
| `Open Play` | the phrase "open play" in the listing |

An event can carry more than one surface tag. Most iBeach leagues list both the
sand courts and the indoor courts, because play moves inside later in the
season, so they are tagged `Sand` and `Indoor`.

Discord caps a thread at 5 tags. Derivation is ordered so the kind is never the
tag that gets trimmed, since the forum requires at least one.

### Safety behaviour

Because the forum *requires* a tag, a listing whose tags cannot be resolved
would be rejected mid-run. The command checks every listing up front and
refuses to post anything if any of them is untaggable, naming the offenders.

Posting creates one thread per listing rather than one batched message, so a
failure part way through is possible. Successfully created threads are recorded
before the error is reported, and re-running resumes rather than duplicating.

## How `--new-only` works

Each listing has a stable id from the upstream system, so it gets a stable uid
like `cca:league:102721`. After a successful post, the uid and a fingerprint of
its meaningful fields (date, price, status, deadline, location) are written to
`data/seen.json`.

On the next run:

- uid not in the file → **new**, gets posted
- uid present, fingerprint differs → **updated**, gets posted
- uid present, fingerprint matches → skipped

The fingerprint deliberately ignores cosmetic fields like the description, so an
upstream copy edit does not re-announce a league. Entries are pruned 90 days
after the event date.

State is only written after Discord accepts the message, and a run where any
source failed will refuse to record state unless you pass `--allow-partial`.
Both rules exist for the same reason: if a source is briefly down, its listings
must not be silently marked as "already announced" and then never posted.

## Calendars

Two independent ways to get this onto a calendar. They share the same data and
can be run together.

### .ics subscribe feed

`calendar` writes a standard iCalendar file containing:

- one entry per league or tournament on its start date
- one all-day entry per signup deadline (`--no-deadlines` to omit)

Entries carry the venue address, a map link, price, availability and the signup
URL. Where a source publishes start times (iBeach leagues do) the entry is
timed in `America/Indiana/Indianapolis`; otherwise it is all-day.

Entry UIDs are derived from the stable upstream id, so re-publishing the file
updates existing entries rather than creating duplicates. Subscribers are asked
to refresh every 6 hours.

Subscribe to the URL rather than importing the file: an import is a one-time
copy that never updates, which is the usual reason a calendar goes stale.

### Hosting it on GitHub Pages

`.github/workflows/calendar.yml` rebuilds the calendar four times a day and
publishes it, along with `web/index.html` - a page with the feed URL and
subscribe instructions that you can link members to.

One-time setup:

1. Create a repository on GitHub and push this project to it.
2. In the repo, go to **Settings → Pages** and set **Source** to
   **GitHub Actions**.
3. Go to **Actions**, pick *Publish calendar*, and click **Run workflow**.

The calendar is then at `https://<user>.github.io/<repo>/indyvb.ics`, with the
instructions page at `https://<user>.github.io/<repo>/`.

The calendar build is stateless - it regenerates the whole file every run - so
unlike `post --new-only` it needs nothing persisted between jobs.

The workflow passes `--min-events 10`. If a scrape breaks badly, the build
fails instead of publishing a near-empty calendar, which would otherwise delete
every subscriber's entries. The previously published file stays up until a good
build replaces it.

Nothing secret is involved: the calendar uses only public data, so no repository
secrets are needed unless you also add the Discord posting job.

### Discord scheduled events

`discord-events` mirrors listings into the server's Events tab, where members
get RSVP and native notifications. This one needs a bot:

1. Create an application at <https://discord.com/developers/applications>.
2. Invite it to your server with the **Manage Events** permission.
3. Put its token in `DISCORD_BOT_TOKEN` and your server id in `DISCORD_GUILD_ID`.

It is a plain REST call, so no gateway connection or `discord.py` is needed.

Discord assigns its own event ids, so each description carries a hidden marker
(`[indyvb:cca:league:102721]`) identifying the listing it came from. The sync
matches on that, which keeps it stateless: no local mapping file to lose, and
re-runs update in place instead of duplicating.

Two limits worth knowing: a guild caps at **100 scheduled events**, and Discord
rejects a start time in the past. Both are handled - events past their start are
skipped, and the sync stops creating once the cap is reached rather than
erroring. Use `--within 60` to keep well under the cap.

Always preview with `--dry-run` first; it reports exactly what would change
without touching the server.

## Scheduling

The CLI is the whole tool, so any scheduler works. Two straightforward options:

**Windows Task Scheduler** — runs on this machine, only while it is on:

```bash
schtasks /create /tn "IndyVB Discord" /tr "'C:\Users\chris\OneDrive\Documents\Programming Projects\IndyVBDiscord\.venv\Scripts\python.exe' -m indyvb.cli post --new-only" /sc daily /st 09:00
```

**GitHub Actions** — always on, free. Store the webhook as a repository secret
(`DISCORD_WEBHOOK_URL`). Note that `data/` is gitignored, so to make `--new-only`
work across runs you need to persist `data/seen.json` between jobs, either by
committing it from the workflow or caching it with `actions/cache`. Without that,
every run re-announces everything.

## Maintenance

Scrapers break when sites change. The failure that matters is the quiet one: a
redesign makes the selectors match nothing, and the job cheerfully reports zero
new events forever.

`indyvb.cli health` exists for exactly that: it treats a source returning zero
events as a failure and exits non-zero, so it can be wired to an alert.

The tests run against saved copies of the real pages in `tests/fixtures/`:

```bash
.venv/Scripts/python.exe -m pytest -q
```

Those catch regressions in the parsing logic, but not upstream markup changes —
only `health` against the live sites can see those.

## Adding a source

1. Add a class in `indyvb/sources/` implementing `fetch(self, fetcher)` and
   returning `Event` objects. If the site is LeagueLab-based, subclass
   `_CCAListingPage` and set `url` / `sport` / `event_kind`.
2. Register it in `ALL_SOURCES` in `indyvb/sources/__init__.py`.
3. Save a fixture in `tests/fixtures/` and add a parser test.

Sources are fetched through `safe_fetch`, so one broken site degrades that
source only and never takes down the run.

## Layout

```
indyvb/
  models.py      Event dataclass, uid and fingerprint logic
  http.py        Session with browser UA, retries, throttling, optional cache
  utils.py       Date/price/label parsing shared by the parsers
  store.py       seen.json state and new/updated/unchanged diffing
  render.py      Discord embeds, plus Discord size-limit chunking
  publish.py     Webhook delivery, rate-limit handling, dry-run publisher
  cli.py         Command line interface
  sources/       One adapter per upstream site
tests/           Parser, store and render tests, with saved page fixtures
```

## Notes and limitations

- **CCA dates carry no year.** The page says "Wednesday, August 19", so the year
  is inferred as the nearest sensible one: anything more than ~120 days in the
  past is read as next year.
- **CCA sub-list prices are excluded** from the headline price. A sub spot costs
  ~$8 against a ~$78 league fee and would otherwise make every league look
  mispriced.
- **Team and individual availability are tracked separately**, because CCA
  leagues very often have teams sold out while individual spots stay open.
- **VolleyballLife registration status is not exposed** by the summaries
  endpoint, so tournaments show no open/closed state; the event link has it.
- The VolleyballLife endpoint is not scoped to one organization, so results are
  filtered to `ibeachvolleyball` explicitly.
- **A few CCA venues store coordinates in their address field** instead of a
  street address. Those are parsed as coordinates, so the map link still works
  but no address text is shown.

## Toward a Discord bot

The webhook path posts announcements on a schedule. A bot additionally answers
questions on demand, which needs two things this already provides:

- `Event.uid` is stable (`cca:league:102721`), so it works as a select-menu
  value or button custom_id round-tripped back to a specific listing.
- `Event.to_dict()` is fully serializable, including venues and map urls, so
  the bot layer can read cached JSON instead of scraping per command.

The one thing to add is a cache: a slash command must answer in under 3 seconds,
and a cold scrape of all four sources takes ~4. Run the existing `fetch` on a
timer, write `events.json`, and have commands read that file.

`publish.py` defines a `Publisher` interface with `WebhookPublisher` and
`ConsolePublisher` behind it, so a bot publisher slots in without touching the
scrape or render layers.

# flightdrop

A personal flight-deal alert bot. Every few hours it looks for cheap round-trip fares out of your home
airports (set to **MSP** and **DFW**), decides which are worth your attention, and posts them to a
Discord channel with a one-click Google Flights link. It costs **$0** to run: no paid APIs, no credit card.

It is a DIY take on apps like DailyDrop, for cash fares only (points/award search is not included).

**Economy only, mostly.** The free fare data contains no business-class fares, so business deals only show up when
one of the deal websites below happens to post one from your airports. Expect those to be rare.

## How it decides what's a deal

A fare is posted if it passes **either** rule:

- **Fixed price:** at or under your target for that region and cabin (`thresholds_usd` in `config.yaml`).
- **Cheap vs. usual:** at least 30% below that route's median price over the last 60 days. This needs a few
  weeks of history to kick in; the fixed-price rule works from day one.

On top of that you can restrict where you go, set trip lengths, and add a **watchlist** of specific routes with a
target price that always alerts. At most 5 alerts go out per day, best first, and a deal is never posted twice
unless its price drops by 10% or more.

## When alerts are sent

Alerts are only posted between **8:00 AM and 9:30 PM Central** (it follows daylight saving), so your phone isn't
buzzing overnight. The bot scans five times a day: about **9:15 AM, 12:15 PM, 3:15 PM, 6:15 PM and 8:45 PM** Central.
Anything found outside the window waits and is sent at the first scan after it opens. If you start a run by hand
from the Actions tab, it posts right away regardless of the hour.

Why these times, from the research and from measuring the data:

- The cached fares are a median of about 3.6 days old and are refreshed around the clock by travelers' searches, so
  scanning more often than a few times a day finds little that's new.
- TheFlightDeal posts its daily Dallas deal around midday Central, so the 12:15 and 3:15 PM scans catch it.
- Mistake fares (error fares) usually die within hours. This bot's cached data can't catch those; it is built for
  good deals that last days. Catching error fares needs a faster, paid source.
- Published advice about the "best day or time to book" is mostly folklore, so the timing is about your waking hours,
  not about the fare.

To change the window, edit `alert_hours` in `config.yaml` (quote the times: `"08:00"`). To change the scan times,
edit the `cron` lines in `.github/workflows/scan.yml` (they are in UTC).

## Where the data comes from (all free)

| Source | What it gives | Catch |
|---|---|---|
| Aviasales / Travelpayouts Data API | Cheapest cached economy fares from your airports to ~100-200 destinations | Prices are cached (up to ~7 days old), not live; no business class |
| TheFlightDeal RSS (main + Dallas feeds) | Human-found deals; about one Dallas deal a day, posted around midday Central | Rarely mentions any one airport; no Minneapolis feed |
| SerpApi Google Flights | Checks every fare's live Google Flights price before it is posted | 250 free searches/month, so about 8 a day |

### Why every alert is checked against Google

The cheapest cached fares turned out to be unreliable: they were often 4 to 6 days old and came from small discount
agencies whose advertised prices don't hold up (for example a Rome fare cached at $422 that Google showed at about
$740). So before an alert is posted, the bot asks Google Flights (through SerpApi) for the live price on those exact
dates. The alert shows **Google's price** (the same price your link opens), and a fare that no longer beats your price
bars is dropped. Expect **fewer alerts**, but ones you can actually book.

- A fare that can't be checked (no key, the free quota is used up, or an error) is **held, not posted**, and tried
  again at the next scan. You'll get one Discord notice a day if checking is *impossible* (no key, or SerpApi failing).
- Results are remembered for 24 hours so the free quota isn't spent re-checking the same fare.
- Set `verify: require_live: false` in `config.yaml` if you'd rather get unchecked deals labelled as cached prices.

## Setup

You need a GitHub account, a Discord server, and about 15 minutes.

1. **Travelpayouts token** (required). Sign up free at <https://www.travelpayouts.com>, then copy your API token
   from the developer/API section.
2. **Discord webhook** (required). In your server: channel settings > Integrations > Webhooks > New Webhook >
   Copy Webhook URL.
3. **SerpApi key** (needed for alerts to go out). A free account at <https://serpapi.com> gives you a key: register,
   verify your email, then copy **Your Private API Key** from the dashboard. Without it, deals are held until you add one.
4. **Repo secrets.** In this repo: Settings > Secrets and variables > Actions > New repository secret. Add
   `TRAVELPAYOUTS_TOKEN`, `DISCORD_WEBHOOK_URL`, and `SERPAPI_KEY`.
5. **Run it.** Actions tab > *scan* > Run workflow. Tick *dry_run* first to see what it would post in the log.
   After that it runs by itself five times a day (see "When alerts are sent" below).

Keep the repo **public**: public repos get unlimited free Actions minutes. Your tokens live only in GitHub secrets,
never in the code.

## Changing what it looks for

Everything is in [`config.yaml`](config.yaml), with comments. Edit it on GitHub (pencil icon) and commit; the next
run uses it. If you break it, the run stops and posts the error to your Discord channel instead of misbehaving.

```yaml
destinations:
  include_regions: [europe, asia_oceania]   # empty = anywhere
  exclude_countries: [IQ, AF, RU]           # conflict zones are already skipped by default
airlines:
  exclude: []                               # e.g. [F9, NK] to never show Frontier or Spirit fares
  include: []                               # or list the only carriers you'll fly
  alternatives_for: [F9, NK]                # alerts on these also show the cheapest fare on ANY OTHER airline
watchlist:
  - {to: NRT, cabin: economy, max_price_usd: 800}                      # fixed target: alert at or under $800
  - {from: MEM, to: MSP, alert_when: lower_than_previous}              # moving target: see below
trip:
  min_days: 4
  max_days: 14
  depart_months: [3, 4, 10]
```

### Watchlist: fixed and moving targets

A **fixed** entry (`max_price_usd: 800`) alerts whenever a fare is at or under that price. A **moving** entry
(`alert_when: lower_than_previous`) has no price of its own: it alerts on the first fare it sees for the route as a
starting point, then only when a fare is at least $1 lower than the last one it alerted you about. Each alert
lowers the bar for the next. You can add `max_price_usd` to a moving entry as a ceiling. The bot forgets the last
price after 90 days, so one freak low fare can't block alerts forever. Watchlist entries may start from any airport
(not just your home airports), but the fare data only has routes that travelers have recently searched, so a quiet
route can stay quiet. Business class can't be checked.

## Running it on your own computer

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # run the tests

export TRAVELPAYOUTS_TOKEN=...           # your token
python -m flightdrop.main --dry-run      # fetch for real, print instead of posting, save nothing
```

`--dry-run` works on a throwaway copy of the price history, so it never changes saved state.
Without `--dry-run` you also need `DISCORD_WEBHOOK_URL`.

## Good to know

- **Price history** is stored in a small SQLite file on a `data` branch that the workflow overwrites each run
  (one commit, so the repo never grows).
- **GitHub pauses scheduled workflows** after 60 days without repo activity. The workflow re-enables itself on
  every run to prevent that. If alerts ever stop, open Actions and click *Enable workflow*.
- **Scheduled runs can be late** by several minutes. That's fine for deal alerts.
- Feed deals are matched by city name and may link to fares you'll need to check manually.
- Many of the cheapest US fares are Frontier base fares (no bags). When an alert is for Frontier or Spirit, it
  includes a **Next best (other airlines)** line with the cheapest fare on a different carrier for that route.
  To hide those carriers entirely, put them under `airlines: exclude`.
- Fares and availability change constantly. This tool points you at candidates; always confirm the price and
  conditions before you book.

## Security notes

- **Secrets** live only in your local `.env` (gitignored) and in GitHub Actions secrets. They never appear in the
  code, the config, or the logs. If your Discord webhook URL ever leaks, delete the webhook in Discord (channel
  settings > Integrations > Webhooks) and make a new one.
- **The workflow** only runs on a schedule or when you start it by hand, so pull requests from forks can never see
  your secrets. Its GitHub Actions are pinned to exact commits, and Python packages install from `requirements.lock`,
  which only allows the exact files whose hashes were checked.
- **Refreshing the lock** (when you want newer package versions):
  `pip install pip-tools && pip-compile --generate-hashes --strip-extras --output-file=requirements.lock pyproject.toml`,
  run the tests, then commit the new file.
- **Untrusted text:** deal-feed titles come from strangers. Alerts never ping `@everyone`, only `http(s)` links are
  used, and nothing from the internet is ever run as code.
- This repo is public, so `config.yaml` (your home airports and price targets) is visible to anyone.

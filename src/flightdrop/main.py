"""Command-line entry point: ``python -m flightdrop.main [--dry-run]``.

Secrets come from the environment only:
  TRAVELPAYOUTS_TOKEN   fare source (without it that source is skipped)
  DISCORD_WEBHOOK_URL   where alerts are posted (required unless --dry-run)
  SERPAPI_KEY           optional live-price check
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from functools import partial
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import requests
import yaml

from . import pipeline
from .airlines import airline_name
from .config import Config, ConfigError, load_config
from .links import google_flights_url
from .models import Deal, ScoredDeal
from .notify import discord
from .sources import rss_deals, travelpayouts
from .sources.common import SourceError
from .sources.serpapi_verify import live_quote
from .store import Store

log = logging.getLogger("flightdrop")


def collect(cfg: Config, env: Mapping[str, str], now: datetime, session: requests.Session) -> tuple[list[Deal], int, int]:
    """Run every enabled source. Returns (deals, sources_attempted, sources_failed)."""
    deals: list[Deal] = []
    attempted = failed = 0

    token = env.get("TRAVELPAYOUTS_TOKEN")
    if token:
        attempted += 1
        try:
            deals += travelpayouts.fetch(cfg, token, now, session=session)
        except SourceError as e:
            failed += 1
            log.error("Travelpayouts failed: %s", e)
    else:
        log.warning("TRAVELPAYOUTS_TOKEN is not set; skipping the fare source")

    if cfg.rss.enabled and cfg.rss.feeds:
        attempted += 1
        try:
            deals += rss_deals.fetch(cfg, now, session=session)
        except SourceError as e:
            failed += 1
            log.error("Deal feeds failed: %s", e)

    return deals, attempted, failed


def describe(s: ScoredDeal) -> str:
    d = s.deal
    if d.curated:
        return f"[feed] {d.title}\n        {google_flights_url(d)}"
    if s.candidate:
        cheap = f"; looks {round((1 - s.cheapness) * 100)}% under what we'd expect" if s.cheapness is not None else ""
        return (
            f"{d.origin}→{d.destination} {d.cabin} ${d.price_usd:,.0f} cached {airline_name(d.airline) or ''} "
            f"({d.depart_date} → {d.return_date}) [a live Google check would decide{cheap}]\n        {google_flights_url(d)}"
        )
    dates = f"{d.depart_date} → {d.return_date}"
    text = (
        f"{d.origin}→{d.destination} {d.cabin} ${d.price_usd:,.0f} {airline_name(d.airline) or ''} ({dates}) "
        f"[{'; '.join(s.reasons)}]\n        {google_flights_url(d)}"
    )
    if s.alternative_checked:
        alt = s.alternative
        text += (
            f"\n        next best on another airline: ${alt.price_usd:,.0f} {airline_name(alt.airline)} "
            f"({alt.depart_date} → {alt.return_date})"
            if alt else "\n        next best on another airline: none found in the cached data"
        )
    return text


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    ap = argparse.ArgumentParser(prog="flightdrop", description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="state/flightdrop.sqlite", help="SQLite file holding price history")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and score for real, but print instead of posting; saved state is not changed")
    ap.add_argument("--ignore-alert-hours", action="store_true",
                    help="post even outside the configured alert hours (used for manual runs)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    webhook = env.get("DISCORD_WEBHOOK_URL")
    session = requests.Session()

    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError, yaml.YAMLError) as e:
        log.error("cannot load %s: %s", args.config, e)
        if webhook and not args.dry_run:
            try:
                discord.post_notice(webhook, f"`{args.config}` could not be loaded, so no scan ran:\n{e}", session)
            except discord.NotifyError as ne:
                log.error("also could not send the notice: %s", ne)
        return 1

    if not webhook and not args.dry_run:
        log.error("DISCORD_WEBHOOK_URL is not set (use --dry-run to try without Discord)")
        return 1

    now = now or datetime.now(timezone.utc)     # injectable so tests can pick the time of day
    deals, attempted, failed = collect(cfg, env, now, session)
    if attempted == 0:
        log.error("nothing to scan: set TRAVELPAYOUTS_TOKEN or enable rss in the config")
        return 1
    if failed == attempted:
        log.error("every data source failed")
        return 1
    log.info("%d candidate deal(s) from %d source(s)", len(deals), attempted - failed)

    # A dry run always ignores the window so it shows results at any hour.
    alerts_allowed = args.dry_run or args.ignore_alert_hours or pipeline.in_alert_hours(now, cfg)
    if not alerts_allowed:
        window = cfg.alert_hours
        log.info(
            "outside alert hours (%s-%s %s): scanning and recording prices, but holding new deals "
            "until the next scan inside the window",
            window.start.strftime("%H:%M"), window.end.strftime("%H:%M"), cfg.timezone,
        )

    key = env.get("SERPAPI_KEY")
    verify = None
    if cfg.verify.enabled and key and not args.dry_run:
        verify = partial(live_quote, api_key=key, session=session)

    # Free, read-only lookups, so they also run in a dry run.
    token = env.get("TRAVELPAYOUTS_TOKEN")
    alternative = (
        partial(travelpayouts.find_alternative, cfg=cfg, token=token, now=now, session=session)
        if token and cfg.airlines.alternatives_for
        else None
    )

    if args.dry_run:
        def post(s: ScoredDeal) -> None:
            print("WOULD CHECK:" if s.candidate else "WOULD ALERT:", describe(s))
    else:
        post = partial(_post_discord, webhook, session=session)  # type: ignore[arg-type]

    # A dry run works on a throwaway copy of the database so it simulates the real thing
    # (history, repeat detection, daily cap) without changing any saved state.
    scratch = tempfile.mkdtemp(prefix="flightdrop-dry-") if args.dry_run else None
    db_path = args.db
    if scratch:
        db_path = os.path.join(scratch, "dry-run.sqlite")
        if os.path.exists(args.db):
            shutil.copyfile(args.db, db_path)
    # Only deals whose live Google price was checked may go out (a dry run never spends the quota).
    # The typical-price rule can only judge a fare with Google's live data, so it always requires it.
    require_live = cfg.verify.enabled and (cfg.verify.require_live or cfg.typical.enabled) and not args.dry_run
    if require_live and verify is None:
        log.warning("live price checking is required but SERPAPI_KEY is not set: deals will be held")

    store = Store(db_path)
    try:
        result = pipeline.run_scan(
            deals, cfg, store, now, post=post, verify=verify, alternative=alternative,
            alerts_allowed=alerts_allowed, require_live=require_live, preview_candidates=args.dry_run,
        )
        if result.unverifiable and webhook and not args.dry_run:
            _notify_unverifiable(webhook, result, store, cfg, now, session)
    finally:
        store.close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    log.info(
        "%d fares, %d worth a look, %d new, %d slot(s) left today -> %d alert(s)%s",
        result.candidates, result.qualified, result.new, result.slots, len(result.alerts),
        (f" ({result.failed_posts} failed to post)" if result.failed_posts else "")
        + (f" ({result.held} held for the alert hours)" if result.held else "")
        + (f" ({result.held_unverified} held: live price not checked)" if result.held_unverified else ""),
    )
    return 0


def _notify_unverifiable(
    webhook: str, result: pipeline.RunResult, store: Store, cfg: Config, now: datetime, session: requests.Session
) -> None:
    """Tell the user, at most once a day, that deals are waiting because checking is impossible."""
    day = now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    if store.usage_on("notice-verify", day) > 0:
        return
    if result.unverifiable == "no key":
        message = (
            "Alerts are paused: deals have to be checked against Google's live prices, but no SerpApi key is set. "
            "Add a free key as the SERPAPI_KEY secret in the GitHub repo (Settings > Secrets and variables > Actions) "
            "to resume."
        )
    else:
        message = (
            "Alerts are paused: the live price check (SerpApi) is failing. "
            "Open the latest run in the repo's Actions tab to see why."
        )
    try:
        discord.post_notice(webhook, message, session)
    except discord.NotifyError as e:
        log.error("could not send the notice: %s", e)
        return
    store.add_usage("notice-verify", day)


def _post_discord(webhook: str, s: ScoredDeal, session: requests.Session) -> None:
    discord.post(webhook, s, session=session)


if __name__ == "__main__":
    sys.exit(main())

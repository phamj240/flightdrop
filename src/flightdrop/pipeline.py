"""One scan: score candidates, drop repeats, rank, cap, optionally verify, then post."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .models import Deal, ScoredDeal
from .notify.discord import NotifyError
from .scoring import evaluate, rank_and_cap, should_alert
from .sources.common import SourceError
from .store import Store

log = logging.getLogger("flightdrop.pipeline")

SERPAPI = "serpapi"

PostFn = Callable[[ScoredDeal], None]
VerifyFn = Callable[[Deal], float | None]
AlternativeFn = Callable[[Deal], Deal | None]


@dataclass
class RunResult:
    candidates: int = 0
    qualified: int = 0
    new: int = 0
    slots: int = 0
    alerts: list[ScoredDeal] = field(default_factory=list)
    failed_posts: int = 0
    verified: int = 0
    dropped_after_verify: int = 0
    held: int = 0          # new deals waiting for the next scan inside the alert hours


def in_alert_hours(now: datetime, cfg: Config) -> bool:
    """True if alerts may be posted at ``now`` (always true when no window is configured)."""
    if cfg.alert_hours is None:
        return True
    return cfg.alert_hours.contains(now.astimezone(ZoneInfo(cfg.timezone)).time())


def start_of_today_utc(now: datetime, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    local_midnight = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


def _score_all(deals: list[Deal], cfg: Config, store: Store, now: datetime) -> list[ScoredDeal]:
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    scored = []
    for deal in deals:
        median, samples = (None, 0)
        if not deal.curated and deal.price_usd is not None:
            median, samples = store.usual_price(deal.route_key, cfg.relative.window_days, now)
        s = evaluate(deal, cfg, median, samples, today)
        if s:
            scored.append(s)
    return scored


def _verify_one(
    s: ScoredDeal, cfg: Config, store: Store, verify: VerifyFn, now: datetime, result: RunResult
) -> ScoredDeal | None:
    """Return the deal (possibly marked live-verified) or None if the live price is clearly worse."""
    deal = s.deal
    if deal.curated or deal.price_usd is None:
        return s
    day = now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    if (
        store.usage_on(SERPAPI, day) >= cfg.verify.daily_budget
        or store.usage_in_month(SERPAPI, day[:7]) >= cfg.verify.monthly_budget
    ):
        return s   # out of budget: post it labelled as a cached price
    store.add_usage(SERPAPI, day)
    try:
        live = verify(deal)
    except SourceError as e:
        log.warning("live check failed for %s: %s", deal.deal_key, e)
        return s
    if live is None:
        return s
    result.verified += 1
    if live > deal.price_usd * cfg.verify.max_live_over_cached:
        log.info("dropping %s: cached $%.0f but live $%.0f", deal.deal_key, deal.price_usd, live)
        result.dropped_after_verify += 1
        return None
    return replace(s, verification="live", live_price_usd=live)


def _with_alternative(s: ScoredDeal, alternative: AlternativeFn) -> ScoredDeal:
    """Attach the cheapest fare on a different airline; a failed lookup just omits it."""
    try:
        return replace(s, alternative=alternative(s.deal), alternative_checked=True)
    except SourceError as e:
        log.warning("next-best lookup failed for %s: %s", s.deal.deal_key, e)
        return s


def run_scan(
    deals: list[Deal],
    cfg: Config,
    store: Store,
    now: datetime,
    *,
    post: PostFn,
    verify: VerifyFn | None = None,
    alternative: AlternativeFn | None = None,
    alerts_allowed: bool = True,
) -> RunResult:
    """Process one batch of candidate deals. ``post`` raises NotifyError on failure.

    With ``alerts_allowed=False`` (outside the alert hours) everything runs except posting:
    prices are still recorded and deals scored, but nothing is verified, looked up, posted
    or logged as sent, so the held deals go out at the first scan inside the window.
    """
    result = RunResult(candidates=len(deals))

    scored = _score_all(deals, cfg, store, now)
    # Record sightings only after scoring so a fare is never compared with itself. Curated
    # feed deals are already bargains, so they stay out of the "usual price" history.
    for d in deals:
        if not d.curated:
            store.record_observation(d, now)
    result.qualified = len(scored)

    fresh = [
        s for s in scored
        if should_alert(store.last_alert_price(s.deal.deal_key), s.deal.price_usd, cfg.realert_min_drop)
    ]
    result.new = len(fresh)

    already = store.alerts_since(start_of_today_utc(now, cfg.timezone))
    result.slots = max(cfg.max_alerts_per_day - already, 0)

    if not alerts_allowed:
        result.held = len(fresh)
        fresh = []

    for s in rank_and_cap(fresh, len(fresh)):   # best first
        if len(result.alerts) >= result.slots:
            break
        chosen = _verify_one(s, cfg, store, verify, now, result) if verify else s
        if chosen is None:
            continue
        if alternative and chosen.deal.airline in cfg.airlines.alternatives_for:
            chosen = _with_alternative(chosen, alternative)
        try:
            post(chosen)
        except NotifyError as e:
            result.failed_posts += 1
            log.error("could not post %s: %s", chosen.deal.deal_key, e)
            continue
        store.record_alert(chosen.deal.deal_key, chosen.live_price_usd or chosen.deal.price_usd, now)
        result.alerts.append(chosen)

    store.prune(now)
    store.db.commit()
    return result

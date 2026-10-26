"""One scan: score candidates, drop repeats, rank, cap, confirm against live prices, then post."""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .airports import region_for
from .config import Config
from .models import Deal, LiveQuote, ScoredDeal
from .notify.discord import NotifyError
from .scoring import evaluate, rank_and_cap, should_alert
from .sources.common import SourceError
from .store import Store

log = logging.getLogger("flightdrop.pipeline")

SERPAPI = "serpapi"

# Google Flights cannot price trips much more than ~11 months out; a check would come back empty.
GOOGLE_HORIZON_DAYS = 320

PostFn = Callable[[ScoredDeal], None]
VerifyFn = Callable[[Deal], "LiveQuote | float | None"]
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
    held_unverified: int = 0           # deals held because their live price could not be checked
    unverifiable: str | None = None    # "no key" or "error" if checking was impossible (not just out of quota)


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
    needs_live = cfg.verify.enabled and (cfg.verify.require_live or cfg.typical.enabled)
    for deal in deals:
        if needs_live and deal.depart_date and (deal.depart_date - today).days > GOOGLE_HORIZON_DAYS:
            continue        # Google could not price it, so it could never be verified or posted
        median, samples = (None, 0)
        if not deal.curated and deal.price_usd is not None:
            median, samples = store.usual_price(deal.route_key, cfg.relative.window_days, now)
        s = evaluate(deal, cfg, median, samples, today, watch_previous=store.watch_price,
                     allow_candidates=cfg.typical.enabled)
        if s:
            scored.append(s)
    return _cheapest_per_watch(_rank_candidates(scored, cfg, store))


def _rank_candidates(scored: list[ScoredDeal], cfg: Config, store: Store) -> list[ScoredDeal]:
    """Decide which candidates are worth one of the few Google checks, and in what order.

    A candidate's ``cheapness`` is the live price we expect (cached price x the route's learned
    live/cached ratio) divided by a reference: the route's usual price if a past check taught us
    one, otherwise the median cached price of today's candidates in the same region. Only clearly
    cheap ones (at least ``below_usual_pct`` under the reference) are kept; fares with no
    reference at all are kept at the lowest priority so new destinations still get learned.
    """
    candidates = [s for s in scored if s.candidate]
    if not candidates:
        return scored
    prices_by_region: dict[str | None, list[float]] = defaultdict(list)
    for s in candidates:
        prices_by_region[region_for(s.deal.destination or "")].append(s.deal.price_usd)
    region_median = {r: statistics.median(p) for r, p in prices_by_region.items() if len(p) >= 3}
    keep_at_most = 1 - cfg.typical.below_usual_pct / 100

    ranked = [s for s in scored if not s.candidate]
    for s in candidates:
        d = s.deal
        baseline = store.route_baseline(f"{d.origin}-{d.destination}")
        usual, ratio = baseline if baseline else (None, 1.0)
        reference = usual or region_median.get(region_for(d.destination or ""))
        if reference:
            cheapness = d.price_usd * ratio / reference
            if cheapness > keep_at_most:
                continue
            s.cheapness, s.score = cheapness, max(0.0, 1 - cheapness)
        ranked.append(s)
    return ranked


def _cheapest_per_watch(scored: list[ScoredDeal]) -> list[ScoredDeal]:
    """A moving watch alerts once per scan: the route query returns several options (by stops)."""
    best: dict[str, ScoredDeal] = {}
    for s in scored:
        if s.watch_key and (s.watch_key not in best or s.deal.price_usd < best[s.watch_key].deal.price_usd):
            best[s.watch_key] = s
    return [s for s in scored if not s.watch_key or best[s.watch_key] is s]


def _as_quote(value: "LiveQuote | float | None") -> LiveQuote | None:
    """Accept a bare price (simple verifiers) or a full quote with Google's insights."""
    if value is None or isinstance(value, LiveQuote):
        return value
    return LiveQuote(price=float(value))


def _check_live(
    s: ScoredDeal,
    cfg: Config,
    store: Store,
    verify: VerifyFn | None,
    now: datetime,
    today: date,
    result: RunResult,
) -> tuple[ScoredDeal | None, str]:
    """Confirm a deal against Google's live price.

    Returns ``(deal, "live")`` when the live price is known and still qualifies (the deal then
    carries the live price and reasons), ``(None, "dropped")`` when it no longer qualifies, or
    ``(None, "unavailable:<why>")`` when it could not be checked (no key, budget used up, API
    error, Google had no results). Results are remembered for 24h so the free quota is not spent
    twice on the same fare.
    """
    deal = s.deal
    remembered = store.recent_live_check(deal.deal_key, deal.price_usd, now)
    if remembered is not None:
        outcome, quote = remembered
        if outcome == "dropped":
            return None, "dropped"
        if outcome == "nodata":
            return None, "unavailable:nodata"
    else:
        if verify is None:
            return None, "unavailable:no key"
        day = today.isoformat()
        if (
            store.usage_on(SERPAPI, day) >= cfg.verify.daily_budget
            or store.usage_in_month(SERPAPI, day[:7]) >= cfg.verify.monthly_budget
        ):
            return None, "unavailable:budget"
        store.add_usage(SERPAPI, day)
        try:
            quote = _as_quote(verify(deal))
        except SourceError as e:
            log.warning("live check failed for %s: %s", deal.deal_key, e)
            return None, "unavailable:error"
        if quote is None:
            store.save_live_check(deal.deal_key, deal.price_usd, None, "nodata", now)
            return None, "unavailable:nodata"
        # Learn from every real check, even a rejected one: how Google's price compares to the
        # cached one for this route, and what Google thinks the route usually costs.
        store.set_route_baseline(f"{deal.origin}-{deal.destination}", quote.usual, quote.price / deal.price_usd, now)

    live = quote.price
    result.verified += 1
    rescored = None
    # Under the typical-price rule the live price is judged against Google's own usual price, so the
    # "close to the cached price" sanity gate would only discard real deals behind a fake-cheap cache.
    if cfg.typical.enabled or live <= deal.price_usd * cfg.verify.max_live_over_cached:
        # The live price must still earn an alert on its own: same rules, filters and watch rules.
        samples = cfg.relative.min_samples if s.median_usd is not None else 0
        rescored = evaluate(replace(deal, price_usd=live), cfg, s.median_usd, samples, today,
                            watch_previous=store.watch_price, quote=quote)
    if rescored is None:
        log.info("dropping %s: cached $%.0f but live $%.0f no longer qualifies", deal.deal_key, deal.price_usd, live)
        store.save_live_check(deal.deal_key, deal.price_usd, quote, "dropped", now)
        result.dropped_after_verify += 1
        return None, "dropped"
    store.save_live_check(deal.deal_key, deal.price_usd, quote, "ok", now)
    return replace(rescored, deal=deal, verification="live", live_price_usd=live), "live"


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
    require_live: bool = False,
    preview_candidates: bool = False,
) -> RunResult:
    """Process one batch of candidate deals. ``post`` raises NotifyError on failure.

    With ``alerts_allowed=False`` (outside the alert hours) everything runs except posting:
    prices are still recorded and deals scored, but nothing is verified, looked up, posted
    or logged as sent, so the held deals go out at the first scan inside the window.

    With ``require_live=True`` a deal whose live price cannot be checked is held (not posted, not
    logged as sent) instead of being posted as an unverified cached price.
    """
    result = RunResult(candidates=len(deals))
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    unavailable: set[str] = set()

    store.prune(now)    # expire old history first, so an expired watch price can't block this scan
    scored = _score_all(deals, cfg, store, now)
    # Record sightings only after scoring so a fare is never compared with itself. Curated
    # feed deals are already bargains, so they stay out of the "usual price" history.
    for d in deals:
        if not d.curated:
            store.record_observation(d, now)
    result.qualified = len(scored)

    # A moving watch hit already beat the previous price, so the 10% re-alert rule doesn't apply to it.
    fresh = [
        s for s in scored
        if s.watch_key
        or should_alert(store.last_alert_price(s.deal.deal_key), s.deal.price_usd, cfg.realert_min_drop)
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
        chosen = s
        if not (s.deal.curated or s.deal.price_usd is None) and (verify is not None or require_live):
            checked, status = _check_live(s, cfg, store, verify, now, today, result)
            if checked is not None:
                chosen = checked
            elif status == "dropped":
                continue
            elif s.candidate and status in ("unavailable:budget", "unavailable:nodata"):
                continue                             # a spent quota or an unknown route is normal for candidates
            elif require_live:                       # could not be checked: hold it for a later scan
                result.held_unverified += 1
                unavailable.add(status)
                continue
        if chosen.candidate and not preview_candidates:
            continue                                 # a candidate is never posted without its live check
        if alternative and chosen.deal.airline in cfg.airlines.alternatives_for:
            chosen = _with_alternative(chosen, alternative)
        try:
            post(chosen)
        except NotifyError as e:
            result.failed_posts += 1
            log.error("could not post %s: %s", chosen.deal.deal_key, e)
            continue
        # Log the cached price: that is what the next scan compares against for repeats.
        store.record_alert(chosen.deal.deal_key, chosen.deal.price_usd, now)
        if chosen.watch_key:
            # The price just alerted (the live one when it was checked) is the bar for next time.
            store.set_watch_price(chosen.watch_key, chosen.live_price_usd or chosen.deal.price_usd, now)
        result.alerts.append(chosen)

    if "unavailable:no key" in unavailable:
        result.unverifiable = "no key"
    elif "unavailable:error" in unavailable:
        result.unverifiable = "error"
    store.db.commit()
    return result

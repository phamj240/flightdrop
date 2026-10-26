from __future__ import annotations

from datetime import date
from typing import Callable

from .airports import country_for, country_name, region_for
from .config import Config
from .filters import match_watch, passes_airline, passes_destination, passes_trip
from .models import Deal, LiveQuote, ScoredDeal

# Curated feed deals are already human-vetted bargains. They always qualify
# (subject to destination filters) and rank below strong measured discounts.
CURATED_SCORE = 0.35


def evaluate(
    deal: Deal,
    cfg: Config,
    median: float | None,
    samples: int,
    today: date,
    watch_previous: Callable[[str], float | None] | None = None,
    quote: LiveQuote | None = None,
    allow_candidates: bool = False,
) -> ScoredDeal | None:
    """Return a ScoredDeal if the deal qualifies, else None.

    ``quote`` is Google's live price and price insights for the trip (after a live check). With the
    typical-price rule on, a fare qualifies if that live price is well below the route's usual price
    or Google rates it low. With ``allow_candidates``, a fare that passed every filter but hit no
    rule yet is returned as a *candidate* that still needs that live check.

    Order: trip filter -> airline filter -> watchlist -> destination filter -> fixed/relative rules.
    A watchlist match skips the destination filter and the price rules, but not the trip or
    airline filters. ``watch_previous(route_key)`` gives the last alerted price for moving watches.
    """
    if deal.cabin not in cfg.cabins:
        return None

    if deal.curated:
        if not passes_destination(deal.destination, cfg.destinations):
            return None
        return ScoredDeal(deal, CURATED_SCORE, ["curated deal feed"])

    if deal.price_usd is None or deal.destination is None or deal.destination == deal.origin:
        return None
    if not passes_trip(deal, cfg.trip, today):
        return None
    if not passes_airline(deal, cfg.airlines):   # also applies to watchlist hits
        return None

    price = deal.price_usd
    usual = median if median is not None and samples >= cfg.relative.min_samples else None

    watch = match_watch(deal, cfg.watchlist, watch_previous)
    if watch:
        # Watchlist hits rank above every ordinary deal (score > 1).
        if watch.lower_than_previous:
            last = watch_previous(deal.route_key) if watch_previous else None
            if last is None:
                reason = f"watchlist: first price seen for this route (${price:,.0f}); alerts again whenever it gets lower"
                reference = watch.max_price_usd or price
            else:
                reason = f"watchlist: ${last - price:,.0f} lower than the previous price ${last:,.0f}"
                reference = last
            key = deal.route_key
        else:
            reason, reference, key = f"watchlist target ${watch.max_price_usd:,.0f}", watch.max_price_usd, None
        discount = max(1 - price / reference, 0.0)
        return ScoredDeal(deal, 1 + discount, [reason], usual, discount, watch_key=key)

    if not passes_destination(deal.destination, cfg.destinations):
        return None

    reasons: list[str] = []
    discount: float | None = None

    # A country-specific price bar, if configured, beats the region's.
    country = country_for(deal.destination)
    threshold = cfg.thresholds_by_country.get(country, {}).get(deal.cabin) if country else None
    label = country_name(country) if threshold is not None else None
    if threshold is None:
        region = region_for(deal.destination)
        threshold = cfg.thresholds_usd.get(region, {}).get(deal.cabin) if region else None
        label = region
    if threshold is not None and price <= threshold:
        reasons.append(f"under ${threshold:,.0f} {label} {deal.cabin} target")
        discount = 1 - price / threshold

    if usual is not None and price <= cfg.relative.max_ratio * usual:
        pct = round((1 - price / usual) * 100)
        reasons.append(f"{pct}% below usual ${usual:,.0f}")
        # A measured discount vs. the route's own history beats the fixed-target one for ranking.
        discount = 1 - price / usual

    if cfg.typical.enabled and quote is not None:
        google_usual = quote.usual
        if google_usual and price <= google_usual * (1 - cfg.typical.below_usual_pct / 100):
            pct = round((1 - price / google_usual) * 100)
            reasons.append(f"{pct}% below the usual ${google_usual:,.0f} (Google's recent average for this trip)")
            discount = max(discount or 0.0, 1 - price / google_usual)
        if cfg.typical.accept_google_low and quote.level == "low":
            span = f" (typical ${quote.typical_low:,.0f}-${quote.typical_high:,.0f})" if quote.typical_low and quote.typical_high else ""
            reasons.append(f"Google rates this price low for the trip{span}")
            discount = max(discount or 0.0, (1 - price / google_usual) if google_usual else 0.05)

    if not reasons:
        if allow_candidates and cfg.typical.enabled and quote is None:
            return ScoredDeal(deal, 0.0, [], usual, None, candidate=True)
        return None
    return ScoredDeal(deal, discount if discount is not None else 0.0, reasons, usual, discount)


def should_alert(last_price: float | None, price: float | None, min_drop: float) -> bool:
    """Alert on a new deal, or on a previously alerted deal whose price fell enough."""
    if last_price is None:
        return True
    if price is None or last_price < 0:  # unknown price on either side: never repeat
        return False
    return price <= last_price * (1 - min_drop)


def rank_and_cap(scored: list[ScoredDeal], slots: int) -> list[ScoredDeal]:
    """Best-first selection of at most ``slots`` deals, one per deal_key."""
    if slots <= 0:
        return []
    best: dict[str, ScoredDeal] = {}
    for s in scored:
        cur = best.get(s.deal.deal_key)
        if cur is None or s.score > cur.score:
            best[s.deal.deal_key] = s
    ranked = sorted(best.values(), key=lambda s: (-s.score, s.deal.price_usd or 0.0))
    return ranked[:slots]

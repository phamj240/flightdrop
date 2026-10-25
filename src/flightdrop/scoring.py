from __future__ import annotations

from datetime import date

from .airports import country_for, country_name, region_for
from .config import Config
from .filters import match_watch, passes_airline, passes_destination, passes_trip
from .models import Deal, ScoredDeal

# Curated feed deals are already human-vetted bargains. They always qualify
# (subject to destination filters) and rank below strong measured discounts.
CURATED_SCORE = 0.35


def evaluate(
    deal: Deal, cfg: Config, median: float | None, samples: int, today: date
) -> ScoredDeal | None:
    """Return a ScoredDeal if the deal qualifies, else None.

    Order: trip filter -> airline filter -> watchlist -> destination filter -> fixed/relative rules.
    A watchlist match skips the destination filter and the price rules, but not the trip or
    airline filters.
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

    watch = match_watch(deal, cfg.watchlist)
    if watch:
        # Watchlist hits rank above every ordinary deal (score > 1).
        discount = 1 - price / watch.max_price_usd
        return ScoredDeal(deal, 1 + discount, [f"watchlist target ${watch.max_price_usd:,.0f}"], usual, discount)

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

    if not reasons:
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

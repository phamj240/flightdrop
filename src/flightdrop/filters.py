from __future__ import annotations

from datetime import date
from typing import Callable

from .airports import lookup, region_for
from .config import AirlineFilter, DestinationFilter, TripWindow, WatchItem
from .models import Deal


def passes_trip(deal: Deal, trip: TripWindow, today: date) -> bool:
    """Trip length, departure window and departure month. Missing dates skip that check."""
    dep, ret = deal.depart_date, deal.return_date
    if dep and ret:
        days = (ret - dep).days
        if days < trip.min_days or (trip.max_days is not None and days > trip.max_days):
            return False
    if dep:
        ahead = (dep - today).days
        if ahead < trip.depart_min_days:
            return False
        if trip.depart_max_days is not None and ahead > trip.depart_max_days:
            return False
        if trip.months and dep.month not in trip.months:
            return False
    return True


def passes_airline(deal: Deal, airlines: AirlineFilter) -> bool:
    """Exclusions win. A non-empty include list requires a known, listed carrier."""
    code = deal.airline.upper() if deal.airline else None
    if code and code in airlines.exclude:
        return False
    if airlines.include:
        return code in airlines.include
    return True


def passes_destination(destination: str | None, dest: DestinationFilter) -> bool:
    """Exclusions always win; include_regions and include_countries are unioned.

    An unknown destination (feed deal whose airport couldn't be parsed) can't be
    filtered, so it passes. A known-format code missing from our airport list
    fails only when an include list is set.
    """
    if destination is None:
        return True
    if destination in dest.exclude_airports:
        return False
    airport = lookup(destination)
    country = airport.country if airport else None
    if country and country in dest.exclude_countries:
        return False
    if not (dest.include_regions or dest.include_countries):
        return True
    if airport is None:
        return False
    return region_for(destination) in dest.include_regions or country in dest.include_countries


MIN_DROP_USD = 1.0      # "just lower than the previous" means at least a dollar lower


def match_watch(
    deal: Deal,
    watchlist: tuple[WatchItem, ...],
    previous: Callable[[str], float | None] | None = None,
) -> WatchItem | None:
    """First watchlist entry this deal satisfies.

    A fixed entry needs ``price <= max_price_usd``. A moving entry (``lower_than_previous``)
    needs no earlier alert for the route (the first price seen), or a price at least
    ``MIN_DROP_USD`` below the last alerted one; an optional ``max_price_usd`` is a ceiling.
    ``previous(route_key)`` returns the last alerted price for a route, or None.
    """
    if deal.price_usd is None:
        return None
    for w in watchlist:
        if not (deal.destination == w.destination and deal.origin in w.origins and deal.cabin == w.cabin):
            continue
        if w.max_price_usd is not None and deal.price_usd > w.max_price_usd:
            continue
        if w.lower_than_previous:
            last = previous(deal.route_key) if previous else None
            if last is not None and deal.price_usd > last - MIN_DROP_USD:
                continue
        return w
    return None

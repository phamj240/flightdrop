from __future__ import annotations

from datetime import date

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


def match_watch(deal: Deal, watchlist: tuple[WatchItem, ...]) -> WatchItem | None:
    """First watchlist entry whose route, cabin and target price this deal meets."""
    if deal.price_usd is None:
        return None
    for w in watchlist:
        if (
            deal.destination == w.destination
            and deal.origin in w.origins
            and deal.cabin == w.cabin
            and deal.price_usd <= w.max_price_usd
        ):
            return w
    return None

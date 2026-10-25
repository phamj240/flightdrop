from __future__ import annotations

from datetime import date

from helpers import TODAY, make_deal

from flightdrop.config import AirlineFilter, DestinationFilter, TripWindow, WatchItem
from flightdrop.filters import match_watch, passes_airline, passes_destination, passes_trip

TRIP = TripWindow(min_days=3, max_days=14, depart_min_days=7, depart_max_days=240)


# --- trip window -----------------------------------------------------------

def test_trip_within_bounds_passes():
    assert passes_trip(make_deal(), TRIP, TODAY)  # 7-day trip, departs in 32 days


def test_trip_too_short_or_too_long_fails():
    short = make_deal(return_date=date(2026, 12, 4))   # 1 day
    long_ = make_deal(return_date=date(2027, 1, 20))   # 48 days
    assert not passes_trip(short, TRIP, TODAY)
    assert not passes_trip(long_, TRIP, TODAY)


def test_trip_length_bounds_are_inclusive():
    assert passes_trip(make_deal(return_date=date(2026, 12, 6)), TRIP, TODAY)   # exactly 3
    assert passes_trip(make_deal(return_date=date(2026, 12, 17)), TRIP, TODAY)  # exactly 14


def test_departure_window():
    soon = make_deal(depart_date=date(2026, 11, 5), return_date=date(2026, 11, 12))   # 4 days out
    far = make_deal(depart_date=date(2027, 8, 1), return_date=date(2027, 8, 8))        # 273 days out
    assert not passes_trip(soon, TRIP, TODAY)
    assert not passes_trip(far, TRIP, TODAY)


def test_open_ended_limits():
    open_trip = TripWindow(min_days=0, max_days=None, depart_min_days=0, depart_max_days=None)
    far = make_deal(depart_date=date(2027, 8, 1), return_date=date(2027, 10, 1))
    assert passes_trip(far, open_trip, TODAY)


def test_departure_months():
    trip = TripWindow(months=(3, 4))
    assert not passes_trip(make_deal(), trip, TODAY)  # December
    march = make_deal(depart_date=date(2027, 3, 10), return_date=date(2027, 3, 17))
    assert passes_trip(march, trip, TODAY)


def test_missing_dates_skip_those_checks():
    one_way = make_deal(return_date=None)
    assert passes_trip(one_way, TRIP, TODAY)            # no length check without a return date
    undated = make_deal(depart_date=None, return_date=None)
    assert passes_trip(undated, TripWindow(months=(3,), depart_min_days=30), TODAY)


# --- destination filter ----------------------------------------------------

def test_no_restrictions_passes_everything():
    assert passes_destination("LIS", DestinationFilter())
    assert passes_destination("ZZZ", DestinationFilter())


def test_exclude_airport_and_country():
    assert not passes_destination("JFK", DestinationFilter(exclude_airports=("JFK",)))
    assert passes_destination("LGA", DestinationFilter(exclude_airports=("JFK",)))
    assert not passes_destination("NRT", DestinationFilter(exclude_countries=("JP",)))
    assert passes_destination("LIS", DestinationFilter(exclude_countries=("JP",)))


def test_include_regions():
    d = DestinationFilter(include_regions=("europe",))
    assert passes_destination("LIS", d)
    assert not passes_destination("NRT", d)
    assert not passes_destination("MSP", d)


def test_include_regions_and_countries_are_unioned():
    d = DestinationFilter(include_regions=("europe",), include_countries=("JP",))
    assert passes_destination("LIS", d)   # via region
    assert passes_destination("NRT", d)   # via country
    assert not passes_destination("BKK", d)


def test_exclusion_beats_inclusion():
    d = DestinationFilter(include_regions=("europe",), exclude_countries=("PT",))
    assert not passes_destination("LIS", d)
    assert passes_destination("LHR", d)


def test_unknown_destination_handling():
    assert passes_destination(None, DestinationFilter(include_regions=("europe",)))   # unparsed feed deal
    assert not passes_destination("ZZZ", DestinationFilter(include_regions=("europe",)))
    assert passes_destination("ZZZ", DestinationFilter(exclude_countries=("JP",)))


# --- airline filter --------------------------------------------------------

def test_no_airline_restrictions_passes_everything():
    assert passes_airline(make_deal(airline="F9"), AirlineFilter())
    assert passes_airline(make_deal(airline=None), AirlineFilter())


def test_exclude_airlines():
    f = AirlineFilter(exclude=("F9", "NK"))
    assert not passes_airline(make_deal(airline="F9"), f)
    assert not passes_airline(make_deal(airline="nk"), f)       # case-insensitive
    assert passes_airline(make_deal(airline="DL"), f)
    assert passes_airline(make_deal(airline=None), f)           # unknown carrier can't be excluded


def test_include_airlines():
    f = AirlineFilter(include=("DL", "UA"))
    assert passes_airline(make_deal(airline="DL"), f)
    assert not passes_airline(make_deal(airline="AA"), f)
    assert not passes_airline(make_deal(airline=None), f)       # unknown carrier can't be confirmed


def test_exclusion_beats_inclusion_for_airlines():
    f = AirlineFilter(include=("DL", "F9"), exclude=("F9",))
    assert passes_airline(make_deal(airline="DL"), f)
    assert not passes_airline(make_deal(airline="F9"), f)


# --- watchlist -------------------------------------------------------------

W = (WatchItem(origins=("MSP", "DFW"), destination="NRT", cabin="economy", max_price_usd=800.0),)


def test_watch_match():
    deal = make_deal(destination="NRT", price_usd=790.0)
    assert match_watch(deal, W) is W[0]


def test_watch_requires_price_at_or_under_target():
    assert match_watch(make_deal(destination="NRT", price_usd=800.0), W) is W[0]
    assert match_watch(make_deal(destination="NRT", price_usd=801.0), W) is None


def test_watch_requires_matching_route_origin_and_cabin():
    assert match_watch(make_deal(destination="ICN", price_usd=500.0), W) is None
    assert match_watch(make_deal(destination="NRT", price_usd=500.0, origin="ORD"), W) is None
    assert match_watch(make_deal(destination="NRT", price_usd=500.0, cabin="business"), W) is None


def test_watch_ignores_deals_without_price():
    assert match_watch(make_deal(destination="NRT", price_usd=None), W) is None


# --- moving watch target ---------------------------------------------------------------------

def test_match_watch_moving_target_uses_the_previous_price():
    moving = (WatchItem(origins=("MEM",), destination="MSP", cabin="economy", lower_than_previous=True),)
    deal = lambda p: make_deal(origin="MEM", destination="MSP", price_usd=p)           # noqa: E731
    assert match_watch(deal(500.0), moving) is moving[0]                                # no history: first price
    assert match_watch(deal(500.0), moving, lambda k: None) is moving[0]
    assert match_watch(deal(299.0), moving, lambda k: 300.0) is moving[0]
    assert match_watch(deal(300.0), moving, lambda k: 300.0) is None
    assert match_watch(deal(299.5), moving, lambda k: 300.0) is None                    # must be a dollar lower
    asked = []
    match_watch(deal(299.0), moving, lambda k: asked.append(k) or 300.0)
    assert asked == ["MEM-MSP-economy"]                                                  # state is per origin-destination-cabin


def test_match_watch_moving_target_with_a_ceiling():
    moving = (WatchItem(origins=("MEM",), destination="MSP", cabin="economy", max_price_usd=400.0, lower_than_previous=True),)
    deal = lambda p: make_deal(origin="MEM", destination="MSP", price_usd=p)           # noqa: E731
    assert match_watch(deal(401.0), moving) is None
    assert match_watch(deal(400.0), moving) is moving[0]
    assert match_watch(deal(390.0), moving, lambda k: 390.0) is None

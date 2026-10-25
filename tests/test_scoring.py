from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from helpers import TODAY, make_deal

from flightdrop.config import AirlineFilter, DestinationFilter, WatchItem
from flightdrop.scoring import CURATED_SCORE, evaluate, rank_and_cap, should_alert


def ev(deal, cfg, median=None, samples=0):
    return evaluate(deal, cfg, median, samples, TODAY)


# --- fixed-price rule ------------------------------------------------------

def test_fixed_rule_hit(cfg):
    s = ev(make_deal(price_usd=400.0), cfg)   # europe economy target is 550
    assert s is not None
    assert s.reasons == ["under $550 europe economy target"]
    assert abs(s.discount - (1 - 400 / 550)) < 1e-9


def test_fixed_rule_miss(cfg):
    assert ev(make_deal(price_usd=600.0), cfg) is None


def test_fixed_rule_boundary_is_inclusive(cfg):
    assert ev(make_deal(price_usd=550.0), cfg) is not None
    assert ev(make_deal(price_usd=551.0), cfg) is None


def test_regional_thresholds(cfg):
    t = {r: v["economy"] for r, v in cfg.thresholds_usd.items()}
    assert t == {"domestic": 150, "nearby_intl": 300, "europe": 550, "asia_oceania": 750, "other_intl": 650}


# --- per-country price bars ----------------------------------------------------

def test_a_country_price_bar_overrides_the_regional_one(cfg):
    portugal = replace(cfg, thresholds_by_country={"PT": {"economy": 500.0}})
    assert ev(make_deal(price_usd=520.0), cfg) is not None            # Europe's $550 bar
    assert ev(make_deal(price_usd=520.0), portugal) is None           # Portugal's own $500 bar is stricter
    s = ev(make_deal(price_usd=480.0), portugal)
    assert s.reasons == ["under $500 Portugal economy target"]
    assert abs(s.discount - (1 - 480 / 500)) < 1e-9


def test_a_country_bar_can_be_looser_than_the_region(cfg):
    switzerland = replace(cfg, thresholds_by_country={"CH": {"economy": 750.0}})
    assert ev(make_deal(destination="ZRH", price_usd=740.0), cfg) is None
    assert ev(make_deal(destination="ZRH", price_usd=740.0), switzerland).reasons == ["under $750 Switzerland economy target"]


def test_a_missing_cabin_falls_back_to_the_region(cfg):
    only_economy = replace(cfg, thresholds_by_country={"PT": {"economy": 100.0}})
    assert ev(make_deal(cabin="business", price_usd=1500.0), only_economy) is not None     # europe business bar 1600


def test_country_bars_work_for_metro_codes(cfg):
    japan = replace(cfg, thresholds_by_country={"JP": {"economy": 800.0}})
    assert ev(make_deal(destination="TYO", price_usd=790.0), japan).reasons == ["under $800 Japan economy target"]


# --- the user's real destinations (config.yaml) ---------------------------------

@pytest.mark.parametrize("dest,price,expected_reason", [
    ("ROM", 422.0, "under $650 Italy economy target"),
    ("MIL", 648.0, "under $650 Italy economy target"),
    ("ZRH", 736.0, "under $750 Switzerland economy target"),
    ("GVA", 700.0, "under $750 Switzerland economy target"),
    ("LIS", 540.0, "under $550 europe economy target"),         # rest of Europe uses the regional bar
    ("GUA", 480.0, "under $500 Guatemala economy target"),
    ("DPS", 980.0, "under $1,000 Indonesia economy target"),
    ("TYO", 649.0, "under $750 Japan economy target"),
    ("SGN", 892.0, "under $900 Vietnam economy target"),
    ("FRU", 1050.0, "under $1,100 Kyrgyzstan economy target"),
    ("ALA", 1100.0, "under $1,100 Kazakhstan economy target"),
    ("NQZ", 1090.0, "under $1,100 Kazakhstan economy target"),
])
def test_users_destinations_alert_at_a_good_price(shipped_cfg, dest, price, expected_reason):
    s = ev(make_deal(destination=dest, price_usd=price), shipped_cfg)
    assert s is not None and s.reasons == [expected_reason]


@pytest.mark.parametrize("dest,price", [
    ("ZRH", 760.0), ("FCO", 660.0), ("LIS", 560.0), ("GUA", 520.0), ("DPS", 1010.0),
    ("TYO", 760.0), ("SGN", 910.0), ("FRU", 1110.0), ("ALA", 1110.0),
])
def test_users_destinations_do_not_alert_above_their_bar(shipped_cfg, dest, price):
    assert ev(make_deal(destination=dest, price_usd=price), shipped_cfg) is None


@pytest.mark.parametrize("dest", ["LAS", "DEN", "MIA", "CUN", "GDL", "BKK", "SEL", "DXB", "CAI", "SJO"])
def test_places_outside_the_users_list_never_alert_even_when_dirt_cheap(shipped_cfg, dest):
    assert ev(make_deal(destination=dest, price_usd=50.0), shipped_cfg) is None


def test_the_users_list_still_excludes_conflict_zones_inside_europe(shipped_cfg):
    for code in ("SVO", "MOW", "MSQ"):                       # Russia, Belarus are in Europe but excluded
        assert ev(make_deal(destination=code, price_usd=50.0), shipped_cfg) is None, code
    assert ev(make_deal(destination="KBL", price_usd=50.0), shipped_cfg) is None


def test_the_frontier_flood_is_gone_under_the_users_destinations(shipped_cfg):
    frontier = make_deal(origin="DFW", destination="LAS", price_usd=33.0, airline="F9")
    assert ev(frontier, shipped_cfg) is None


# --- shipped config: skipped places, airline filter ---------------------------

def test_conflict_zones_are_skipped_by_the_shipped_config(cfg):
    for code in ("EBL", "BGW", "KBL", "DAM", "MOW"):         # Iraq, Iraq, Afghanistan, Syria, Russia
        assert ev(make_deal(destination=code, price_usd=100.0), cfg) is None, code
    assert ev(make_deal(destination="DXB", price_usd=521.0), cfg) is not None


def test_excluded_airline_is_never_alerted(cfg):
    no_frontier = replace(cfg, airlines=AirlineFilter(exclude=("F9",)))
    frontier = make_deal(destination="LAS", price_usd=33.0, airline="F9")
    delta = make_deal(destination="LAS", price_usd=120.0, airline="DL")
    assert ev(frontier, cfg) is not None                      # allowed by default
    assert ev(frontier, no_frontier) is None
    assert ev(delta, no_frontier) is not None


def test_include_list_keeps_only_those_airlines(cfg):
    only_big3 = replace(cfg, airlines=AirlineFilter(include=("DL", "UA", "AA")))
    assert ev(make_deal(airline="DL", price_usd=400.0), only_big3) is not None
    assert ev(make_deal(airline="TP", price_usd=400.0), only_big3) is None
    assert ev(make_deal(airline=None, price_usd=400.0), only_big3) is None


def test_airline_filter_also_applies_to_watchlist_hits(cfg):
    item = WatchItem(origins=("MSP",), destination="NRT", cabin="economy", max_price_usd=800.0)
    wc = replace(cfg, watchlist=(item,), airlines=AirlineFilter(exclude=("F9",)))
    assert ev(make_deal(destination="NRT", price_usd=700.0, airline="DL"), wc) is not None
    assert ev(make_deal(destination="NRT", price_usd=700.0, airline="F9"), wc) is None


def test_airline_filter_does_not_apply_to_feed_deals(cfg):
    no_everything = replace(cfg, airlines=AirlineFilter(include=("ZZ",)))
    feed = make_deal(origin="DFW", destination=None, price_usd=None, depart_date=None, return_date=None,
                     curated=True, source_url="https://x.test/1", title="Delta: Dallas – Lisbon $400")
    assert ev(feed, no_everything) is not None


def test_fixed_rule_uses_destination_region_and_cabin(cfg):
    assert ev(make_deal(destination="LAX", price_usd=140.0), cfg) is not None     # domestic <= 150
    assert ev(make_deal(destination="LAX", price_usd=160.0), cfg) is None
    biz = make_deal(cabin="business", price_usd=1500.0)                             # europe business <= 1600
    assert ev(biz, cfg) is not None


# --- relative rule ---------------------------------------------------------

def test_relative_rule_hit_without_fixed_hit(cfg):
    deal = make_deal(cabin="business", price_usd=1700.0)    # above the $1,600 europe business target
    s = ev(deal, cfg, median=3000.0, samples=6)             # 1700 <= 0.7 * 3000
    assert s is not None
    assert s.reasons == ["43% below usual $3,000"]
    assert s.median_usd == 3000.0
    assert abs(s.discount - (1 - 1700 / 3000)) < 1e-9


def test_relative_rule_needs_enough_samples(cfg):
    deal = make_deal(cabin="business", price_usd=1700.0)
    assert ev(deal, cfg, median=3000.0, samples=4) is None   # min_samples is 5
    assert ev(deal, cfg, median=3000.0, samples=5) is not None


def test_relative_rule_miss_when_not_cheap_enough(cfg):
    deal = make_deal(cabin="business", price_usd=2200.0)    # 2200 > 0.7 * 3000
    assert ev(deal, cfg, median=3000.0, samples=10) is None


def test_both_rules_listed_and_relative_discount_ranks(cfg):
    s = ev(make_deal(price_usd=300.0), cfg, median=600.0, samples=8)
    assert len(s.reasons) == 2
    assert abs(s.discount - 0.5) < 1e-9     # measured discount vs. usual wins over the fixed-target one


def test_median_shown_only_when_trusted(cfg):
    s = ev(make_deal(price_usd=400.0), cfg, median=600.0, samples=2)   # fixed hit, too few samples
    assert s is not None and s.median_usd is None


# --- general gates ---------------------------------------------------------

def test_disabled_cabin_is_skipped(cfg):
    only_econ = replace(cfg, cabins=("economy",))
    assert ev(make_deal(cabin="business", price_usd=100.0), only_econ) is None


def test_missing_price_or_destination_or_self_route_skipped(cfg):
    assert ev(make_deal(price_usd=None), cfg) is None
    assert ev(make_deal(destination=None), cfg) is None
    assert ev(make_deal(destination="MSP"), cfg) is None


def test_trip_window_applies_to_ordinary_deals(cfg):
    too_long = make_deal(return_date=date(2027, 2, 1))      # 60-day trip, config max is 30
    assert ev(too_long, cfg) is None


def test_destination_filter_applies_to_ordinary_deals(cfg):
    asia_only = replace(cfg, destinations=DestinationFilter(include_regions=("asia_oceania",)))
    assert ev(make_deal(price_usd=100.0), asia_only) is None                       # Lisbon
    assert ev(make_deal(destination="NRT", price_usd=600.0), asia_only) is not None


# --- watchlist -------------------------------------------------------------

def watch_cfg(cfg, **kw):
    item = WatchItem(origins=("MSP",), destination="NRT", cabin="economy", max_price_usd=800.0)
    return replace(cfg, watchlist=(item,), **kw)


def test_watchlist_hit_outranks_ordinary_deals(cfg):
    wc = watch_cfg(cfg)
    w = ev(make_deal(destination="NRT", price_usd=700.0), wc)
    ordinary = ev(make_deal(price_usd=100.0), wc)    # a huge fixed-rule discount
    assert w.reasons == ["watchlist target $800"]
    assert w.score > 1 > ordinary.score


def test_watchlist_bypasses_price_rules_and_destination_filter(cfg):
    # NRT at $790 is far above the $650 asia target, and Asia is excluded entirely
    wc = watch_cfg(cfg, destinations=DestinationFilter(exclude_countries=("JP",)))
    assert ev(make_deal(destination="NRT", price_usd=790.0), wc) is not None
    assert ev(make_deal(destination="NRT", price_usd=790.0), cfg) is None


def test_watchlist_does_not_bypass_trip_window(cfg):
    wc = watch_cfg(cfg)
    too_long = make_deal(destination="NRT", price_usd=700.0, return_date=date(2027, 2, 1))
    assert ev(too_long, wc) is None


def test_watchlist_miss_above_target_falls_through_to_normal_rules(cfg):
    wc = watch_cfg(cfg)
    assert ev(make_deal(destination="NRT", price_usd=900.0), wc) is None


# --- curated feed deals ----------------------------------------------------

def curated(**kw):
    base = dict(
        destination=None, price_usd=None, depart_date=None, return_date=None,
        source="reddit_flightdeals", curated=True,
        source_url="https://example.com/post/1", title="[MSP] to Lisbon $412",
    )
    base.update(kw)
    return make_deal(**base)


def test_curated_deal_always_qualifies(cfg):
    s = ev(curated(), cfg)
    assert s is not None and s.score == CURATED_SCORE and s.reasons == ["curated deal feed"]


def test_curated_respects_destination_filter_when_known(cfg):
    excl = replace(cfg, destinations=DestinationFilter(exclude_countries=("PT",)))
    assert ev(curated(destination="LIS", price_usd=412.0), excl) is None
    assert ev(curated(destination=None), excl) is not None    # unparsed destination can't be filtered


def test_curated_respects_enabled_cabins(cfg):
    only_econ = replace(cfg, cabins=("economy",))
    assert ev(curated(cabin="business"), only_econ) is None


# --- dedupe ----------------------------------------------------------------

def test_should_alert_new_deal():
    assert should_alert(None, 400.0, 0.10)


def test_should_alert_requires_meaningful_drop():
    assert not should_alert(400.0, 400.0, 0.10)
    assert not should_alert(400.0, 370.0, 0.10)     # 7.5% drop
    assert should_alert(400.0, 360.0, 0.10)         # exactly 10%
    assert should_alert(400.0, 300.0, 0.10)


def test_should_alert_unknown_prices_never_repeat():
    assert not should_alert(-1.0, 300.0, 0.10)     # previously alerted with unknown price
    assert not should_alert(400.0, None, 0.10)


# --- rank and cap ----------------------------------------------------------

def scored(cfg, price, dest="LIS", **kw):
    return ev(make_deal(destination=dest, price_usd=price, **kw), cfg)


def test_rank_orders_by_score_then_price(cfg):
    a, b, c = scored(cfg, 440), scored(cfg, 300, dest="LHR"), scored(cfg, 400, dest="CDG")
    ranked = rank_and_cap([a, b, c], 5)
    assert [s.deal.price_usd for s in ranked] == [300, 400, 440]


def test_cap_limits_results(cfg):
    items = [scored(cfg, 300 + i, dest=d) for i, d in enumerate(["LIS", "LHR", "CDG", "FCO", "MAD"])]
    assert len(rank_and_cap(items, 3)) == 3
    assert rank_and_cap(items, 0) == []
    assert rank_and_cap(items, -2) == []


def test_rank_dedupes_same_deal_keeping_best_score(cfg):
    cheap, pricey = scored(cfg, 300), scored(cfg, 440)       # same route + dates -> same deal_key
    ranked = rank_and_cap([pricey, cheap], 5)
    assert len(ranked) == 1 and ranked[0].deal.price_usd == 300


# --- the user's watchlist entry: Memphis -> Minneapolis ---------------------------------

def mem_msp(price, **kw):
    return make_deal(origin="MEM", destination="MSP", price_usd=price, **kw)


def test_the_users_memphis_to_minneapolis_watch_is_configured(shipped_cfg):
    (w,) = shipped_cfg.watchlist
    assert (w.origins, w.destination, w.cabin) == (("MEM",), "MSP", "economy")
    assert w.lower_than_previous is True and w.max_price_usd is None       # moving target, no ceiling


def test_memphis_to_minneapolis_alerts_on_a_first_fare_despite_the_destination_filter(shipped_cfg):
    # Minneapolis is US domestic, which the user's include list would otherwise reject.
    assert ev(make_deal(origin="DFW", destination="MSP", price_usd=100.0), shipped_cfg) is None
    s = ev(mem_msp(240.0), shipped_cfg)
    assert s is not None and s.watch_key == "MEM-MSP-economy"
    assert s.score >= 1.0                       # no drop yet, but still ranks above every ordinary deal (< 1)
    assert s.reasons == ["watchlist: first price seen for this route ($240); alerts again whenever it gets lower"]


# --- moving watch target: "just lower than the previous" -------------------------------------

def moving(cfg, ceiling=None):
    return replace(cfg, watchlist=(WatchItem(("MEM",), "MSP", "economy", ceiling, True),))


def with_previous(cfg, price, previous):
    return evaluate(mem_msp(price), cfg, None, 0, TODAY, watch_previous=lambda key: previous)


def test_a_moving_watch_alerts_on_a_fare_at_least_a_dollar_below_the_previous_one(cfg):
    wc = moving(cfg)
    s = with_previous(wc, 299.0, 300.0)
    assert s is not None and s.reasons == ["watchlist: $1 lower than the previous price $300"]
    assert s.watch_key == "MEM-MSP-economy" and s.score > 1
    assert with_previous(wc, 250.0, 300.0).reasons == ["watchlist: $50 lower than the previous price $300"]


def test_a_moving_watch_ignores_equal_higher_and_sub_dollar_changes(cfg):
    wc = moving(cfg)
    for price in (300.0, 300.5, 299.5, 301.0, 450.0):
        assert with_previous(wc, price, 300.0) is None, price


def test_a_moving_watch_with_no_previous_treats_the_first_fare_as_the_baseline(cfg):
    wc = moving(cfg)
    assert evaluate(mem_msp(900.0), wc, None, 0, TODAY) is not None                         # no history callable
    assert with_previous(wc, 900.0, None).reasons[0].startswith("watchlist: first price seen")


def test_a_ceiling_stops_a_moving_watch_from_alerting_on_a_high_first_fare(cfg):
    capped = moving(cfg, ceiling=400.0)
    assert with_previous(capped, 450.0, None) is None
    assert with_previous(capped, 390.0, None) is not None
    assert with_previous(capped, 390.0, 380.0) is None                  # under the ceiling but not lower than the previous
    assert with_previous(capped, 370.0, 380.0) is not None


def test_a_fixed_watch_is_unaffected_and_does_not_track_state(cfg):
    fixed = replace(cfg, watchlist=(WatchItem(("MEM",), "MSP", "economy", 250.0),))
    s = evaluate(mem_msp(240.0), fixed, None, 0, TODAY, watch_previous=lambda key: 100.0)
    assert s.reasons == ["watchlist target $250"] and s.watch_key is None
    assert evaluate(mem_msp(260.0), fixed, None, 0, TODAY) is None


def test_the_watch_is_one_direction_economy_and_respects_the_trip_window(shipped_cfg):
    assert ev(make_deal(origin="MSP", destination="MEM", price_usd=100.0), shipped_cfg) is None       # reverse not watched
    assert ev(mem_msp(100.0, cabin="business"), shipped_cfg) is None
    too_long = mem_msp(100.0, return_date=date(2027, 3, 1))
    assert ev(too_long, shipped_cfg) is None                                                           # 88-day trip

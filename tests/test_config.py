from __future__ import annotations

import copy

import pytest
import yaml
from helpers import BASELINE_CONFIG

from flightdrop.config import ConfigError, parse_config


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(BASELINE_CONFIG.read_text())


def test_baseline_config_loads(cfg):
    assert cfg.home_airports == ("MSP", "DFW")
    assert cfg.cabins == ("economy", "business")
    assert cfg.max_alerts_per_day == 5
    assert cfg.destinations.include_regions == () and cfg.destinations.include_countries == ()
    assert cfg.thresholds_by_country == {}
    assert cfg.airlines.include == () and cfg.airlines.exclude == ()
    assert cfg.watchlist == ()
    assert cfg.trip.min_days == 2 and cfg.trip.max_days == 30


def test_the_real_config_yaml_loads_and_has_the_users_destinations(shipped_cfg):
    assert shipped_cfg.home_airports == ("MSP", "DFW")
    assert shipped_cfg.destinations.include_regions == ("europe",)
    assert set(shipped_cfg.destinations.include_countries) == {"GT", "ID", "JP", "VN", "KG", "KZ"}
    assert set(shipped_cfg.destinations.exclude_countries) == {"IQ", "AF", "SY", "YE", "LY", "SO", "SD", "SS", "RU", "BY"}
    assert shipped_cfg.thresholds_by_country == {
        "GT": {"economy": 500.0}, "CH": {"economy": 750.0}, "IT": {"economy": 650.0}, "ID": {"economy": 1000.0},
        "JP": {"economy": 750.0}, "VN": {"economy": 900.0}, "KG": {"economy": 1100.0}, "KZ": {"economy": 1100.0},
    }
    assert shipped_cfg.airlines.exclude == () and shipped_cfg.airlines.alternatives_for == ("F9", "NK")
    assert shipped_cfg.max_alerts_per_day == 5


def test_every_priced_country_is_actually_a_destination_the_user_wants(shipped_cfg):
    """Guards against pricing a country the include filter would never let through."""
    from flightdrop.airports import region_for
    from flightdrop.filters import passes_destination
    wanted_codes = {"GT": "GUA", "CH": "ZRH", "IT": "FCO", "ID": "DPS", "JP": "NRT", "VN": "SGN", "KG": "FRU", "KZ": "ALA"}
    assert set(wanted_codes) == set(shipped_cfg.thresholds_by_country)
    for country, airport in wanted_codes.items():
        assert passes_destination(airport, shipped_cfg.destinations), (country, airport, region_for(airport))


def test_country_price_bars_are_validated(raw):
    ok = mutated(raw, lambda r: r.update(thresholds_by_country={"gt": {"economy": 500}, "CH": {"economy": "750", "business": 2000}}))
    parsed = parse_config(ok).thresholds_by_country
    assert parsed == {"GT": {"economy": 500.0}, "CH": {"economy": 750.0, "business": 2000.0}}
    for bad, message in [
        ({"GTM": {"economy": 500}}, "2-letter"),
        ({"GT": {"first": 500}}, "unknown cabin"),
        ({"GT": {"economy": "cheap"}}, "must be a number"),
        ({"GT": {"economy": -5}}, "positive"),
        ({"GT": {}}, "at least one cabin"),
        ({"GT": 500}, "at least one cabin"),
        ([("GT", 500)], "mapping"),
    ]:
        with pytest.raises(ConfigError, match=message):
            parse_config(mutated(raw, lambda r, b=bad: r.update(thresholds_by_country=b)))


def mutated(raw, fn):
    r = copy.deepcopy(raw)
    fn(r)
    return r


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda r: r.update(home_airports=["MSPX"]), "home_airports"),
        (lambda r: r.update(home_airports=[]), "at least one"),
        (lambda r: r.update(cabins=["first"]), "cabins"),
        (lambda r: r["thresholds_usd"]["europe"].pop("business"), "thresholds_usd.europe.business"),
        (lambda r: r.update(max_alerts_per_day=0), "max_alerts_per_day"),
        (lambda r: r["relative"].update(max_ratio=1.5), "max_ratio"),
        (lambda r: r["destinations"].update(include_regions=["mars"]), "unknown region"),
        (lambda r: r["destinations"].update(include_countries=["USA"]), "2-letter"),
        (lambda r: r["destinations"].update(exclude_airports=["JF"]), "3-letter"),
        (lambda r: r.update(airlines={"exclude": ["Frontier"]}), "airlines.exclude"),
        (lambda r: r.update(airlines={"include": ["D"]}), "airlines.include"),
        (lambda r: r.update(airlines={"exclude": ["F-"]}), "airlines.exclude"),
        (lambda r: r["trip"].update(min_days=10, max_days=5), "min_days"),
        (lambda r: r["trip"].update(depart_months=[0]), "1-12"),
        (lambda r: r["trip"].update(depart_in_days={"min": 50, "max": 10}), "depart_in_days"),
        (lambda r: r.update(watchlist=[{"to": "NRT"}]), r"watchlist\[0\]"),
        (lambda r: r.update(watchlist=[{"to": "NRT", "max_price_usd": -5}]), "positive"),
        (lambda r: r.update(watchlist=[{"to": "NRT", "max_price_usd": "x"}]), "number"),
        (lambda r: r.update(cabins=["economy"], watchlist=[{"to": "NRT", "cabin": "business", "max_price_usd": 900}]), "not enabled"),
    ],
)
def test_invalid_config_is_rejected(raw, mutate, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(mutated(raw, mutate))


def test_watchlist_defaults_origin_to_home_airports(raw):
    r = mutated(raw, lambda r: r.update(watchlist=[{"to": "nrt", "max_price_usd": 800}]))
    item = parse_config(r).watchlist[0]
    assert item.origins == ("MSP", "DFW")
    assert item.destination == "NRT"
    assert item.cabin == "economy"


def test_watchlist_explicit_origin_and_cabin(raw):
    r = mutated(raw, lambda r: r.update(
        watchlist=[{"from": "msp", "to": "LIS", "cabin": "business", "max_price_usd": 1500}]))
    item = parse_config(r).watchlist[0]
    assert item.origins == ("MSP",) and item.cabin == "business" and item.max_price_usd == 1500.0


def test_filters_are_normalized(raw):
    r = mutated(raw, lambda r: r["destinations"].update(
        include_regions=["Europe"], include_countries=["jp"], exclude_airports=["jfk"]))
    d = parse_config(r).destinations
    assert d.include_regions == ("europe",)
    assert d.include_countries == ("JP",)
    assert d.exclude_airports == ("JFK",)


def test_airline_codes_are_normalized(raw):
    r = mutated(raw, lambda r: r.update(airlines={"include": ["dl", " ua "], "exclude": ["f9", "9W"]}))
    a = parse_config(r).airlines
    assert a.include == ("DL", "UA") and a.exclude == ("F9", "9W")


def test_missing_optional_sections_default_to_no_restriction(raw):
    for key in ("destinations", "trip", "watchlist", "airlines"):
        raw.pop(key)
    cfg = parse_config(raw)
    assert cfg.destinations.include_regions == ()
    assert cfg.trip.max_days is None
    assert cfg.watchlist == ()


def test_alternatives_for_defaults_and_validation(raw, cfg):
    assert cfg.airlines.alternatives_for == ("F9", "NK")              # shipped default
    r = mutated(raw, lambda r: r.update(airlines={"alternatives_for": ["f9"]}))
    assert parse_config(r).airlines.alternatives_for == ("F9",)
    with pytest.raises(ConfigError, match="airlines.alternatives_for"):
        parse_config(mutated(raw, lambda r: r.update(airlines={"alternatives_for": ["Frontier"]})))
    assert parse_config(mutated(raw, lambda r: r.update(airlines={}))).airlines.alternatives_for == ()


# --- watchlist: moving target ---------------------------------------------------------------

def test_moving_watch_entries_parse(raw):
    r = mutated(raw, lambda r: r.update(watchlist=[
        {"from": "MEM", "to": "MSP", "alert_when": "lower_than_previous"},
        {"to": "LIS", "alert_when": "Lower_Than_Previous", "max_price_usd": 400},
        {"to": "NRT", "max_price_usd": 800},
    ]))
    a, b, c = parse_config(r).watchlist
    assert (a.lower_than_previous, a.max_price_usd, a.origins) == (True, None, ("MEM",))
    assert (b.lower_than_previous, b.max_price_usd) == (True, 400.0)
    assert (c.lower_than_previous, c.max_price_usd) == (False, 800.0)


@pytest.mark.parametrize("entry,message", [
    ({"to": "MSP"}, r"watchlist\[0\] needs"),
    ({"to": "MSP", "alert_when": "cheaper"}, "alert_when must be"),
    ({"to": "MSP", "alert_when": "lower_than_previous", "max_price_usd": -1}, "positive"),
    ({"to": "MSP", "alert_when": "lower_than_previous", "max_price_usd": "x"}, "must be a number"),
    ({"alert_when": "lower_than_previous"}, r"watchlist\[0\] needs"),
])
def test_invalid_moving_watch_entries(raw, entry, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(mutated(raw, lambda r: r.update(watchlist=[entry])))


def test_require_live_defaults_off_and_the_real_config_turns_it_on(raw, cfg, shipped_cfg):
    assert cfg.verify.require_live is False
    raw["verify"]["require_live"] = True
    assert parse_config(raw).verify.require_live is True
    assert shipped_cfg.verify.enabled is True and shipped_cfg.verify.require_live is True

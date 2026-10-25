from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date

import pytest
import requests
from helpers import FIXTURES, NOW, make_deal

from flightdrop.config import WatchItem
from flightdrop.sources import travelpayouts as tp
from flightdrop.sources.common import SourceError, get_json, to_date, to_datetime


def fixture(name: str = "tp_cheap.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad_json = status, body, bad_json

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Answers GETs from a callable (params -> response) or a queue; records every request."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url.removeprefix(tp.BASE), dict(params or {}), dict(headers or {})))
        if callable(self.answer):
            return self.answer(params)
        item = self.answer.pop(0) if len(self.answer) > 1 else self.answer[0]
        if isinstance(item, Exception):
            raise item
        return item


# --- shared helpers --------------------------------------------------------

def test_date_parsing_keeps_the_local_date_of_offset_timestamps():
    assert to_date("2026-12-03") == date(2026, 12, 3)
    assert to_date("2026-12-03T09:00:00-06:00") == date(2026, 12, 3)
    assert to_date("2027-08-06T19:25:00Z") == date(2027, 8, 6)
    assert to_date(None) is None and to_date("nope") is None and to_date("2026-13-45") is None


def test_datetime_parsing_is_always_utc_aware():
    assert to_datetime("2026-11-03T00:00:00Z").utcoffset().total_seconds() == 0
    assert to_datetime("2026-10-31T09:00:00").tzinfo is not None
    assert to_datetime("2026-11-03T03:00:00+03:00").hour == 0
    assert to_datetime("garbage") is None


# --- parser ----------------------------------------------------------------

def test_parse_cheap_keeps_good_rows_only():
    deals = tp.parse_cheap(fixture(), "MSP", NOW)
    got = {(d.destination, d.price_usd) for d in deals}
    # expired, one-way, no-price, no-departure and malformed rows are dropped
    assert got == {("LAX", 228.0), ("CUN", 438.0), ("NYC", 187.0), ("NRT", 845.5), ("NRT", 799.0)}


def test_parse_cheap_maps_fields():
    lax = next(d for d in tp.parse_cheap(fixture(), "MSP", NOW) if d.destination == "LAX")
    assert (lax.origin, lax.cabin, lax.source) == ("MSP", "economy", "travelpayouts")
    assert (lax.depart_date, lax.return_date) == (date(2026, 11, 13), date(2026, 11, 16))
    assert (lax.airline, lax.stops, lax.price_usd) == ("F9", 1, 228.0)
    assert lax.found_at == NOW and not lax.curated and lax.source_url is None


def test_stops_come_from_the_inner_key():
    stops = {d.price_usd: d.stops for d in tp.parse_cheap(fixture(), "MSP", NOW) if d.destination == "NRT"}
    assert stops == {845.5: 0, 799.0: 2}
    assert next(d for d in tp.parse_cheap(fixture(), "MSP", NOW) if d.destination == "CUN").stops == 0


def test_metro_code_destinations_are_kept_and_resolve_to_a_region():
    from flightdrop.airports import region_for
    nyc = next(d for d in tp.parse_cheap(fixture(), "MSP", NOW) if d.destination == "NYC")
    assert region_for(nyc.destination) == "domestic"


def test_a_route_to_the_origin_itself_is_skipped():
    row = {"airline": "XX", "departure_at": "2026-12-03T00:00:00Z", "return_at": "2026-12-10T00:00:00Z",
           "expires_at": "2026-11-03T00:00:00Z", "price": 50}
    assert tp.parse_cheap({"data": {"MSP": {"0": row}}}, "MSP", NOW) == []


def test_skip_reasons_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="flightdrop.travelpayouts")
    tp.parse_cheap(fixture(), "MSP", NOW)
    assert "expired" in caplog.text and "one-way" in caplog.text and "no price" in caplog.text


def test_wrong_currency_is_a_hard_error():
    with pytest.raises(SourceError, match="USD"):
        tp.parse_cheap(fixture() | {"currency": "rub"}, "MSP", NOW)


def test_empty_payloads_are_fine():
    assert tp.parse_cheap({"success": True, "data": {}}, "MSP", NOW) == []
    assert tp.parse_cheap({"success": True}, "MSP", NOW) == []
    assert tp.parse_cheap({"data": None}, "MSP", NOW) == []


# --- HTTP retry behavior ---------------------------------------------------

def test_get_json_retries_then_succeeds():
    sleeps = []
    session = FakeSession([FakeResponse(503), FakeResponse(429), FakeResponse(200, {"ok": 1})])
    assert get_json(session, tp.BASE + "/x", sleep=sleeps.append) == {"ok": 1}
    assert sleeps == [1.0, 2.0]


def test_get_json_retries_network_errors_then_gives_up():
    session = FakeSession([requests.ConnectionError("boom")])
    with pytest.raises(SourceError, match="gave up after 4 attempts.*ConnectionError"):
        get_json(session, tp.BASE + "/x", sleep=lambda s: None)
    assert len(session.calls) == 4


def test_get_json_auth_failure_is_immediate_and_never_leaks_the_token():
    session = FakeSession([FakeResponse(401)])
    with pytest.raises(SourceError) as exc:
        get_json(session, tp.BASE + "/x", headers={"X-Access-Token": "SECRET123"}, sleep=lambda s: None)
    assert "SECRET123" not in str(exc.value) and len(session.calls) == 1


def test_get_json_bad_json_and_client_errors():
    with pytest.raises(SourceError, match="not valid JSON"):
        get_json(FakeSession([FakeResponse(200, bad_json=True)]), tp.BASE + "/x")
    with pytest.raises(SourceError, match="HTTP 404"):
        get_json(FakeSession([FakeResponse(404)]), tp.BASE + "/x")


# --- fetch orchestration ---------------------------------------------------

def ok(_params=None):
    return FakeResponse(200, fixture())


def test_fetch_queries_anywhere_from_each_home_airport(cfg):
    session = FakeSession(ok)
    deals = tp.fetch(cfg, "TOKEN", NOW, session=session, sleep=lambda s: None)
    assert [(p, q["origin"], q["destination"]) for p, q, _ in session.calls] == [
        ("/v1/prices/cheap", "MSP", "-"), ("/v1/prices/cheap", "DFW", "-"),
    ]
    assert {d.origin for d in deals} == {"MSP", "DFW"} and all(d.cabin == "economy" for d in deals)


def test_fetch_sends_token_in_header_not_url_and_asks_for_usd(cfg):
    session = FakeSession(ok)
    tp.fetch(cfg, "TOKEN", NOW, session=session, sleep=lambda s: None)
    for _, params, headers in session.calls:
        assert headers["X-Access-Token"] == "TOKEN"
        assert "token" not in params and params["currency"] == "usd"


def test_fetch_queries_economy_watchlist_routes_directly(cfg):
    watch = (WatchItem(("MSP",), "NRT", "economy", 800.0), WatchItem(("MSP", "DFW"), "LIS", "economy", 500.0))
    session = FakeSession(ok)
    tp.fetch(replace(cfg, watchlist=watch), "T", NOW, session=session, sleep=lambda s: None)
    direct = [(q["origin"], q["destination"]) for _, q, _ in session.calls if q["destination"] != "-"]
    assert direct == [("MSP", "NRT"), ("MSP", "LIS"), ("DFW", "LIS")]


def test_business_watchlist_entries_are_skipped_with_a_warning(cfg, caplog):
    watch = (WatchItem(("MSP",), "LHR", "business", 1500.0),)
    session = FakeSession(ok)
    tp.fetch(replace(cfg, watchlist=watch), "T", NOW, session=session, sleep=lambda s: None)
    assert all(q["destination"] == "-" for _, q, _ in session.calls)       # nothing sent for the business entry
    assert "no business-class fare data" in caplog.text


def test_business_only_config_makes_no_requests(cfg):
    session = FakeSession(ok)
    assert tp.fetch(replace(cfg, cabins=("business",)), "T", NOW, session=session) == []
    assert session.calls == []


def test_fetch_survives_a_partial_failure(cfg):
    def answer(params):
        return FakeResponse(404) if params["origin"] == "DFW" else FakeResponse(200, fixture())
    deals = tp.fetch(cfg, "T", NOW, session=FakeSession(answer), sleep=lambda s: None)
    assert deals and {d.origin for d in deals} == {"MSP"}


def test_fetch_raises_when_everything_fails(cfg):
    session = FakeSession([FakeResponse(401)])
    with pytest.raises(SourceError, match="every Travelpayouts request failed"):
        tp.fetch(cfg, "BADTOKEN", NOW, session=session, sleep=lambda s: None)


def test_api_level_failure_flag_is_an_error(cfg):
    bad = FakeResponse(200, {"success": False, "error": "Wrong token"})
    with pytest.raises(SourceError):
        tp.fetch(cfg, "T", NOW, session=FakeSession([bad]), sleep=lambda s: None)


# --- next-best fare on another airline ---------------------------------------

def alt_fixture() -> dict:
    return fixture("tp_prices_for_dates.json")


def test_parse_prices_for_dates_keeps_complete_round_trips_only():
    deals = tp.parse_prices_for_dates(alt_fixture(), NOW)
    assert [(d.origin, d.airline, d.price_usd) for d in deals] == [
        ("DFW", "F9", 33.0), ("DFW", "F9", 52.0), ("DFW", "WN", 64.0), ("DFW", "WN", 40.0),
        ("DFW", "WN", 45.0), ("DFW", "AA", 140.0), ("DFW", "NK", 58.0), ("MSP", "DL", 25.0),
    ]   # one-way, no-airline, no-price and junk rows dropped
    aa = next(d for d in deals if d.airline == "AA")
    assert (aa.destination, aa.stops, aa.depart_date, aa.return_date) == ("LAS", 1, date(2026, 12, 8), date(2026, 12, 12))


def test_parse_prices_for_dates_wrong_currency_is_an_error():
    with pytest.raises(SourceError, match="USD"):
        tp.parse_prices_for_dates(alt_fixture() | {"currency": "eur"}, NOW)


def frontier_deal(**kw):
    return make_deal(origin="DFW", destination="LAS", airline="F9", price_usd=33.0, **kw)


def find(cfg, payload=None, **kw):
    session = FakeSession([FakeResponse(200, payload or alt_fixture())])
    return tp.find_alternative(frontier_deal(), cfg, "TOKEN", NOW, session=session, sleep=lambda s: None, **kw), session


def test_next_best_is_the_cheapest_fare_on_a_different_airline(cfg):
    alt, _ = find(cfg)
    # F9/NK are skipped, WN $40 departs too soon, WN $45 is a 50-day trip, MSP row is another route
    assert (alt.airline, alt.price_usd, alt.origin, alt.destination) == ("WN", 64.0, "DFW", "LAS")
    assert (alt.depart_date, alt.return_date, alt.stops) == (date(2026, 11, 14), date(2026, 11, 17), 0)


def test_next_best_respects_the_airline_filter(cfg):
    no_wn = replace(cfg, airlines=replace(cfg.airlines, exclude=("WN",)))
    alt, _ = find(no_wn)
    assert (alt.airline, alt.price_usd) == ("AA", 140.0)


def test_next_best_respects_the_trip_window(cfg):
    short = replace(cfg, trip=replace(cfg.trip, max_days=3))
    alt, _ = find(short)
    assert alt.airline == "WN" and alt.price_usd == 64.0           # 3-day trip still fits
    tiny = replace(cfg, trip=replace(cfg.trip, max_days=1))
    assert find(tiny)[0] is None


def test_next_best_is_none_when_only_budget_carriers_fly_it(cfg):
    only_budget = {"currency": "usd", "data": [r for r in alt_fixture()["data"] if isinstance(r, dict) and r.get("airline") in ("F9", "NK")]}
    assert find(cfg, only_budget)[0] is None


def test_next_best_request(cfg):
    _, session = find(cfg)
    path, params, headers = session.calls[0]
    assert path == "/aviasales/v3/prices_for_dates"
    assert (params["origin"], params["destination"], params["unique"], params["one_way"]) == ("DFW", "LAS", "false", "false")
    assert params["currency"] == "usd" and params["sorting"] == "price" and "token" not in params
    assert headers["X-Access-Token"] == "TOKEN"


def test_next_best_failure_raises_and_deals_without_a_destination_skip_the_call(cfg):
    with pytest.raises(SourceError):
        tp.find_alternative(frontier_deal(), cfg, "T", NOW, session=FakeSession([FakeResponse(500)]), sleep=lambda s: None)
    session = FakeSession([FakeResponse(200, alt_fixture())])
    assert tp.find_alternative(make_deal(destination=None), cfg, "T", NOW, session=session) is None
    assert session.calls == []

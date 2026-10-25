from __future__ import annotations

import pytest
import requests
from helpers import make_deal

from flightdrop.sources import serpapi_verify as sv
from flightdrop.sources.common import SourceError


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.ok = body, status, status < 400

    def json(self):
        return self._body


class Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def price(deal=None, body=None, **kw):
    session = Session(Resp(body if body is not None else {}))
    return sv.live_price(deal or make_deal(), "KEY", session=session, sleep=lambda s: None, **kw), session


def test_lowest_price_across_best_and_other_flights():
    body = {"best_flights": [{"price": 450}, {"price": 410}], "other_flights": [{"price": 395}, {"price": 700}]}
    assert price(body=body)[0] == 395.0


def test_request_parameters_for_economy_and_business():
    _, s = price()
    url, params = s.calls[0]
    assert url == sv.URL
    assert params == {
        "engine": "google_flights", "departure_id": "MSP", "arrival_id": "LIS",
        "outbound_date": "2026-12-03", "return_date": "2026-12-10", "type": 1, "travel_class": 1,
        "adults": 1, "currency": "USD", "hl": "en", "api_key": "KEY",
    }
    _, s2 = price(make_deal(cabin="business"))
    assert s2.calls[0][1]["travel_class"] == 3


def test_no_results_means_unknown_not_failure():
    assert price(body={"error": "Google Flights hasn't returned any results for this query."})[0] is None
    assert price(body={"best_flights": []})[0] is None
    assert price(body={"best_flights": [{"price": None}, {"nope": 1}, "junk"]})[0] is None


def test_other_api_errors_raise_without_leaking_the_key():
    with pytest.raises(SourceError, match="Invalid API key"):
        price(body={"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"})
    with pytest.raises(SourceError) as exc:
        sv.live_price(make_deal(), "SUPERSECRET", session=Session(Resp({}, 401)), sleep=lambda s: None)
    assert "SUPERSECRET" not in str(exc.value)
    with pytest.raises(SourceError) as exc2:
        sv.live_price(make_deal(), "SUPERSECRET", session=Session(requests.ConnectionError("x SUPERSECRET")),
                      sleep=lambda s: None)
    assert "SUPERSECRET" not in str(exc2.value)


def test_deals_without_a_route_or_dates_are_not_looked_up():
    for d in (make_deal(destination=None), make_deal(depart_date=None), make_deal(return_date=None)):
        value, session = price(d)
        assert value is None and session.calls == []


def test_metro_codes_are_expanded_to_airport_lists_for_google():
    deal = make_deal(origin="DFW", destination="ROM")
    _, s = price(deal)
    params = s.calls[0][1]
    assert (params["departure_id"], params["arrival_id"]) == ("DFW", "FCO,CIA")
    _, s2 = price(make_deal(origin="MSP", destination="TYO"))
    assert s2.calls[0][1]["arrival_id"] == "HND,NRT"
    _, s3 = price(make_deal(origin="MSP", destination="LIS"))
    assert (s3.calls[0][1]["departure_id"], s3.calls[0][1]["arrival_id"]) == ("MSP", "LIS")        # plain airports unchanged

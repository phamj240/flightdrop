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


# --- Google's price insights: the "usual price" ------------------------------------------------

def google_response(prices, history=None, level="typical", rng=(560, 820), with_insights=True):
    flights = [{"price": p} for p in prices]
    body = {"best_flights": flights[:2], "other_flights": flights[2:]}
    if with_insights:
        insights = {"lowest_price": min(prices), "price_level": level, "typical_price_range": list(rng) if rng else None}
        if history is not None:
            insights["price_history"] = [[1_787_720_400 + i * 86_400, p] for i, p in enumerate(history)]
        body["price_insights"] = insights
    return body


def quote(body, deal=None):
    session = Session(Resp(body))
    return sv.live_quote(deal or make_deal(), "KEY", session=session, sleep=lambda s: None)


def test_usual_is_the_median_of_googles_price_history():
    history = [620, 610, 760, 750, 750, 750, 740, 735, 735, 740]          # median 740
    q = quote(google_response([740, 750], history=history))
    assert (q.price, q.usual, q.level, q.typical_low, q.typical_high) == (740.0, 740.0, "typical", 560.0, 820.0)


def test_a_short_history_falls_back_to_the_middle_of_the_typical_range():
    q = quote(google_response([700], history=[700, 710, 720], rng=(560, 820)))
    assert q.usual == 690.0


def test_no_history_and_no_range_means_no_usual_price():
    q = quote(google_response([700], history=None, rng=None))
    assert q.price == 700.0 and q.usual is None and q.typical_low is None


def test_a_response_without_insights_still_gives_the_price():
    q = quote(google_response([700, 705], with_insights=False))
    assert (q.price, q.usual, q.level) == (700.0, None, None)


def test_junk_in_the_insights_is_ignored():
    body = google_response([700], history=[700] * 9 + ["x", [1], None, [5, -3], [5, "y"]], level="great", rng=("a", 5))
    q = quote(body)
    assert q.usual == 700.0                              # the 9 good history points
    assert q.level is None and q.typical_low is None     # unknown label and malformed range are dropped


def test_each_price_level_is_passed_through():
    for level in ("low", "typical", "high"):
        assert quote(google_response([700], level=level)).level == level


def test_live_price_is_just_the_price_from_the_quote():
    session = Session(Resp(google_response([700, 650, 900])))
    assert sv.live_price(make_deal(), "KEY", session=session, sleep=lambda s: None) == 650.0
    assert quote({"best_flights": [], "other_flights": [], "price_insights": {"price_level": "low"}}) is None     # no flights, no quote

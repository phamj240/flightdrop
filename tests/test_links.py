from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

from helpers import make_deal

from flightdrop.links import google_flights_url


def query(url: str) -> str:
    return parse_qs(urlparse(url).query)["q"][0]


def test_round_trip_link():
    url = google_flights_url(make_deal())
    assert url.startswith("https://www.google.com/travel/flights?q=")
    assert query(url) == "Flights from MSP to LIS on 2026-12-03 through 2026-12-10"


def test_business_class_is_requested():
    assert query(google_flights_url(make_deal(cabin="business"))).endswith("business class")


def test_one_way_and_undated():
    assert query(google_flights_url(make_deal(return_date=None))) == "Flights from MSP to LIS on 2026-12-03"
    undated = make_deal(depart_date=None, return_date=None)
    assert query(google_flights_url(undated)) == "Flights from MSP to LIS"


def test_unknown_destination():
    assert query(google_flights_url(make_deal(destination=None, depart_date=None, return_date=None))) == "Flights from MSP"


def test_curated_deal_links_to_source_post():
    deal = make_deal(source_url="https://example.com/post/1", curated=True)
    assert google_flights_url(deal) == "https://example.com/post/1"


def test_link_is_url_safe():
    assert " " not in google_flights_url(make_deal())
    assert date(2026, 12, 3).isoformat() in google_flights_url(make_deal())


def test_deal_keys_for_feed_posts_use_sha256_and_are_stable():
    a = make_deal(source_url="https://example.com/post/1", curated=True)
    b = make_deal(source_url="https://example.com/post/1", curated=True, price_usd=1.0)
    c = make_deal(source_url="https://example.com/post/2", curated=True)
    assert a.deal_key == b.deal_key != c.deal_key
    assert a.deal_key == "url:" + __import__("hashlib").sha256(b"https://example.com/post/1").hexdigest()[:16]

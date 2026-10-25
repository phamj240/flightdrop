from __future__ import annotations

from dataclasses import replace

import pytest
import requests
from helpers import FIXTURES, NOW

from flightdrop.airports import find_by_city
from flightdrop.config import RssConfig
from flightdrop.sources import rss_deals as rss
from flightdrop.sources.common import SourceError

XML = (FIXTURES / "rss_theflightdeal.xml").read_bytes()


@pytest.fixture
def kw(cfg):
    return rss._keyword_origins(cfg)


# --- airports.find_by_city --------------------------------------------------

def test_find_by_city_is_case_and_accent_insensitive():
    assert [a.iata for a in find_by_city("lisbon")] == ["LIS"]
    assert {a.country for a in find_by_city("Paris")} >= {"FR"}
    assert find_by_city("Nowhereville") == []


# --- title parsing ----------------------------------------------------------

def test_theflightdeal_title(kw):
    p = rss.parse_title("Qatar Airways: Dallas &#8211; Tashkent, Uzbekistan. $903. Roundtrip", kw)
    assert (p.origin, p.destination, p.price_usd, p.cabin) == ("DFW", "TAS", 903.0, "economy")


def test_first_price_wins_when_several_fares_listed(kw):
    p = rss.parse_title("Delta: Minneapolis – Lisbon, Portugal. $421 (Basic) / $561 (Regular). Roundtrip", kw)
    assert (p.origin, p.destination, p.price_usd) == ("MSP", "LIS", 421.0)


def test_reddit_style_title_with_airport_tag(kw):
    p = rss.parse_title("[MSP] Minneapolis to Hong Kong 1720€ Round Trip", kw)
    assert (p.origin, p.destination, p.price_usd) == ("MSP", "HKG", None)    # euro prices are not trusted


def test_business_class_detected_and_thousands_separator_parsed(kw):
    p = rss.parse_title("Business Class Deal: Dallas to Paris $2,450 Round Trip", kw)
    assert (p.cabin, p.price_usd) == ("business", 2450.0)


def test_ambiguous_city_leaves_destination_unknown(kw):
    assert rss.parse_title("Dallas to Paris $400", kw).destination is None   # Paris, FR vs Paris, TX


def test_inbound_flights_are_ignored(kw):
    assert rss.parse_title("Air Canada: Los Angeles – Dallas, Texas. $212. Roundtrip", kw) is None
    assert rss.parse_title("Brussels to Hong Kong $1,720 Round Trip", kw) is None


@pytest.mark.parametrize("title", [
    "[EXPIRED] Dallas to Cancun $150",
    "First Class Deal: Dallas to Tokyo $4,000",
    "Premium Economy: Minneapolis to London $700",
])
def test_unwanted_titles_are_skipped(kw, title):
    assert rss.parse_title(title, kw) is None


def test_airport_code_keyword_is_case_sensitive_and_word_bounded(kw):
    assert rss.parse_title("[DFW] to Cancun $200", kw) is not None
    assert rss.parse_title("[dfw] to Cancun $200", kw) is None      # codes must be upper-case
    assert rss.parse_title("DFWX to Cancun $200", kw) is None


def test_title_without_separator_still_matches_origin(kw):
    p = rss.parse_title("Cheap flights from Dallas $199", kw)
    assert (p.origin, p.destination, p.price_usd) == ("DFW", None, 199.0)


def test_keywords_that_match_no_home_airport_are_dropped(cfg):
    odd = replace(cfg, rss=replace(cfg.rss, keywords=("Denver", "MSP")))
    assert [o for _, o in rss._keyword_origins(odd)] == ["MSP"]


# --- feed parsing -----------------------------------------------------------

def test_parse_feed_keeps_only_fresh_outbound_deals(cfg):
    deals = rss.parse_feed("theflightdeal", XML, cfg, NOW)
    by_url = {d.source_url: d for d in deals}
    # kept: 1 (DFW-TAS), 2 (MSP-LIS), 4 (business Paris), 8 (MSP-HKG, no price)
    assert sorted(by_url) == [f"https://example.com/deal/{n}" for n in (1, 2, 4, 8)]
    d = by_url["https://example.com/deal/1"]
    assert d.curated and d.source == "theflightdeal" and d.title.startswith("Qatar Airways: Dallas")
    assert d.found_at.isoformat() == "2026-10-31T12:00:00+00:00"
    assert d.deal_key.startswith("url:")
    assert by_url["https://example.com/deal/4"].cabin == "business"


def test_feed_age_limit_is_configurable(cfg):
    loose = replace(cfg, rss=replace(cfg.rss, max_age_hours=24 * 30))
    urls = {d.source_url for d in rss.parse_feed("x", XML, loose, NOW)}
    assert "https://example.com/deal/5" in urls          # the 6-day-old post is now accepted


def test_garbage_feed_yields_nothing(cfg):
    assert rss.parse_feed("x", b"<html>not a feed</html>", cfg, NOW) == []


# --- fetch ------------------------------------------------------------------

class Resp:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content = status, content
        self.ok = status < 400


class Session:
    def __init__(self, by_url):
        self.by_url, self.calls = by_url, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        r = self.by_url[url]
        if isinstance(r, Exception):
            raise r
        return r


def two_feeds(cfg):
    feeds = {"good": "https://good.example/feed", "bad": "https://bad.example/feed"}
    return replace(cfg, rss=RssConfig(True, cfg.rss.keywords, feeds, 48))


def test_fetch_survives_one_dead_feed(cfg):
    cfg2 = two_feeds(cfg)
    session = Session({"https://good.example/feed": Resp(200, XML), "https://bad.example/feed": Resp(403)})
    deals = rss.fetch(cfg2, NOW, session=session)
    assert len(deals) == 4 and {d.source for d in deals} == {"good"}
    assert all("flightdrop" in (h or {}).get("User-Agent", "") for _, h in session.calls)


def test_fetch_raises_if_all_feeds_fail(cfg):
    cfg2 = two_feeds(cfg)
    session = Session({
        "https://good.example/feed": requests.ConnectionError("down"),
        "https://bad.example/feed": Resp(500),
    })
    with pytest.raises(SourceError, match="every deal feed failed"):
        rss.fetch(cfg2, NOW, session=session)


def test_fetch_disabled_makes_no_requests(cfg):
    off = replace(cfg, rss=replace(cfg.rss, enabled=False))
    session = Session({})
    assert rss.fetch(off, NOW, session=session) == [] and session.calls == []


def test_feed_links_must_be_http_or_https(cfg):
    def item(n, link):
        return (f"<item><title>Delta: Dallas &#8211; Lisbon, Portugal. $421. Roundtrip</title>"
                f"<link>{link}</link><pubDate>Sun, 01 Nov 2026 09:00:00 +0000</pubDate></item>")
    xml = ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title><link>https://e.test</link><description>d</description>"
           + item(1, "javascript:alert(1)") + item(2, "ftp://example.com/x") + item(3, "https://example.com/ok")
           + "</channel></rss>")
    deals = rss.parse_feed("x", xml.encode(), cfg, NOW)
    assert [d.source_url for d in deals] == ["https://example.com/ok"]

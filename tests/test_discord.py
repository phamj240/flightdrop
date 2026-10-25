from __future__ import annotations

import pytest
import requests
from helpers import NOW, make_deal

from flightdrop.models import ScoredDeal
from flightdrop.notify import discord

HOOK = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


def scored(**kw):
    deal = kw.pop("deal", None) or make_deal(airline="TP", stops=1)
    return ScoredDeal(deal, kw.pop("score", 0.5), kw.pop("reasons", ["12% below usual $450"]), **kw)


class Resp:
    def __init__(self, status=204, body=None):
        self.status_code, self._body, self.ok = status, body or {}, status < 400

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def test_embed_for_a_normal_deal():
    e = discord.build_embed(scored(reasons=["12% below usual $450", "under $450 europe economy target"]))
    assert e["title"] == "MSP → Lisbon · $400 round trip"
    assert e["url"].startswith("https://www.google.com/travel/flights?q=")
    assert e["description"] == "• 12% below usual $450\n• under $450 europe economy target"
    assert e["color"] == discord.COLOR_DEAL
    assert e["footer"]["text"] == "cached price - verify before booking"
    fields = {f["name"]: f["value"] for f in e["fields"]}
    assert fields == {
        "Cabin": "Economy",
        "Dates": "Dec 3 → Dec 10 (7 nights)",
        "Flight": "TAP Air Portugal (TP) · 1 stop",
    }
    assert e["timestamp"] == NOW.isoformat()


def test_embed_for_business_nonstop_watchlist_hit():
    deal = make_deal(cabin="business", price_usd=1480.0, stops=0, airline=None)
    e = discord.build_embed(scored(deal=deal, reasons=["watchlist target $1,500"]))
    assert e["title"].endswith("· $1,480 round trip (business)")
    assert e["color"] == discord.COLOR_WATCHLIST
    assert {f["name"]: f["value"] for f in e["fields"]}["Flight"] == "nonstop"


def test_live_verified_embed_shows_live_price_and_cached_price():
    e = discord.build_embed(scored(verification="live", live_price_usd=412.0))
    assert "· $412 round trip" in e["title"]
    assert e["footer"]["text"] == "live-verified price"
    assert {f["name"]: f["value"] for f in e["fields"]}["Cached price"] == "$400"


def test_embed_for_a_curated_feed_deal():
    deal = make_deal(destination=None, price_usd=None, depart_date=None, return_date=None, curated=True,
                     source="theflightdeal", source_url="https://example.com/deal/1",
                     title="Qatar Airways: Dallas – Tashkent. $903")
    e = discord.build_embed(scored(deal=deal, reasons=["curated deal feed"]))
    assert e["title"] == "Qatar Airways: Dallas – Tashkent. $903"
    assert e["url"] == "https://example.com/deal/1"
    assert e["color"] == discord.COLOR_FEED and "fields" not in e
    assert e["footer"]["text"].startswith("via theflightdeal")


def test_long_feed_titles_are_truncated_to_discords_limit():
    deal = make_deal(destination=None, curated=True, source_url="https://x.test/1", title="A" * 400)
    assert len(discord.build_embed(scored(deal=deal))["title"]) == 256


def test_post_disables_mentions_and_sends_one_embed():
    s = Session(Resp(204))
    discord.post(HOOK, scored(), session=s)
    url, payload = s.calls[0]
    assert url == HOOK
    assert payload["allowed_mentions"] == {"parse": []}      # feed titles are untrusted: no @everyone
    assert len(payload["embeds"]) == 1


def test_rate_limit_is_retried_once_after_waiting():
    waits = []
    s = Session(Resp(429, {"retry_after": 2.5}), Resp(204))
    discord.post(HOOK, scored(), session=s, sleep=waits.append)
    assert waits == [2.5] and len(s.calls) == 2


def test_rate_limit_wait_is_capped():
    waits = []
    s = Session(Resp(429, {"retry_after": 999}), Resp(204))
    discord.post(HOOK, scored(), session=s, sleep=waits.append)
    assert waits == [discord.MAX_RETRY_WAIT]


def test_persistent_rate_limit_fails():
    s = Session(Resp(429, {"retry_after": 1}))
    with pytest.raises(discord.NotifyError, match="HTTP 429"):
        discord.post(HOOK, scored(), session=s, sleep=lambda x: None)
    assert len(s.calls) == 2


def test_errors_never_contain_the_webhook_secret():
    for session in (Session(Resp(404)), Session(requests.ConnectionError(f"failed for {HOOK}"))):
        with pytest.raises(discord.NotifyError) as exc:
            discord.post(HOOK, scored(), session=session)
        assert "SECRET-TOKEN" not in str(exc.value) and "SECRET-TOKEN" not in repr(exc.value)
        assert exc.value.__cause__ is None


def test_notice_message():
    s = Session(Resp(204))
    discord.post_notice(HOOK, "config.yaml is invalid: trip.min_days", session=s)
    embed = s.calls[0][1]["embeds"][0]
    assert embed["color"] == discord.COLOR_NOTICE and "trip.min_days" in embed["description"]


# --- next-best fare field ----------------------------------------------------

def test_embed_shows_the_next_best_fare_on_another_airline():
    frontier = make_deal(destination="LAS", origin="DFW", price_usd=33.0, airline="F9", stops=0)
    alt = make_deal(destination="LAS", origin="DFW", price_usd=140.0, airline="AA", stops=1)
    e = discord.build_embed(scored(deal=frontier, alternative=alt, alternative_checked=True))
    field = next(f for f in e["fields"] if f["name"] == "Next best (other airlines)")
    assert field["inline"] is False
    assert field["value"].startswith("[$140 · American (AA) · 1 stop · Dec 3 → Dec 10](https://www.google.com/travel/flights?q=")
    assert field["value"].endswith(")")


def test_embed_says_so_when_no_other_airline_was_found():
    e = discord.build_embed(scored(alternative=None, alternative_checked=True))
    field = next(f for f in e["fields"] if f["name"] == "Next best (other airlines)")
    assert field["value"] == "none found in the cached data"


def test_embed_has_no_next_best_field_when_nothing_was_looked_up():
    e = discord.build_embed(scored())
    assert all(f["name"] != "Next best (other airlines)" for f in e["fields"])


def test_stops_text():
    assert [discord.stops_text(n) for n in (None, 0, 1, 2)] == [None, "nonstop", "1 stop", "2 stops"]

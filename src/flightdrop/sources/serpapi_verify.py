"""Optional live-price check through SerpApi's Google Flights engine (free: 250 searches/month).

Used only to confirm a fare that is about to be posted, never to scan. Whether Google Flights
searches count against the free quota is confirmed when the account is created.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import Callable

import requests

from ..airports import airport_codes_for
from ..models import Deal, LiveQuote
from .common import SourceError, get_json

log = logging.getLogger("flightdrop.serpapi")

URL = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "business": 3}   # SerpApi: 1 economy, 2 premium, 3 business, 4 first


MIN_HISTORY_POINTS = 8      # fewer daily prices than this and the typical range is used instead


def live_quote(
    deal: Deal,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LiveQuote | None:
    """Google's lowest current round-trip price for this deal's route and dates, with its price insights.

    ``usual`` is the median of Google's ~60-day price history for the trip (the average price it has
    been at); if the history is short it falls back to the middle of Google's "typical price range".
    None means "couldn't tell" (no dates, or Google returned nothing), not "no flights".
    Raises SourceError on API/network failure.
    """
    if not (deal.destination and deal.depart_date and deal.return_date):
        return None
    params = {
        "engine": "google_flights",
        "departure_id": ",".join(airport_codes_for(deal.origin)),
        "arrival_id": ",".join(airport_codes_for(deal.destination)),
        "outbound_date": deal.depart_date.isoformat(),
        "return_date": deal.return_date.isoformat(),
        "type": 1,                                  # round trip
        "travel_class": TRAVEL_CLASS[deal.cabin],
        "adults": 1,
        "currency": "USD",
        "hl": "en",
        "api_key": api_key,
    }
    payload = get_json(session or requests.Session(), URL, params=params, retries=1, sleep=sleep)
    if not isinstance(payload, dict):
        raise SourceError("unexpected SerpApi response shape")
    error = payload.get("error")
    if error:
        if "hasn't returned any results" in str(error):
            return None
        raise SourceError(f"SerpApi: {str(error)[:120]}")

    prices = [
        float(f["price"])
        for key in ("best_flights", "other_flights")
        for f in payload.get(key) or []
        if isinstance(f, dict) and isinstance(f.get("price"), (int, float)) and f["price"] > 0
    ]
    if not prices:
        return None

    insights = payload.get("price_insights")
    insights = insights if isinstance(insights, dict) else {}
    history = [
        float(p[1])
        for p in insights.get("price_history") or []
        if isinstance(p, (list, tuple)) and len(p) == 2 and isinstance(p[1], (int, float)) and p[1] > 0
    ]
    rng = insights.get("typical_price_range")
    low = high = None
    if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in rng):
        low, high = float(rng[0]), float(rng[1])
    usual = None
    if len(history) >= MIN_HISTORY_POINTS:
        usual = statistics.median(history)
    elif low is not None and high is not None:
        usual = (low + high) / 2
    level = insights.get("price_level")
    return LiveQuote(
        price=min(prices),
        usual=usual,
        level=level if level in ("low", "typical", "high") else None,
        typical_low=low,
        typical_high=high,
    )


def live_price(
    deal: Deal,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> float | None:
    """Just the lowest live price (see ``live_quote``)."""
    quote = live_quote(deal, api_key, session=session, sleep=sleep)
    return quote.price if quote else None

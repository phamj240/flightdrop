"""Optional live-price check through SerpApi's Google Flights engine (free: 250 searches/month).

Used only to confirm a fare that is about to be posted, never to scan. Whether Google Flights
searches count against the free quota is confirmed when the account is created.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from ..models import Deal
from .common import SourceError, get_json

log = logging.getLogger("flightdrop.serpapi")

URL = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "business": 3}   # SerpApi: 1 economy, 2 premium, 3 business, 4 first


def live_price(
    deal: Deal,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> float | None:
    """Lowest current round-trip price in USD for this deal's route and dates, or None.

    None means "couldn't tell" (no dates, or Google returned nothing), not "no flights".
    Raises SourceError on API/network failure.
    """
    if not (deal.destination and deal.depart_date and deal.return_date):
        return None
    params = {
        "engine": "google_flights",
        "departure_id": deal.origin,
        "arrival_id": deal.destination,
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
    return min(prices) if prices else None

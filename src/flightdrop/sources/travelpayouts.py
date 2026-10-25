"""Aviasales / Travelpayouts Data API: cached (not live) economy fares, free with an affiliate token.

Verified against the live API (see docs/LLD.md):
- ``GET /v1/prices/cheap?origin=MSP&destination=-`` returns the cheapest round trip to ~100-200
  destinations, as ``data[destination][stops] = {price, airline, departure_at, return_at,
  expires_at, ...}``. The inner key is the number of stops.
- Only economy exists: every business/first-class request is refused or returns nothing.
- Destinations can be metro codes (NYC, LON, TYO...), which ``airports`` knows about.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable

from zoneinfo import ZoneInfo

import requests

from ..config import Config
from ..filters import passes_airline, passes_trip
from ..models import Deal
from .common import SourceError, get_json, to_date, to_datetime

log = logging.getLogger("flightdrop.travelpayouts")

BASE = "https://api.travelpayouts.com"
SOURCE = "travelpayouts"


def _price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def parse_cheap(payload: dict[str, Any], origin: str, now: datetime) -> list[Deal]:
    """Turn a /v1/prices/cheap response into economy round-trip Deals.

    Bad rows are skipped (and counted in the log), not fatal. A response in a currency
    other than USD is fatal: prices would be meaningless.
    """
    currency = payload.get("currency")
    if currency is not None and str(currency).lower() != "usd":
        raise SourceError(f"expected USD prices but the API returned {currency!r}")

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    deals = []
    for dest, options in (payload.get("data") or {}).items():
        if not isinstance(options, dict):
            skip("malformed")
            continue
        for stops_key, row in options.items():
            if not isinstance(row, dict):
                skip("malformed")
                continue
            price = _price(row.get("price"))
            depart, ret = to_date(row.get("departure_at")), to_date(row.get("return_at"))
            expires = to_datetime(row.get("expires_at"))
            destination = str(dest).upper()
            if price is None:
                skip("no price")
            elif depart is None:
                skip("no departure date")
            elif ret is None:
                skip("one-way")
            elif expires is not None and expires <= now:
                skip("expired")
            elif destination == origin:
                skip("route to origin")
            else:
                deals.append(
                    Deal(
                        origin=origin,
                        destination=destination,
                        cabin="economy",
                        price_usd=price,
                        source=SOURCE,
                        found_at=now,
                        depart_date=depart,
                        return_date=ret,
                        airline=row.get("airline") or None,
                        stops=int(stops_key) if str(stops_key).isdigit() else None,
                    )
                )
    if skipped:
        log.info("prices/cheap %s: skipped rows %s", origin, dict(sorted(skipped.items())))
    return deals


def parse_prices_for_dates(payload: dict[str, Any], now: datetime) -> list[Deal]:
    """/aviasales/v3/prices_for_dates: many economy fares for one route, across airlines.

    Rows carry airline, price, outbound/return timestamps and the outbound stop count
    (``transfers``). There is no expiry or found-at field, so freshness is unknown.
    """
    currency = payload.get("currency")
    if currency is not None and str(currency).lower() != "usd":
        raise SourceError(f"expected USD prices but the API returned {currency!r}")
    deals = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        price = _price(row.get("price"))
        depart, ret = to_date(row.get("departure_at")), to_date(row.get("return_at"))
        origin, dest = str(row.get("origin") or "").upper(), str(row.get("destination") or "").upper()
        airline = row.get("airline")
        if price is None or depart is None or ret is None or not origin or not dest or not airline:
            continue
        stops = row.get("transfers")
        deals.append(
            Deal(
                origin=origin, destination=dest, cabin="economy", price_usd=price, source=SOURCE,
                found_at=now, depart_date=depart, return_date=ret, airline=str(airline).upper(),
                stops=stops if isinstance(stops, int) else None,
            )
        )
    return deals


def find_alternative(
    deal: Deal,
    cfg: Config,
    token: str,
    now: datetime,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Deal | None:
    """Cheapest fare for the same route on an airline NOT in ``alternatives_for``, or None.

    Respects the trip window and airline filter. Raises SourceError if the request fails.
    """
    if not deal.destination:
        return None
    client = _Client(token, session or requests.Session(), sleep)
    payload = client.get(
        "/aviasales/v3/prices_for_dates",
        origin=deal.origin, destination=deal.destination,
        one_way="false", unique="false", sorting="price", limit=100,
    )
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    skip = set(cfg.airlines.alternatives_for)
    options = [
        d for d in parse_prices_for_dates(payload, now)
        if d.origin == deal.origin
        and d.destination == deal.destination
        and d.airline not in skip
        and passes_trip(d, cfg.trip, today)
        and passes_airline(d, cfg.airlines)
    ]
    return min(options, key=lambda d: d.price_usd or 0.0) if options else None


class _Client:
    def __init__(self, token: str, session: requests.Session, sleep: Callable[[float], None]):
        self._headers = {"X-Access-Token": token, "Accept": "application/json"}
        self._session = session
        self._sleep = sleep

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        params.setdefault("currency", "usd")
        payload = get_json(
            self._session, BASE + path, params=params, headers=self._headers, sleep=self._sleep
        )
        if not isinstance(payload, dict):
            raise SourceError(f"{path}: unexpected response shape")
        if payload.get("success") is False:
            raise SourceError(f"{path}: API reported failure ({payload.get('error') or 'no detail'})")
        return payload


def fetch(
    cfg: Config,
    token: str,
    now: datetime,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Deal]:
    """Cheapest cached economy fares from every home airport, plus each watched route directly.

    A failed request is logged and skipped; if every request fails the whole source is
    reported as failed so the run can surface it.
    """
    if "economy" not in cfg.cabins:
        log.info("economy is not enabled; the fare source only has economy fares, so nothing to fetch")
        return []
    if "business" in cfg.cabins:
        log.info("this fare source has no business-class fares; business deals come from the deal feeds only")

    client = _Client(token, session or requests.Session(), sleep)
    deals: list[Deal] = []
    attempted = failed = 0

    def run(label: str, origin: str, **params: str) -> None:
        nonlocal attempted, failed
        attempted += 1
        try:
            found = parse_cheap(client.get("/v1/prices/cheap", origin=origin, **params), origin, now)
        except SourceError as e:
            failed += 1
            log.warning("%s failed: %s", label, e)
            return
        log.info("%s: %d candidate deal(s)", label, len(found))
        deals.extend(found)

    for origin in cfg.home_airports:
        run(f"anywhere from {origin}", origin, destination="-")

    # Watched routes are queried directly so they're checked even when they aren't
    # among the cheapest "anywhere" results.
    for w in cfg.watchlist:
        if w.cabin != "economy":
            log.warning("watchlist %s->%s %s can't be checked: no business-class fare data", "/".join(w.origins), w.destination, w.cabin)
            continue
        for origin in w.origins:
            run(f"watch {origin}-{w.destination}", origin, destination=w.destination)

    if attempted and failed == attempted:
        raise SourceError("every Travelpayouts request failed")
    return deals

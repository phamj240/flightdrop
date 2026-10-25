from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from ..airlines import airline_name
from ..airports import city_name
from ..links import google_flights_url
from ..models import ScoredDeal

log = logging.getLogger("flightdrop.discord")

COLOR_WATCHLIST = 0xF1C40F   # gold
COLOR_DEAL = 0x2ECC71        # green
COLOR_FEED = 0x3498DB        # blue
COLOR_NOTICE = 0xE74C3C      # red

MAX_RETRY_WAIT = 30.0


class NotifyError(RuntimeError):
    """Posting to Discord failed. Never contains the webhook URL (it is a secret)."""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_date(d) -> str:
    return f"{d:%b} {d.day}"


def stops_text(stops: int | None) -> str | None:
    if stops is None:
        return None
    return "nonstop" if stops == 0 else f"{stops} stop{'s' * (stops > 1)}"


def alternative_text(alt: Any) -> str:
    """One line describing the next-best fare on another airline, linked to Google Flights."""
    parts = [f"${alt.price_usd:,.0f}", airline_name(alt.airline), stops_text(alt.stops)]
    if alt.depart_date and alt.return_date:
        parts.append(f"{_fmt_date(alt.depart_date)} → {_fmt_date(alt.return_date)}")
    return f"[{' · '.join(p for p in parts if p)}]({google_flights_url(alt)})"


def build_embed(s: ScoredDeal) -> dict[str, Any]:
    d = s.deal
    fields: list[dict[str, Any]] = []

    if d.curated:
        title = d.title or f"Deal from {d.origin}"
        color = COLOR_FEED
        footer = f"via {d.source} - check the post for dates and conditions"
    else:
        price = f"${s.live_price_usd or d.price_usd:,.0f}"
        cabin = " (business)" if d.cabin == "business" else ""
        title = f"{d.origin} → {city_name(d.destination or '')} · {price} round trip{cabin}"
        color = COLOR_WATCHLIST if any(r.startswith("watchlist") for r in s.reasons) else COLOR_DEAL
        footer = (
            "live-verified price"
            if s.verification == "live"
            else "cached price - verify before booking"
        )
        fields.append({"name": "Cabin", "value": d.cabin.title(), "inline": True})
        if d.depart_date and d.return_date:
            nights = (d.return_date - d.depart_date).days
            value = f"{_fmt_date(d.depart_date)} → {_fmt_date(d.return_date)} ({nights} nights)"
            fields.append({"name": "Dates", "value": value, "inline": True})
        details = " · ".join(x for x in (airline_name(d.airline), stops_text(d.stops)) if x)
        if details:
            fields.append({"name": "Flight", "value": details, "inline": True})
        if s.verification == "live" and d.price_usd and s.live_price_usd:
            fields.append({"name": "Cached price", "value": f"${d.price_usd:,.0f}", "inline": True})
        if s.alternative_checked:
            value = alternative_text(s.alternative) if s.alternative else "none found in the cached data"
            fields.append({"name": "Next best (other airlines)", "value": _cut(value, 1000), "inline": False})

    embed: dict[str, Any] = {
        "title": _cut(title, 256),
        "url": google_flights_url(d),
        "color": color,
        "description": _cut("\n".join(f"• {r}" for r in s.reasons), 2000),
        "footer": {"text": _cut(footer, 2000)},
        "timestamp": d.found_at.isoformat(),
    }
    if fields:
        embed["fields"] = fields
    return embed


def _send(
    webhook_url: str,
    payload: dict[str, Any],
    session: requests.Session,
    sleep: Callable[[float], None],
) -> None:
    # Post content can come from public feeds: never let it trigger @everyone / role pings.
    payload = {"username": "flightdrop", "allowed_mentions": {"parse": []}, **payload}
    for attempt in (1, 2):
        try:
            resp = session.post(webhook_url, json=payload, timeout=20)
        except requests.RequestException as e:
            raise NotifyError(f"could not reach Discord ({type(e).__name__})") from None
        if resp.status_code == 429 and attempt == 1:
            try:
                wait = float(resp.json().get("retry_after", 1.0))
            except (ValueError, AttributeError):
                wait = 1.0
            log.info("Discord rate limit, retrying in %.1fs", wait)
            sleep(min(wait, MAX_RETRY_WAIT))
            continue
        if not resp.ok:
            raise NotifyError(f"Discord rejected the message (HTTP {resp.status_code})")
        return


def post(
    webhook_url: str,
    scored: ScoredDeal,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    _send(webhook_url, {"embeds": [build_embed(scored)]}, session or requests.Session(), sleep)


def post_notice(
    webhook_url: str,
    message: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Operational message (e.g. 'config.yaml is invalid') shown as a red embed."""
    embed = {"title": "flightdrop notice", "description": _cut(message, 3000), "color": COLOR_NOTICE}
    _send(webhook_url, {"embeds": [embed]}, session or requests.Session(), sleep)

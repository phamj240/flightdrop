from __future__ import annotations

from urllib.parse import quote

from .models import Deal

_BASE = "https://www.google.com/travel/flights?q="


def google_flights_url(deal: Deal) -> str:
    """Link that opens Google Flights pre-filled for this deal (free, no API).

    Curated feed deals link to the original post instead.
    """
    if deal.source_url:
        return deal.source_url
    parts = [f"Flights from {deal.origin}"]
    if deal.destination:
        parts.append(f"to {deal.destination}")
    if deal.depart_date:
        parts.append(f"on {deal.depart_date.isoformat()}")
        if deal.return_date:
            parts.append(f"through {deal.return_date.isoformat()}")
    if deal.cabin == "business":
        parts.append("business class")
    return _BASE + quote(" ".join(parts))

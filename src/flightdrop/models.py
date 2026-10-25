from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Cabin = Literal["economy", "business"]
Verification = Literal["live", "cached"]

CABINS: tuple[Cabin, ...] = ("economy", "business")


@dataclass(frozen=True)
class Deal:
    """One candidate fare, normalized from any source.

    API deals have a destination, dates and a price. Curated feed deals
    (``curated=True``) come from human-posted deal feeds and may be missing
    any of those; they carry ``source_url`` and ``title`` instead.
    """

    origin: str
    destination: str | None
    cabin: Cabin
    price_usd: float | None
    source: str
    found_at: datetime
    depart_date: date | None = None
    return_date: date | None = None
    airline: str | None = None
    stops: int | None = None
    source_url: str | None = None
    title: str | None = None
    curated: bool = False

    @property
    def route_key(self) -> str:
        return f"{self.origin}-{self.destination or 'ANY'}-{self.cabin}"

    @property
    def deal_key(self) -> str:
        if self.source_url:
            return "url:" + hashlib.sha256(self.source_url.encode()).hexdigest()[:16]
        dep = self.depart_date.isoformat() if self.depart_date else "-"
        ret = self.return_date.isoformat() if self.return_date else "-"
        return f"{self.route_key}|{dep}|{ret}"


@dataclass
class ScoredDeal:
    deal: Deal
    score: float
    reasons: list[str] = field(default_factory=list)
    median_usd: float | None = None
    discount: float | None = None
    verification: Verification = "cached"
    live_price_usd: float | None = None
    # Cheapest fare on a different airline for the same route (looked up for ultra-low-cost
    # carriers). ``alternative_checked`` separates "none found" from "didn't look".
    alternative: Deal | None = None
    alternative_checked: bool = False

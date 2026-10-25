from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from flightdrop.models import Deal

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
BASELINE_CONFIG = FIXTURES / "baseline_config.yaml"

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 1)


def make_deal(**kw) -> Deal:
    """A default MSP -> LIS economy round trip (7 days, departing 32 days out)."""
    base = dict(
        origin="MSP",
        destination="LIS",
        cabin="economy",
        price_usd=400.0,
        source="travelpayouts",
        found_at=NOW,
        depart_date=date(2026, 12, 3),
        return_date=date(2026, 12, 10),
    )
    base.update(kw)
    return Deal(**base)

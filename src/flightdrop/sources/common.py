from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable

import requests

log = logging.getLogger("flightdrop.sources")

USER_AGENT = "flightdrop/0.1 (personal flight-deal bot)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A whole data source failed (as opposed to one bad row, which is just skipped)."""


def to_date(value: Any) -> date | None:
    """'2026-12-03' or '2026-12-03T09:00:00Z' -> date; anything else -> None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def to_datetime(value: Any) -> datetime | None:
    """ISO-8601 string -> timezone-aware UTC datetime (naive input is assumed UTC)."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 1.0,
    timeout: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """GET and decode JSON. Retries network errors, 429 and 5xx with exponential backoff.

    Never includes the request headers (which may carry a token) in error messages.
    """
    last = "no attempt made"
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last = f"network error ({type(e).__name__})"
        else:
            if resp.status_code in RETRY_STATUSES:
                last = f"HTTP {resp.status_code}"
            elif resp.status_code in (401, 403):
                raise SourceError(f"HTTP {resp.status_code}: credentials were rejected")
            elif not resp.ok:
                raise SourceError(f"HTTP {resp.status_code}")
            else:
                try:
                    return resp.json()
                except ValueError:
                    raise SourceError("response was not valid JSON") from None
        if attempt < retries:
            sleep(backoff * 2**attempt)
    raise SourceError(f"gave up after {retries + 1} attempts: {last}")

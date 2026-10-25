from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Deal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY,
  route_key TEXT NOT NULL,
  deal_key TEXT NOT NULL,
  price_usd REAL NOT NULL,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_route_time ON observations(route_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_deal_time ON observations(deal_key, observed_at);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  deal_key TEXT NOT NULL,
  price_usd REAL,
  alerted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_deal ON alerts(deal_key);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(alerted_at);
CREATE TABLE IF NOT EXISTS watch_state(
  watch_key TEXT PRIMARY KEY,
  last_price REAL NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage(
  day TEXT NOT NULL,
  service TEXT NOT NULL,
  used INTEGER NOT NULL,
  PRIMARY KEY(day, service)
);
"""

OBSERVATION_RETENTION_DAYS = 90
ALERT_RETENTION_DAYS = 180
WATCH_STATE_RETENTION_DAYS = 90     # a moving watch forgets its last price after this long


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- price history -------------------------------------------------

    def record_observation(self, deal: Deal, now: datetime) -> None:
        """Log a price sighting, unless the same deal at the same price was logged in the last day.

        Cached fares repeat unchanged across runs; skipping repeats keeps the database small
        without changing the per-deal minimum the median is built from.
        """
        if deal.price_usd is None:
            return
        self.db.execute(
            "INSERT INTO observations(route_key, deal_key, price_usd, source, observed_at)"
            " SELECT ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS (SELECT 1 FROM observations"
            "   WHERE deal_key = ? AND price_usd = ? AND observed_at >= ?)",
            (
                deal.route_key, deal.deal_key, deal.price_usd, deal.source, _iso(now),
                deal.deal_key, deal.price_usd, _iso(now - timedelta(hours=24)),
            ),
        )

    def usual_price(self, route_key: str, window_days: int, now: datetime) -> tuple[float | None, int]:
        """Median of per-deal minimum prices for a route over the window, and the sample count.

        One sample per distinct deal (route + dates) so a fare seen on every run
        doesn't dominate the median.
        """
        since = _iso(now - timedelta(days=window_days))
        rows = self.db.execute(
            "SELECT MIN(price_usd) FROM observations"
            " WHERE route_key = ? AND observed_at >= ? GROUP BY deal_key",
            (route_key, since),
        ).fetchall()
        prices = [r[0] for r in rows]
        if not prices:
            return None, 0
        return statistics.median(prices), len(prices)

    # --- alerts --------------------------------------------------------

    def last_alert_price(self, deal_key: str) -> float | None:
        """Price of the most recent alert for this deal, or None if never alerted.

        Returns -1.0 if alerted but the price was unknown (curated feed deal).
        """
        row = self.db.execute(
            "SELECT price_usd FROM alerts WHERE deal_key = ? ORDER BY id DESC LIMIT 1", (deal_key,)
        ).fetchone()
        if row is None:
            return None
        return -1.0 if row[0] is None else row[0]

    def record_alert(self, deal_key: str, price_usd: float | None, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO alerts(deal_key, price_usd, alerted_at) VALUES (?, ?, ?)",
            (deal_key, price_usd, _iso(now)),
        )
        self.db.commit()

    def alerts_since(self, since: datetime) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM alerts WHERE alerted_at >= ?", (_iso(since),)
        ).fetchone()
        return int(row[0])

    # --- moving watchlist targets ------------------------------------------

    def watch_price(self, watch_key: str) -> float | None:
        """The last price alerted for a moving watch (None if never alerted or expired)."""
        row = self.db.execute("SELECT last_price FROM watch_state WHERE watch_key = ?", (watch_key,)).fetchone()
        return None if row is None else float(row[0])

    def set_watch_price(self, watch_key: str, price: float, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO watch_state(watch_key, last_price, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(watch_key) DO UPDATE SET last_price = excluded.last_price, updated_at = excluded.updated_at",
            (watch_key, price, _iso(now)),
        )
        self.db.commit()

    # --- API budgets ---------------------------------------------------

    def usage_on(self, service: str, day: str) -> int:
        row = self.db.execute(
            "SELECT used FROM api_usage WHERE service = ? AND day = ?", (service, day)
        ).fetchone()
        return int(row[0]) if row else 0

    def usage_in_month(self, service: str, month: str) -> int:
        """``month`` is ``YYYY-MM``."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(used), 0) FROM api_usage WHERE service = ? AND day LIKE ?",
            (service, month + "-%"),
        ).fetchone()
        return int(row[0])

    def add_usage(self, service: str, day: str, n: int = 1) -> None:
        self.db.execute(
            "INSERT INTO api_usage(day, service, used) VALUES (?, ?, ?)"
            " ON CONFLICT(day, service) DO UPDATE SET used = used + excluded.used",
            (day, service, n),
        )
        self.db.commit()

    # --- housekeeping --------------------------------------------------

    def prune(self, now: datetime) -> None:
        self.db.execute(
            "DELETE FROM observations WHERE observed_at < ?",
            (_iso(now - timedelta(days=OBSERVATION_RETENTION_DAYS)),),
        )
        self.db.execute(
            "DELETE FROM alerts WHERE alerted_at < ?",
            (_iso(now - timedelta(days=ALERT_RETENTION_DAYS)),),
        )
        self.db.execute(
            "DELETE FROM watch_state WHERE updated_at < ?",
            (_iso(now - timedelta(days=WATCH_STATE_RETENTION_DAYS)),),
        )
        self.db.commit()

from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import NOW, make_deal

from flightdrop.models import LiveQuote
from flightdrop.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def obs(store, deal, when=NOW):
    store.record_observation(deal, when)


def test_usual_price_empty(store):
    assert store.usual_price("MSP-LIS-economy", 60, NOW) == (None, 0)


def test_usual_price_median_of_distinct_deals(store):
    for day, price in [(1, 500), (2, 600), (3, 700)]:
        obs(store, make_deal(depart_date=NOW.date() + timedelta(days=day), price_usd=price))
    median, n = store.usual_price("MSP-LIS-economy", 60, NOW)
    assert (median, n) == (600, 3)


def test_repeated_sightings_of_one_deal_count_once_at_their_minimum(store):
    deal = make_deal()
    for price in (500, 450, 480):
        obs(store, make_deal(price_usd=price))
    obs(store, make_deal(depart_date=NOW.date() + timedelta(days=9), price_usd=700))
    median, n = store.usual_price(deal.route_key, 60, NOW)
    assert n == 2 and median == (450 + 700) / 2


def test_usual_price_ignores_old_observations_and_other_routes(store):
    obs(store, make_deal(price_usd=500), when=NOW - timedelta(days=61))
    obs(store, make_deal(destination="LHR", price_usd=300))
    obs(store, make_deal(cabin="business", price_usd=2000))
    assert store.usual_price("MSP-LIS-economy", 60, NOW) == (None, 0)
    assert store.usual_price("MSP-LIS-economy", 90, NOW)[1] == 1


def count_obs(store):
    return store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def test_identical_sightings_within_a_day_are_stored_once(store):
    obs(store, make_deal(price_usd=500))
    obs(store, make_deal(price_usd=500), when=NOW + timedelta(hours=3))
    obs(store, make_deal(price_usd=500), when=NOW + timedelta(hours=23))
    assert count_obs(store) == 1
    obs(store, make_deal(price_usd=480), when=NOW + timedelta(hours=3))        # price changed: new row
    obs(store, make_deal(price_usd=500), when=NOW + timedelta(hours=26))       # day later: new row
    assert count_obs(store) == 3
    obs(store, make_deal(destination="LHR", price_usd=500))                    # different deal: new row
    assert count_obs(store) == 4


def test_observation_without_price_is_ignored(store):
    obs(store, make_deal(price_usd=None))
    assert store.usual_price("MSP-LIS-economy", 60, NOW) == (None, 0)


def test_alert_log_and_last_price(store):
    assert store.last_alert_price("k") is None
    store.record_alert("k", 400.0, NOW)
    store.record_alert("k", 350.0, NOW + timedelta(hours=3))
    assert store.last_alert_price("k") == 350.0


def test_alert_with_unknown_price_is_flagged(store):
    store.record_alert("feed", None, NOW)
    assert store.last_alert_price("feed") == -1.0


def test_alerts_since_counts_every_post_including_re_alerts(store):
    store.record_alert("a", 400.0, NOW - timedelta(days=1))
    store.record_alert("a", 350.0, NOW)
    store.record_alert("b", 200.0, NOW)
    assert store.alerts_since(NOW - timedelta(hours=1)) == 2
    assert store.alerts_since(NOW - timedelta(days=2)) == 3


def test_api_usage_accumulates_per_day_and_month(store):
    assert store.usage_on("serpapi", "2026-11-01") == 0
    store.add_usage("serpapi", "2026-11-01")
    store.add_usage("serpapi", "2026-11-01", 2)
    store.add_usage("serpapi", "2026-11-05")
    store.add_usage("serpapi", "2026-10-30", 4)
    store.add_usage("other", "2026-11-01", 9)
    assert store.usage_on("serpapi", "2026-11-01") == 3
    assert store.usage_in_month("serpapi", "2026-11") == 4
    assert store.usage_in_month("serpapi", "2026-10") == 4
    assert store.usage_in_month("serpapi", "2026-09") == 0


def test_prune_drops_old_rows_only(store):
    obs(store, make_deal(price_usd=500), when=NOW - timedelta(days=91))
    obs(store, make_deal(depart_date=NOW.date() + timedelta(days=9), price_usd=600))
    store.record_alert("old", 1.0, NOW - timedelta(days=181))
    store.record_alert("new", 1.0, NOW - timedelta(days=10))
    store.prune(NOW)
    assert store.usual_price("MSP-LIS-economy", 365, NOW)[1] == 1
    assert store.last_alert_price("old") is None
    assert store.last_alert_price("new") == 1.0


def test_store_persists_to_disk(tmp_path):
    path = tmp_path / "nested" / "db.sqlite"
    s = Store(path)
    s.record_alert("k", 123.0, NOW)
    s.close()
    again = Store(path)
    assert again.last_alert_price("k") == 123.0
    again.close()


# --- moving watch state ------------------------------------------------------------------------

def test_watch_price_roundtrip_and_update(store):
    assert store.watch_price("MEM-MSP-economy") is None
    store.set_watch_price("MEM-MSP-economy", 320.0, NOW)
    assert store.watch_price("MEM-MSP-economy") == 320.0
    store.set_watch_price("MEM-MSP-economy", 310.0, NOW + timedelta(hours=3))        # overwrites, no duplicate row
    assert store.watch_price("MEM-MSP-economy") == 310.0
    assert store.watch_price("MSP-MEM-economy") is None                                # per route
    assert store.db.execute("SELECT COUNT(*) FROM watch_state").fetchone()[0] == 1


def test_watch_price_expires_after_90_days(store):
    store.set_watch_price("MEM-MSP-economy", 320.0, NOW)
    store.prune(NOW + timedelta(days=89))
    assert store.watch_price("MEM-MSP-economy") == 320.0
    store.prune(NOW + timedelta(days=91))
    assert store.watch_price("MEM-MSP-economy") is None                                # starts fresh: next fare is a new baseline


# --- live check memory ---------------------------------------------------------------------------

def test_live_check_memory_matches_the_exact_fare_and_expires_after_a_day(store):
    assert store.recent_live_check("k", 400.0, NOW) is None
    store.save_live_check("k", 400.0, 430.0, "ok", NOW)
    assert store.recent_live_check("k", 400.0, NOW + timedelta(hours=23)) == ("ok", LiveQuote(price=430.0))
    assert store.recent_live_check("k", 401.0, NOW + timedelta(hours=1)) is None             # cached price changed: new fare
    assert store.recent_live_check("k", 400.0, NOW + timedelta(hours=25)) is None            # older than a day
    assert store.recent_live_check("other", 400.0, NOW) is None


def test_live_check_memory_can_be_overwritten_and_holds_no_data_results(store):
    store.save_live_check("k", 400.0, None, "nodata", NOW)
    assert store.recent_live_check("k", 400.0, NOW) == ("nodata", None)
    store.save_live_check("k", 400.0, 412.0, "ok", NOW + timedelta(hours=1))
    assert store.recent_live_check("k", 400.0, NOW + timedelta(hours=2)) == ("ok", LiveQuote(price=412.0))
    assert store.db.execute("SELECT COUNT(*) FROM live_checks").fetchone()[0] == 1


def test_old_live_checks_are_pruned(store):
    store.save_live_check("k", 400.0, 410.0, "ok", NOW)
    store.prune(NOW + timedelta(days=2))
    assert store.db.execute("SELECT COUNT(*) FROM live_checks").fetchone()[0] == 1
    store.prune(NOW + timedelta(days=4))
    assert store.db.execute("SELECT COUNT(*) FROM live_checks").fetchone()[0] == 0


# --- what we learn about each route ----------------------------------------------------------------

def test_route_baseline_starts_empty_and_stores_the_first_check(store):
    assert store.route_baseline("MSP-ROM") is None
    store.set_route_baseline("MSP-ROM", 740.0, 1.75, NOW)
    assert store.route_baseline("MSP-ROM") == (740.0, 1.75)
    assert store.route_baseline("MSP-TYO") is None


def test_route_baseline_ratio_is_smoothed_and_usual_is_kept_when_a_check_has_none(store):
    store.set_route_baseline("MSP-ROM", 740.0, 2.0, NOW)
    store.set_route_baseline("MSP-ROM", 700.0, 1.0, NOW + timedelta(days=1))
    assert store.route_baseline("MSP-ROM") == (700.0, 1.5)                    # newest usual; ratio averaged
    store.set_route_baseline("MSP-ROM", None, 1.0, NOW + timedelta(days=2))
    assert store.route_baseline("MSP-ROM") == (700.0, 1.25)                   # no insights: keep the old usual price


def test_route_baselines_expire_after_60_days(store):
    store.set_route_baseline("MSP-ROM", 740.0, 1.75, NOW)
    store.prune(NOW + timedelta(days=59))
    assert store.route_baseline("MSP-ROM") is not None
    store.prune(NOW + timedelta(days=61))
    assert store.route_baseline("MSP-ROM") is None


def test_live_check_memory_keeps_googles_insights(store):
    quote = LiveQuote(price=600.0, usual=750.0, level="low", typical_low=700.0, typical_high=800.0)
    store.save_live_check("k", 580.0, quote, "ok", NOW)
    outcome, got = store.recent_live_check("k", 580.0, NOW + timedelta(hours=1))
    assert outcome == "ok" and (got.price, got.usual, got.level) == (600.0, 750.0, "low")


def test_an_older_saved_database_gains_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE live_checks(deal_key TEXT PRIMARY KEY, cached_price REAL NOT NULL, live_price REAL,
                         outcome TEXT NOT NULL, checked_at TEXT NOT NULL);
                         INSERT INTO live_checks VALUES ('k', 400.0, 410.0, 'ok', '2026-11-01T17:00:00Z');""")
    old.commit(); old.close()
    s = Store(path)                                          # opening it migrates it
    assert s.recent_live_check("k", 400.0, NOW) == ("ok", LiveQuote(price=410.0))
    s.save_live_check("k2", 1.0, LiveQuote(price=2.0, usual=3.0, level="low"), "ok", NOW)
    assert s.recent_live_check("k2", 1.0, NOW)[1].usual == 3.0
    s.close()

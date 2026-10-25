from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import NOW, make_deal

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

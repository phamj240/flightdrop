from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml
from helpers import BASELINE_CONFIG, NOW, ROOT, make_deal

from flightdrop.config import AlertHours, ConfigError, parse_config
from flightdrop.pipeline import in_alert_hours, run_scan
from flightdrop.store import Store

WINDOW = AlertHours(time(8, 0), time(21, 30))


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# --- the window itself ---------------------------------------------------------

@pytest.mark.parametrize("hh,mm,expected", [
    (7, 59, False), (8, 0, True), (12, 0, True), (21, 30, True), (21, 31, False), (23, 0, False), (3, 0, False),
])
def test_window_boundaries_are_inclusive(hh, mm, expected):
    assert WINDOW.contains(time(hh, mm)) is expected


def test_a_window_that_wraps_past_midnight():
    night = AlertHours(time(22, 0), time(6, 0))
    assert [night.contains(time(h)) for h in (21, 22, 23, 0, 5, 6, 7)] == [False, True, True, True, True, True, False]


# --- config parsing --------------------------------------------------------------

@pytest.fixture
def raw():
    return yaml.safe_load(BASELINE_CONFIG.read_text())


def test_alert_hours_parse_and_default_to_off(raw):
    assert parse_config(raw).alert_hours is None
    raw["alert_hours"] = {"start": "08:00", "end": "21:30"}
    assert parse_config(raw).alert_hours == WINDOW


@pytest.mark.parametrize("bad,message", [
    ({"start": "08:00"}, "needs both"),
    ({"start": 480, "end": "21:30"}, "quoted"),
    ({"start": "8am", "end": "21:30"}, "24-hour"),
    ({"start": "25:00", "end": "21:30"}, "24-hour"),
    ({"start": "08:00", "end": "08:00"}, "differ"),
    ("08:00-21:30", "needs both"),
])
def test_invalid_alert_hours(raw, bad, message):
    raw["alert_hours"] = bad
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_unquoted_times_in_yaml_get_a_helpful_message(raw):
    # YAML reads an unquoted 21:30 as the number 1290, so the loader must explain the quoting rule.
    raw["alert_hours"] = yaml.safe_load("{start: 08:00, end: 21:30}")
    with pytest.raises(ConfigError, match='quoted 24-hour time like "08:00"'):
        parse_config(raw)


def test_a_bad_timezone_is_a_clear_error(raw):
    raw["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match="timezone"):
        parse_config(raw)


# --- timezone and daylight saving ---------------------------------------------------

def test_the_real_config_has_the_chosen_window(shipped_cfg):
    assert shipped_cfg.alert_hours == WINDOW and shipped_cfg.timezone == "America/Chicago"


@pytest.mark.parametrize("when,expected,why", [
    (utc(2026, 10, 31, 12, 55), False, "7:55 CDT"),
    (utc(2026, 10, 31, 13, 5), True, "8:05 CDT"),
    (utc(2026, 11, 2, 13, 55), False, "7:55 CST"),
    (utc(2026, 11, 2, 14, 5), True, "8:05 CST"),
    (utc(2026, 11, 1, 2, 30), True, "9:30 PM CDT, the last minute of Oct 31's window"),
    (utc(2026, 11, 1, 3, 1), False, "10:01 PM CDT"),
    (utc(2026, 11, 2, 3, 30), True, "9:30 PM CST"),
    (utc(2026, 11, 2, 3, 31), False, "9:31 PM CST"),
])
def test_the_window_follows_the_chicago_clock_across_the_nov_1_change(shipped_cfg, when, expected, why):
    assert in_alert_hours(when, shipped_cfg) is expected, why


def test_the_same_utc_time_moves_an_hour_when_daylight_time_ends(shipped_cfg):
    assert in_alert_hours(utc(2026, 10, 31, 13, 15), shipped_cfg) is True       # 8:15 CDT
    assert in_alert_hours(utc(2026, 11, 2, 13, 15), shipped_cfg) is False       # 7:15 CST


def test_no_window_means_always(cfg):
    assert cfg.alert_hours is None
    assert all(in_alert_hours(utc(2026, 11, 2, h), cfg) for h in range(24))


# --- the schedule in the workflow file matches the window -------------------------------

def scheduled_times():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "scan.yml").read_text())
    crons = [entry["cron"] for entry in workflow[True]["schedule"]]       # YAML reads the key `on` as True
    times = []
    for cron in crons:
        minute, hours, day, month, weekday = cron.split()
        assert re.fullmatch(r"\d+", minute) and (day, month, weekday) == ("*", "*", "*"), cron
        times += [(int(h), int(minute)) for h in hours.split(",")]
    return sorted(times)


def test_five_scans_a_day_and_every_one_is_inside_the_alert_hours_in_both_seasons(shipped_cfg):
    times = scheduled_times()
    assert len(times) == 5
    for label, day in (("daylight time", (2026, 7, 15)), ("standard time", (2026, 12, 15))):
        for hour, minute in times:
            assert in_alert_hours(utc(*day, hour, minute), shipped_cfg), (label, hour, minute)


def test_scans_avoid_the_top_of_the_hour_and_are_spread_through_the_waking_day():
    from zoneinfo import ZoneInfo
    times = scheduled_times()
    assert all(minute in (15, 45) for _, minute in times)               # GitHub is busiest at :00
    for day in ((2026, 7, 15), (2026, 12, 15)):                           # daylight and standard time
        local = sorted(utc(*day, h, m).astimezone(ZoneInfo("America/Chicago")) for h, m in times)
        stamps = [t.hour * 60 + t.minute for t in local]
        assert stamps[0] >= 8 * 60 and stamps[-1] <= 21 * 60 + 30          # all inside the waking window
        assert max(b - a for a, b in zip(stamps, stamps[1:])) <= 3 * 60   # never more than 3h apart by day


# --- the pipeline holds deals outside the window --------------------------------------------

@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


class Poster:
    def __init__(self):
        self.sent = []

    def __call__(self, scored):
        self.sent.append(scored)


def deals():
    return [make_deal(destination=d, price_usd=300.0 + i, airline="F9") for i, d in enumerate(["LIS", "LHR", "CDG"])]


def test_outside_the_window_nothing_is_posted_but_everything_is_remembered(cfg, store):
    post, verify_calls, alt_calls = Poster(), [], []
    r = run_scan(deals(), cfg, store, NOW, post=post, alerts_allowed=False,
                 verify=lambda d: verify_calls.append(d) or 300.0,
                 alternative=lambda d: alt_calls.append(d))
    assert post.sent == [] and r.alerts == [] and r.held == 3 and r.qualified == 3
    assert verify_calls == [] and alt_calls == []                          # no API calls wasted on held deals
    assert store.alerts_since(NOW - timedelta(days=1)) == 0                # not logged as sent
    assert store.usual_price("MSP-LIS-economy", 60, NOW)[1] == 1           # but the prices were recorded


def test_held_deals_go_out_at_the_next_scan_inside_the_window(cfg, store):
    post = Poster()
    run_scan(deals(), cfg, store, NOW, post=post, alerts_allowed=False)
    r = run_scan(deals(), cfg, store, NOW + timedelta(hours=6), post=post, alerts_allowed=True)
    assert r.held == 0 and sorted(s.deal.destination for s in post.sent) == ["CDG", "LHR", "LIS"]


def test_the_daily_cap_is_unaffected_by_holding(cfg, store):
    post = Poster()
    run_scan(deals(), cfg, store, NOW, post=post, alerts_allowed=False)
    r = run_scan(deals(), cfg, store, NOW + timedelta(hours=1), post=post)
    assert r.slots == 5 and len(r.alerts) == 3

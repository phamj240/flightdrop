from __future__ import annotations

import shutil

import pytest
from helpers import BASELINE_CONFIG, make_deal

from flightdrop import main as cli
from flightdrop.notify.discord import NotifyError
from flightdrop.sources import rss_deals, travelpayouts
from flightdrop.sources.common import SourceError

HOOK = "https://discord.com/api/webhooks/1/SECRET"
TOKEN = "tp-secret-value"
CONFIG = str(BASELINE_CONFIG)


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Run the CLI with a temp DB, fake sources, and a recording Discord poster."""
    state = {"deals": [make_deal(price_usd=400.0)], "posted": [], "notices": [], "fail_tp": False, "fail_rss": False,
             "alt_calls": [], "live_calls": [], "live_price": None}

    def fake_tp(cfg, token, now, session=None, sleep=None):
        if state["fail_tp"]:
            raise SourceError("down")
        return state["deals"]

    def fake_rss(cfg, now, session=None):
        if state["fail_rss"]:
            raise SourceError("down")
        return []

    def fake_alt(deal, cfg, token, now, session=None, sleep=None):
        state["alt_calls"].append(deal.destination)      # tests must never reach the live API
        return make_deal(destination=deal.destination, origin=deal.origin, price_usd=150.0, airline="AA", stops=1)

    def fake_live(deal, api_key, session=None, sleep=None):
        state["live_calls"].append((deal.destination, api_key))     # tests must never reach SerpApi
        if state.get("live_error"):
            raise SourceError("HTTP 500")
        price = state["live_price"]
        return deal.price_usd if price is None else price

    monkeypatch.setattr(cli, "live_price", fake_live)
    monkeypatch.setattr(travelpayouts, "fetch", fake_tp)
    monkeypatch.setattr(travelpayouts, "find_alternative", fake_alt)
    monkeypatch.setattr(rss_deals, "fetch", fake_rss)
    monkeypatch.setattr(cli, "_post_discord", lambda webhook, s, session: state["posted"].append((webhook, s)))
    monkeypatch.setattr(cli.discord, "post_notice", lambda webhook, msg, session=None: state["notices"].append(msg))

    db = tmp_path / "state" / "db.sqlite"
    base_env = {"TRAVELPAYOUTS_TOKEN": TOKEN, "DISCORD_WEBHOOK_URL": HOOK}

    def go(*args, env=None, config=CONFIG, now=None):
        return cli.main(["--config", config, "--db", str(db), *args], env=base_env if env is None else env, now=now)

    go.state, go.db = state, db
    return go


def test_a_normal_run_posts_and_a_second_run_does_not_repeat(run):
    assert run() == 0
    assert [(w, s.deal.destination) for w, s in run.state["posted"]] == [(HOOK, "LIS")]
    assert run() == 0
    assert len(run.state["posted"]) == 1


def test_dry_run_prints_and_changes_no_saved_state(run, capsys):
    assert run("--dry-run", env={"TRAVELPAYOUTS_TOKEN": TOKEN}) == 0      # no webhook needed
    out = capsys.readouterr().out
    assert "WOULD ALERT: MSP→LIS economy $400" in out and "google.com/travel/flights" in out
    assert run.state["posted"] == []
    assert not run.db.exists()
    assert run() == 0                                  # the real run still sends it
    assert len(run.state["posted"]) == 1


def test_dry_run_sees_existing_history_but_does_not_modify_it(run, capsys):
    run()
    before = run.db.read_bytes()
    run("--dry-run")
    assert "WOULD ALERT" not in capsys.readouterr().out        # already sent, so not a new deal
    assert run.db.read_bytes() == before


def test_missing_webhook_is_an_error_unless_dry_run(run):
    assert run(env={"TRAVELPAYOUTS_TOKEN": TOKEN}) == 1
    assert run.state["posted"] == []


def test_invalid_config_exits_1_and_tells_discord(run, tmp_path):
    bad = tmp_path / "bad.yaml"
    shutil.copy(CONFIG, bad)
    bad.write_text(bad.read_text().replace("min_days: 2", "min_days: 99"))
    assert run(config=str(bad)) == 1
    assert len(run.state["notices"]) == 1 and "min_days" in run.state["notices"][0]
    assert run.state["posted"] == []


def test_missing_config_file_is_a_clean_error(run, tmp_path):
    assert run(config=str(tmp_path / "nope.yaml")) == 1


def test_one_failing_source_is_tolerated(run):
    run.state["fail_rss"] = True
    assert run() == 0 and len(run.state["posted"]) == 1


def test_all_sources_failing_exits_1(run):
    run.state["fail_tp"] = run.state["fail_rss"] = True
    assert run() == 1


def test_nothing_to_scan_exits_1(run, tmp_path):
    cfg_text = BASELINE_CONFIG.read_text().replace("  enabled: true\n  max_age_hours", "  enabled: false\n  max_age_hours")
    off = tmp_path / "norss.yaml"
    off.write_text(cfg_text)
    assert run(env={"DISCORD_WEBHOOK_URL": HOOK}, config=str(off)) == 1      # no token and no feeds


def test_a_discord_outage_leaves_deals_unsent_for_next_time(run, monkeypatch):
    def broken(webhook, s, session):
        raise NotifyError("down")
    monkeypatch.setattr(cli, "_post_discord", broken)
    assert run() == 0
    monkeypatch.setattr(cli, "_post_discord", lambda webhook, s, session: run.state["posted"].append(s))
    assert run() == 0 and len(run.state["posted"]) == 1


def test_secrets_are_never_printed(run, capsys, caplog):
    caplog.set_level("DEBUG")
    run()
    run("--dry-run")
    run(env={"TRAVELPAYOUTS_TOKEN": TOKEN})           # missing webhook error path
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    assert text.strip(), "expected some output to check"
    assert "SECRET" not in text and TOKEN not in text


# --- next-best fare on another airline ---------------------------------------

def frontier_las():
    return make_deal(origin="DFW", destination="LAS", price_usd=33.0, airline="F9", stops=0)


def test_dry_run_shows_the_next_best_fare_for_frontier(run, capsys):
    run.state["deals"] = [frontier_las()]
    assert run("--dry-run") == 0
    out = capsys.readouterr().out
    assert "DFW→LAS economy $33 Frontier (F9)" in out
    assert "next best on another airline: $150 American (AA)" in out
    assert run.state["alt_calls"] == ["LAS"]


def test_real_run_attaches_the_next_best_fare_to_the_alert(run):
    run.state["deals"] = [frontier_las()]
    assert run() == 0
    (_, scored), = run.state["posted"]
    assert scored.alternative_checked and (scored.alternative.airline, scored.alternative.price_usd) == ("AA", 150.0)


def test_no_next_best_lookup_for_other_airlines_or_without_a_token(run):
    run.state["deals"] = [make_deal(price_usd=400.0, airline="TP")]
    run()
    assert run.state["alt_calls"] == []
    run.state["deals"] = [frontier_las()]
    run("--dry-run", env={"DISCORD_WEBHOOK_URL": HOOK, "TRAVELPAYOUTS_TOKEN": ""})     # no token -> no lookup, no crash
    assert run.state["alt_calls"] == []


# --- alert hours ---------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

MIDDAY = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)      # 12:00 noon Central standard time
NIGHT = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)       # 4:00 AM Central standard time


@pytest.fixture
def windowed(tmp_path):
    path = tmp_path / "windowed.yaml"
    path.write_text(BASELINE_CONFIG.read_text() + '\nalert_hours: {start: "08:00", end: "21:30"}\n')
    return str(path)


def test_a_scan_in_the_middle_of_the_night_holds_deals_until_morning(run, windowed, caplog):
    caplog.set_level("INFO")
    assert run(config=windowed, now=NIGHT) == 0
    assert run.state["posted"] == []
    assert "outside alert hours (08:00-21:30 America/Chicago)" in caplog.text and "1 held" in caplog.text
    assert run(config=windowed, now=MIDDAY) == 0                       # the next scan inside the window sends it
    assert len(run.state["posted"]) == 1


def test_a_manual_run_ignores_the_alert_hours(run, windowed):
    assert run("--ignore-alert-hours", config=windowed, now=NIGHT) == 0
    assert len(run.state["posted"]) == 1


def test_a_dry_run_shows_results_at_any_hour_and_still_changes_nothing(run, windowed, capsys):
    assert run("--dry-run", config=windowed, now=NIGHT) == 0
    assert "WOULD ALERT: MSP→LIS economy $400" in capsys.readouterr().out
    assert run.state["posted"] == [] and not run.db.exists()


def test_without_a_window_alerts_go_out_at_any_hour(run):
    assert run(now=NIGHT) == 0 and len(run.state["posted"]) == 1


# --- live price checks (SerpApi) ---------------------------------------------------------------

SERP = "serp-secret-key"
ENV_WITH_KEY = {"TRAVELPAYOUTS_TOKEN": TOKEN, "DISCORD_WEBHOOK_URL": HOOK, "SERPAPI_KEY": SERP}


@pytest.fixture
def required(tmp_path):
    path = tmp_path / "required.yaml"
    path.write_text(BASELINE_CONFIG.read_text().replace("verify:\n  enabled: true\n", "verify:\n  enabled: true\n  require_live: true\n"))
    return str(path)


def test_a_checked_alert_carries_the_live_google_price(run, required):
    run.state["live_price"] = 430.0
    assert run(config=required, env=ENV_WITH_KEY, now=MIDDAY) == 0
    (_, scored), = run.state["posted"]
    assert (scored.verification, scored.live_price_usd, scored.deal.price_usd) == ("live", 430.0, 400.0)
    assert run.state["live_calls"] == [("LIS", SERP)]


def test_required_live_without_a_key_holds_deals_and_sends_one_notice_a_day(run, required):
    assert run(config=required, now=MIDDAY) == 0
    assert run.state["posted"] == []
    assert len(run.state["notices"]) == 1 and "no SerpApi key" in run.state["notices"][0] and "SERPAPI_KEY" in run.state["notices"][0]
    run(config=required, now=MIDDAY.replace(hour=20))                              # same Central day: no second notice
    assert len(run.state["notices"]) == 1
    run(config=required, now=MIDDAY.replace(day=3))                                # next day: a reminder
    assert len(run.state["notices"]) == 2


def test_deals_held_for_a_missing_key_post_once_the_key_is_added(run, required):
    run(config=required, now=MIDDAY)
    assert run.state["posted"] == []
    run.state["live_price"] = 410.0
    run(config=required, env=ENV_WITH_KEY, now=MIDDAY.replace(hour=20))
    assert [s.live_price_usd for _, s in run.state["posted"]] == [410.0]


def test_a_failing_live_check_holds_deals_and_says_so(run, required):
    run.state["live_error"] = True
    assert run(config=required, env=ENV_WITH_KEY, now=MIDDAY) == 0
    assert run.state["posted"] == []
    assert len(run.state["notices"]) == 1 and "failing" in run.state["notices"][0]


def test_a_dry_run_never_spends_the_live_check_quota_and_ignores_require_live(run, required, capsys):
    assert run("--dry-run", config=required, env=ENV_WITH_KEY, now=MIDDAY) == 0
    assert "WOULD ALERT: MSP→LIS economy $400" in capsys.readouterr().out
    assert run.state["live_calls"] == [] and run.state["notices"] == []


def test_without_require_live_a_key_still_checks_and_a_missing_key_still_posts(run):
    run.state["live_price"] = 420.0
    run(env=ENV_WITH_KEY, now=MIDDAY)                                              # baseline config: require_live off
    assert run.state["posted"][0][1].live_price_usd == 420.0
    run.state["posted"].clear()
    run.state["deals"] = [make_deal(destination="LHR", price_usd=400.0)]
    run(now=MIDDAY)                                                                # no key: posts as cached
    assert [s.verification for _, s in run.state["posted"]] == ["cached"]


def test_the_serpapi_key_is_never_printed(run, required, capsys, caplog):
    caplog.set_level("DEBUG")
    run(config=required, env=ENV_WITH_KEY, now=MIDDAY)
    run("--dry-run", config=required, env=ENV_WITH_KEY, now=MIDDAY)
    captured = capsys.readouterr()
    assert SERP not in captured.out + captured.err + caplog.text

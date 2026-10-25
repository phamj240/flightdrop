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
             "alt_calls": []}

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

    monkeypatch.setattr(travelpayouts, "fetch", fake_tp)
    monkeypatch.setattr(travelpayouts, "find_alternative", fake_alt)
    monkeypatch.setattr(rss_deals, "fetch", fake_rss)
    monkeypatch.setattr(cli, "_post_discord", lambda webhook, s, session: state["posted"].append((webhook, s)))
    monkeypatch.setattr(cli.discord, "post_notice", lambda webhook, msg, session=None: state["notices"].append(msg))

    db = tmp_path / "state" / "db.sqlite"
    base_env = {"TRAVELPAYOUTS_TOKEN": TOKEN, "DISCORD_WEBHOOK_URL": HOOK}

    def go(*args, env=None, config=CONFIG):
        return cli.main(["--config", config, "--db", str(db), *args], env=base_env if env is None else env)

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

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from helpers import NOW, make_deal

from flightdrop.notify.discord import NotifyError
from flightdrop.pipeline import run_scan, start_of_today_utc
from flightdrop.sources.common import SourceError
from flightdrop.store import Store

EUROPE = ["LIS", "LHR", "CDG", "FCO", "MAD", "BCN", "AMS"]


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


class Poster:
    def __init__(self, fail_on=()):
        self.sent, self.fail_on = [], set(fail_on)

    def __call__(self, scored):
        if scored.deal.destination in self.fail_on:
            raise NotifyError("boom")
        self.sent.append(scored)

    @property
    def destinations(self):
        return [s.deal.destination for s in self.sent]


def europe_deals(prices=None):
    prices = prices or [300 + 10 * i for i in range(len(EUROPE))]
    return [make_deal(destination=d, price_usd=p) for d, p in zip(EUROPE, prices)]


def test_start_of_today_in_the_users_timezone():
    # 2026-11-01 18:00Z is noon in Chicago; the local day began at 00:00 CDT = 05:00Z.
    assert start_of_today_utc(NOW, "America/Chicago") == datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)   # still Oct 31 evening in Chicago
    assert start_of_today_utc(late, "America/Chicago") == datetime(2026, 10, 31, 5, 0, tzinfo=timezone.utc)


def test_posts_best_deals_first_and_respects_the_daily_cap(cfg, store):
    post = Poster()
    r = run_scan(europe_deals(), cfg, store, NOW, post=post)
    assert (r.candidates, r.qualified, r.new, r.slots) == (7, 7, 7, 5)
    assert len(post.sent) == 5
    assert post.destinations == ["LIS", "LHR", "CDG", "FCO", "MAD"]    # cheapest = biggest discount first


def test_a_second_scan_does_not_repeat_deals_and_stops_at_the_cap(cfg, store):
    post = Poster()
    run_scan(europe_deals(), cfg, store, NOW, post=post)
    later = NOW + timedelta(hours=3)
    r = run_scan(europe_deals(), cfg, store, later, post=post)
    # the 5 sent are repeats; the other 2 are new but the day's 5 slots are used up
    assert (r.new, r.slots, len(r.alerts)) == (2, 0, 0)
    assert len(post.sent) == 5


def test_remaining_deals_go_out_the_next_day(cfg, store):
    post = Poster()
    run_scan(europe_deals(), cfg, store, NOW, post=post)
    r = run_scan(europe_deals(), cfg, store, NOW + timedelta(days=1), post=post)
    assert sorted(a.deal.destination for a in r.alerts) == ["AMS", "BCN"]


def test_price_drop_triggers_a_re_alert_only_when_big_enough(cfg, store):
    post = Poster()
    run_scan([make_deal(price_usd=400.0)], cfg, store, NOW, post=post)
    run_scan([make_deal(price_usd=380.0)], cfg, store, NOW + timedelta(days=1), post=post)    # 5% drop
    assert len(post.sent) == 1
    run_scan([make_deal(price_usd=350.0)], cfg, store, NOW + timedelta(days=1), post=post)    # 12.5% drop
    assert [s.deal.price_usd for s in post.sent] == [400.0, 350.0]


def test_non_qualifying_deals_are_not_posted(cfg, store):
    post = Poster()
    r = run_scan([make_deal(price_usd=900.0)], cfg, store, NOW, post=post)
    assert (r.qualified, post.sent) == (0, [])


def test_history_makes_the_relative_rule_work_over_time(cfg, store):
    # Business Lisbon sits well above the $1,600 fixed target, so only the relative rule can fire.
    from datetime import date
    for i in range(6):
        d = make_deal(cabin="business", price_usd=3000.0, depart_date=date(2026, 12, 3) + timedelta(days=i),
                      return_date=date(2026, 12, 10) + timedelta(days=i))
        run_scan([d], cfg, store, NOW - timedelta(days=10 - i), post=Poster())
    post = Poster()
    run_scan([make_deal(cabin="business", price_usd=1900.0)], cfg, store, NOW, post=post)
    assert len(post.sent) == 1 and post.sent[0].reasons[0].endswith("below usual $3,000")


def test_observations_are_recorded_but_curated_deals_stay_out_of_history(cfg, store):
    feed = make_deal(destination="LIS", price_usd=300.0, curated=True, source_url="https://x.test/1",
                     title="x", depart_date=None, return_date=None)
    run_scan([make_deal(price_usd=800.0), feed], cfg, store, NOW, post=Poster())
    median, n = store.usual_price("MSP-LIS-economy", 60, NOW + timedelta(minutes=1))
    assert (median, n) == (800.0, 1)      # the $800 sighting counted; the $300 feed price did not


def test_a_failed_post_is_not_recorded_and_is_retried_next_run(cfg, store):
    flaky = Poster(fail_on={"LIS"})
    r = run_scan(europe_deals()[:2], cfg, store, NOW, post=flaky)
    assert r.failed_posts == 1 and flaky.destinations == ["LHR"]
    healthy = Poster()
    r2 = run_scan(europe_deals()[:2], cfg, store, NOW + timedelta(hours=3), post=healthy)
    assert healthy.destinations == ["LIS"]
    assert r2.failed_posts == 0


def test_a_failed_post_does_not_use_up_a_slot(cfg, store):
    cfg1 = replace(cfg, max_alerts_per_day=2)
    post = Poster(fail_on={"LIS"})
    run_scan(europe_deals(), cfg1, store, NOW, post=post)
    assert post.destinations == ["LHR", "CDG"]


def test_curated_deals_post_once(cfg, store):
    feed = make_deal(origin="DFW", destination=None, price_usd=903.0, curated=True, source="theflightdeal",
                     source_url="https://example.com/deal/1", title="t", depart_date=None, return_date=None)
    post = Poster()
    run_scan([feed], cfg, store, NOW, post=post)
    run_scan([feed], cfg, store, NOW + timedelta(hours=3), post=post)
    assert len(post.sent) == 1


# --- live verification -----------------------------------------------------

def verifier(prices, calls=None):
    def verify(deal):
        if calls is not None:
            calls.append(deal.destination)
        result = prices[deal.destination]
        if isinstance(result, Exception):
            raise result
        return result
    return verify


def test_live_price_marks_the_deal_verified(cfg, store):
    post = Poster()
    r = run_scan([make_deal(price_usd=400.0)], cfg, store, NOW, post=post, verify=verifier({"LIS": 410.0}))
    s = post.sent[0]
    assert (s.verification, s.live_price_usd, r.verified) == ("live", 410.0, 1)
    assert store.last_alert_price(s.deal.deal_key) == 410.0       # the real price is what we record


def test_a_much_higher_live_price_drops_the_deal_and_the_next_one_takes_the_slot(cfg, store):
    cfg1 = replace(cfg, max_alerts_per_day=1)
    post = Poster()
    deals = [make_deal(destination="LIS", price_usd=300.0), make_deal(destination="LHR", price_usd=350.0)]
    r = run_scan(deals, cfg1, store, NOW, post=post, verify=verifier({"LIS": 600.0, "LHR": 355.0}))
    assert post.destinations == ["LHR"] and r.dropped_after_verify == 1


def test_unverifiable_deals_are_still_posted_as_cached(cfg, store):
    post = Poster()
    deals = [make_deal(destination="LIS", price_usd=300.0), make_deal(destination="LHR", price_usd=310.0)]
    run_scan(deals, cfg, store, NOW, post=post,
             verify=verifier({"LIS": SourceError("down"), "LHR": None}))
    assert [s.verification for s in post.sent] == ["cached", "cached"]


def test_verification_stops_when_the_daily_budget_is_spent(cfg, store):
    cfg2 = replace(cfg, verify=replace(cfg.verify, daily_budget=2))
    calls, post = [], Poster()
    run_scan(europe_deals(), cfg2, store, NOW, post=post,
             verify=verifier({d: 300.0 + 10 * i for i, d in enumerate(EUROPE)}, calls))
    assert len(calls) == 2
    assert [s.verification for s in post.sent] == ["live", "live", "cached", "cached", "cached"]
    assert store.usage_on("serpapi", "2026-11-01") == 2


def test_verification_respects_the_monthly_budget(cfg, store):
    cfg_m = replace(cfg, verify=replace(cfg.verify, monthly_budget=250, daily_budget=100))
    store.add_usage("serpapi", "2026-10-20", 240)        # last month's usage must not count
    calls = []
    run_scan(europe_deals()[:3], cfg_m, store, NOW, post=Poster(),
             verify=verifier({d: 300.0 for d in EUROPE}, calls))
    assert len(calls) == 3

    store.add_usage("serpapi", "2026-11-02", 247)        # November total is now 3 + 247 = 250
    calls.clear()
    run_scan(europe_deals()[3:5], cfg_m, store, NOW + timedelta(hours=3), post=Poster(),
             verify=verifier({d: 300.0 for d in EUROPE}, calls))
    assert calls == []


# --- next-best fare on another airline ---------------------------------------

def alt_fn(result, calls=None):
    def alternative(deal):
        if calls is not None:
            calls.append((deal.destination, deal.airline))
        if isinstance(result, Exception):
            raise result
        return result
    return alternative


def test_next_best_is_attached_to_frontier_and_spirit_alerts_only(cfg, store):
    other = make_deal(destination="LHR", price_usd=300.0, airline="TP")
    frontier = make_deal(destination="LAS", price_usd=33.0, airline="F9")
    spirit = make_deal(destination="DEN", price_usd=40.0, airline="NK")
    better = make_deal(destination="LAX", origin="DFW", price_usd=140.0, airline="AA")
    calls, post = [], Poster()
    run_scan([other, frontier, spirit, better], cfg, store, NOW, post=post, alternative=alt_fn(better, calls))
    assert sorted(calls) == [("DEN", "NK"), ("LAS", "F9")]            # not for TP or AA alerts
    by_dest = {s.deal.destination: s for s in post.sent}
    assert by_dest["LAS"].alternative_checked and by_dest["LAS"].alternative is better
    assert not by_dest["LHR"].alternative_checked and by_dest["LHR"].alternative is None


def test_no_alternative_found_is_recorded_as_checked(cfg, store):
    post = Poster()
    run_scan([make_deal(destination="LAS", price_usd=33.0, airline="F9")], cfg, store, NOW,
             post=post, alternative=alt_fn(None))
    assert post.sent[0].alternative_checked and post.sent[0].alternative is None


def test_a_failed_next_best_lookup_does_not_stop_the_alert(cfg, store):
    post = Poster()
    run_scan([make_deal(destination="LAS", price_usd=33.0, airline="F9")], cfg, store, NOW,
             post=post, alternative=alt_fn(SourceError("down")))
    assert len(post.sent) == 1 and not post.sent[0].alternative_checked


def test_next_best_is_only_looked_up_for_alerts_that_will_be_posted(cfg, store):
    cfg1 = replace(cfg, max_alerts_per_day=1)
    deals = [make_deal(destination=d, price_usd=30.0 + i, airline="F9") for i, d in enumerate(["LAS", "DEN", "MIA"])]
    calls = []
    run_scan(deals, cfg1, store, NOW, post=Poster(), alternative=alt_fn(None, calls))
    assert len(calls) == 1


def test_no_lookup_when_the_feature_is_off_or_unconfigured(cfg, store):
    calls = []
    off = replace(cfg, airlines=replace(cfg.airlines, alternatives_for=()))
    run_scan([make_deal(destination="LAS", price_usd=33.0, airline="F9")], off, store, NOW,
             post=Poster(), alternative=alt_fn(None, calls))
    assert calls == []
    s2 = Store(":memory:")
    p = Poster()
    run_scan([make_deal(destination="LAS", price_usd=33.0, airline="F9")], cfg, s2, NOW, post=p)    # no alternative fn
    assert len(p.sent) == 1 and not p.sent[0].alternative_checked
    s2.close()


# --- moving watch target end to end ----------------------------------------------------------

from flightdrop.config import WatchItem  # noqa: E402


def moving_cfg(cfg, ceiling=None):
    return replace(cfg, watchlist=(WatchItem(("MEM",), "MSP", "economy", ceiling, True),))


def mem_fare(price, **kw):
    return make_deal(origin="MEM", destination="MSP", price_usd=price, **kw)


def test_a_moving_watch_ratchets_down_scan_after_scan(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    t = NOW
    for price, expect_alert in [(320, True), (320, False), (330, False), (310, True), (312, False), (309, True), (309, False), (250, True)]:
        before = len(post.sent)
        run_scan([mem_fare(float(price))], wc, store, t, post=post)
        assert (len(post.sent) - before == 1) is expect_alert, price
        t += timedelta(hours=3)
    assert [s.deal.price_usd for s in post.sent] == [320, 310, 309, 250]
    assert store.watch_price("MEM-MSP-economy") == 250.0


def test_the_first_alert_is_labelled_as_the_baseline_and_later_ones_show_the_drop(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    run_scan([mem_fare(320.0)], wc, store, NOW, post=post)
    run_scan([mem_fare(300.0, depart_date=NOW.date() + timedelta(days=40), return_date=NOW.date() + timedelta(days=45))],
             wc, store, NOW + timedelta(hours=3), post=post)
    assert post.sent[0].reasons[0].startswith("watchlist: first price seen")
    assert post.sent[1].reasons == ["watchlist: $20 lower than the previous price $320"]


def test_a_small_drop_on_the_same_dates_still_alerts_because_the_ten_percent_rule_does_not_apply(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    run_scan([mem_fare(320.0)], wc, store, NOW, post=post)
    run_scan([mem_fare(315.0)], wc, store, NOW + timedelta(hours=3), post=post)        # same route + dates, only 1.6% lower
    assert [s.deal.price_usd for s in post.sent] == [320.0, 315.0]


def test_several_fares_for_the_same_watch_in_one_scan_alert_once_at_the_cheapest(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    options = [mem_fare(330.0, stops=0), mem_fare(300.0, stops=1), mem_fare(310.0, stops=2)]
    options[1] = mem_fare(300.0, stops=1, depart_date=NOW.date() + timedelta(days=50), return_date=NOW.date() + timedelta(days=55))
    options[2] = mem_fare(310.0, stops=2, depart_date=NOW.date() + timedelta(days=60), return_date=NOW.date() + timedelta(days=65))
    run_scan(options, wc, store, NOW, post=post)
    assert [s.deal.price_usd for s in post.sent] == [300.0]
    assert store.watch_price("MEM-MSP-economy") == 300.0


def test_a_failed_post_does_not_move_the_target(cfg, store):
    wc = moving_cfg(cfg)
    run_scan([mem_fare(320.0)], wc, store, NOW, post=Poster(fail_on={"MSP"}))
    assert store.watch_price("MEM-MSP-economy") is None
    post = Poster()
    run_scan([mem_fare(320.0)], wc, store, NOW + timedelta(hours=3), post=post)         # retried as the baseline
    assert len(post.sent) == 1 and store.watch_price("MEM-MSP-economy") == 320.0


def test_a_held_deal_does_not_move_the_target_and_goes_out_later(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    run_scan([mem_fare(320.0)], wc, store, NOW, post=post, alerts_allowed=False)
    assert post.sent == [] and store.watch_price("MEM-MSP-economy") is None
    run_scan([mem_fare(320.0)], wc, store, NOW + timedelta(hours=6), post=post)
    assert len(post.sent) == 1


def test_the_target_resets_after_90_quiet_days(cfg, store):
    wc, post = moving_cfg(cfg), Poster()

    def fare_on(day_offset, price):      # trip dates relative to the scan, so the trip window never interferes
        start = (NOW + timedelta(days=day_offset)).date() + timedelta(days=20)
        return mem_fare(price, depart_date=start, return_date=start + timedelta(days=5))

    run_scan([fare_on(0, 200.0)], wc, store, NOW, post=post)                             # a one-off low fare sets the bar
    run_scan([fare_on(30, 300.0)], wc, store, NOW + timedelta(days=30), post=post)
    assert len(post.sent) == 1                                                           # 300 is not lower than 200
    run_scan([fare_on(95, 300.0)], wc, store, NOW + timedelta(days=95), post=post)       # memory expired: new baseline
    assert [s.deal.price_usd for s in post.sent] == [200.0, 300.0]


def test_a_moving_watch_uses_the_cached_price_even_when_a_live_check_says_otherwise(cfg, store):
    wc, post = moving_cfg(cfg), Poster()
    run_scan([mem_fare(300.0)], wc, store, NOW, post=post, verify=lambda d: 310.0)       # live price a bit higher
    assert post.sent[0].live_price_usd == 310.0 and store.watch_price("MEM-MSP-economy") == 300.0


def test_the_users_real_config_drives_the_same_ratchet(shipped_cfg, store):
    post = Poster()
    run_scan([mem_fare(280.0)], shipped_cfg, store, NOW, post=post)                     # domestic MSP is outside the include list
    run_scan([mem_fare(280.0)], shipped_cfg, store, NOW + timedelta(hours=3), post=post)
    run_scan([mem_fare(270.0)], shipped_cfg, store, NOW + timedelta(hours=6), post=post)
    assert [s.deal.price_usd for s in post.sent] == [280.0, 270.0]

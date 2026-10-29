"""Native venue accounts: the session PnL a native crew's loop card shows, the
dashboard views, and the overview the chat agent reads.

Fake services stand in for the venues (nothing here reaches an exchange) and the
value history is the real sqlite store in a temp data dir.
"""

import asyncio

import pytest

from condor import native_accounts as na
from condor import native_venues
from condor import portfolio_history as ph

T0 = 1_790_000_000
EXCHANGE = native_venues.get("crypto_com_exchange")
KRAKEN = native_venues.get("kraken_xstocks")
REVOLUT = native_venues.get("revolut_x")
PAPER = native_venues.get("xstocks_paper")


class FakeService:
    def __init__(self, total=None, connected=True, positions=None, error=None):
        self.total = total
        self.connected = connected
        self.positions = positions or {}
        self.error = error
        self.calls = 0

    def status(self, user_id):
        return {
            "connected": self.connected,
            "enabled": True,
            "ledger": {
                "quote": "USD",
                "cash": "20",
                "positions": self.positions,
                "uncertain_order": None,
                "synced": True,
            },
        }

    async def portfolio(self, user_id):
        self.calls += 1
        if self.error:
            raise self.error
        return {
            "connector": "x",
            "total_usd": self.total,
            "balances": [
                {"token": "USD", "total": 20, "available": 20, "usd_value": 20},
                {"token": "BTC", "total": 0.001, "available": 0.001, "usd_value": 28},
                {"token": "DUST", "total": 1, "available": 1, "usd_value": 0.001},
            ],
            "note": "Whole account.",
        }


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    na.forget()
    yield tmp_path
    na.forget()


def run(coro):
    return asyncio.run(coro)


def sample(venue, ts, total, user=1):
    # The recorder throttles by wall-clock gap; each call here uses its own stamp.
    assert ph.record(user, venue.label, total, now=ts)


def perf(venue, spans, service, now, user=1):
    return run(
        na.performance(venue, user, spans, services={venue.key: service}, now=now)
    )


# ── the registry ──


def test_agents_map_to_their_venue_and_others_to_none():
    assert native_venues.for_agent("crypto_com_exchange_trader") is EXCHANGE
    assert native_venues.for_agent("kraken_xstocks_trader") is KRAKEN
    assert native_venues.for_agent("revolut_x_trader") is REVOLUT
    assert native_venues.for_agent("xstocks_paper_trader") is PAPER
    assert native_venues.for_agent("adaptive_grid_trader") is None


def test_a_venue_added_to_the_registry_is_seen_by_the_history_recorder(monkeypatch):
    extra = native_venues.NativeVenue(
        key="demo", label="Demo", module="condor.native_venues", agents=("demo_trader",)
    )
    monkeypatch.setattr(native_venues, "VENUES", native_venues.VENUES + (extra,))
    assert native_venues.for_agent("demo_trader") is extra
    assert extra in native_venues.account_venues()
    fake = FakeService(total=12.5)
    run(ph.sample_user(1, demo=fake))
    assert ph.series(1, since=0)["Demo"][0][1] == 12.5


# ── choosing the equity that stands for a moment ──


def test_a_recent_sample_at_or_before_the_moment_wins():
    reading = na.pick((T0 - 60, 10.0), (T0 + 30, 11.0), T0)
    assert (reading.total, reading.exact) == (10.0, True)


def test_a_stale_sample_before_gives_way_to_a_nearer_one_after():
    reading = na.pick((T0 - 7200, 10.0), (T0 + 120, 11.0), T0)
    assert (reading.total, reading.exact) == (11.0, True)


def test_a_stale_sample_before_beats_a_much_later_one_after():
    reading = na.pick((T0 - 1800, 10.0), (T0 + 86400, 11.0), T0)
    assert (reading.total, reading.exact) == (10.0, False)


def test_only_one_side_is_used_and_flagged_when_far():
    after = na.pick(None, (T0 + 7200, 11.0), T0)
    assert (after.total, after.exact) == (11.0, False)
    assert na.pick(None, None, T0) is None


# ── session PnL ──


def test_a_running_session_is_the_live_equity_minus_the_equity_at_its_start():
    sample(EXCHANGE, T0 - 100, 50.0)
    sample(EXCHANGE, T0 + 600, 52.0)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(total=55.5),
        now=T0 + 3600,
    )
    assert out.unavailable == ""
    assert out.sessions[0].pnl == pytest.approx(5.5)
    assert out.sessions[0].estimated is False
    assert out.total_pnl == pytest.approx(5.5)
    assert out.since == T0


def test_a_session_that_began_before_the_first_sample_uses_it_and_says_so():
    sample(EXCHANGE, T0 + 7200, 50.0)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(total=51.0),
        now=T0 + 7300,
    )
    session = out.sessions[0]
    assert session.pnl == pytest.approx(1.0)
    assert session.estimated is True
    assert "2.0 h after" in session.note
    assert out.estimated is True


def test_no_history_is_unavailable_not_a_made_up_zero():
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(total=55.0),
        now=T0 + 10,
    )
    assert out.unavailable == "no_history"
    assert out.sessions == []


def test_the_paper_crew_has_an_equity_but_no_history_yet():
    out = perf(PAPER, [na.SessionSpan(1, T0, live=True)], FakeService(), now=T0)
    assert out.unavailable == "no_history"
    assert "Paper test only" in out.note


def test_venues_read_their_own_history_only():
    sample(EXCHANGE, T0, 100.0)
    sample(KRAKEN, T0, 10.0)
    spans = [na.SessionSpan(1, T0, live=True)]
    ex = perf(EXCHANGE, spans, FakeService(total=101.0), now=T0 + 600)
    kr = perf(KRAKEN, spans, FakeService(total=9.5), now=T0 + 600)
    assert ex.sessions[0].pnl == pytest.approx(1.0)
    assert kr.sessions[0].pnl == pytest.approx(-0.5)


def test_history_belongs_to_the_user():
    sample(EXCHANGE, T0, 100.0, user=1)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(total=101.0),
        now=T0 + 60,
        user=2,
    )
    assert out.unavailable == "no_history"


def test_a_finished_session_stops_at_its_end_and_does_not_drift_with_the_account():
    sample(EXCHANGE, T0, 50.0)
    sample(EXCHANGE, T0 + 3600, 53.0)
    sample(EXCHANGE, T0 + 7200, 49.0)
    out = perf(
        EXCHANGE,
        [
            na.SessionSpan(1, T0, end=T0 + 3600),
            na.SessionSpan(2, T0 + 7200, live=True),
        ],
        FakeService(total=60.0),
        now=T0 + 7300,
    )
    first, second = out.sessions
    assert first.pnl == pytest.approx(3.0)
    assert second.pnl == pytest.approx(11.0)
    # Overall: from the first session's start to now, the gap between sessions included.
    assert out.total_pnl == pytest.approx(10.0)


def test_when_the_account_cannot_be_read_the_latest_sample_stands_in_and_is_flagged():
    sample(EXCHANGE, T0, 50.0)
    sample(EXCHANGE, T0 + 900, 52.0)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(error=ValueError("down")),
        now=T0 + 1200,
    )
    assert out.sessions[0].pnl == pytest.approx(2.0)
    assert out.sessions[0].estimated is True
    assert "not read live" in out.sessions[0].note


def test_a_session_with_no_recorded_start_has_no_figure():
    sample(EXCHANGE, T0, 50.0)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, 0.0, live=True)],
        FakeService(total=51.0),
        now=T0 + 60,
    )
    assert out.sessions[0].pnl is None
    # With no session start the total counts from the first recorded sample.
    assert out.total_pnl == pytest.approx(1.0)
    assert out.since == T0


def test_the_live_value_is_reused_for_thirty_seconds_then_read_again():
    sample(EXCHANGE, T0, 50.0)
    service = FakeService(total=51.0)
    spans = [na.SessionSpan(1, T0, live=True)]
    perf(EXCHANGE, spans, service, now=T0 + 60)
    perf(EXCHANGE, spans, service, now=T0 + 70)
    assert service.calls == 1
    na.forget()
    perf(EXCHANGE, spans, service, now=T0 + 120)
    assert service.calls == 2


def test_open_positions_come_from_the_ledger():
    sample(EXCHANGE, T0, 50.0)
    out = perf(
        EXCHANGE,
        [na.SessionSpan(1, T0, live=True)],
        FakeService(total=51.0, positions={"BTC": {"units": "1", "cost": "2"}}),
        now=T0 + 60,
    )
    assert out.open_positions == 1


# ── dashboard views ──


def test_collect_views_skips_unconnected_and_skipped_venues_and_records_history():
    services = {
        "crypto_com_exchange": FakeService(total=48.0),
        "kraken_xstocks": FakeService(total=9.0, connected=False),
    }
    result = run(na.collect_views(1, services=services))
    assert [v["total_usd"] for v in result.views] == [48.0]
    assert ph.series(1, since=0)[ph.EXCHANGE][0][1] == 48.0

    skipped = run(
        na.collect_views(
            1,
            skip=("crypto_com_exchange",),
            services={**services, "kraken_xstocks": FakeService(total=9.0)},
        )
    )
    assert [v["total_usd"] for v in skipped.views] == [9.0]


def test_one_venue_failing_is_a_note_and_hides_nothing_else():
    services = {
        "crypto_com_exchange": FakeService(error=ValueError("down")),
        "kraken_xstocks": FakeService(total=9.0),
    }
    result = run(na.collect_views(1, services=services))
    assert [v["total_usd"] for v in result.views] == [9.0]
    assert result.notes == [EXCHANGE.unvalued_note]


# ── the chat overview ──


def test_overview_reports_each_account_with_cash_positions_ledger_and_equity_change():
    sample(EXCHANGE, T0 - 86400, 40.0)
    sample(EXCHANGE, T0 - 3600, 47.0)
    services = {
        "crypto_com_exchange": FakeService(
            total=48.0, positions={"BTC": {"units": "0.001", "cost": "27"}}
        ),
        "kraken_xstocks": FakeService(connected=False),
        "revolut_x": FakeService(connected=False),
    }
    out = run(na.overview(1, services=services, now=T0))
    assert out["total_usd"] == 48.0
    exchange, kraken, revolut, paper = out["venues"]
    assert revolut["connected"] is False and revolut["connector"] == "Revolut X"
    assert paper["paper"] is True and paper["connected"] is False
    assert exchange["connected"] is True
    assert exchange["account"]["total_usd"] == 48.0
    assert exchange["account"]["cash_usd"] == 20.0
    # Dust under a cent is not listed; positions carry their USD value.
    assert exchange["account"]["positions"] == [
        {"token": "BTC", "units": 0.001, "available": 0.001, "usd_value": 28.0}
    ]
    assert exchange["ledger"]["synced"] is True
    assert exchange["ledger"]["trading_enabled"] is True
    assert exchange["ledger"]["uncertain_order"] is False
    assert exchange["ledger"]["positions"] == [
        {"symbol": "BTC", "units": "0.001", "cost": "27"}
    ]
    change = exchange["equity_change"]
    assert change["since_first_sample"]["change_usd"] == 8.0
    assert change["last_24h"]["change_usd"] == 8.0
    assert "deposits" in change["basis"]
    assert kraken["connected"] is False and "account" not in kraken
    assert any("get_portfolio_overview" in n for n in out["notes"])


def test_overview_says_so_when_a_venue_cannot_be_valued_and_leaves_it_out_of_the_total():
    services = {
        "crypto_com_exchange": FakeService(error=ValueError("down")),
        "kraken_xstocks": FakeService(total=9.0),
        "revolut_x": FakeService(connected=False),
    }
    out = run(na.overview(1, services=services, now=T0))
    exchange, kraken, _revolut, _paper = out["venues"]
    assert "Could not be valued" in exchange["error"]
    assert "account" not in exchange
    assert out["total_usd"] == 9.0
    assert kraken["equity_change"] == {
        "available": False,
        "reason": "no_history",
        "basis": na.BASIS_LABEL,
    }


# ── Revolut X: one registry entry, a euro ledger valued in USD ──


class EuroService(FakeService):
    """Revolut X reports its ledger in EUR but values the account in USD."""

    def status(self, user_id):
        status = super().status(user_id)
        status["ledger"]["quote"] = "EUR"
        status["ledger"]["cash"] = "40"
        return status

    async def portfolio(self, user_id):
        self.calls += 1
        return {
            "connector": "Revolut X",
            "total_usd": self.total,
            "balances": [
                {"token": "EUR", "total": 40, "available": 40, "usd_value": 44.5},
                {"token": "SUI", "total": 5, "available": 5, "usd_value": 16.7},
            ],
            "note": "Euros converted at the USDC/EUR rate.",
        }


def test_revolut_x_is_registered_once_and_is_a_real_valued_account():
    assert [v.key for v in native_venues.VENUES].count("revolut_x") == 1
    assert REVOLUT.label == "Revolut X"
    assert REVOLUT.module == "condor.revolut_x"
    assert REVOLUT.agents == ("revolut_x_trader",)
    assert REVOLUT in native_venues.account_venues()
    assert "Revolut X" in ph.SOURCES and ph.REVOLUT == "Revolut X"
    assert "Revolut X could not be valued" in REVOLUT.unvalued_note


def test_revolut_x_joins_the_dashboard_views_and_the_history_once():
    services = {
        "crypto_com_exchange": FakeService(total=48.0, connected=False),
        "kraken_xstocks": FakeService(total=9.0, connected=False),
        "revolut_x": EuroService(total=61.2),
    }
    result = run(na.collect_views(1, services=services))
    assert [v["connector"] for v in result.views] == ["Revolut X"]
    assert [v["total_usd"] for v in result.views] == [61.2]
    assert [v for _, v in ph.series(1, since=0)[ph.REVOLUT]] == [61.2]
    # Hummingbot's kraken connector never stands in for Revolut X.
    kept = run(na.collect_views(1, skip=("kraken_xstocks",), services=services))
    assert [v["total_usd"] for v in kept.views] == [61.2]


def test_revolut_x_failing_is_its_own_note():
    services = {
        "crypto_com_exchange": FakeService(total=48.0),
        "kraken_xstocks": FakeService(connected=False),
        "revolut_x": FakeService(error=ValueError("no EUR/USD rate")),
    }
    result = run(na.collect_views(1, services=services))
    assert [v["total_usd"] for v in result.views] == [48.0]
    assert result.notes == [REVOLUT.unvalued_note]


def test_the_history_recorder_samples_revolut_x_from_the_registry():
    run(ph.sample_user(1, revolut_x=EuroService(total=61.2)))
    assert ph.series(1, since=0)[ph.REVOLUT][0][1] == 61.2


def test_revolut_x_equity_change_is_measured_in_usd_not_euros():
    sample(REVOLUT, T0 - 86400, 55.0)
    services = {
        "crypto_com_exchange": FakeService(connected=False),
        "kraken_xstocks": FakeService(connected=False),
        "revolut_x": EuroService(total=61.2),
    }
    out = run(na.overview(1, services=services, now=T0))
    revolut = out["venues"][2]
    assert revolut["ledger"]["quote"] == "EUR"
    assert revolut["ledger"]["cash"] == "40"
    # Cash is the EUR balance valued in USD (not 40), and counted once.
    assert revolut["account"]["cash_usd"] == 44.5
    assert [p["token"] for p in revolut["account"]["positions"]] == ["SUI"]
    assert revolut["account"]["total_usd"] == 61.2
    assert out["total_usd"] == 61.2
    assert revolut["equity_change"]["since_first_sample"]["change_usd"] == 6.2


def test_a_revolut_x_session_pnl_is_its_usd_equity_change():
    sample(REVOLUT, T0 - 100, 55.0)
    out = perf(
        REVOLUT,
        [na.SessionSpan(1, T0, live=True)],
        EuroService(total=61.2),
        now=T0 + 3600,
    )
    assert out.unavailable == ""
    assert out.sessions[0].pnl == pytest.approx(6.2)

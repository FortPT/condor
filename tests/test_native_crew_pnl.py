"""A native crew's loop shows its venue's account-equity change, not executor PnL.

The Crypto.com Exchange and Kraken xStocks crews trade outside Hummingbot, so the
executor aggregate used to answer ``no_server`` (a dash) or, with a server
configured, a meaningless ``$0.00``. These pin the replacement on the agents
routes: the right figure per session, the labelled limits, the new unavailable
reasons, and that other agents still read Hummingbot executors.
"""

import asyncio
import json
import os
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from condor import native_accounts as na
from condor import native_venues
from condor import portfolio_history as ph
from condor.crypto_com_exchange import service as exchange_service
from condor.runtime import loops as loops_module
from condor.runtime.registry_file import BOOT_ID
from condor.web.models import WebUser
from condor.web.routes import agents as routes

T0 = 1_790_000_000
EXCHANGE_KEY = "crypto_com_exchange_trader.daily_spot"
USER = WebUser(id=7, username="owner", first_name="O", role="admin")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(routes, "_PERF_CACHE", {})
    monkeypatch.setattr(routes, "_CLOSED_PERF_CACHE", OrderedDict())
    monkeypatch.setattr(loops_module.get_supervisor(), "_engines", {})
    na.forget()
    yield
    na.forget()


def fake_account(monkeypatch, total, connected=True):
    async def portfolio(user_id):
        return {"total_usd": total, "balances": [], "note": ""}

    monkeypatch.setattr(exchange_service, "portfolio", portfolio)
    monkeypatch.setattr(
        exchange_service,
        "status",
        lambda user_id: {
            "connected": connected,
            "enabled": True,
            "ledger": {"positions": {"BTC": {}, "ETH": {}}},
        },
    )


def make_session(root, num, start, state=None, ended=None):
    session = root / "sessions" / f"session_{num}"
    session.mkdir(parents=True)
    (session / "config.yml").write_text("agent_key: x\n")
    os.utime(session / "config.yml", (start, start))
    if state:
        status = {
            "state": state,
            "boot_id": BOOT_ID,
            "agent_id": f"{EXCHANGE_KEY}_{num}",
        }
        if ended:
            status["updated_at"] = ended
        (session / "status.json").write_text(json.dumps(status))
    return session


def compute(root, principal=7, run_key=EXCHANGE_KEY):
    return asyncio.run(
        routes._compute_strategy_performance(run_key, root, None, principal)
    )


def test_a_running_native_session_reads_the_account_equity_change(
    tmp_path, monkeypatch
):
    make_session(tmp_path, 1, T0 - 7200, state="stopped", ended=T0 - 3600)
    make_session(tmp_path, 2, T0, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0 - 7200)
    ph.record(7, ph.EXCHANGE, 103.0, now=T0 - 3600)
    ph.record(7, ph.EXCHANGE, 104.0, now=T0)
    fake_account(monkeypatch, 110.0)

    sessions, totals, unavailable = compute(tmp_path)

    assert unavailable == ""
    by_num = {s.session_num: s for s in sessions}
    assert by_num[1].total_pnl == pytest.approx(3.0)
    assert by_num[2].total_pnl == pytest.approx(6.0)
    assert by_num[2].pnl_basis == "account_equity"
    assert by_num[2].started_at == T0
    # Overall: first session's start to now (the gap between sessions included).
    assert totals["total_pnl"] == pytest.approx(10.0)
    assert totals["pnl_since"] == T0 - 7200
    assert totals["open_positions"] == 2.0


def test_no_recorded_equity_is_no_history_not_a_zero(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    fake_account(monkeypatch, 110.0)
    sessions, totals, unavailable = compute(tmp_path)
    assert unavailable == "no_history"
    assert totals["total_pnl"] == 0.0
    assert [(s.reading, s.total_pnl) for s in sessions] == [("missing", 0.0)]


def test_the_paper_crew_reports_that_it_has_no_ledger(tmp_path):
    make_session(tmp_path, 1, T0, state="running")
    _, totals, unavailable = compute(
        tmp_path, run_key="xstocks_paper_trader.paper_session"
    )
    assert unavailable == "no_ledger"
    assert totals["total_pnl"] == 0.0


def test_a_stopped_session_without_a_recorded_end_has_no_figure(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0)
    ph.record(7, ph.EXCHANGE, 100.0, now=T0)
    fake_account(monkeypatch, 110.0)
    sessions, _, _ = compute(tmp_path)
    assert sessions[0].reading == "missing"
    assert sessions[0].total_pnl == 0.0
    assert "end is not recorded" in sessions[0].pnl_note


def test_a_native_crew_never_asks_for_a_hummingbot_client(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0)
    fake_account(monkeypatch, 101.0)

    async def no_client(*args):
        raise AssertionError("a native crew has no Hummingbot server")

    monkeypatch.setattr(routes, "_get_client_for_strategy", no_client)
    _, totals, unavailable = compute(tmp_path)
    assert (unavailable, round(totals["total_pnl"], 2)) == ("", 1.0)


def test_the_running_figure_refreshes_after_the_cache_window(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0)
    fake_account(monkeypatch, 101.0)
    assert compute(tmp_path)[1]["total_pnl"] == pytest.approx(1.0)
    fake_account(monkeypatch, 105.0)
    # Inside the 30 s windows the figure is reused.
    assert compute(tmp_path)[1]["total_pnl"] == pytest.approx(1.0)
    routes._PERF_CACHE.clear()
    na.forget()
    assert compute(tmp_path)[1]["total_pnl"] == pytest.approx(5.0)


def test_each_user_reads_their_own_account(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0)
    fake_account(monkeypatch, 101.0)
    assert compute(tmp_path, principal=7)[2] == ""
    assert compute(tmp_path, principal=8)[2] == "no_history"


def strategy_for(tmp_path, agent_slug="crypto_com_exchange_trader"):
    return SimpleNamespace(
        slug="daily_spot",
        name="Daily spot",
        description="",
        agent_slug=agent_slug,
        home=tmp_path,
        default_config=None,
        created_by=0,
    )


def test_the_loop_card_carries_the_equity_change_and_how_to_read_it(
    tmp_path, monkeypatch
):
    make_session(tmp_path, 1, T0 + 7200, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0 + 7100)
    fake_account(monkeypatch, 98.5)
    monkeypatch.setattr(routes, "_get_engines_for", lambda a, s: [])
    monkeypatch.setattr(routes, "_strategy_principal", lambda s, u: 7)

    summary = asyncio.run(routes._build_strategy_summary(strategy_for(tmp_path), USER))

    assert summary.unavailable == ""
    assert summary.latest_session_pnl == pytest.approx(-1.5)
    assert summary.total_pnl == pytest.approx(-1.5)
    assert summary.pnl_basis == "account_equity"
    assert summary.pnl_estimated is False
    assert summary.open_positions == 2


def test_the_loop_card_says_no_history_rather_than_a_zero(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    fake_account(monkeypatch, 98.5)
    monkeypatch.setattr(routes, "_get_engines_for", lambda a, s: [])
    monkeypatch.setattr(routes, "_strategy_principal", lambda s, u: 7)
    summary = asyncio.run(routes._build_strategy_summary(strategy_for(tmp_path), USER))
    assert summary.unavailable == "no_history"
    assert summary.latest_session_pnl == 0.0


def test_the_session_detail_serves_the_same_equity_row(tmp_path, monkeypatch):
    make_session(tmp_path, 1, T0, state="running")
    ph.record(7, ph.EXCHANGE, 100.0, now=T0)
    fake_account(monkeypatch, 104.0)
    monkeypatch.setattr(
        routes, "_get_strategy", lambda slug, sslug: strategy_for(tmp_path)
    )
    monkeypatch.setattr(routes, "_strategy_principal", lambda s, u: 7)
    out = asyncio.run(
        routes.get_session_executors(
            "crypto_com_exchange_trader", "daily_spot", 1, USER
        )
    )
    assert out["executors"] == []
    assert out["performance"]["total_pnl"] == pytest.approx(4.0)
    assert out["performance"]["pnl_basis"] == "account_equity"
    assert out["unavailable"] == ""


def test_other_agents_keep_the_executor_path(tmp_path, monkeypatch):
    assert native_venues.for_agent("adaptive_grid_trader") is None
    calls = []

    async def no_server(strategy_dir, default_config, principal):
        calls.append(principal)
        return None, "", "no_server"

    monkeypatch.setattr(routes, "_get_client_for_strategy", no_server)
    make_session(tmp_path, 1, T0)
    _, _, unavailable = compute(tmp_path, run_key="adaptive_grid_trader.grid")
    assert unavailable == "no_server"
    assert calls == [7]


def summary(**over):
    base = dict(
        slug="s",
        name="S",
        description="",
        status="running",
        pnl_basis="account_equity",
        latest_session_pnl=2.0,
        total_pnl=5.0,
        open_positions=1,
        pnl_since=T0,
    )
    return routes.StrategySummary(**{**base, **over})


def test_two_loops_on_one_account_are_not_added_twice():
    rolled = routes._aggregate_strategy_perf(
        [
            summary(),
            summary(
                slug="t", latest_session_pnl=3.0, total_pnl=9.0, pnl_since=T0 - 100
            ),
        ]
    )
    assert rolled["pnl_basis"] == "account_equity"
    # One account, counted once: the longer window of the two running loops.
    assert (rolled["latest_session_pnl"], rolled["total_pnl"]) == (3.0, 9.0)


def test_an_unpriced_loop_is_not_the_one_counted():
    rolled = routes._aggregate_strategy_perf(
        [
            summary(unavailable="no_history", total_pnl=0.0, latest_session_pnl=0.0),
            summary(),
        ]
    )
    assert rolled["total_pnl"] == 5.0


def test_executor_strategies_still_add_up():
    plain = routes.StrategySummary(
        slug="p",
        name="P",
        description="",
        status="idle",
        latest_session_pnl=1.0,
        total_pnl=2.0,
    )
    rolled = routes._aggregate_strategy_perf([plain, plain, summary()])
    assert rolled["total_pnl"] == 9.0
    assert rolled["pnl_basis"] == "account_equity"

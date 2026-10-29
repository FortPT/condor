"""What a native venue earns and loses: the order replay, today/since-start with
deposits netted out, and the four ``/performance`` routes.

Fake services and a temp data dir: no exchange is contacted.
"""

import asyncio
from decimal import Decimal

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor import native_accounts as na
from condor import native_flows as nf
from condor import native_performance as perf
from condor import native_venues
from condor import portfolio_history as ph
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import crypto_com_exchange as crypto_routes
from condor.web.routes import revolut_x as revolut_routes
from condor.web.routes import trading212 as t212_routes

#: A UTC midnight, so "today" has a known start.
DAY = 1_789_948_800
NOW = DAY + 12 * 3600
USER = 1
REVOLUT = native_venues.get("revolut_x")
EXCHANGE = native_venues.get("crypto_com_exchange")


def test_the_day_constant_is_a_utc_midnight():
    assert DAY % 86400 == 0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    na.forget()
    perf.forget()
    yield
    na.forget()
    perf.forget()


def run(coro):
    return asyncio.run(coro)


_clock = {"t": 0}


def order(side, symbol, quantity, value, fee="0", currency="USD", **extra):
    _clock["t"] += 100
    return {
        "time": float(DAY + _clock["t"]),
        "side": side,
        "symbol": symbol,
        "order_id": f"o{_clock['t']}",
        "status": "FILLED",
        "quantity": str(quantity),
        "value": str(value),
        "fee": str(fee),
        "fee_currency": currency,
        **extra,
    }


def ledger(orders, positions=None, quote="USD", cash="0"):
    return {
        "quote": quote,
        "cash": cash,
        "positions": positions or {},
        "orders": orders,
        "synced": True,
        "uncertain_order": None,
    }


def pnl(result, index=0):
    return result.sells[index].pnl


# ── the replay: cost basis, fees, wins and losses ──


def test_a_round_trip_realises_proceeds_minus_cost_after_fees():
    orders = [
        order("buy", "BTC", 10, 100, fee=1),
        order("sell", "BTC", 10, 120, fee="1.2"),
    ]
    result = perf.replay("crypto_com_exchange", ledger(orders))
    (sell,) = result.sells
    # cost 101 (the fee is part of it), proceeds 118.8
    assert sell.pnl == Decimal("17.8")
    assert sell.closed_position is True
    block = perf.realized_block(result, "USD")
    assert block["realized"]["value"] == 17.8
    assert block["fees"]["value"] == 2.2
    assert block["trades"]["round_trips_closed"] == 1


def test_a_partial_sell_realises_only_its_share_of_the_average_cost():
    orders = [
        order("buy", "BTC", 10, 100),
        order("sell", "BTC", 4, 60),
        order("sell", "BTC", 6, 54),
    ]
    final = ledger(orders)
    result = perf.replay("crypto_com_exchange", final)
    first, second = result.sells
    assert (first.pnl, first.closed_position) == (Decimal(20), False)
    assert (second.pnl, second.closed_position) == (Decimal(-6), True)
    block = perf.realized_block(result, "USD")
    assert block["realized"]["value"] == 14.0
    trades = block["trades"]
    assert (trades["wins"], trades["losses"], trades["win_rate"]) == (1, 1, 0.5)
    assert trades["best"]["pnl"] == 20.0 and trades["worst"]["pnl"] == -6.0
    assert trades["filled_orders"] == 3 and trades["buys"] == 1


def test_average_cost_blends_two_buys_before_a_sell():
    orders = [
        order("buy", "SOL", 1, 10),
        order("buy", "SOL", 1, 20),
        order("sell", "SOL", 1, 17),
    ]
    remaining = {"SOL": {"units": "1", "cost": "15"}}
    result = perf.replay("crypto_com_exchange", ledger(orders, remaining))
    assert pnl(result) == Decimal(2)  # sold at 17 against an average of 15


def test_a_fee_paid_in_the_coin_reduces_the_units_and_counts_as_a_fee():
    orders = [
        order("buy", "SOL", 10, 100, fee="0.1", currency="SOL"),
        order("sell", "SOL", "9.9", 110, fee="0.11"),
    ]
    result = perf.replay("crypto_com_exchange", ledger(orders))
    # 9.9 units cost 100; proceeds 110 - 0.11
    assert pnl(result) == Decimal("9.89")
    # the coin fee is worth 0.1 * 10 (the fill price) in USD, plus the sale's 0.11
    assert perf.realized_block(result, "USD")["fees"]["value"] == 1.11


def test_a_fee_in_a_third_currency_is_not_guessed():
    orders = [
        order("buy", "SOL", 1, 10, fee="0.5", currency="CRO"),
        order("sell", "SOL", 1, 12),
    ]
    block = perf.realized_block(
        perf.replay("crypto_com_exchange", ledger(orders)), "USD"
    )
    assert block["fees"]["value"] == 0.0
    assert block["fees"]["unconverted"] == 1 and block["fees"]["complete"] is False


def test_an_estimated_fee_is_labelled_an_estimate():
    orders = [
        order("buy", "BTC", 1, 100, fee="0.5", currency="EUR", fee_source="estimate"),
        order("sell", "BTC", 1, 101, fee="0.5", currency="EUR", fee_source="reported"),
    ]
    block = perf.realized_block(
        perf.replay("revolut_x", ledger(orders, quote="EUR")), "EUR"
    )
    assert block["fees"]["value"] == 1.0
    assert block["fees"]["estimated"] == 0.5 and block["fees"]["is_estimate"] is True


def test_a_balance_correction_removes_units_but_not_cost():
    orders = [
        order("buy", "ETH", 10, 100, fee="0.5", currency="ETH", fee_source="estimate"),
    ]
    orders[0]["balance_correction"] = {
        "units": {"ledger": "9.5", "account_available": "9.4", "set_to": "9.4"}
    }
    orders.append(order("sell", "ETH", "9.4", 110, fee=1, currency="EUR"))
    result = perf.replay("revolut_x", ledger(orders, quote="EUR"))
    assert pnl(result) == Decimal(9)  # 110 - 1 - 100
    assert result.assets["ETH"].mismatch == ""


def test_a_loss_is_negative_and_counted():
    orders = [order("buy", "BTC", 1, 100), order("sell", "BTC", 1, 90)]
    block = perf.realized_block(
        perf.replay("trading212", ledger(orders, quote="EUR")), "EUR"
    )
    assert block["realized"]["value"] == -10.0
    trades = block["trades"]
    assert (trades["wins"], trades["losses"], trades["best"]) == (0, 1, None)
    assert trades["worst"]["pnl"] == -10.0 and trades["worst"]["pnl_pct"] == -10.0


def test_no_sells_is_zero_realized_and_no_win_rate():
    result = perf.replay(
        "revolut_x",
        ledger(
            [order("buy", "XRP", 5, 10, currency="EUR")],
            {"XRP": {"units": "5", "cost": "10"}},
            quote="EUR",
        ),
    )
    block = perf.realized_block(result, "EUR")
    assert block["realized"]["value"] == 0.0 and block["realized"]["sells"] == 0
    assert block["trades"]["win_rate"] is None and block["trades"]["best"] is None


# ── adoption at a sync and a truncated order list ──


def test_a_position_adopted_at_a_sync_has_its_cost_solved_from_the_ledger():
    # 4 ETH adopted at a bid of 25 (cost 100), then 1 sold for 30.
    orders = [order("sell", "ETH", 1, 30)]
    final = ledger(orders, {"ETH": {"units": "3", "cost": "75"}})
    result = perf.replay("revolut_x", final)
    asset = result.assets["ETH"]
    assert asset.opening_units == Decimal(4)
    assert asset.opening_cost == Decimal(100)
    assert asset.opening_origin == "adopted_at_sync"
    assert asset.partial_history is False
    assert pnl(result) == Decimal(5)


def test_a_truncated_order_list_is_flagged_and_still_priced():
    orders = [order("buy", "ETH", "0.01", "0.25") for _ in range(199)]
    orders.append(order("sell", "ETH", 1, 30))
    final = ledger(orders, {"ETH": {"units": "4.99", "cost": "124.75"}})
    result = perf.replay("revolut_x", final)
    assert len(final["orders"]) == perf.ORDER_CAP
    asset = result.assets["ETH"]
    assert asset.opening_origin == "before_recorded_orders"
    assert asset.partial_history is True
    assert asset.opening_cost == Decimal(100)
    assert float(pnl(result)) == pytest.approx(5.0)
    block = perf.realized_block(result, "USD")
    assert block["realized"]["partial_history"] is True
    assert block["realized"]["partial_assets"] == ["ETH"]


def test_an_adopted_lot_that_was_sold_out_has_no_recoverable_cost():
    # The adopted lot is sold completely (cost gone with the position); a later
    # round trip is exact again.
    orders = [
        order("sell", "ETH", 4, 90),
        order("buy", "ETH", 1, 30),
        order("sell", "ETH", 1, 33),
    ]
    result = perf.replay("revolut_x", ledger(orders))
    first, second = result.sells
    assert first.pnl is None and "not on record" in first.reason
    assert second.pnl == Decimal(3)
    block = perf.realized_block(result, "USD")
    assert block["realized"]["value"] == 3.0
    assert block["realized"]["complete"] is False
    assert block["realized"]["unknown_sells"] == 1
    assert block["trades"]["wins"] == 1 and block["trades"]["sells"] == 2


def test_a_lot_almost_entirely_sold_is_not_solved_from_rounded_ledger_cost():
    # 4 ETH adopted, 3.9998 sold: 0.005 % of the lot is left, so the ledger's
    # eight-decimal cost cannot recover what the lot cost.
    orders = [order("sell", "ETH", "3.9998", 100)]
    final = ledger(orders, {"ETH": {"units": "0.0002", "cost": "0.005"}})
    result = perf.replay("revolut_x", final)
    assert result.assets["ETH"].opening_cost is None
    assert result.sells[0].pnl is None


def test_only_unknown_sells_gives_no_realized_figure():
    result = perf.replay("revolut_x", ledger([order("sell", "ETH", 4, 90)]))
    block = perf.realized_block(result, "USD")
    assert block["realized"]["value"] is None
    assert "not on record" in block["realized"]["reason"]


def test_orders_that_do_not_fit_the_ledger_are_flagged_not_guessed():
    # An order bought a coin the ledger does not hold and nothing explains it.
    result = perf.replay("revolut_x", ledger([order("buy", "ETH", 1, 10)], quote="EUR"))
    assert result.assets["ETH"].mismatch == "orders and ledger positions disagree"


def test_an_unfilled_or_malformed_order_is_ignored():
    zero = order("buy", "BTC", 0, 0)
    zero["status"] = "CANCELED"
    junk = {"side": "buy", "symbol": "BTC", "quantity": "x", "time": 5}
    result = perf.replay("revolut_x", ledger([zero, junk], quote="EUR"))
    assert result.fills == [] and result.assets == {}


def test_trading212_round_trip_and_an_adopted_stock_beside_it():
    orders = [
        order("buy", "AAPL", "0.02", "5.94", fee="0.01", currency="EUR"),
        order("sell", "AAPL", "0.02", "5.92", fee="0.01", currency="EUR"),
    ]
    final = ledger(
        orders, {"MRNA": {"units": "0.4", "cost": "9.10"}}, quote="EUR", cash="3"
    )
    result = perf.replay("trading212", final)
    assert list(result.assets) == ["AAPL"]  # MRNA has no orders: nothing to realise
    assert pnl(result) == Decimal("-0.04")  # 5.91 - 5.95
    block = perf.realized_block(result, "EUR")
    assert block["trades"]["round_trips_closed"] == 1
    assert block["fees"]["value"] == 0.02


# ── the report: today and since start, deposits never count as profit ──


class FakeService:
    def __init__(
        self,
        *,
        total=110.0,
        equity="130",
        orders=None,
        positions=None,
        connected=True,
        valuation_error=None,
        quote="USD",
    ):
        self.total = total
        self.equity = equity
        self.valuation_error = valuation_error
        self.valuations = 0
        self._status = {
            "connected": connected,
            "enabled": True,
            "ledger": ledger(
                orders or [],
                positions or {"BTC": {"units": "1", "cost": "100"}},
                quote=quote,
                cash="20",
            ),
        }

    def status(self, user_id):
        return self._status

    async def valuation(self, user_id):
        self.valuations += 1
        if self.valuation_error:
            raise self.valuation_error
        return {"equity": self.equity}

    async def portfolio(self, user_id):
        return {"total_usd": self.total, "balances": []}


def get_report(service, now=NOW, venue=EXCHANGE):
    return run(perf.report(venue.key, USER, services={venue.key: service}, now=now))


def sample(ts, total, venue=EXCHANGE):
    assert ph.record(USER, venue.label, total, now=ts)


def test_today_is_the_change_since_the_utc_day_start_minus_deposits():
    sample(DAY - 3600, 90.0)
    sample(DAY + 60, 100.0)
    # A deposit of 8 USD during the day: the account grew 100 -> 110, 2 was earned.
    nf.add_flow(
        USER,
        EXCHANGE.key,
        amount=8,
        currency="USD",
        ts=DAY + 6 * 3600,
        usd_value=8,
        source="manual",
    )
    out = get_report(FakeService(total=110.0))
    today = out["today"]
    assert today["value"] == pytest.approx(2.0)
    assert today["since"] == DAY and today["partial"] is False
    assert today["flows_usd"] == pytest.approx(8.0)
    # Since the first recorded sample (90): +20 grown, 8 deposited.
    assert out["since_start"]["value"] == pytest.approx(12.0)
    assert out["since_start"]["since"] == DAY - 3600
    assert out["account_equity_usd"] == 110.0
    assert today["currency"] == "USD"


def test_without_a_confirmed_deposit_the_untracked_wording_is_visible():
    sample(DAY + 60, 100.0)
    out = get_report(FakeService(total=104.0))
    assert out["today"]["value"] == pytest.approx(4.0)
    assert "not tracked" in out["today"]["note"]


def test_an_unconfirmed_deposit_is_named_not_subtracted():
    sample(DAY + 60, 100.0)
    nf.add_flow(
        USER,
        EXCHANGE.key,
        amount=8,
        currency="USD",
        ts=DAY + 6 * 3600,
        usd_value=8,
        source="detected",
        status="pending",
    )
    out = get_report(FakeService(total=108.0))
    assert out["today"]["value"] == pytest.approx(8.0)
    assert "unconfirmed" in out["today"]["note"]


def test_a_history_that_starts_after_midnight_says_since_when():
    sample(DAY + 5 * 3600, 100.0)
    out = get_report(FakeService(total=103.0))
    today = out["today"]
    assert today["partial"] is True and today["since"] == DAY + 5 * 3600
    assert today["value"] == pytest.approx(3.0)
    assert "History starts at" in today["note"]


def test_no_history_is_null_with_a_reason_not_zero():
    out = get_report(FakeService())
    for key in ("today", "since_start"):
        assert out[key]["value"] is None
        assert out[key]["reason"] == "No equity history recorded yet"


def test_a_baseline_too_far_from_midnight_is_not_used():
    sample(DAY - 5 * 3600, 100.0)
    sample(DAY + 11 * 3600, 105.0)
    out = get_report(FakeService(total=106.0))
    assert out["today"]["value"] is None
    assert "No equity sample near the start" in out["today"]["reason"]
    # The whole history is still fine for the since-start figure.
    assert out["since_start"]["value"] == pytest.approx(6.0)


def test_the_report_carries_realized_unrealized_fees_and_trades():
    orders = [
        order("buy", "BTC", 1, 100, fee=1),
        order("buy", "ETH", 2, 20),
        order("sell", "ETH", 2, 25, fee="0.5"),
    ]
    sample(DAY + 60, 100.0)
    service = FakeService(orders=orders, equity="130")
    out = get_report(service)
    assert out["available"] is True and out["connected"] and out["currency"] == "USD"
    # equity 130 = cash 20 + BTC at the bid 110; BTC cost 101 ... positions cost 100
    assert out["equity"]["now"] == 130.0
    assert out["unrealized"]["value"] == pytest.approx(130 - 20 - 100)
    assert out["realized"]["value"] == pytest.approx(4.5)  # 25 - 0.5 - 20
    assert out["fees"]["value"] == 1.5
    assert out["trades"]["filled_orders"] == 3 and out["trades"]["wins"] == 1
    (sell_row,) = out["orders"]
    assert sell_row["symbol"] == "ETH" and sell_row["pnl"] == 4.5
    assert sell_row["pnl_pct"] == pytest.approx(22.5)


def test_a_failed_valuation_leaves_the_recorded_figures_and_names_the_reason():
    sample(DAY + 60, 100.0)
    service = FakeService(
        valuation_error=ValueError("Reconcile the uncertain order before valuing")
    )
    out = get_report(service)
    assert out["equity"]["now"] is None and out["unrealized"]["value"] is None
    assert out["unrealized"]["reason"].startswith("Reconcile the uncertain order")
    assert out["today"]["value"] == pytest.approx(10.0)
    assert out["realized"]["value"] == 0.0


def test_an_unconnected_venue_still_reports_its_ledger():
    out = get_report(FakeService(connected=False, orders=[order("buy", "BTC", 1, 100)]))
    assert out["connected"] is False
    assert out["today"]["reason"] == "Not connected"
    assert out["equity"]["reason"] == "Not connected"
    assert out["trades"]["filled_orders"] == 1


def test_an_unreadable_ledger_is_a_state_not_an_error():
    class Broken(FakeService):
        def status(self, user_id):
            raise OSError("disk")

    out = get_report(Broken())
    assert out["available"] is False and "could not be read" in out["reason"]


def test_a_report_is_reused_for_a_few_seconds():
    sample(DAY + 60, 100.0)
    service = FakeService()
    first = run(perf.report(EXCHANGE.key, USER, services={EXCHANGE.key: service}))
    second = run(perf.report(EXCHANGE.key, USER, services={EXCHANGE.key: service}))
    assert first is second and service.valuations == 1


def test_all_three_venues_are_covered_by_the_registry():
    assert [v.key for v in native_venues.account_venues()] == [
        "crypto_com_exchange",
        "revolut_x",
        "trading212",
    ]


# ── the routes ──


@pytest.fixture
def client():
    app = FastAPI()
    for module in (crypto_routes, revolut_routes, t212_routes):
        app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=USER, username="u", first_name="U", role="user"
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "path,key,label",
    [
        ("/crypto-exchange/performance", "crypto_com_exchange", "Crypto.com"),
        ("/revolut-x/performance", "revolut_x", "Revolut X"),
        ("/trading212/performance", "trading212", "Trading 212"),
    ],
)
def test_each_venue_route_answers_for_an_unconnected_account(client, path, key, label):
    res = client.get(path)
    assert res.status_code == 200
    body = res.json()
    assert (body["venue"], body["label"], body["connected"]) == (key, label, False)
    assert body["today"]["reason"] == "Not connected"
    assert body["realized"]["value"] == 0.0 and body["trades"]["filled_orders"] == 0


def test_a_route_reads_the_real_ledger_and_values_it(client, monkeypatch):
    from condor.revolut_x import service as revolut_service

    fake = FakeService(
        quote="EUR", orders=[order("buy", "BTC", 1, 100, currency="EUR")]
    )
    monkeypatch.setattr(revolut_service, "status", fake.status)
    monkeypatch.setattr(revolut_service, "valuation", fake.valuation)
    monkeypatch.setattr(revolut_service, "portfolio", fake.portfolio)
    body = client.get("/revolut-x/performance").json()
    assert body["currency"] == "EUR" and body["connected"] is True
    assert body["equity"]["now"] == 130.0
    assert body["trades"]["buys"] == 1

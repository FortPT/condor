"""Deposits and withdrawals of the native venues: the store, the fetchers' parsers,
detection, and the PnL maths that subtracts what the owner confirmed.

Nothing here reaches an exchange: every service is a fake, and the response
fixtures are fabricated from each venue's documentation (see
``docs/native-flows.md`` for what is and is not verified against a live account).
"""

import asyncio
import sqlite3
import time
from decimal import Decimal

import pytest

from condor import native_accounts as na
from condor import native_flow_fetchers as fetchers
from condor import native_flows as nf
from condor import native_venues
from condor import portfolio_history as ph

T0 = 1_790_000_000
EXCHANGE = native_venues.get("crypto_com_exchange")
REVOLUT = native_venues.get("revolut_x")
USER = 1


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    na.forget()
    yield tmp_path
    na.forget()


def run(coro):
    return asyncio.run(coro)


def add(venue="crypto_com_exchange", amount="5", usd=None, ts=T0, **over):
    args = dict(
        amount=amount,
        currency="USD",
        ts=ts,
        usd_value=amount if usd is None else usd,
        source="manual",
        status="confirmed",
        now=T0 + 10_000,
    )
    args.update(over)
    return nf.add_flow(USER, venue, **args)


# ── store ──


def test_an_api_row_is_idempotent_by_venue_and_external_id():
    first, created = add(source="api", external_id="deposit:1")
    again, created_again = add(source="api", external_id="deposit:1", amount="99")
    assert created and not created_again
    assert again["id"] == first["id"] and again["amount"] == "5"
    assert len(nf.list_flows(USER, "crypto_com_exchange")) == 1
    # The same id on another venue is another flow.
    _, other = add("revolut_x", source="api", external_id="deposit:1")
    assert other


def test_a_pending_api_row_is_promoted_once_confirmed_and_a_dismissal_sticks():
    pending, _ = add(source="api", status="pending", external_id="d:1")
    promoted, created = add(
        source="api", status="confirmed", external_id="d:1", ts=T0 + 5
    )
    assert not created
    assert promoted["id"] == pending["id"]
    assert (promoted["status"], promoted["ts"]) == ("confirmed", T0 + 5)

    other, _ = add(source="api", external_id="d:2")
    nf.set_status(USER, other["id"], "dismissed")
    again, _ = add(source="api", external_id="d:2")
    assert again["status"] == "dismissed"


def test_input_is_validated_and_the_usd_value_takes_the_amounts_sign():
    for bad in ("0", "abc", "NaN", "Infinity", "1e30", True):
        with pytest.raises(ValueError):
            add(amount=bad)
    with pytest.raises(ValueError):
        add(currency="not a code")
    with pytest.raises(ValueError):
        add("nope")
    out, _ = add(amount="-3", usd="3.5")
    assert out["usd_value_at_time"] == -3.5
    out, _ = add(amount="3", usd="-3.5")
    assert out["usd_value_at_time"] == 3.5
    out, _ = add(amount="3", usd=None)
    assert out["usd_value_at_time"] == 3.0
    out, _ = nf.add_flow(
        USER,
        "revolut_x",
        amount="1",
        currency="eur",
        ts=T0,
        usd_value=None,
        source="manual",
    )
    assert out["usd_value_at_time"] is None and out["currency"] == "EUR"


def test_times_parse_dates_and_refuse_the_future():
    later = 1_800_000_000
    assert nf.parse_ts("2026-10-28", now=later) == pytest.approx(1_793_188_800.0)
    assert nf.parse_ts(None, now=T0) == T0
    assert nf.parse_ts("2026-10-28T10:00:00Z", now=later) == pytest.approx(
        1_793_181_600
    )
    with pytest.raises(ValueError):
        nf.parse_ts("tomorrow", now=T0)
    with pytest.raises(ValueError):
        nf.parse_ts(T0 + 86400, now=T0)
    with pytest.raises(ValueError):
        nf.parse_ts(-5, now=T0)


def test_confirm_dismiss_and_delete_rules():
    detected, _ = add(source="detected", status="pending")
    assert nf.set_status(USER, detected["id"], "confirmed")["status"] == "confirmed"
    assert nf.set_status(USER, detected["id"], "dismissed")["status"] == "dismissed"
    assert nf.list_flows(USER, "crypto_com_exchange") == []
    assert len(nf.list_flows(USER, "crypto_com_exchange", include_dismissed=True)) == 1
    with pytest.raises(ValueError):
        nf.delete_manual(USER, detected["id"])
    manual, _ = add()
    nf.delete_manual(USER, manual["id"])
    with pytest.raises(KeyError):
        nf.set_status(USER, manual["id"], "confirmed")
    with pytest.raises(KeyError):
        nf.set_status(USER, "not-an-id", "confirmed")
    with pytest.raises(ValueError):
        nf.set_status(USER, detected["id"], "pending")


def test_flows_are_per_user_and_reads_create_no_file(data_dir):
    add()
    assert nf.list_flows(2, "crypto_com_exchange") == []
    assert nf.summary(2, "crypto_com_exchange")["confirmed_count"] == 0
    assert not (data_dir / "native_flows" / "2").exists()
    with pytest.raises(ValueError):
        nf.list_flows(0)


# ── the maths ──


def test_the_window_is_open_at_the_start_and_closed_at_the_end():
    add(amount="1", ts=T0)
    add(amount="2", ts=T0 + 100)
    add(amount="-0.5", ts=T0 + 200)
    window = nf.window_net(USER, "crypto_com_exchange", T0, T0 + 200)
    assert (window.net_usd, window.count) == (pytest.approx(1.5), 2)
    # A flow exactly at the next window's start belongs to the previous one only.
    assert nf.window_net(USER, "crypto_com_exchange", T0 + 200, T0 + 300).count == 0
    assert nf.window_net(USER, "crypto_com_exchange", T0 + 5, T0 + 5).count == 0


def test_only_confirmed_priced_flows_are_subtracted():
    add(amount="10", ts=T0 + 1)
    add(amount="7", ts=T0 + 2, status="pending", source="detected")
    add(amount="3", usd="", ts=T0 + 3, usd_value=None)
    dismissed, _ = add(amount="4", ts=T0 + 4)
    nf.set_status(USER, dismissed["id"], "dismissed")
    window = nf.window_net(USER, "crypto_com_exchange", T0, T0 + 100)
    assert (window.net_usd, window.count, window.unpriced) == (10.0, 2, 1)
    info = nf.summary(USER, "crypto_com_exchange")
    assert info["recorded_net_flows_usd"] == 10.0
    assert (info["confirmed_count"], info["pending_count"]) == (2, 1)
    assert info["pending_net_usd"] == 7.0 and info["unpriced_count"] == 1
    assert "7.00" in nf.pending_note(info) and "1 item" in nf.pending_note(info)
    assert nf.pending_note({"pending_count": 0}) == ""


# ── fetchers: parsers on fabricated fixtures ──

CRYPTO_DEPOSITS = {
    "deposit_list": [
        {
            "currency": "USDC",
            "fee": 0,
            "create_time": 1_790_000_100_000,
            "id": "6407",
            "update_time": 1_790_000_200_000,
            "amount": 25,
            "address": "0xabc",
            "status": "1",
            "txid": "aa",
        },
        {
            "currency": "DOGE",
            "fee": 0,
            "create_time": 1_790_000_300_000,
            "id": "6408",
            "update_time": 1_790_000_300_000,
            "amount": 100,
            "address": "D1",
            "status": "0",
            "txid": "bb",
        },
        {
            "currency": "USDC",
            "fee": 0,
            "create_time": 1_790_000_400_000,
            "id": "6409",
            "update_time": 1_790_000_400_000,
            "amount": 9,
            "address": "0xabc",
            "status": "2",
            "txid": "cc",
        },
    ]
}
CRYPTO_WITHDRAWALS = {
    "withdrawal_list": [
        {
            "currency": "USDC",
            "client_wid": "w1",
            "fee": 1,
            "create_time": 1_790_000_500_000,
            "id": "77",
            "update_time": 1_790_000_600_000,
            "amount": 10,
            "address": "0x1",
            "status": "5",
            "txid": "dd",
            "network_id": "ETH",
        },
        {
            "currency": "USDC",
            "client_wid": "w2",
            "fee": 1,
            "create_time": 1_790_000_700_000,
            "id": "78",
            "update_time": 1_790_000_700_000,
            "amount": 4,
            "address": "0x1",
            "status": "1",
            "txid": "",
            "network_id": "ETH",
        },
        {
            "currency": "USDC",
            "client_wid": "w3",
            "fee": 1,
            "create_time": 1_790_000_800_000,
            "id": "79",
            "update_time": 1_790_000_800_000,
            "amount": 4,
            "address": "0x1",
            "status": "6",
            "txid": "",
            "network_id": "ETH",
        },
    ]
}


def test_crypto_com_deposits_and_withdrawals_parse_with_status_and_sign():
    deposits = fetchers.parse_crypto_com(CRYPTO_DEPOSITS, True)
    assert [(f.external_id, f.currency, f.amount, f.status) for f in deposits] == [
        ("deposit:6407", "USDC", Decimal(25), "confirmed"),
        ("deposit:6408", "DOGE", Decimal(100), "pending"),
    ]
    assert deposits[0].ts == pytest.approx(1_790_000_200.0)
    withdrawals = fetchers.parse_crypto_com(CRYPTO_WITHDRAWALS, False)
    assert [(f.external_id, f.amount, f.status) for f in withdrawals] == [
        ("withdrawal:77", Decimal(-10), "confirmed"),
        ("withdrawal:78", Decimal(-4), "pending"),
    ]


@pytest.mark.parametrize("bad", [None, [], {}, {"deposit_list": "x"}])
def test_crypto_com_rejects_an_unexpected_shape(bad):
    with pytest.raises(fetchers.FetchUnavailable):
        fetchers.parse_crypto_com(bad, True)


class FakeCrypto:
    def __init__(self, deposits=None, withdrawals=None, errors=None):
        self.deposits, self.withdrawals = deposits, withdrawals
        self.errors = errors or {}
        self.calls = []

    async def private(self, user_id, method, params=None):
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        return self.deposits if method == "get-deposit-history" else self.withdrawals

    def status(self, user_id):
        return {"connected": True, "ledger": {"quote": "USD", "orders": []}}


def test_crypto_com_one_failing_part_never_hides_the_other():
    service = FakeCrypto(
        deposits=CRYPTO_DEPOSITS,
        errors={
            "get-withdrawal-history": ValueError("Crypto.com rejected (code 40101)")
        },
    )
    got = run(fetchers.fetch("crypto_com_exchange", service, USER, T0, T0 + 1000))
    assert len(got.flows) == 2
    assert got.problems == ["withdrawals: Crypto.com rejected (code 40101)"]
    method, params = service.calls[0]
    assert method == "get-deposit-history"
    assert params["start_ts"] == T0 * 1000 and params["page_size"] == 200


REVOLUT_TRANSACTIONS = {
    "data": [
        {
            "id": "t1",
            "status": "completed",
            "type": "receive",
            "source": {
                "amount": "4.27",
                "currency": "EUR",
                "account": {"type": "revolut"},
            },
            "destination": {
                "amount": "4.27",
                "currency": "EUR",
                "account": {"type": "revolut_x"},
            },
            "created_date": 1_790_000_000_000,
            "processed_date": 1_790_000_100_000,
        },
        {
            "id": "t2",
            "status": "completed",
            "type": "buy",
            "source": {"amount": "10", "currency": "EUR"},
            "destination": {"amount": "0.0001", "currency": "BTC"},
            "created_date": 1_790_000_200_000,
            "processed_date": 1_790_000_200_000,
        },
        {
            "id": "t3",
            "status": "pending",
            "type": "send",
            "source": {
                "amount": "2",
                "currency": "USDC",
                "account": {"type": "revolut_x"},
            },
            "destination": {
                "amount": "2",
                "currency": "USDC",
                "account": {"type": "external_crypto_wallet"},
            },
            "created_date": 1_790_000_300_000,
        },
        {
            "id": "t4",
            "status": "failed",
            "type": "receive",
            "destination": {"amount": "9", "currency": "EUR"},
            "created_date": 1_790_000_400_000,
        },
    ],
    "metadata": {"timestamp": 1_790_000_500_000},
}


def test_revolut_transactions_keep_only_completed_or_pending_transfers():
    flows = fetchers.parse_revolut_transactions(REVOLUT_TRANSACTIONS)
    assert [(f.external_id, f.currency, f.amount, f.status) for f in flows] == [
        ("transaction:t1", "EUR", Decimal("4.27"), "confirmed"),
        ("transaction:t3", "USDC", Decimal(-2), "pending"),
    ]
    assert flows[0].ts == pytest.approx(1_790_000_100.0)
    assert flows[1].ts == pytest.approx(1_790_000_300.0)  # created: no processed date
    assert "from revolut" in flows[0].note
    with pytest.raises(fetchers.FetchUnavailable):
        fetchers.parse_revolut_transactions({"oops": 1})


class FakeRevolut:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, []

    async def private(self, user_id, method, path, query=None, body=None):
        self.calls.append((method, path, dict(query)))
        if self.error:
            raise self.error
        return self.data

    async def eur_usd(self):
        return Decimal("1.08")


def test_revolut_asks_in_windows_no_longer_than_thirty_days():
    service = FakeRevolut(REVOLUT_TRANSACTIONS)
    got = run(fetchers.fetch("revolut_x", service, USER, T0 - 70 * 86400, T0))
    assert got.problems == []
    assert len(service.calls) == 3
    for method, path, query in service.calls:
        assert (method, path) == ("GET", "/1.0/transactions")
        assert query["end_date"] - query["start_date"] <= 30 * 86400 * 1000
        assert query["types"] == "receive,send"
    starts = [q["start_date"] for _, _, q in service.calls]
    ends = [q["end_date"] for _, _, q in service.calls]
    assert starts[1:] == ends[:-1]


def test_a_venue_without_a_fetcher_or_without_private_reads_is_unavailable():
    with pytest.raises(fetchers.FetchUnavailable):
        run(fetchers.fetch("revolut_x", object(), USER, T0, T0))


# ── sync ──


def test_sync_stores_priced_rows_idempotently_and_marks_the_venue_tracked():
    service = FakeRevolut(REVOLUT_TRANSACTIONS)
    out = run(nf.sync(USER, "revolut_x", service, now=T0, force=True))
    assert out["ok"] and out["fetched"] == 2 and out["new"] == 2
    flows = {f["external_id"]: f for f in nf.list_flows(USER, "revolut_x")}
    # EUR at the venue's own eur_usd, stored so history is never re-priced.
    assert flows["transaction:t1"]["usd_value_at_time"] == pytest.approx(4.27 * 1.08)
    assert flows["transaction:t3"]["status"] == "pending"
    info = nf.summary(USER, "revolut_x", now=T0 + 60)
    assert (
        info["tracked"] and info["confirmed_count"] == 1 and info["pending_count"] == 1
    )
    assert info["fetch"]["ok"] is True

    # Within the throttle nothing is asked again; forced, nothing is duplicated.
    calls = len(service.calls)
    skipped = run(nf.sync(USER, "revolut_x", service, now=T0 + 60))
    assert skipped["skipped"] and len(service.calls) == calls
    again = run(nf.sync(USER, "revolut_x", service, now=T0 + 120, force=True))
    assert again["new"] == 0 and len(nf.list_flows(USER, "revolut_x")) == 2


def test_sync_failures_degrade_to_an_unavailable_reason_and_never_raise():
    for service in (
        FakeRevolut(error=ValueError("Revolut X rejected the request (HTTP 403)")),
        FakeRevolut(error=RuntimeError("boom")),
        object(),
    ):
        out = run(nf.sync(USER, "revolut_x", service, now=T0, force=True))
        assert out["ok"] is False and out["reason"].startswith("unavailable: ")
    info = nf.summary(USER, "revolut_x", now=T0)
    assert info["tracked"] is False
    assert info["fetch"]["reason"].startswith("unavailable: ")
    # A venue the registry does not know is a plain answer.
    assert run(nf.sync(USER, "nope", FakeRevolut(), now=T0))["ok"] is False


def test_a_partial_history_is_saved_but_does_not_claim_the_venue_is_tracked():
    service = FakeCrypto(
        deposits=CRYPTO_DEPOSITS,
        errors={"get-withdrawal-history": ValueError("denied")},
    )
    out = run(nf.sync(USER, "crypto_com_exchange", service, now=T0, force=True))
    assert out["ok"] is False and "withdrawals: denied" in out["reason"]
    assert out["new"] == 2
    info = nf.summary(USER, "crypto_com_exchange", now=T0)
    # The deposit is confirmed, which is itself a tracked flow; the reason is kept.
    assert info["confirmed_count"] == 1
    assert "withdrawals: denied" in info["fetch"]["reason"]


def test_euros_fall_back_to_revolut_xs_public_rate_and_never_to_kraken(monkeypatch):
    from condor import revolut_x, xstocks_market

    class NoRate:  # a venue that cannot price euros itself
        async def eur_usd(self):
            return None

    async def revolut_rate():
        return Decimal("1.09")

    monkeypatch.setattr(revolut_x.service, "eur_usd", revolut_rate)
    assert run(nf.usd_rate(NoRate(), "EUR")) == Decimal("1.09")
    assert not hasattr(xstocks_market.service, "eur_usd")

    async def no_rate():
        return None

    monkeypatch.setattr(revolut_x.service, "eur_usd", no_rate)
    assert run(nf.usd_rate(NoRate(), "EUR")) is None


def test_an_unpriceable_currency_is_stored_without_a_usd_value():
    service = FakeCrypto(deposits=CRYPTO_DEPOSITS, withdrawals={"withdrawal_list": []})
    run(nf.sync(USER, "crypto_com_exchange", service, now=T0, force=True, view=None))
    rows = {f["currency"]: f for f in nf.list_flows(USER, "crypto_com_exchange")}
    assert rows["USDC"]["usd_value_at_time"] == 25.0
    assert rows["DOGE"]["usd_value_at_time"] is None
    view = {"balances": [{"token": "DOGE", "total": 100, "usd_value": 12.5}]}
    assert run(nf.usd_rate(service, "DOGE", view)) == Decimal("0.125")


# ── detection ──


def view_of(**tokens):
    """``view_of(USD=(units, usd), BTC=(units, usd))`` as a portfolio view."""
    return {
        "balances": [
            {"token": t, "total": u, "available": u, "usd_value": v}
            for t, (u, v) in tokens.items()
        ]
    }


def ledger_of(orders=(), quote="USD", uncertain=None):
    return {"quote": quote, "orders": list(orders), "uncertain_order": uncertain}


def detect(view, ledger=None, now=T0, venue="crypto_com_exchange"):
    return nf.detect(USER, venue, view, ledger or ledger_of(), now=now)


def test_the_first_reading_only_sets_the_reference():
    assert detect(view_of(USD=(20, 20))) == []
    assert nf.pending(USER, "crypto_com_exchange") == []


def test_a_deposit_is_a_pending_row_that_is_not_subtracted_until_confirmed():
    detect(view_of(USD=(20, 20)), now=T0)
    created = detect(view_of(USD=(30, 30)), now=T0 + 300)
    assert len(created) == 1
    flow = created[0]
    assert (flow["currency"], flow["amount"], flow["source"], flow["status"]) == (
        "USD",
        "10",
        "detected",
        "pending",
    )
    assert flow["usd_value_at_time"] == pytest.approx(10.0)
    assert nf.window_net(USER, "crypto_com_exchange", T0, T0 + 1000).net_usd == 0.0
    info = nf.summary(USER, "crypto_com_exchange")
    assert info["pending_count"] == 1 and not info["tracked"]
    # The same reading again explains nothing new.
    assert detect(view_of(USD=(30, 30)), now=T0 + 600) == []

    nf.set_status(USER, flow["id"], "confirmed")
    assert nf.window_net(USER, "crypto_com_exchange", T0, T0 + 1000).net_usd == 10.0


def test_a_withdrawal_is_detected_with_its_sign():
    detect(view_of(USD=(50, 50)), now=T0)
    created = detect(view_of(USD=(30, 30)), now=T0 + 300)
    assert [(f["amount"], f["usd_value_at_time"]) for f in created] == [("-20", -20.0)]


def test_a_change_the_bots_own_order_explains_is_ignored():
    detect(view_of(USD=(20, 20), BTC=(0, 0)), now=T0)
    order = {
        "time": T0 + 100,
        "side": "buy",
        "symbol": "BTC",
        "status": "FILLED",
        "quantity": "0.0002",
        "value": "12",
        "fee": "0.06",
        "fee_currency": "USD",
    }
    after = view_of(USD=(7.94, 7.94), BTC=(0.0002, 12))
    assert detect(after, ledger_of([order]), now=T0 + 300) == []


def test_an_order_from_before_the_last_reading_explains_nothing_now():
    detect(view_of(USD=(20, 20)), now=T0)
    sold_earlier = {
        "time": T0 - 5,
        "side": "sell",
        "symbol": "BTC",
        "status": "FILLED",
        "quantity": "0.0002",
        "value": "12",
        "fee": "0",
        "fee_currency": "USD",
    }
    created = detect(view_of(USD=(32, 32)), ledger_of([sold_earlier]), now=T0 + 300)
    assert [f["currency"] for f in created] == ["USD"]


def test_a_fee_taken_in_the_units_is_part_of_what_the_order_explains():
    detect(view_of(USD=(20, 20), SUI=(0, 0)), now=T0)
    order = {
        "time": T0 + 100,
        "side": "buy",
        "symbol": "SUI",
        "status": "FILLED",
        "quantity": "4",
        "value": "12",
        "fee": "0.01",
        "fee_currency": "SUI",
    }
    after = view_of(USD=(8, 8), SUI=(3.99, 11.97))
    assert detect(after, ledger_of([order]), now=T0 + 300) == []


def test_noise_below_the_floor_and_the_percentage_is_ignored():
    detect(view_of(USD=(20, 20), BTC=(0.01, 1000)), now=T0)
    # 0.4 USD of cash is under the 0.5 USD floor; 15 USD is 1.5% of the 1000 USD
    # holding, under 2%.
    quiet = view_of(USD=(20.4, 20.4), BTC=(0.01015, 1015))
    assert detect(quiet, now=T0 + 300) == []
    # 25 USD of the same coin is 2.5%: worth a look.
    detect(view_of(BTC=(0.01, 1000)), now=T0, venue="revolut_x")
    loud = view_of(BTC=(0.01025, 1025))
    created = detect(loud, now=T0 + 600, venue="revolut_x")
    assert [f["currency"] for f in created] == ["BTC"]


def test_a_conversion_between_the_accounts_own_currencies_is_not_a_flow():
    detect(view_of(EUR=(100, 110), USD=(5, 5)), now=T0, venue="revolut_x")
    # 100 EUR sold for 109.8 USD (a 0.2 USD cost): two moves that offset.
    converted = view_of(EUR=(0, 0), USD=(114.8, 114.8))
    assert detect(converted, now=T0 + 300, venue="revolut_x") == []
    assert nf.pending(USER, "revolut_x") == []


def test_a_recorded_flow_explains_the_move_and_a_late_one_supersedes_the_detection():
    detect(view_of(USD=(20, 20)), now=T0)
    # The history fetch got there first: the move is explained.
    nf.add_flow(
        USER,
        "crypto_com_exchange",
        amount="10",
        currency="USD",
        ts=T0 + 200,
        usd_value=10,
        source="api",
        external_id="deposit:1",
        now=T0 + 250,
    )
    assert detect(view_of(USD=(30, 30)), now=T0 + 300) == []

    # Detected first, the history catches up later: the pending row is dismissed.
    created = detect(view_of(USD=(45, 45)), now=T0 + 600)
    assert len(created) == 1
    nf.add_flow(
        USER,
        "crypto_com_exchange",
        amount="15",
        currency="USD",
        ts=T0 + 500,
        usd_value=15,
        source="api",
        external_id="deposit:2",
        now=T0 + 900,
    )
    assert nf.pending(USER, "crypto_com_exchange") == []
    dismissed = nf.list_flows(USER, "crypto_com_exchange", include_dismissed=True)
    assert any(
        f["status"] == "dismissed" and "superseded" in f["note"] for f in dismissed
    )
    # The late flow was credited before the last reading: it must not echo.
    assert detect(view_of(USD=(45, 45)), now=T0 + 1200) == []


def test_a_backfilled_old_flow_does_not_masquerade_as_the_next_movement():
    detect(view_of(USD=(20, 20)), now=T0)
    nf.add_flow(
        USER,
        "crypto_com_exchange",
        amount="4.27",
        currency="USD",
        ts=T0 - 86400,
        usd_value=4.27,
        source="manual",
        now=T0 + 100,
    )
    assert detect(view_of(USD=(20, 20)), now=T0 + 300) == []


def test_an_uncertain_order_pauses_detection_without_losing_the_reference():
    detect(view_of(USD=(20, 20)), now=T0)
    pending_order = {"client_oid": "x"}
    assert (
        detect(view_of(USD=(5, 5)), ledger_of(uncertain=pending_order), now=T0 + 300)
        == []
    )
    created = detect(view_of(USD=(5, 5)), now=T0 + 600)
    assert [f["amount"] for f in created] == ["-15"]


def test_observe_swallows_every_failure():
    class Broken:
        def status(self, user_id):
            raise RuntimeError("no ledger")

    run(
        nf.observe(
            USER, "crypto_com_exchange", Broken(), {"balances": "bad"}, fetch=True
        )
    )
    run(nf.observe(USER, "nope", Broken(), view_of(USD=(1, 1))))


# ── PnL: native_accounts ──


class FakeVenue:
    def __init__(self, total, quote="USD", excluded=None):
        self.total = total
        self.quote = quote
        self.excluded = excluded

    def status(self, user_id):
        return {
            "connected": True,
            "enabled": True,
            "ledger": {
                "quote": self.quote,
                "cash": "20",
                "positions": {},
                "synced": True,
            },
        }

    async def portfolio(self, user_id):
        view = {
            "connector": "x",
            "total_usd": self.total,
            "balances": [
                {"token": "USD", "total": 20, "available": 20, "usd_value": 20}
            ],
            "note": "",
        }
        if self.excluded is not None:
            view["excluded_value_usd"] = self.excluded
        return view


def sample(venue, ts, total):
    assert ph.record(USER, venue.label, total, now=ts)


def perf(spans, total, now, venue=EXCHANGE, excluded=None):
    return run(
        na.performance(
            venue,
            USER,
            spans,
            services={venue.key: FakeVenue(total, excluded=excluded)},
            now=now,
        )
    )


def span(num=1, start=T0, end=0.0, live=True):
    return na.SessionSpan(num, start, end, live)


def test_a_deposit_in_the_window_is_not_profit():
    sample(EXCHANGE, T0, 100.0)
    add(amount="8", ts=T0 + 600)
    out = perf([span()], 110.0, T0 + 3600)
    # Equity rose 10, of which 8 was the owner's money.
    assert out.sessions[0].pnl == pytest.approx(2.0)
    assert out.sessions[0].flows_usd == pytest.approx(8.0)
    assert out.total_pnl == pytest.approx(2.0) and out.flows_usd == pytest.approx(8.0)
    assert out.flows_tracked is True


def test_a_withdrawal_is_not_a_loss():
    sample(EXCHANGE, T0, 100.0)
    add(amount="-15", ts=T0 + 600)
    out = perf([span()], 90.0, T0 + 3600)
    assert out.sessions[0].pnl == pytest.approx(5.0)


def test_a_pending_movement_is_surfaced_but_not_subtracted():
    sample(EXCHANGE, T0, 100.0)
    add(amount="8", ts=T0 + 600, status="pending", source="detected")
    out = perf([span()], 110.0, T0 + 3600)
    assert out.sessions[0].pnl == pytest.approx(10.0)
    assert (out.pending_count, out.pending_usd) == (1, pytest.approx(8.0))
    assert out.flows_tracked is False


def test_a_flow_exactly_on_a_boundary_counts_once_in_the_window_it_ends():
    sample(EXCHANGE, T0, 100.0)
    sample(EXCHANGE, T0 + 3600, 130.0)
    sample(EXCHANGE, T0 + 7200, 130.0)
    add(amount="1", ts=T0)  # at session 1's start: before its window
    add(amount="20", ts=T0 + 3600)  # at the boundary: session 1 only
    spans = [
        span(1, T0, T0 + 3600, live=False),
        span(2, T0 + 3600, live=True),
    ]
    out = perf(spans, 130.0, T0 + 7200)
    by_num = {s.num: s for s in out.sessions}
    assert by_num[1].pnl == pytest.approx(30.0 - 20.0)
    assert by_num[2].pnl == pytest.approx(0.0)
    # Overall: from the first start to now, the flow at the start excluded.
    assert out.total_pnl == pytest.approx(30.0 - 20.0)


def test_a_flow_after_the_last_sample_is_still_subtracted_from_the_live_reading():
    sample(EXCHANGE, T0, 100.0)
    # No sample after the deposit: the live reading (now) carries it.
    add(amount="6", ts=T0 + 5000)
    out = perf([span()], 107.0, T0 + 6000)
    assert out.sessions[0].pnl == pytest.approx(1.0)


def test_a_flow_before_the_first_sample_is_already_in_the_starting_equity():
    add(amount="50", ts=T0 - 600)
    sample(EXCHANGE, T0, 150.0)
    out = perf([], 152.0, T0 + 3600)
    assert out.total_pnl == pytest.approx(2.0)


def test_the_window_is_the_readings_compared_not_the_session_clock():
    # The only start sample is 10 minutes before the session: a deposit in that gap
    # is in "now" but not in the sample, so it must come off.
    sample(EXCHANGE, T0 - 600, 100.0)
    add(amount="5", ts=T0 - 300)
    out = perf([span(start=T0)], 106.0, T0 + 3600)
    assert out.sessions[0].pnl == pytest.approx(1.0)


def test_an_unpriced_confirmed_flow_is_named_not_silently_dropped():
    sample(EXCHANGE, T0, 100.0)
    add(amount="3", ts=T0 + 10, usd_value=None)
    out = perf([span()], 101.0, T0 + 3600)
    assert out.sessions[0].pnl == pytest.approx(1.0)
    assert "without a USD value" in out.sessions[0].note
    assert "without a USD value" in out.note


def test_the_old_wording_stays_when_nothing_is_recorded_and_no_history_was_read():
    sample(EXCHANGE, T0, 100.0)
    out = perf([span()], 105.0, T0 + 3600)
    assert out.flows_tracked is False
    assert na.basis_label(False) == na.BASIS_LABEL
    # A history read that worked makes "no flows" a fact: the figure is a trading result.
    run(
        nf.sync(
            USER,
            "crypto_com_exchange",
            FakeCrypto(
                deposits={"deposit_list": []}, withdrawals={"withdrawal_list": []}
            ),
            now=time.time(),
            force=True,
        )
    )
    out = perf([span()], 105.0, T0 + 3600)
    assert out.flows_tracked is True
    assert na.basis_label(True).startswith("Trading result:")


def test_the_overview_reports_recorded_flows_pending_ones_and_a_net_change():
    sample(EXCHANGE, T0, 40.0)
    add(amount="8", ts=T0 + 100)
    add(amount="2", ts=T0 + 200, status="pending", source="detected")
    service = FakeVenue(None)

    async def portfolio(user_id):
        return {
            "total_usd": 50.0,
            "balances": [
                {"token": "USD", "total": 50, "available": 50, "usd_value": 50}
            ],
            "note": "",
        }

    service.portfolio = portfolio
    out = run(
        na.overview(
            USER,
            services={
                "crypto_com_exchange": service,
                "revolut_x": FakeVenue(0.0),
            },
            now=T0 + 3600,
        )
    )
    entry = next(v for v in out["venues"] if v["venue"] == "crypto_com_exchange")
    change = entry["equity_change"]
    assert change["basis"] == na.BASIS_LABEL_NET
    since = change["since_first_sample"]
    assert since["equity_change_usd"] == 10.0
    assert since["net_flows_usd"] == 8.0
    assert since["change_usd"] == 2.0
    flows = entry["flows"]
    assert flows["recorded_net_flows_usd"] == 8.0
    assert flows["pending_count"] == 1 and flows["tracked"] is True
    assert "Unconfirmed cash movement" in flows["note"]
    # A venue with nothing recorded keeps the old wording and an empty flows block.
    other = next(v for v in out["venues"] if v["venue"] == "revolut_x")
    assert other["flows"]["pending_count"] == 0 and other["flows"]["tracked"] is False
    assert any("flows block" in n for n in out["notes"])


def test_the_chat_tool_documents_the_flows_it_returns():
    from mcp_servers.condor import server

    doc = server.native_portfolio.__doc__
    assert "pending_count" in doc and "recorded_net_flows_usd" in doc


def test_flow_store_errors_never_break_the_pnl(monkeypatch):
    sample(EXCHANGE, T0, 100.0)

    def boom(*args, **kwargs):
        raise sqlite3.DatabaseError("corrupt")

    monkeypatch.setattr(nf, "summary", boom)
    monkeypatch.setattr(nf, "window_net", boom)
    out = perf([span()], 104.0, T0 + 3600)
    assert out.sessions[0].pnl == pytest.approx(4.0)
    assert out.flows_tracked is False


def test_the_recorder_reads_history_and_detects_without_ever_failing():
    from condor import portfolio_history

    class Venue(FakeCrypto):
        def __init__(self):
            super().__init__(
                deposits={"deposit_list": []}, withdrawals={"withdrawal_list": []}
            )
            self.total = 30.0

        async def portfolio(self, user_id):
            return view_of(USD=(self.total, self.total)) | {"total_usd": self.total}

    service = Venue()
    run(portfolio_history.sample_user(USER, exchange=service))
    assert [m for m, _ in service.calls] == [
        "get-deposit-history",
        "get-withdrawal-history",
    ]
    assert nf.summary(USER, "crypto_com_exchange")["tracked"] is True


# ── a detected deposit and the venue's own history row are one deposit ──


def _detected_deposit(venue="revolut_x", amount=50, at=T0 + 300, confirm=True):
    """The recorder sees +amount USD with no order; the owner may confirm it."""
    detect(view_of(USD=(20, 20)), now=T0, venue=venue)
    (row,) = detect(view_of(USD=(20 + amount, 20 + amount)), now=at, venue=venue)
    assert (row["source"], row["status"]) == ("detected", "pending")
    if confirm:
        row = nf.set_status(USER, row["id"], "confirmed", now=at + 1)
        assert row["status"] == "confirmed"
    return row


def _history_row(venue="revolut_x", amount="50", ts=T0 + 200, ident="dep:1", **over):
    args = dict(
        amount=amount,
        currency="USD",
        ts=ts,
        usd_value=amount,
        source="api",
        status="confirmed",
        external_id=ident,
        now=T0 + 9000,
    )
    args.update(over)
    return nf.add_flow(USER, venue, **args)


def _states(venue):
    return {
        (f["source"], f["status"])
        for f in nf.list_flows(USER, venue, include_dismissed=True)
    }


def test_a_confirmed_detection_is_superseded_when_the_history_row_arrives_later():
    detected = _detected_deposit()
    assert nf.summary(USER, "revolut_x")["recorded_net_flows_usd"] == 50.0

    _history_row()

    rows = {
        f["id"]: f for f in nf.list_flows(USER, "revolut_x", include_dismissed=True)
    }
    assert rows[detected["id"]]["status"] == "dismissed"
    assert "superseded by venue history" in rows[detected["id"]]["note"]
    summary = nf.summary(USER, "revolut_x")
    # One deposit, counted once: the history row only.
    assert (summary["confirmed_count"], summary["recorded_net_flows_usd"]) == (1, 50.0)
    assert nf.window_net(USER, "revolut_x", T0, T0 + 5000).net_usd == 50.0


def test_the_same_holds_for_revolut_x_and_for_a_manual_entry():
    _detected_deposit("revolut_x", 30)
    _history_row("revolut_x", "30", ident="dep:r")
    assert nf.window_net(USER, "revolut_x", T0, T0 + 5000).net_usd == 30.0

    detected = _detected_deposit("crypto_com_exchange", 12)
    nf.add_flow(
        USER,
        "crypto_com_exchange",
        amount="12",
        currency="USD",
        ts=T0 + 250,
        usd_value=12,
        source="manual",
        status="confirmed",
        now=T0 + 9000,
    )
    flows = {
        f["id"]: f
        for f in nf.list_flows(USER, "crypto_com_exchange", include_dismissed=True)
    }
    assert flows[detected["id"]]["status"] == "dismissed"
    assert nf.window_net(USER, "crypto_com_exchange", T0, T0 + 5000).net_usd == 12.0


def test_confirming_a_detection_the_history_already_records_dismisses_it():
    detected = _detected_deposit(confirm=False)
    # The history row names the same deposit but arrives after the pending rule's day
    # window, so the pending detection is still on screen when the owner confirms it.
    _history_row(ts=T0 + 400, now=T0 + 300 + 2 * 86400)
    assert [f["id"] for f in nf.pending(USER, "revolut_x")] == [detected["id"]]
    confirmed = nf.set_status(USER, detected["id"], "confirmed", now=T0 + 3 * 86400)
    assert confirmed["status"] == "dismissed"
    assert "superseded by venue history" in confirmed["note"]
    assert nf.window_net(USER, "revolut_x", T0 - 10, T0 + 5000).net_usd == 50.0


def test_a_detection_is_dismissed_on_creation_when_a_matching_history_row_exists():
    detect(view_of(USD=(20, 20)), now=T0, venue="revolut_x")
    # Recorded before the reference reading (created and stamped earlier), so it does
    # not "explain" the move, but it names the same money.
    _history_row(ts=T0 + 100, now=T0 - 50)
    created = detect(view_of(USD=(70, 70)), now=T0 + 300, venue="revolut_x")
    assert created == []
    assert nf.pending(USER, "revolut_x") == []
    (twin,) = [
        f
        for f in nf.list_flows(USER, "revolut_x", include_dismissed=True)
        if f["source"] == "detected"
    ]
    assert twin["status"] == "dismissed" and "venue history" in twin["note"]


def test_a_detection_stamped_apart_or_of_another_amount_is_kept():
    detected = _detected_deposit(amount=50, at=T0 + 300)
    # More than 30 minutes apart: a separate deposit of the same size.
    _history_row(ts=T0 + 300 + 3600, ident="late")
    # More than 5% apart.
    _history_row(amount="60", ts=T0 + 310, ident="big")
    # A withdrawal of the same size is another movement.
    _history_row(amount="-50", ts=T0 + 310, ident="out")
    # Another venue is another account.
    _history_row("trading212", "50", ts=T0 + 310, ident="elsewhere")
    # Another currency is another balance.
    _history_row(currency="EUR", ts=T0 + 310, ident="eur")
    kept = {
        f["id"]: f for f in nf.list_flows(USER, "revolut_x", include_dismissed=True)
    }
    assert kept[detected["id"]]["status"] == "confirmed"
    window = nf.window_net(USER, "revolut_x", T0, T0 + 9000)
    # The detection plus the four other rows that are not the same money.
    assert (window.count, window.net_usd) == (5, 160.0)


def test_superseding_is_idempotent():
    detected = _detected_deposit()
    _history_row()
    _history_row()  # the same transfer read again
    twin = nf.list_flows(USER, "revolut_x", include_dismissed=True)
    note = next(f for f in twin if f["id"] == detected["id"])["note"]
    assert note.count("superseded") == 1
    # Dismissing, confirming or re-reading it changes nothing further.
    nf.set_status(USER, detected["id"], "dismissed")
    again = next(
        f
        for f in nf.list_flows(USER, "revolut_x", include_dismissed=True)
        if f["id"] == detected["id"]
    )
    assert again["note"] == note and again["status"] == "dismissed"
    assert nf.summary(USER, "revolut_x")["confirmed_count"] == 1


# ── detection: trades against the venue's own cash, excluded holdings, the ledger ──


def ledger_212(excluded=("MRNA",), orders=(), cash="30", positions=None):
    return {
        "quote": "EUR",
        "orders": list(orders),
        "uncertain_order": None,
        "excluded_tickers": list(excluded),
        "cash": cash,
        "positions": positions or {},
    }


def detect_212(view, ledger=None, now=T0):
    return nf.detect(USER, "trading212", view, ledger or ledger_212(), now=now)


def test_the_live_false_detections_are_gone_a_swap_and_an_excluded_holding():
    # Trading 212, 2026-10-29: the owner bought MRNA by hand (a holding on the venue's
    # exclusion list; its 9.11 EUR was reserved, so no cash leg was visible) and the
    # bot's first AAPL order filled while its record was gone. Three "unconfirmed cash
    # movements" were offered; none was a deposit or a withdrawal.
    detect_212(view_of(EUR=(30.0, 33.0), AAPL=(0, 0), MRNA=(0, 0)), now=T0)
    created = detect_212(
        view_of(EUR=(24.05, 26.455), AAPL=(0.0188, 6.5), MRNA=(0.0467626, 10.0)),
        now=T0 + 300,
    )
    assert created == []
    assert nf.pending(USER, "trading212") == []


def test_a_manual_buy_of_an_excluded_ticker_is_neither_flagged_nor_a_flow():
    detect_212(view_of(EUR=(40.0, 44.0), MRNA=(0, 0)), now=T0)
    # 9.11 EUR of cash became MRNA: value moved inside the account.
    created = detect_212(
        view_of(EUR=(30.89, 33.98), MRNA=(0.0467626, 10.1)), now=T0 + 300
    )
    assert created == []
    assert nf.pending(USER, "trading212") == []


def test_an_excluded_ticker_moving_alone_is_never_offered_as_a_flow():
    detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0, 0)), now=T0)
    assert detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0.05, 10.0)), now=T0 + 300) == []
    # Also when the owner sells some of it and the proceeds are not in the reading yet.
    assert detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0.02, 4.0)), now=T0 + 600) == []
    assert nf.pending(USER, "trading212") == []


def test_the_exclusion_list_only_covers_what_it_names():
    detect_212(view_of(EUR=(30.0, 33.0), TSLA=(0, 0)), now=T0)
    created = detect_212(view_of(EUR=(30.0, 33.0), TSLA=(0.05, 12.0)), now=T0 + 300)
    assert [(r["currency"], r["amount"]) for r in created] == [("TSLA", "0.05")]


@pytest.mark.parametrize(
    "venue,before,after",
    [
        # A stock bought by hand with the venue's own cash.
        (
            "revolut_x",
            view_of(USD=(100, 100), AAPLx=(0, 0)),
            view_of(USD=(90, 90), AAPLx=(0.04, 10.05)),
        ),
        # A coin sold by hand for cash.
        (
            "crypto_com_exchange",
            view_of(USD=(20, 20), BTC=(0.01, 1000)),
            view_of(USD=(1018, 1018), BTC=(0, 0)),
        ),
        # A coin bought by hand with cash.
        (
            "revolut_x",
            view_of(USD=(500, 500), ETH=(0, 0)),
            view_of(USD=(380, 380), ETH=(0.04, 119.5)),
        ),
    ],
)
def test_a_manual_trade_against_the_venues_own_cash_is_not_a_flow(venue, before, after):
    detect(before, now=T0, venue=venue)
    assert detect(after, now=T0 + 300, venue=venue) == []
    assert nf.pending(USER, venue) == []


def test_a_deposit_beside_a_trade_is_not_swallowed_by_it():
    detect(
        view_of(USD=(100, 100), AAPLx=(0, 0), EUR=(0, 0)),
        now=T0,
        venue="revolut_x",
    )
    created = detect(
        view_of(USD=(89.8, 89.8), AAPLx=(0.04, 10.0), EUR=(50, 55)),
        now=T0 + 300,
        venue="revolut_x",
    )
    # The buy (-10.2 against +10.0) is one conversion; the 50 EUR is a deposit.
    assert [(r["currency"], r["amount"]) for r in created] == [("EUR", "50")]


def test_a_real_deposit_is_still_detected_with_an_excluded_holding_untouched():
    detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0.0467626, 10.0)), now=T0)
    created = detect_212(
        view_of(EUR=(80.0, 88.0), MRNA=(0.0467626, 10.0)), now=T0 + 300
    )
    assert [(r["currency"], r["amount"], r["status"]) for r in created] == [
        ("EUR", "50", "pending")
    ]


def test_a_real_deposit_is_still_detected_beside_an_excluded_move():
    detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0, 0)), now=T0)
    created = detect_212(
        view_of(EUR=(80.0, 88.0), MRNA=(0.0467626, 10.0)), now=T0 + 300
    )
    assert [(r["currency"], r["amount"]) for r in created] == [("EUR", "50")]


def test_a_real_withdrawal_is_still_detected():
    detect_212(view_of(EUR=(60.0, 66.0), AAPL=(0.02, 7.0)), now=T0)
    created = detect_212(view_of(EUR=(20.0, 22.0), AAPL=(0.02, 7.0)), now=T0 + 300)
    assert [(r["currency"], r["amount"], r["usd_value_at_time"]) for r in created] == [
        ("EUR", "-40", pytest.approx(-44.0))
    ]


def test_a_one_sided_cash_change_is_still_detected_not_paired_with_nothing():
    detect_212(view_of(EUR=(30.0, 33.0), MRNA=(0.0467626, 10.0)), now=T0)
    created = detect_212(
        view_of(EUR=(24.0, 26.4), MRNA=(0.0467626, 10.0)), now=T0 + 300
    )
    assert [(r["currency"], r["amount"]) for r in created] == [("EUR", "-6")]


def test_two_moves_of_very_different_value_are_not_one_conversion():
    detect(view_of(USD=(100, 100), AAPLx=(0, 0)), now=T0, venue="revolut_x")
    created = detect(
        view_of(USD=(70, 70), AAPLx=(0.04, 10.0)), now=T0 + 300, venue="revolut_x"
    )
    assert sorted(r["currency"] for r in created) == ["AAPLX", "USD"]


def test_a_fill_the_ledger_booked_without_an_order_row_explains_the_account_move():
    # The order record was cleared (an older reconcile bug) but the module's own ledger
    # holds the position it booked: the account's rise is the bot's, not a flow.
    detect_212(view_of(EUR=(30.0, 33.0), AAPL=(0, 0)), ledger_212(), now=T0)
    booked = ledger_212(positions={"AAPL": {"units": "0.0188", "cost": "5.95"}})
    assert (
        detect_212(view_of(EUR=(30.0, 33.0), AAPL=(0.0188, 6.6)), booked, T0 + 300)
        == []
    )


def test_a_ledger_booking_never_explains_more_than_the_account_moved():
    detect_212(view_of(EUR=(30.0, 33.0), AAPL=(0, 0)), ledger_212(), now=T0)
    booked = ledger_212(positions={"AAPL": {"units": "0.0188", "cost": "5.95"}})
    created = detect_212(view_of(EUR=(30.0, 33.0), AAPL=(0.05, 17.5)), booked, T0 + 300)
    # 0.0188 is the bot's; the other 0.0312 is not.
    assert [(r["currency"], r["amount"]) for r in created] == [("AAPL", "0.0312")]


def test_a_ledger_booking_in_the_other_direction_explains_nothing():
    detect_212(
        view_of(EUR=(30.0, 33.0), AAPL=(0.05, 17.5)),
        ledger_212(positions={"AAPL": {"units": "0.05", "cost": "15"}}),
        now=T0,
    )
    # The ledger sold 0.02, yet the account's units ROSE: that rise is not explained.
    created = detect_212(
        view_of(EUR=(30.0, 33.0), AAPL=(0.07, 24.5)),
        ledger_212(positions={"AAPL": {"units": "0.03", "cost": "9"}}),
        T0 + 300,
    )
    assert [(r["currency"], r["amount"]) for r in created] == [("AAPL", "0.02")]


def test_a_flow_store_from_before_the_ledger_snapshot_is_upgraded_in_place():
    path = nf._db(USER)
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE marks (venue TEXT PRIMARY KEY, ts REAL NOT NULL,
                            balances TEXT NOT NULL);
        INSERT INTO marks VALUES ('trading212', %d, '{"EUR": ["30", 33.0]}');
        """ % T0)
    conn.commit()
    conn.close()
    created = detect_212(view_of(EUR=(80.0, 88.0)), now=T0 + 300)
    assert [(r["currency"], r["amount"]) for r in created] == [("EUR", "50")]
    check = sqlite3.connect(path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(marks)")}
    finally:
        check.close()
    assert "ledger" in columns


# ── the trading result: whole account, owner's own holdings, a wrong first sample ──

EUR_USD = 1.114


def test_the_recorder_stores_the_excluded_value_beside_the_sample():
    assert ph.record(USER, "Trading 212", 43.61, now=T0, excluded_usd=10.25)
    assert ph.record(USER, "Trading 212", 43.7, now=T0 + 300)  # value not given
    assert ph.excluded_at(USER, "Trading 212", T0) == pytest.approx(10.25)
    # Not recorded is not zero.
    assert ph.excluded_at(USER, "Trading 212", T0 + 300) is None
    assert ph.excluded_at(USER, "Trading 212", T0 + 5) is None
    assert ph.excluded_at(99, "Trading 212", T0) is None
    # The history chart still reads the whole account.
    rows = ph.series(USER, T0 - 1, T0 + 600)["Trading 212"]
    assert [round(v, 2) for _, v in rows] == [43.61, 43.7]


T212 = native_venues.get("trading212")


def result_212(total_now, excluded_now, now=T0 + 7200):
    return perf([], total_now, now, venue=T212, excluded=excluded_now)


def test_the_wrong_first_sample_shows_the_phantom_gain_without_a_correction():
    # First sample 30.00 EUR = 33.42 USD: the 9.11 EUR that a pending MRNA order had
    # reserved was in no field. After the fill the account is 29.95 EUR (the bot's:
    # -0.05 EUR) + MRNA worth 9.20 EUR = 39.15 EUR = 43.61 USD.
    ph.record(USER, "Trading 212", 33.42, now=T0)  # recorded before excluded was stored
    out = result_212(43.61, 10.25)
    assert out.total_pnl == pytest.approx(10.19)
    # The start sample has no excluded value, so none is removed, and it says so.
    assert out.excluded_usd == 0.0
    assert "did not record" in out.note


def test_a_manual_flow_of_the_reserved_cash_nets_the_wrong_first_sample_out():
    ph.record(USER, "Trading 212", 33.42, now=T0)
    # The 9.11 EUR the owner's own money held in the pending order, priced at the
    # first sample's own rate, dated just AFTER that sample (a flow stamped exactly at
    # the sample is "already in it").
    add("trading212", amount="9.11", currency="EUR", usd="10.15", ts=T0 + 1)
    out = result_212(43.61, 10.25)
    assert out.flows_usd == pytest.approx(10.15)
    # 43.61 - 33.42 - 10.15: the bot's -0.06 plus MRNA's 0.09 EUR price move (+0.10).
    assert out.total_pnl == pytest.approx(0.04, abs=0.005)


def test_a_flow_dated_exactly_on_the_first_sample_does_not_count():
    ph.record(USER, "Trading 212", 33.42, now=T0)
    add("trading212", amount="9.11", currency="EUR", usd="10.15", ts=T0)
    assert result_212(43.61, 10.25).total_pnl == pytest.approx(10.19)


def test_samples_that_recorded_the_excluded_value_need_no_correction_at_all():
    # From a sample recorded with the excluded value the owner's shares never move
    # the result, whatever they do.
    ph.record(USER, "Trading 212", 43.50, now=T0, excluded_usd=10.20)
    out = result_212(43.61, 10.25)
    # Bot side: 33.30 then 33.36.
    assert out.total_pnl == pytest.approx(0.06)
    assert out.excluded_usd == pytest.approx(0.05)
    # And when the owner's holding rallies 40%, the result does not move.
    na.forget()  # the live reading is reused for a short while
    out = result_212(43.50 + 4.08, 10.20 + 4.08)
    assert out.total_pnl == pytest.approx(0.0)


def test_a_manual_purchase_of_an_excluded_holding_reads_as_a_loss_until_recorded():
    # Known limit, kept visible: cash that becomes the owner's own share leaves the
    # bot's side. Record it as a withdrawal to keep the result honest.
    ph.record(USER, "Trading 212", 43.50, now=T0, excluded_usd=0.0)
    assert result_212(43.50, 10.0).total_pnl == pytest.approx(-10.0)
    na.forget()
    add("trading212", amount="-10", currency="USD", usd="-10", ts=T0 + 60)
    assert result_212(43.50, 10.0).total_pnl == pytest.approx(0.0)


def test_session_results_leave_the_owners_holdings_out_too():
    ph.record(USER, "Trading 212", 43.50, now=T0, excluded_usd=10.20)
    out = perf([span(1, T0)], 43.61, T0 + 7200, venue=T212, excluded=10.25)
    assert out.sessions[0].pnl == pytest.approx(0.06)
    assert out.sessions[0].excluded_usd == pytest.approx(0.05)


def test_a_venue_without_an_exclusion_list_is_unchanged():
    ph.record(USER, EXCHANGE.label, 100.0, now=T0, excluded_usd=0.0)
    assert perf([span()], 110.0, T0 + 3600).sessions[0].pnl == pytest.approx(10.0)
    # An older sample with nothing stored behaves the same.
    ph.record(USER, REVOLUT.label, 100.0, now=T0)
    assert perf([span()], 110.0, T0 + 3600, venue=REVOLUT).sessions[
        0
    ].pnl == pytest.approx(10.0)


def test_the_overview_reports_the_excluded_value_and_takes_it_out_of_the_change():
    ph.record(USER, "Trading 212", 43.50, now=T0, excluded_usd=10.20)
    add("trading212", amount="5", currency="USD", usd="5", ts=T0 + 100)
    service = FakeVenue(43.61, excluded=10.25)
    out = run(
        na.overview(
            USER,
            services={
                "crypto_com_exchange": FakeVenue(0.0),
                "revolut_x": FakeVenue(0.0),
                "trading212": service,
            },
            now=T0 + 3600,
        )
    )
    entry = next(v for v in out["venues"] if v["venue"] == "trading212")
    assert entry["account"]["total_usd"] == 43.61
    assert entry["account"]["excluded_value_usd"] == 10.25
    since = entry["equity_change"]["since_first_sample"]
    assert since["equity_change_usd"] == 0.11
    assert since["excluded_change_usd"] == 0.05
    assert since["change_usd"] == pytest.approx(0.11 - 5.0 - 0.05, abs=0.006)

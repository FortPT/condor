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
KRAKEN = native_venues.get("kraken_xstocks")
REVOLUT = native_venues.get("revolut_x")
PAPER = native_venues.get("xstocks_paper")
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
    _, other = add("kraken_xstocks", source="api", external_id="deposit:1")
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
        add("xstocks_paper")
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


KRAKEN_PAGE_1 = {
    "ledger": {
        "L1": {
            "refid": "R1",
            "time": 1_790_000_100.5,
            "type": "deposit",
            "subtype": "",
            "aclass": "currency",
            "asset": "ZEUR",
            "amount": "100.0000",
            "fee": "0.0000",
            "balance": "100.0000",
        },
        "L2": {
            "refid": "R2",
            "time": 1_790_000_200.0,
            "type": "trade",
            "subtype": "",
            "aclass": "currency",
            "asset": "ZEUR",
            "amount": "-50.0000",
            "fee": "0.2",
            "balance": "50.0000",
        },
    },
    "count": 3,
}
KRAKEN_PAGE_2 = {
    "ledger": {
        "L3": {
            "refid": "R3",
            "time": 1_790_000_300.0,
            "type": "deposit",
            "subtype": "",
            "aclass": "currency",
            "asset": "XXBT",
            "amount": "0.001",
            "fee": "0.00001",
            "balance": "0.001",
        },
    },
    "count": 3,
}
KRAKEN_WITHDRAWAL = {
    "ledger": {
        "L9": {
            "refid": "R9",
            "time": 1_790_000_900.0,
            "type": "withdrawal",
            "subtype": "",
            "aclass": "currency",
            "asset": "ZUSD",
            "amount": "-20.0000",
            "fee": "1.0000",
            "balance": "5.0000",
        },
    },
    "count": 1,
}


def test_kraken_ledger_keeps_deposits_and_withdrawals_net_of_fee():
    flows = fetchers.parse_kraken_ledger(KRAKEN_PAGE_1)
    assert [(f.external_id, f.currency, f.amount) for f in flows] == [
        ("ledger:L1", "EUR", Decimal("100.0000"))
    ]
    withdrawal = fetchers.parse_kraken_ledger(KRAKEN_WITHDRAWAL)[0]
    assert (withdrawal.currency, withdrawal.amount) == ("USD", Decimal("-21.0000"))
    btc = fetchers.parse_kraken_ledger(KRAKEN_PAGE_2)[0]
    assert (btc.currency, btc.amount) == ("BTC", Decimal("0.00099"))
    assert all(f.status == "confirmed" for f in flows)
    with pytest.raises(fetchers.FetchUnavailable):
        fetchers.parse_kraken_ledger({"result": {}})


class FakeKraken:
    def __init__(self, pages, error=None):
        self.pages = pages  # {"deposit": [page, ...], "withdrawal": [page, ...]}
        self.error = error
        self.calls = []

    async def private(self, user_id, method, params=None):
        self.calls.append((method, dict(params)))
        if self.error:
            raise self.error
        pages = self.pages[params["type"]]
        return pages[min(params["ofs"] // 2, len(pages) - 1)]

    async def eur_usd(self):
        return Decimal("1.1")


def test_kraken_pages_through_the_ledger_and_asks_only_for_ledger_entries():
    service = FakeKraken(
        {"deposit": [KRAKEN_PAGE_1, KRAKEN_PAGE_2], "withdrawal": [KRAKEN_WITHDRAWAL]}
    )
    got = run(fetchers.fetch("kraken_xstocks", service, USER, T0, T0 + 1000))
    assert got.problems == []
    assert sorted(f.external_id for f in got.flows) == [
        "ledger:L1",
        "ledger:L3",
        "ledger:L9",
    ]
    assert {method for method, _ in service.calls} == {"Ledgers"}
    assert [p["ofs"] for m, p in service.calls if p["type"] == "deposit"] == [0, 2]


def test_kraken_failure_is_reported_per_part():
    service = FakeKraken({}, error=ValueError("EAPI:Invalid key"))
    got = run(fetchers.fetch("kraken_xstocks", service, USER, T0, T0 + 1000))
    assert got.flows == []
    assert got.problems == [
        "deposits: EAPI:Invalid key",
        "withdrawals: EAPI:Invalid key",
    ]


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
        run(fetchers.fetch("xstocks_paper", object(), USER, T0, T0))
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
    # A venue the registry does not know, or one with no account, is a plain answer.
    assert run(nf.sync(USER, "nope", FakeRevolut(), now=T0))["ok"] is False
    assert run(nf.sync(USER, "xstocks_paper", FakeRevolut(), now=T0))["ok"] is False


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


def test_kraken_xstock_tokens_match_their_symbol_and_a_base_fee_is_in_usd():
    detect(view_of(USD=(100, 100), AAPLx=(0, 0)), now=T0, venue="kraken_xstocks")
    order = {
        "time": T0 + 100,
        "side": "buy",
        "symbol": "AAPL",
        "status": "closed",
        "quantity": "0.2",
        "value": "50",
        "fee": "0.2",
        "fee_currency": "AAPLx",
    }
    # Fee 0.2 USD at 250 USD/share = 0.0008 shares taken from the units.
    after = view_of(USD=(50, 50), AAPLx=(0.1992, 49.8))
    assert detect(after, ledger_of([order]), now=T0 + 300, venue="kraken_xstocks") == []


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
    detect(view_of(EUR=(100, 110), USD=(5, 5)), now=T0, venue="kraken_xstocks")
    # 100 EUR sold for 109.8 USD (a 0.2 USD cost): two moves that offset.
    converted = view_of(EUR=(0, 0), USD=(114.8, 114.8))
    assert detect(converted, now=T0 + 300, venue="kraken_xstocks") == []
    assert nf.pending(USER, "kraken_xstocks") == []


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


def test_detection_never_runs_for_a_venue_without_an_account():
    with pytest.raises(ValueError):
        detect(view_of(USD=(1, 1)), venue="xstocks_paper")


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
    def __init__(self, total, quote="USD"):
        self.total = total
        self.quote = quote

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
        return {
            "connector": "x",
            "total_usd": self.total,
            "balances": [
                {"token": "USD", "total": 20, "available": 20, "usd_value": 20}
            ],
            "note": "",
        }


def sample(venue, ts, total):
    assert ph.record(USER, venue.label, total, now=ts)


def perf(spans, total, now, venue=EXCHANGE):
    return run(
        na.performance(
            venue, USER, spans, services={venue.key: FakeVenue(total)}, now=now
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
                "kraken_xstocks": FakeVenue(0.0),
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
    other = next(v for v in out["venues"] if v["venue"] == "kraken_xstocks")
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

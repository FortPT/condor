"""Deposits and result of the Gateway's Solana wallet, read from the chain (condor/solana_wallet.py)."""

import asyncio
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import condor.web.routes.dex as dex_routes
from condor import solana_wallet as sw
from condor.web.auth import get_current_user
from condor.web.models import WebUser

WALLET = "85maohASGbekdraAsBkX9XLXppwmFDpF9nnboKPCSqbn"
PHANTOM = "Bnvnmj27khnr6yxtV6RzNamaXJT838NTi31d3Kab24Nw"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def run(coro):
    return asyncio.run(coro)


def tx(payer, wallet_pre, wallet_post, ts=1_793_489_183, tokens=None):
    """A getTransaction answer: ``payer`` signs, the wallet's SOL goes pre -> post (in lamports)."""
    keys = (
        [{"pubkey": payer}, {"pubkey": WALLET}]
        if payer != WALLET
        else [{"pubkey": WALLET}]
    )
    index = len(keys) - 1
    pre, post = [0] * len(keys), [0] * len(keys)
    pre[index], post[index] = wallet_pre, wallet_post
    meta = {
        "preBalances": pre,
        "postBalances": post,
        "preTokenBalances": [],
        "postTokenBalances": tokens or [],
    }
    return {
        "blockTime": ts,
        "transaction": {"message": {"accountKeys": keys}},
        "meta": meta,
    }


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    return tmp_path


class FakeRpc:
    def __init__(self, signatures, txs, fail=False):
        self.signatures, self.txs, self.fail, self.calls = signatures, txs, fail, []

    async def __call__(self, method, params):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError("rpc down")
        if method == "getSignaturesForAddress":
            return [{"signature": s, "err": None} for s in self.signatures]
        return self.txs.get(params[0])


# ── what counts as a deposit ──


def test_a_transfer_in_from_another_wallet_is_valued_at_the_sol_price():
    deposit = tx(PHANTOM, 0, 312_000_000)
    assert sw.deposit_usd(deposit, WALLET, 95.0) == pytest.approx(29.64)


def test_a_transaction_the_wallet_signed_is_not_a_deposit():
    swap = tx(WALLET, 500_000_000, 300_000_000)
    assert sw.deposit_usd(swap, WALLET, 95.0) == 0.0


def test_a_transfer_out_that_the_wallet_did_not_sign_nets_off():
    assert sw.deposit_usd(
        tx(PHANTOM, 300_000_000, 200_000_000), WALLET, 100.0
    ) == pytest.approx(-10.0)


def test_usdc_in_is_worth_its_face_value():
    tokens = [
        {
            "accountIndex": 1,
            "owner": WALLET,
            "mint": USDC,
            "uiTokenAmount": {"uiAmount": 12.5},
        }
    ]
    deposit = tx(PHANTOM, 5000, 5000, tokens=tokens)
    assert sw.deposit_usd(deposit, WALLET, None) == pytest.approx(12.5)


def test_sol_cannot_be_valued_without_a_price_yet():
    assert sw.deposit_usd(tx(PHANTOM, 0, 1_000_000_000), WALLET, None) is None
    assert sw.deposit_usd(tx(PHANTOM, 0, 1_000_000_000), WALLET, 0) is None


def test_a_transaction_without_keys_is_not_a_deposit():
    assert sw.deposit_usd({}, WALLET, 95.0) == 0.0


# ── remembering them ──


def test_new_transactions_are_read_valued_once_and_remembered():
    rpc = FakeRpc(
        ["a", "b"],
        {"a": tx(PHANTOM, 0, 312_000_000, ts=2), "b": tx(WALLET, 5, 4, ts=3)},
    )
    state = run(sw.refresh_deposits(WALLET, 95.0, rpc))
    assert [row["usd"] for row in sw.deposits_of(state)] == [pytest.approx(29.64)]
    assert state["txs"]["b"]["own"] is True
    # a second read costs no transaction lookups, and a later price does not re-value the deposit
    again = FakeRpc(["a", "b"], {})
    state = run(sw.refresh_deposits(WALLET, 200.0, again))
    assert again.calls == ["getSignaturesForAddress"]
    assert sw.deposits_of(state)[0]["usd"] == pytest.approx(29.64)


def test_a_failing_rpc_keeps_what_is_known():
    run(
        sw.refresh_deposits(
            WALLET, 95.0, FakeRpc(["a"], {"a": tx(PHANTOM, 0, 100_000_000, ts=2)})
        )
    )
    state = run(sw.refresh_deposits(WALLET, 95.0, FakeRpc([], {}, fail=True)))
    assert len(sw.deposits_of(state)) == 1


def test_a_deposit_waiting_for_a_price_is_left_for_the_next_read():
    rpc = FakeRpc(["a"], {"a": tx(PHANTOM, 0, 100_000_000, ts=2)})
    assert sw.deposits_of(run(sw.refresh_deposits(WALLET, None, rpc))) == []
    assert len(sw.deposits_of(run(sw.refresh_deposits(WALLET, 95.0, rpc)))) == 1


def test_at_most_a_few_new_transactions_are_looked_up_per_read():
    names = [f"s{i}" for i in range(9)]
    rpc = FakeRpc(
        names, {n: tx(PHANTOM, 0, 10_000_000, ts=i + 1) for i, n in enumerate(names)}
    )
    state = run(sw.refresh_deposits(WALLET, 100.0, rpc))
    assert rpc.calls.count("getTransaction") == sw.MAX_NEW_TX
    assert len(state["txs"]) == sw.MAX_NEW_TX


def test_state_for_another_address_is_not_reused():
    run(
        sw.refresh_deposits(
            WALLET, 95.0, FakeRpc(["a"], {"a": tx(PHANTOM, 0, 100_000_000, ts=2)})
        )
    )
    assert sw.load_state("someoneelse")["txs"] == {}


# ── the result ──


def test_the_result_is_value_minus_deposits_and_a_new_wallet_starts_at_zero():
    state = {
        "address": WALLET,
        "txs": {"a": {"ts": 2, "usd": 29.75}, "b": {"ts": 1, "usd": 1.67}},
    }
    out = sw.overview(state, 31.42, now=1_793_490_000)
    assert out["deposits_usd"] == pytest.approx(31.42)
    assert out["since_start_usd"] == pytest.approx(0.0)
    assert out["today_usd"] == pytest.approx(0.0)
    assert [d["signature"] for d in out["deposits"]] == ["a", "b"], "newest first"


def test_today_is_the_change_since_the_first_read_of_the_utc_day():
    state = {"address": WALLET, "txs": {"a": {"ts": 2, "usd": 30.0}}}
    day1 = 1_793_490_000
    sw.overview(state, 30.0, now=day1)
    assert sw.overview(state, 30.5, now=day1 + 600)["today_usd"] == pytest.approx(0.5)
    out = sw.overview(state, 29.0, now=day1 + 86_400)
    assert out["today_usd"] == pytest.approx(
        0.0
    ), "a new day starts from where the last one ended"
    assert out["since_start_usd"] == pytest.approx(-1.0)


def test_a_profit_is_not_a_deposit():
    state = {"address": WALLET, "txs": {"a": {"ts": 2, "usd": 30.0}}}
    assert sw.overview(state, 33.0, now=time.time())[
        "since_start_usd"
    ] == pytest.approx(3.0)


# ── reading the portfolio ──


def test_value_and_sol_price_come_from_the_solana_connector_only():
    state = {
        "master_account": {
            "solana-mainnet-beta": [
                {"token": "SOL", "units": 0.5, "value": 47.5},
                {"token": "USDC", "units": 5, "value": 5},
            ],
            "kraken": [{"token": "SOL", "units": 9, "value": 9999}],
        }
    }
    assert sw.wallet_from_portfolio(state) == (52.5, pytest.approx(95.0))


@pytest.mark.parametrize("state", [None, {}, {"a": {"solana-mainnet-beta": []}}])
def test_an_empty_portfolio_is_worth_nothing_and_has_no_price(state):
    assert sw.wallet_from_portfolio(state) == (0.0, None)


# ── the routes ──

USER = WebUser(id=111, username="u", first_name="U", role="user")


class FakeAccounts:
    def __init__(self, groups):
        self.groups = groups

    async def list_gateway_wallets(self):
        return self.groups


class FakePortfolio:
    async def get_state(self, **kwargs):
        return {
            "m": {
                "solana-mainnet-beta": [{"token": "SOL", "units": 0.33, "value": 31.4}]
            }
        }


class FakeSwap:
    def __init__(self, known):
        self.known = known

    async def get_swap_quote(self, **kwargs):
        base = kwargs["trading_pair"].split("-")[0]
        if base not in self.known:
            raise RuntimeError("Token not found")
        return {"price": str(self.known[base]), "price_impact_pct": "0.07"}


class FakeClient:
    def __init__(self, groups=None, known=None):
        self.accounts = FakeAccounts(
            groups
            if groups is not None
            else [{"chain": "solana", "default_address": WALLET}]
        )
        self.portfolio = FakePortfolio()
        self.gateway_swap = FakeSwap(known or {})


class FakeConfigManager:
    def __init__(self, client):
        self._client = client

    def has_server_access(self, user_id, name, *a, **kw):
        return True

    async def get_client(self, name):
        return self._client


@pytest.fixture
def route_client(monkeypatch):
    def build(client):
        cm = FakeConfigManager(client)
        monkeypatch.setattr(dex_routes, "get_config_manager", lambda: cm)
        monkeypatch.setattr("condor.web.auth.get_config_manager", lambda: cm)
        app = FastAPI()
        app.include_router(dex_routes.router)
        app.dependency_overrides[get_current_user] = lambda: USER
        return TestClient(app)

    return build


def test_the_overview_route_reports_value_deposits_and_result(
    route_client, monkeypatch
):
    rpc = FakeRpc(["a"], {"a": tx(PHANTOM, 0, 330_000_000, ts=2)})
    monkeypatch.setattr(sw, "http_rpc", rpc)
    body = route_client(FakeClient()).get("/servers/srv/solana/overview").json()
    assert body["address"] == WALLET
    assert body["value_usd"] == pytest.approx(31.4)
    assert body["deposits_usd"] == pytest.approx(0.33 * (31.4 / 0.33))
    assert body["since_start_usd"] == pytest.approx(0.0, abs=1e-3)


def test_the_overview_route_says_so_when_the_gateway_has_no_solana_wallet(route_client):
    body = (
        route_client(FakeClient(groups=[])).get("/servers/srv/solana/overview").json()
    )
    assert body == {"address": None, "deposits": []}


def test_quotes_price_each_token_and_mark_the_unknown_ones(route_client):
    client = route_client(FakeClient(known={"SOL": 95.6, "JUP": 0.26}))
    rows = client.get("/servers/srv/dex/quotes?tokens=SOL,JUP,WIF,USDC").json()[
        "quotes"
    ]
    by = {r["token"]: r for r in rows}
    assert by["SOL"]["price"] == pytest.approx(95.6)
    assert by["JUP"]["price_impact_pct"] == pytest.approx(0.07)
    assert by["WIF"] == {
        "token": "WIF",
        "price": None,
        "price_impact_pct": None,
        "error": "not quotable",
    }
    assert by["USDC"]["price"] == 1.0
    assert [r["token"] for r in rows] == ["SOL", "JUP", "WIF", "USDC"], "order is kept"


@pytest.mark.parametrize(
    "tokens", ["", ",", "SOL,../x", "A", ",".join(f"T{i}X" for i in range(13))]
)
def test_quotes_refuse_a_bad_token_list(route_client, tokens):
    assert (
        route_client(FakeClient())
        .get(f"/servers/srv/dex/quotes?tokens={tokens}")
        .status_code
        == 422
    )

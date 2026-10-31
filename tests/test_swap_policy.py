"""The swap policy: token whitelist, per-swap size and SOL fee reserve.

Three limits on ``execute_swap`` in loop mode, each off by default. They exist
because a Solana loop signs straight from a small wallet: without them nothing
but the agent's own prompt stops it swapping into an unknown token or spending the
SOL it needs to pay the fee of the swap that sells a position back.
"""

import pytest

from condor.agents.risk import (
    RiskEngine,
    RiskLimits,
    RiskState,
    _sol_in_state,
    _wallet_sol_balance,
)


def _swap(pair="SOL-USDC", side="BUY", amount="0.05") -> dict:
    return {
        "tool": "execute_swap",
        "input": {
            "connector": "jupiter/router",
            "network": "solana-mainnet-beta",
            "trading_pair": pair,
            "side": side,
            "amount": amount,
        },
    }


def _engine(**limits) -> RiskEngine:
    return RiskEngine(RiskLimits(max_position_size_quote=1000.0, **limits))


def _check(engine, call, notional=10.0, sol_balance=None):
    return engine.check_dex_action(call, RiskState(), notional, sol_balance)


# ── Defaults: nothing changes for an install that sets none of them ──


def test_all_three_limits_are_off_by_default():
    limits = RiskLimits()
    assert limits.allowed_swap_tokens == ()
    assert limits.max_swap_quote == -1.0
    assert limits.min_swap_sol_reserve == -1.0
    assert limits.max_swap_slippage_pct == -1.0
    assert limits.swap_default_wallet_only is False
    allowed, reason = _check(RiskEngine(RiskLimits()), _swap("ANYTHING-ELSE"))
    assert allowed and reason == ""


def test_the_lp_loops_unenforced_key_stays_unenforced():
    limits = RiskLimits.from_dict({"min_wallet_sol_reserve": 0.3, "max_open_slots": 3})
    assert limits.min_swap_sol_reserve == -1.0
    assert set(limits.unrecognized) == {"min_wallet_sol_reserve", "max_open_slots"}


def test_tokens_are_read_from_a_list_or_a_comma_string():
    assert RiskLimits(allowed_swap_tokens=["SOL", " usdc "]).allowed_swap_tokens == (
        "sol",
        "usdc",
    )
    assert RiskLimits(allowed_swap_tokens="SOL, USDC,").allowed_swap_tokens == (
        "sol",
        "usdc",
    )
    assert RiskLimits.from_dict({"allowed_swap_tokens": "JUP"}).allowed_swap_tokens == (
        "jup",
    )


# ── Token whitelist ──


def test_a_swap_between_allowed_tokens_passes():
    engine = _engine(allowed_swap_tokens="SOL,USDC,JUP")
    assert _check(engine, _swap("JUP-USDC"))[0]
    assert _check(engine, _swap("jup-sol", side="SELL"))[0]


def test_a_token_outside_the_list_is_refused_on_either_leg():
    engine = _engine(allowed_swap_tokens="SOL,USDC")
    allowed, reason = _check(engine, _swap("SCAMCOIN-USDC"))
    assert not allowed and "SCAMCOIN" in reason
    allowed, reason = _check(engine, _swap("SOL-SCAMCOIN"))
    assert not allowed and "SCAMCOIN" in reason


def test_a_mint_address_is_refused_unless_it_is_listed():
    mint = "9cRCexampleMintAddress1111111111111111pump"
    engine = _engine(allowed_swap_tokens="SOL,USDC")
    assert not _check(engine, _swap(f"{mint}-SOL"))[0]
    engine = _engine(allowed_swap_tokens=f"SOL,USDC,{mint}")
    assert _check(engine, _swap(f"{mint}-SOL"))[0]


@pytest.mark.parametrize("pair", ["", "SOL", "-USDC", "SOL-"])
def test_an_unreadable_pair_is_refused_when_the_list_is_on(pair):
    engine = _engine(allowed_swap_tokens="SOL,USDC")
    allowed, reason = _check(engine, _swap(pair))
    assert not allowed and "cannot be read" in reason


# ── Per-swap size ──


def test_a_swap_above_the_per_swap_limit_is_refused():
    engine = _engine(max_swap_quote=5.0)
    assert _check(engine, _swap(), notional=5.0)[0]
    allowed, reason = _check(engine, _swap(), notional=5.01)
    assert not allowed and "per-swap limit" in reason


def test_a_zero_per_swap_limit_refuses_every_swap():
    allowed, _ = _check(_engine(max_swap_quote=0.0), _swap(), notional=0.01)
    assert not allowed


# ── Slippage and wallet ──


def test_slippage_must_be_stated_and_within_the_limit():
    engine = _engine(max_swap_slippage_pct=1.0)
    call = _swap()
    assert not _check(engine, call)[0]  # none stated
    call["input"]["slippage_pct"] = "1"
    assert _check(engine, call)[0]
    call["input"]["slippage_pct"] = 1.5
    allowed, reason = _check(engine, call)
    assert not allowed and "above the limit" in reason
    call["input"]["slippage_pct"] = "lots"
    assert not _check(engine, call)[0]


def test_slippage_zero_is_a_real_value():
    engine = _engine(max_swap_slippage_pct=1.0)
    call = _swap()
    call["input"]["slippage_pct"] = "0"
    assert _check(engine, call)[0]


def test_a_named_wallet_is_refused_when_only_the_default_may_sign():
    call = _swap()
    call["input"]["wallet_address"] = "BWxBgc9VutumXPV4DD4WyBkJJxrTCfcpFvY79gYrJpea"
    assert _check(_engine(), call)[0]
    allowed, reason = _check(_engine(swap_default_wallet_only=True), call)
    assert not allowed and "default wallet" in reason
    assert _check(_engine(swap_default_wallet_only=True), _swap())[0]


# ── SOL reserve ──


def test_selling_sol_must_leave_the_reserve():
    engine = _engine(min_swap_sol_reserve=0.01)
    call = _swap("SOL-USDC", side="SELL", amount="0.1")
    assert _check(engine, call, sol_balance=0.12)[0]
    allowed, reason = _check(engine, call, sol_balance=0.105)
    assert not allowed and "kept for fees" in reason


def test_buying_a_token_with_sol_must_leave_the_reserve():
    engine = _engine(min_swap_sol_reserve=0.01)
    call = _swap("JUP-SOL", side="BUY", amount="3")
    assert _check(engine, call, notional=0.05, sol_balance=0.07)[0]
    assert not _check(engine, call, notional=0.05, sol_balance=0.06)[0]


def test_a_swap_that_spends_no_sol_needs_no_balance():
    engine = _engine(min_swap_sol_reserve=0.01)
    assert _check(engine, _swap("JUP-USDC", side="SELL"), sol_balance=None)[0]
    assert _check(engine, _swap("JUP-USDC", side="BUY"), sol_balance=None)[0]


def test_an_unreadable_balance_refuses_a_swap_that_spends_sol():
    engine = _engine(min_swap_sol_reserve=0.01)
    call = _swap("SOL-USDC", side="SELL", amount="0.02")
    allowed, reason = _check(engine, call, sol_balance=None)
    assert not allowed and "could not be read" in reason
    assert not _check(engine, call, sol_balance=float("nan"))[0]


def test_a_bad_amount_is_refused_when_the_reserve_is_on():
    engine = _engine(min_swap_sol_reserve=0.01)
    assert not _check(engine, _swap("SOL-USDC", "SELL", "lots"), sol_balance=1.0)[0]
    assert not _check(engine, _swap("SOL-USDC", "SELL", "-1"), sol_balance=1.0)[0]


def test_the_position_limit_still_applies_after_the_policy_passes():
    engine = RiskEngine(
        RiskLimits(max_position_size_quote=5.0, allowed_swap_tokens="SOL,USDC")
    )
    allowed, reason = _check(engine, _swap(), notional=6.0)
    assert not allowed and "position limit" in reason


def test_the_policy_does_not_touch_other_signing_calls():
    engine = _engine(allowed_swap_tokens="SOL,USDC", max_swap_quote=1.0)
    call = {"tool": "manage_clmm", "input": {"action": "open", "pool_address": "x"}}
    assert _check(engine, call, notional=500.0)[0]


# ── Reading the SOL balance ──


def _state(**row):
    return {
        "master_account": {
            "solana-mainnet-beta": [{"token": "SOL", **row}],
            "kraken": [{"token": "SOL", "units": 99, "available_units": 99}],
        }
    }


def test_the_sol_balance_comes_from_the_solana_connector_only():
    assert _sol_in_state(_state(units=0.5, available_units=0.4)) == 0.4
    assert _sol_in_state(_state(units=0.5)) == 0.5
    assert _sol_in_state(_state(total=0.3)) == 0.3


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"a": {"solana-mainnet-beta": [{"token": "USDC", "units": 5}]}},
        {"a": {"solana-mainnet-beta": [{"token": "SOL", "units": "n/a"}]}},
        {"a": {"solana-mainnet-beta": [{"token": "SOL", "units": -1}]}},
        {"a": {"solana-mainnet-beta": [{"token": "SOL", "units": True}]}},
    ],
)
def test_an_unreadable_state_gives_no_balance(state):
    assert _sol_in_state(state) is None


class _Portfolio:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def get_state(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class _Client:
    def __init__(self, portfolio):
        self.portfolio = portfolio


@pytest.mark.asyncio
async def test_the_balance_is_read_live_not_from_cache():
    portfolio = _Portfolio(_state(units=0.25, available_units=0.25))
    assert await _wallet_sol_balance(_Client(portfolio)) == 0.25
    assert portfolio.calls == [{"refresh": True}]


@pytest.mark.asyncio
async def test_a_failing_or_missing_client_gives_no_balance():
    assert await _wallet_sol_balance(None) is None
    failing = _Portfolio(error=RuntimeError("gateway down"))
    assert await _wallet_sol_balance(_Client(failing)) is None

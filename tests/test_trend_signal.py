"""The slow daily signals: closed candles only, the two rules, no data instead of a guess."""

import asyncio
import inspect
import typing

import pytest

from condor import native_venues, trend_signal as ts

DAY = 86_400_000
T0 = 1_760_000_000_000  # an arbitrary UTC day start, in ms


def candle(day, close, high=None, low=None, open_=None):
    return {
        "t": T0 + day * DAY,
        "o": str(open_ if open_ is not None else close),
        "h": str(high if high is not None else close),
        "l": str(low if low is not None else close),
        "c": str(close),
        "v": "1",
    }


def series(closes):
    return [candle(i, c) for i, c in enumerate(closes)]


def now_after(n_days):
    """A clock reading just after day ``n_days - 1`` closed, in seconds."""
    return (T0 + n_days * DAY) / 1000 + 60


def run(coro):
    return asyncio.run(coro)


def test_only_finished_valid_days_come_back_oldest_first():
    rows = [
        candle(2, 12),
        candle(0, 10),
        candle(1, 11),
        candle(1, 99),  # a duplicate day: one survives
        {"t": T0 + 3 * DAY, "o": "x", "h": "1", "l": "1", "c": "1"},  # not a number
        {"t": T0 + 4 * DAY, "o": "5", "h": "4", "l": "6", "c": "5"},  # high below low
        candle(5, 13),  # still open at the clock below
    ]
    bars = ts.closed_daily(rows, now=(T0 + 5 * DAY) / 1000 + 3600)
    assert [b[0] for b in bars] == [T0, T0 + DAY, T0 + 2 * DAY]
    assert ts.closed_daily(None) == [] and ts.closed_daily("junk") == []


def test_seconds_are_read_as_seconds():
    rows = [{"t": T0 // 1000, "o": "1", "h": "1", "l": "1", "c": "1"}]
    assert ts.closed_daily(rows, now=(T0 + 2 * DAY) / 1000)[0][0] == T0


def test_a_steady_rise_is_long_and_a_drop_below_the_average_is_flat():
    rising = [100 + i for i in range(200)]
    states, _ = ts.trend_states(ts.closed_daily(series(rising), now_after(200)))
    assert states[-1] is True
    assert not any(states[:50])  # no signal before the average has 50 days
    falling = rising + [rising[-1] - 3 * k for k in range(1, 40)]
    bars = ts.closed_daily(series(falling), now_after(len(falling)))
    assert ts.trend_states(bars)[0][-1] is False


def test_the_breakout_needs_a_new_20_day_high_and_leaves_below_the_10_day_low():
    closes = [100.0] * 30
    rows = series(closes)
    rows.append(candle(30, 105, high=106))  # closes above the prior 20-day high
    for i in range(31, 40):
        rows.append(candle(i, 104, high=105, low=103))
    bars = ts.closed_daily(rows, now_after(40))
    states = ts.breakout_states(bars)
    assert states[29] is False and states[30] is True and states[39] is True
    rows.append(candle(40, 90, high=104, low=89))  # below the prior 10-day low
    states = ts.breakout_states(ts.closed_daily(rows, now_after(41)))
    assert states[-1] is False


def test_too_little_or_stale_history_is_no_data_never_a_guess():
    short = ts.evaluate(
        ts.TREND_50,
        ts.closed_daily(series([1.0 + i for i in range(60)]), now_after(60)),
        now_after(60),
    )
    assert short["signal"] == "no_data" and "need" in short["reason"]
    closes = [100 + i for i in range(150)]
    bars = ts.closed_daily(series(closes), now_after(150))
    stale = ts.evaluate(ts.TREND_50, bars, now_after(150) + 10 * DAY / 1000)
    assert stale["signal"] == "no_data" and "days old" in stale["reason"]
    fresh = ts.evaluate(ts.TREND_50, bars, now_after(150))
    assert (
        fresh["signal"] == "long"
        and fresh["gap_pct"] > 0
        and fresh["days_in_state"] > 50
    )
    assert fresh["last_closed_day"]


def test_the_breakout_answer_names_its_levels():
    rows = series([100.0] * 130)
    rows.append(candle(130, 110, high=111))
    out = ts.evaluate(
        ts.BREAKOUT_20_10, ts.closed_daily(rows, now_after(131)), now_after(131)
    )
    assert out["signal"] == "long" and out["days_in_state"] == 1
    assert out["prior_high"] == 100.0 and out["prior_low"] == 100.0


@pytest.mark.parametrize(
    ("signal", "held", "action"),
    [
        ("long", False, "buy"),
        ("long", True, "hold"),
        ("flat", True, "sell"),
        ("flat", False, "hold"),
        ("no_data", True, "none"),
        ("no_data", False, "none"),
    ],
)
def test_the_action_follows_signal_and_holding(signal, held, action):
    assert ts.action_for(signal, held) == action


def test_the_report_maps_each_coin_and_treats_dust_as_not_held():
    rising = series([100 + i for i in range(150)])
    clock = now_after(150)

    async def fetch(coin):
        if coin == "ETH":
            raise TimeoutError("down")
        return rising

    out = run(
        ts.report(ts.TREND_50, fetch, {"BTC": {"units": "0.0001", "cost": "1"}}, clock)
    )
    btc, eth = out["coins"]
    # 0.0001 units of a ~250 coin is worth 0.025: dust, so the long signal still says buy
    assert btc["signal"] == "long" and btc["held"] is False and btc["action"] == "buy"
    assert (
        eth["signal"] == "no_data"
        and eth["action"] == "none"
        and "TimeoutError" in eth["reason"]
    )
    held = run(
        ts.report(ts.TREND_50, fetch, {"BTC": {"units": "0.5", "cost": "50"}}, clock)
    )["coins"][0]
    assert (
        held["held"] is True
        and held["action"] == "hold"
        and held["held_value"] > ts.DUST_VALUE
    )
    assert out["rule"] == "trend50" and "places no order" in out["note"]


def test_each_venue_service_answers_from_its_own_daily_candles(monkeypatch):
    from condor import crypto_com_exchange as cx
    from condor import revolut_x as rx

    rising = series([100 + i for i in range(150)])
    clock = now_after(150)
    monkeypatch.setattr(ts.time, "time", lambda: clock)
    asked = []

    for module, name, ledger in (
        (rx, "TREND_50", {"positions": {"ETH": {"units": "1", "cost": "100"}}}),
        (cx, "BREAKOUT_20_10", {"positions": {}}),
    ):
        service = module.service

        async def fake_candles(user_id, coin, timeframe, count, _asked=asked):
            _asked.append((coin, timeframe, count))
            return {"candles": rising}

        monkeypatch.setattr(service, "candles", fake_candles)
        monkeypatch.setattr(module, "_read_ledger", lambda user_id, _l=ledger: _l)
        out = run(service.trend(1))
        assert out["rule"] == getattr(ts, name).key
        assert [c["coin"] for c in out["coins"]] == ["BTC", "ETH"]
    assert set(asked) == {("BTC", "1D", 300), ("ETH", "1D", 300)}


def test_the_new_crew_agents_trade_their_venue():
    assert native_venues.for_agent("revolut_x_trend_trader").key == "revolut_x"
    assert (
        native_venues.for_agent("crypto_com_trend_trader").key == "crypto_com_exchange"
    )
    assert native_venues.for_agent("revolut_x_trader").key == "revolut_x"


def test_the_tool_offers_the_trend_action_on_both_venues():
    from mcp_servers.condor import server

    for tool in (server.revolut_x, server.crypto_com_exchange):
        action = inspect.signature(tool).parameters["action"].annotation
        assert "trend" in typing.get_args(action)

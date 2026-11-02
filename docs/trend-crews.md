# Slow trend crews (Revolut X and Crypto.com)

Two small crews that follow a daily rule instead of judging charts. The rule is computed in
code (`condor/trend_signal.py`); the crew compares the answer with what the bot holds and
orders only when they differ. There is no analyst and no reviewer: the native guards (bot
allocation, 40% cap per coin below 1000 of equity, equity floor, the cost guard on Revolut X
buys and the owner's enable switch) stay authoritative.

| Crew | Agent | Rule | Coins |
|---|---|---|---|
| Revolut X | `revolut_x_trend_trader` | long while the last closed day is above its 50-day average | BTC, ETH |
| Crypto.com | `crypto_com_trend_trader` | long after a close above the prior 20-day high, flat after a close below the prior 10-day low | BTC, ETH |

## The signal

`revolut_x(action="trend")` and `crypto_com_exchange(action="trend")` (also
`GET /api/v1/revolut-x/trend` and `/crypto-exchange/trend`, read-only) read 300 daily candles from
the venue itself, drop today's unfinished candle, and answer per coin: `signal` (`long`, `flat`
or `no_data`), the numbers behind it, what the bot holds and the `action` that follows (`buy`,
`sell`, `hold`, `none`). Too few or stale candles, or a failed read, is `no_data`: never a guess.
A holding worth under 2 in the quote currency is dust, not a position.

## Why these rules

On 30 days of 15-minute candles and a year of hourly ones, no intraday setup beat a round trip
of about 0.3% (Revolut X) to 1.2% (Crypto.com), and Revolut X's maker fee equals its taker fee
below $10K of 30-day volume. On nine years of BTC and ETH daily candles (long only, signal at
the close, trade at the next open, after those costs) these rules made 5 to 12 trades a year and
ended at or above holding with a smaller worst drop. They do not remove losses: a bear market
still costs 40% or more, and results come from two big bull cycles.

## Running them

The loop (`daily`) ticks every four hours; the signal changes at most once a day, so most ticks
do nothing and a missed tick costs hours. Entries are about 35% of the bot's equity per coin.
Starting a crew is an owner action. If the signal is already long when the loop first runs and
the bot holds nothing, the rule buys.

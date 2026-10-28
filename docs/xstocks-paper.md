# xStocks paper test (Kraken public data)

A paper-only test of intraday trading on tokenized US stocks. It needs no Kraken
account, no API key and no money: Condor reads Kraken's public market data, an agent
logs paper calls, and the scorecard scores them against later prices after a
round-trip cost. The tool has no order action, so nothing here can trade.

## Why paper first

Stock trading is cost-sensitive. At Kraken's reported taker fee of 0.40% each way
(from a support article, not verified against Kraken's fee page) plus spread, a round
trip costs about 0.85% (`ROUND_TRIP_COST_PCT` in `condor/xstocks_paper.py`). A broad
ETF such as SPY moves about 0.4% a day and cannot clear that; a volatile single stock
(TSLA, NVDA, META) moves 1.5-3.5% and might. The test measures whether an agent's
calls beat the cost before any real money or key is involved.

## Data

* `GET /api/v1/xstocks-paper/coins` lists the most traded xStocks, quoted in USD,
  with bid, ask, spread, 24h dollar volume, 24h range and the move since the open.
  Only online `<TICKER>x/USD` pairs from Kraken's `tokenized_asset` asset class are
  listed (about 177). Kraken needs `asset_class=tokenized_asset` on its public
  endpoints; the service sends it.
* `GET .../candles` returns OHLCV for 5m, 15m, 30m, 1h, 4h and 1D.
* `GET .../status` says whether the regular US session (09:30-16:00 New York, Monday
  to Friday) is open. The US daylight-saving rule is built in; exchange holidays are
  not modelled.

## Paper calls and the scorecard

`xstocks_paper(action="predict", symbol, verdict, target_pct, stop_pct,
horizon_hours, note)` logs one call; `verdict` is `trade` for a call the agent would
have entered and `pass` for a near-miss it declined. The scorecard is the one the
Crypto.com Exchange trader uses, kept in its own database
(`CONDOR_DATA_DIR/xstocks_paper/<user>/scorecard.db`) so the two never mix. Outcomes
are recomputed on every report: target if the price reaches it before the stop, stopped
if the stop comes first, otherwise the last price inside the window, all net of the
round-trip cost, against the average of all listed stocks as a baseline. About 30
resolved calls are needed before the numbers mean anything.

## Agent

`xstocks_paper_trader` runs the `paper_session` loop every 10 minutes. Outside the
regular session it journals one line and stops, so it costs almost nothing overnight and
at weekends. During the session it shortlists liquid stocks (spread at most 0.1%, 24h
dollar volume at least 50,000, range at least 1.5%), reads their candles and logs one
call per tick. It starts only by an explicit owner action.

## Known limits

Kraken's xStock fees are not published on the public pair data and are unverified.
Trading hours and holidays follow the US session, but the tokens also trade outside it
(thinly), so the scorecard can see prices the real market has not set. Fills are not
simulated: a paper call is scored at the listed price, so slippage is covered only by
the cost assumption. Eligibility, the fee on the real account and whether a real
connector works with the `tokenized_asset` class are separate questions this test does
not answer.

# Crypto.com Exchange and Claude

The container includes pinned Claude Code 2.1.311 and claude-agent-acp 0.97.0.
`HOME=/var/lib/condor` is the existing persistent, private state volume. Authenticate
as container UID 10001 with `claude auth login`; no API key or copied account token
is needed. Do not embed credentials in Compose, an image or Git.

Settings → Crypto.com Exchange accepts an Exchange API key and secret, created under
User Center → API on the Crypto.com Exchange site. Create the key with trading
permission only (no withdrawal) and restrict it to this server's IP address. Keys are
stored in a per-user 0600 file under `CONDOR_DATA_DIR/crypto_com_exchange`, never sent
back to the UI or the model. Saving or replacing them pauses trading. The environment
can be `production` (`api.crypto.com`) or `uat` (the Exchange sandbox, with its own
keys), so the whole path can be tried without real money.

This is the Exchange, not the Crypto.com App. Orders go to the public order book, so a
buy followed by a sell loses the spread plus the fee (hundredths of a percent on liquid
coins, measured 0.02-0.19% on the first six coins), where the App's quotations cost
about 5% per round trip. Funds move between the App and the Exchange by a transfer the
owner makes; Condor never moves money.

## Allocation and ledger

The bot trades only an allocation the owner syncs from the Exchange account
(Settings → Sync, `POST /crypto-exchange/sync`): the quote-currency cash (USD by
default; USDT, USDC or EUR when the account holds those) and every coin with a pair in
that currency. Cost basis for coins already in the account restarts at their current
bid. Syncing is refused while trading is on or an order is uncertain, and trading
cannot be enabled before the first sync.

The ledger is in the quote currency. Below `GUARD_THRESHOLD` (1000) of total bot equity
the bot may use all of its money; only two rules stay: no single asset above 40% of
equity (`MAX_ASSET_FRACTION`) and a stop of new exposure at 2 total equity (`FLOOR`;
an equity floor, not cumulative losses). From 1000 the 50% cash reserve, three positions
and one-sixth position limit apply as well. Equity is cash plus each position at the
live best bid.

## Orders

`preview` sizes and prices an order from the live book without placing it. `order`
places an immediate-or-cancel limit priced `max_slippage_pct` (default 0.3, at most 2)
through the touch, so a fast market cannot fill it at a worse price than accepted.
A buy's amount is the quote currency to spend; a sell's amount is coin units. Both
round down to the pair's lot size (SHIB trades in lots of 10,000), so a remainder
smaller than one lot stays in the ledger. A pair whose spread is above 1% is refused.

The fill is read from the Exchange order detail (cumulative quantity, value and fee),
not assumed. A fee paid in the coin or the quote currency adjusts the ledger; a fee in
another currency (for example CRO) is recorded but not applied. POSTs are never
retried. A new order is written to the ledger as uncertain before it is sent, with a
`client_oid` the Exchange rejects if reused; if the outcome is unknown, trading pauses
until `reconcile` reads the order by that id. An order the Exchange never saw is
cleared only after a minute and a check of recent history. Resuming after
reconciliation remains an operator action.

Requests are signed with HMAC-SHA256 over the method, id, API key, the flattened
parameters and the nonce, as the Exchange documents. The Exchange allows 15 requests
per 100 ms for order methods; Condor limits itself to 90 per minute.

## Market data

`coins` lists the top pairs by 24-hour volume in the quote currency with bid, ask,
spread and lot size (held coins are always included); `candles` returns OHLCV
(1m, 5m, 15m, 30m, 1h, 2h, 4h, 12h, 1D, 7D, 14D, 1M); `book` returns the top levels.
These are public endpoints. Only 7 pairs are quoted in EUR (ADA, BTC, DOGE, ETH, PAXG,
SOL, XRP); most are USD or USDT.

## Trader scorecard (paper test)

Before trusting the trader with money, its calls are scored against real prices.
Two things are stored in `scorecard.db` beside the ledger:

* **Prices**: the listed price of every coin the coin list returns, at most once per
  4 minutes per coin, kept 90 days.
* **Calls**: logged by the trader with `crypto_com_exchange(action="predict",
  symbol="SOL", verdict="trade"|"pass", target_pct, stop_pct, horizon_hours, note)`.
  One open call per coin. `pass` records the best near-miss it declined. Logging
  never places an order.

Outcomes are not stored; `crypto_com_exchange(action="scorecard")` recomputes them
from the prices: *target* if the price reaches it before the stop, *stopped* if the
stop comes first, otherwise the last price inside the window; all net of
`ROUND_TRIP_COST_PCT` (1%, a conservative Exchange estimate: up to 0.5% fee a side plus
half a spread). The report also shows the average of all listed coins over the same
windows as a baseline, and what each cost multiple (1x, 1.5x, 2x, 3x) would have
returned. About 30 resolved calls are needed before the numbers mean anything.

## Agents

The shared `crypto_com_exchange` MCP tool is mounted through existing tool profiles.
`order` is classified as dangerous/mutating, so attended confirmations and dry-run
rejection still work. Credentials, sync and enable/disable are not MCP actions. The
shipped Crypto.com Exchange Trader / Daily Spot loop evaluates every 15 minutes, can
choose any listed pair, and uses the same enforced order path. It starts only by an
explicit operator action, then may resume after a restart.

Known limits: only the synced allocation and its confirmed proceeds are tracked;
outside trades and transfers are not imported into the ledger. Reconciliation reads
the latest 50 orders. Fee schedules, minimum order sizes and the order-detail field
names must be checked against the actual account (use `uat` first) before enabling
autonomous execution; fixtures are not proof of a working live association or a
profitable strategy.

## Portfolio dashboard

The main Portfolio page includes the authenticated user's Exchange balances
automatically, even while trading is paused. They are added after reading the shared
Hummingbot cache and never written into it. Balances are valued in USD at the last
traded price (EUR through BTC_USD / BTC_EUR); a coin with no USD price is listed with
zero value and named in a note. The whole-account balance does not increase the bot's
allocation.

The packaged runtime disables automatic `uv` synchronization for MCP children as well
as the parent process. Otherwise the default development group is resolved inside an
offline production image and both stdio connections close before they can expose any
tools. The container smoke check initializes and lists tools on both actual MCP
subprocesses with networking disabled, without calling an account or placing an order.

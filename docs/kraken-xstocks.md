# Kraken xStocks (live trading)

Live trading of Kraken's tokenized US stocks (xStocks, pairs like `AAPLxUSD`) through
Kraken's spot REST API, within an allocation the owner syncs from the Kraken account. It
mirrors the Crypto.com Exchange service (`condor/crypto_com_exchange.py`); it changes neither.

## Read this first: what is verified and what is not

**Verified** (on Kraken's public data, no key involved; `condor/xstocks_market.py`):

* The pair list: `AssetPairs` with `aclass_base=tokenized_asset`, about 177 online
  `<TICKER>xUSD` pairs (the `...SPVUSD` pairs are ignored).
* `Ticker`, `OHLC` and `Depth` with `asset_class=tokenized_asset`.
* The regular-session clock (`GET /api/v1/kraken-xstocks/market`) and the scorecard.
* The request signature formula, against the vector published in Kraken's own docs
  (`tests/test_kraken_xstocks.py`). The formula is verified; that Kraken accepts the
  Condor request built from it has not been.

**NOT verified against a real account** until the first tiny real order (nothing here has
ever talked to a private Kraken endpoint; every test uses fakes):

* The field names and shapes of the private answers: `BalanceEx` / `Balance`, `AddOrder`
  (`txid`), `QueryOrders`, `OpenOrders` / `ClosedOrders` (`vol_exec`, `cost`, `fee`,
  `price`, `status`, `oflags`, `cl_ord_id`, `opentm`, `closetm`) and `TradeVolume`
  (`fees` / `fees_maker`, `fee`).
* Whether `asset_class=tokenized_asset` belongs on `AddOrder` (it is sent) and whether any
  other private endpoint needs it (it is not sent there). If Kraken refuses an unknown
  parameter, the first order is rejected as a definite refusal and nothing is placed.
* The exact asset code Kraken uses for a tokenized balance (`AAPLx` is assumed; the
  AssetPairs `base` field is used, with the websocket name as fallback) and whether
  `Balance` / `BalanceEx` list tokenized assets without an asset-class argument.
* Whether `QueryOrders` / `ClosedOrders` echo `cl_ord_id`. If they do not, `reconcile`
  cannot match by client id and stops with "keep trading paused" rather than guess.
* The fee tier the account is on, and whether the owner's Kraken+ zero-fee plan covers
  API orders or xStocks at all. The service never assumes a fee: `preview` reports the
  account's own taker fee from `TradeVolume`, and says "fee unknown" (never zero) when
  Kraken does not answer.
* Whether `oflags=fciq` (fee in USD on both sides) is honoured. The ledger handles a fee
  taken in the stock (`fcib` in the order's `oflags`), but that branch is untested live.
* There is no sandbox here. Kraken has only production, so the first order is the test.

**The first order must be tiny**: the owner will use the 0.5 USD minimum order
(`costmin`). Place it by hand (Settings sync and enable, then one `preview` and one
`order` of about 1 USD) during the regular US session, look at the Kraken order in the
Kraken UI, and compare it with the ledger before enabling any loop. If the fill read-back
or the balance fields differ from what is documented here, fix the field names before
going further.

## Connecting

Settings → Kraken xStocks takes an API key and secret created on Kraken under
Security → API. Give it query-funds, query-orders and create/modify-orders only (no
withdrawal) and restrict it to this server's IP address. The secret must be base64 (as
Kraken issues it). They are stored in a per-user 0600 file under
`CONDOR_DATA_DIR/kraken_xstocks/<user>/credentials.json`, never sent back to the UI or the
model. Saving or replacing them pauses trading.

The Settings page shows a pending label on every button while it runs, a visible error
line for every failed call (with a fallback text when the server gives no reason) and, for a
disabled button, the reason it is disabled. An earlier Settings page (Crypto.com) once
appeared to do nothing on Sync and Enable; a disabled button or an empty error text were the
likely causes, so neither can be silent here.

## Allocation and ledger

The bot trades only an allocation the owner syncs from the Kraken account (Settings → Sync,
`POST /kraken-xstocks/sync`): the available USD (`ZUSD`) and every online xStock balance.
Cost basis for stocks already in the account restarts at their current bid. Syncing is
refused while trading is on or an order is uncertain, and trading cannot be enabled before
the first sync.

The ledger (`ledger.json`, beside the credentials) is in USD and keyed by the plain ticker
(`AAPL`), while Kraken names the asset `AAPLx` and the pair `AAPLxUSD`. The same rules as the
Exchange bot apply: below 1000 of total bot equity no single stock may exceed 40% of equity;
new exposure stops at 2 total equity (a floor, not cumulative losses); from 1000 the 50%
cash reserve, three positions and one-sixth position limit apply too. Equity is cash plus
each position at the live best bid. A buy must also leave 1% of its value in cash for the
fee.

## Orders

* Only the online `<TICKER>xUSD` pairs can be traded. Any other symbol, quote currency or
  asset class is refused before anything is sent.
* Orders are refused outside the regular US session (Monday to Friday, 09:30 to 16:00 New
  York; the daylight-saving rule is built in, exchange holidays are not modelled). The tokens
  also trade outside it, thinly; the bot does not.
* `preview` sizes and prices an order from the live book without placing it, and reports
  `taker_fee_pct` and `estimated_round_trip_pct` (spread plus the taker fee each way) from the
  account's own `TradeVolume`; both are `null` with a `fee_note` when the fee is unknown.
* `order` places an immediate-or-cancel limit (`ordertype=limit`, `timeinforce=IOC`,
  `oflags=fciq`, `asset_class=tokenized_asset`, a UUID `cl_ord_id`) priced `max_slippage_pct`
  (default 0.3, at most 2) through the touch. Price rounds to the pair's `tick_size` (up for a
  buy, down for a sell) and volume down to `lot_decimals` (8: xStocks are fractional). A
  buy's amount is the USD to spend; a sell's amount is stock units. Orders below the pair's
  `ordermin` or `costmin` (0.5 USD) are refused locally. A spread above 1% is refused.
* The fill is read back with `QueryOrders` (`vol_exec`, `cost`, `fee`): an IOC ends `closed`
  (filled or partly) or `canceled`/`expired` with nothing executed. Only the executed
  quantity moves the ledger. The fee is added to a buy's cost and taken off a sale's proceeds.
* POSTs are never retried. A new order is written to the ledger as uncertain before it is sent.

### Definite versus uncertain

Kraken answers HTTP 200 with `{"error": [...], "result": {...}}`; entries starting with `W` are
warnings. A refusal of `AddOrder` is **definite** (no order exists, the uncertain entry is
cleared, trading stays as it was) when every error starts with `EAPI:Invalid`,
`EAPI:Rate limit`, `EGeneral:Permission denied`, `EGeneral:Invalid`, `EOrder:` or `EQuery:`.
Everything else is **uncertain**: `EService:*`, `EGeneral:Internal error`,
`EGeneral:Temporary lockout`, an unknown code, HTTP 5xx, a timeout or a transport error. An
uncertain order pauses trading until `reconcile`. An error from the read-back after `AddOrder`
succeeded is never treated as a rejection: the order exists, so the entry stays uncertain (with
its `txid`).

`reconcile` looks the order up by `txid` when known, otherwise in `OpenOrders` and
`ClosedOrders` by `cl_ord_id`. A found final order is applied to the ledger; a still-open one
is left alone; an order Kraken never saw is cleared only after a minute and a history check.
An order of the same pair and side opened since the attempt that carries no client id is
treated as possibly ours, and reconcile refuses to clear (clear it by hand after checking
Kraken's history). Resuming trading after reconciliation remains an owner action.

## Rate limits and signing

`POST https://api.kraken.com/0/private/<Method>` with an urlencoded body (an increasing
millisecond `nonce` first), headers `API-Key` and `API-Sign` =
`base64(HMAC-SHA512(base64decode(secret), urlpath + SHA256(nonce + body)))`. Condor limits
itself to 18 private calls a minute (Kraken's starter counter decays 0.33 a second).

## Agents and scorecard

The `kraken_xstocks` MCP tool exposes `status`, `valuation`, `accounts`, `history`, `coins`,
`candles`, `book`, `preview`, `order`, `reconcile`, `predict` and `scorecard`; credentials, sync
and enable/disable are not MCP actions. `order` is classified dangerous/mutating (attended
sessions confirm it, a rehearsal refuses it); the other actions are read-only.

The crew is `kraken_xstocks_trader` (loop `intraday`, every 5 minutes), `kraken_xstocks_analyst`
and `kraken_xstocks_risk_reviewer`, with the same bounded-intent protocol as the Crypto.com
crew (analyst, then preview, then reviewer, then a fresh preview, then one order). The analyst
and reviewer have only the journal; the trader reads the candles and passes the analyst a
summary. The owner's instruction is aggressive intraday trading (about 100 round trips a
month): a target of at least 1.5 times the preview's round-trip cost and at least 1.0%, gross
reward-to-risk of at least 1.5:1, a horizon that ends before the close, entries of 15-25% of
equity, every held stock reviewed for an exit each tick, and no new entries on a day the bot's
equity is 5% below its open. The fee is never hard-coded: it comes from the preview. The loop
starts only by an explicit owner action.

The live scorecard (`predict` / `scorecard`) keeps its own database
(`CONDOR_DATA_DIR/kraken_xstocks/<user>/scorecard.db`, venue `kraken_xstocks`) apart from the
Crypto.com one. It scores at `2 x taker fee + 0.05%` when `TradeVolume`
answers, otherwise at 0.85%.

## Portfolio dashboard

The Portfolio page adds the authenticated user's Kraken balances (connector "Kraken xStocks"),
after reading the shared Hummingbot cache and never written into it. USD counts at 1; each
xStock at its last traded price; other assets are listed with zero value and named in a note.
The whole-account balance does not increase the bot's allocation.

## Known limits

Only the synced allocation and its confirmed proceeds are tracked; outside trades and
transfers are not imported. `history` and `reconcile` read the latest 50 closed orders per page
(up to four pages for reconcile). Stocks that stop trading, corporate actions and exchange
holidays are not modelled.

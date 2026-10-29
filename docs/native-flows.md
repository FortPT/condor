# Deposits and withdrawals of the native venues

The loop card PnL of a native crew (Crypto.com Exchange, Kraken xStocks, Revolut X) is
the change in whole-account equity between two readings. Money the owner moves in or
out of the account is not trading, but used to read as profit or loss (a 4.27 EUR top-up
showed as +4.74 USD on Revolut X). `condor/native_flows.py` keeps a small per-venue
record of those external flows and the PnL surfaces subtract it.

Nothing in this feature moves money. The only exchange calls it adds are read-only
history reads.

## The number

```
trading result = equity at the end reading
               - equity at the start reading
               - net confirmed flows with  start reading < time <= end reading
```

* Only `confirmed` flows with a USD value are subtracted. `pending` ones (detected, or
  not final at the venue) and `dismissed` ones are not, but pending ones are shown.
* The window is the span between the two equity readings actually compared (usually the
  session boundaries, when a recorded sample sits on them; otherwise the nearest
  sample, which the card already flags as an estimate). A flow stamped exactly at the
  start belongs to the window before it (the start reading already includes it); one
  stamped exactly at the end belongs to this window, so consecutive sessions count each
  flow once.
* A flow after the last recorded sample but before "now" (no sample exists for that
  stretch) is still subtracted, because the live reading includes it. A flow before the
  first recorded sample is not, because that sample already includes it.
* The USD value is stored when the row is written (`usd_value_at_time`): EUR through the
  venue's own EUR/USD path (Revolut X's `eur_usd`, Kraken's EURUSD; Crypto.com falls back
  to Kraken's public EURUSD), USD/USDT/USDC at 1, any other coin from the account's own
  price at that moment. History is never re-priced later. A flow that cannot be priced
  is kept without a value, is not subtracted, and the figure's note says so; the owner
  can fix it by entering the USD value.
* Where it applies: the loop cards (`pnl_*` fields on the strategy summary and the
  session rows), the Now view's note, the Portfolio KPI, and the chat tool
  `native_portfolio` (`equity_change`: `change_usd` is the trading result,
  `equity_change_usd` the raw difference, `net_flows_usd` what was taken out).
* **Honest labels.** When a venue has no confirmed flow and its history could not be
  read, the figure is still the plain equity change and the label keeps saying
  "deposits and withdrawals are not tracked". Once its history was read successfully in
  the last day, or any flow is confirmed, the label becomes "Trading result: equity
  change net of recorded deposits and withdrawals".

## Where flows come from

| source | status | counted in PnL |
|---|---|---|
| `api`: the venue's own transfer history | `confirmed` (credited/debited) or `pending` | when confirmed |
| `detected`: a balance moved with no explanation | `pending` | only after the owner confirms it |
| `manual`: entered by the owner | `confirmed` | yes |

Rows are `{id, venue, ts, currency, amount (signed), usd_value_at_time, source, status,
note, external_id}` in `$CONDOR_DATA_DIR/native_flows/<user>/flows.db` (sqlite, 0700
directory). API rows are idempotent by `(venue, external_id)`; a pending one is promoted
when the venue later confirms it; one the owner dismissed stays dismissed. A dismissed
row is kept (hidden) so history is never lost.

### Venue history readers (best effort, `condor/native_flow_fetchers.py`)

Each degrades to `unavailable: <reason>` (stored with the last read, shown on the panel)
and never raises into the dashboard. They run in the five-minute value recorder (at most
every 15 minutes per venue, first read looks back 30 days) and on demand with
"Re-read venue history" (`POST /native-flows/sync`, up to 365 days back).

**None of these has been run against a live account. The shapes below are from
documentation or, where marked, a third-party client; tests use fabricated fixtures.**

* **Crypto.com Exchange**: `private/get-deposit-history` and
  `private/get-withdrawal-history`, same signing as every other private call. Params
  `start_ts`, `end_ts` (ms), `page_size` (200); answers `result.deposit_list` /
  `result.withdrawal_list`. *UNVERIFIED against the official reference*: the reference
  page (exchange-docs.crypto.com, now exchange-developer.crypto.com) could not be
  fetched as text, so names, the ms unit and the status codes were taken from the ccxt
  client. Deposit status 1 = arrived (confirmed), 0 and 3 = pending, 2 = failed
  (ignored). Withdrawal status 5 = completed (confirmed), 0, 1 and 3 = pending, 2, 4 and
  6 = failed/cancelled (ignored). The withdrawal fee is not added to the amount (the
  note says so); if the venue deducts it on top, add the fee by hand. Only one page of
  200 rows per request. **Key permission:** these are account-history reads; the
  existing trading key is expected to be enough, but that is not verified. If the call
  is refused, the panel shows the venue's error text.
* **Kraken**: `/0/private/Ledgers` with `type=deposit` and `type=withdrawal` (paged with
  `ofs`, 50 per page, up to 20 pages). Chosen over `DepositStatus`/`WithdrawStatus`,
  which need *Funds permissions: Query* (a funds permission, and `DepositStatus` is
  deprecated in favour of the Funding API) for one read-only permission:
  **`Data - Query ledger entries`**. Add it to the owner's key (no funds or withdrawal
  permission is needed). The net move is `amount - fee`; asset codes are mapped
  (`ZEUR` to `EUR`, `XXBT` to `BTC`). Tokenised-asset (xStock) transfers use another
  asset class and are not read; enter them by hand. The ledger endpoint's
  parameters are from Kraken's documentation as remembered plus the page fetched for
  `QueryLedgers`; the exact `Ledgers` page was not fetched.
* **Revolut X**: `GET /1.0/transactions` is in the OpenAPI spec
  (`revolut-x-crypto-exchange.yml`): `start_date`/`end_date` in ms (range at most 30
  days, so the reader walks 29-day windows), `types` filter including `receive` and
  `send`, response `data[]` with `id`, `status` (`completed`, `pending`, `cancelled`,
  `failed`, `reverted`), `type`, `source`/`destination` legs (`amount`, `currency`,
  `account.type` in `revolut`, `revolut_x`, `external_bank_account`,
  `external_crypto_wallet`), `created_date`, `processed_date` (ms), and
  `metadata.next_cursor`. **The spec does not say that a fiat top-up from the owner's
  Revolut account (the 4.27 EUR) appears as a `receive`.** It probably does (the
  `revolut` and `external_bank_account` account types exist), but if the answer is
  empty, rely on detection plus manual entry. Key permission: not documented in the
  spec beyond the signed headers; the key that can read `/1.0/balances` and orders is
  assumed to read transactions too.

### Detection (the fallback)

On every venue reading (the recorder, and the dashboard's portfolio read; the latter
makes no extra exchange call) the per-token balances are compared with the previous
reading. For each token:

```
unexplained = units now - units before
            - what the bot's own filled orders since the last reading explain
              (ledger.orders: units, quote value and fees)
            - recorded api/manual flows stamped after the last reading
```

It becomes a `pending` row when `|unexplained x price| > max(0.5 USD, 2% of that
token's value at the last reading)`. Then:

* Candidates that offset each other (positive and negative, net below the same
  threshold) are an internal conversion, for example EUR to USD, or a trade placed by
  hand on the venue app: not flows, nothing recorded.
* The first reading only sets the reference; a reading while an order is uncertain is
  skipped and does not move the reference.
* A later API or manual row of about the same money (same currency and sign, within 5%,
  detected in the last day) dismisses the pending detection as superseded, so confirming
  both never double-counts.
* Staking rewards, interest and trades made by hand on the venue also look unexplained.
  Confirm a real transfer; dismiss anything else.
* An order recorded in the ledger a moment after the reading that already contains its
  fill can look unexplained once; dismiss it.

Pending rows are never subtracted. They are shown on the panel, in the API (`pending_count`,
`pending_net_usd`), on the loop card ("Unconfirmed cash movement of +$4.74", visible, not
hover only) and in the `native_portfolio` tool output (`flows.pending_count`).

## Using it

On the Trade page, under the venue's panel: **Deposits and withdrawals**.

* **Pending cash movements** list: **Confirm** (counts as a real deposit or withdrawal
  from now on) or **Dismiss**. Every button shows a pending label and any error.
* **Add deposit/withdrawal** form: type, positive amount, currency, date (empty = now),
  optional note and USD value (only needed for a currency Condor cannot price). It
  confirms at once and is priced at the current rate unless you give the value. An
  entry added by hand can be deleted; API and detected rows are dismissed instead.
* **Re-read venue history** asks the venue for its transfer history and runs detection.

API (all per signed-in user): `GET /api/v1/native-flows?venue=&include_dismissed=`,
`POST /api/v1/native-flows` `{venue, amount (signed), currency, ts?, note?, usd_value?}`,
`POST /api/v1/native-flows/sync` `{venue?, lookback_days?}`,
`POST /api/v1/native-flows/{id}/confirm`, `.../dismiss`, `DELETE /api/v1/native-flows/{id}`
(manual rows only). The MCP `native_portfolio` tool only reads: it has no write action.

## Backfilling the owner's known flows

Add these by hand once (Trade page, venue panel, "Add deposit/withdrawal"), or enter them
through `POST /api/v1/native-flows`. They are dated in the past; the PnL only subtracts
flows inside the window between the two equity readings, so an old flow is harmless.

* **Revolut X**: deposit, 4.27, EUR, date 2026-10-28, note "top-up from Revolut".
  (A bare date is noon UTC. If the exact time matters for a session that started that
  day, enter the real time through the API with `ts`.) Priced at today's EUR/USD, not
  the rate of that day: enter the USD value if you know it.
* **Kraken xStocks**: each EUR deposit as a deposit in EUR with its date. If the
  Ledgers read works with the new key permission they appear on their own.
* **Kraken EUR to USD conversion**: a conversion is not a flow, so detection ignores
  it, but its cost (spread and fee) is a real loss that is not trading either. To take
  it out of the trading result, add one manual **withdrawal** in USD for the cost
  amount (not the converted amount) with the note "EUR to USD conversion cost". The
  figure stays honest: equity fell by the cost, and the cost is recorded as not
  trading.

## Not done / limits

* No venue endpoint was exercised live; see the verification notes above.
* Revolut X and Crypto.com deposits of coins use the account's current price for the USD
  value; a coin the account does not hold cannot be priced and needs a manual USD value.
* Withdrawal fees on Crypto.com may not be included; Kraken's are (amount minus fee).
* Detection works on balances, so a deposit smaller than the threshold (0.5 USD or 2% of
  the token) is not noticed; enter it by hand if it matters.
* The 30 s loop-card cache means a confirmation shows on the cards within about 30
  seconds.

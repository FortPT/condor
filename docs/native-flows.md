# Deposits and withdrawals of the native venues

The loop card PnL of a native crew (Crypto.com, Revolut X, Trading 212) is
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
  venue's own EUR/USD path (Revolut X's `eur_usd`; Crypto.com falls back
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

* **Crypto.com**: `private/get-deposit-history` and
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

* Candidates that offset each other are an internal conversion, not a flow, nothing
  recorded: a set of moves of both signs whose USD values sum to within the same threshold
  (`max(0.5 USD, 2% of the account at the last reading)`). That covers EUR to USD, **and a
  buy or sale of a token or stock against the venue's own cash**, the bot's or one the owner
  made by hand: a holding that rose while cash fell by about the same value is a trade. The
  smallest sets are matched first, each move once, so a real deposit made in the same
  reading as a trade still shows (before, a deposit next to a trade stopped the whole set
  from netting and every move was flagged). One-sided moves are never matched: a deposit,
  a withdrawal or a cash change with no holding behind it is still detected.
* A token on the venue's exclusion list (`excluded_tickers`, Trading 212) is never offered
  as a flow itself (the owner holds it outside the bot), but it still counts as the other
  side of a conversion, so the owner's manual purchase of it with cash nets out. Its value
  is removed from the trading result instead (`docs/trading212.md`).
* The bot ledger's own bookings are matched too. Besides the filled orders in the window,
  the detector keeps the ledger's cash and units at each reading and credits the ledger's
  change since then to a token no order row mentions (a fill the module booked from the
  account's evidence, or whose order record an older reconcile bug cleared), but never more
  than the account moved and never in the opposite direction.
* The first reading only sets the reference; a reading while an order is uncertain is
  skipped and does not move the reference.
* A history (API) or manual row of about the same money (same venue, currency and sign,
  within 5%) supersedes the detection in either state: a pending detection from the last day
  is dismissed, and so is one the owner already confirmed when the two are stamped within 30
  minutes ("superseded by venue history" in its note), so the deposit is counted once however
  the two arrive. The other way round, a detection that is created or confirmed while such a
  row already exists is dismissed on the spot. Rows of another amount, sign, currency, venue or
  time are kept.
* Staking rewards, interest and trades made by hand on the venue also look unexplained.
  Confirm a real transfer; dismiss anything else.
* An order recorded in the ledger a moment after the reading that already contains its
  fill can look unexplained once; dismiss it. (A trade also shows its cash leg, which now
  nets it out; this remains for a fill whose cash leg is invisible, such as one paid from
  cash a pending order had reserved.)

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
## The Portfolio Evolution chart

`GET /servers/{name}/portfolio/history` keeps `points`, `interval`, `top_tokens` and
`change` as they were (`points` is still the merged series that steps up when an account
starts being recorded or money is deposited). It adds the fields the chart draws
(`condor/portfolio_chart.py`), so the line no longer reads those steps as performance.

* **Baseline.** `baseline_ts` is the first moment every included source has a recorded
  reading: the first-ever reading of each native account, and the first Hummingbot point
  (a Hummingbot history that only trails the window start by its cache age counts as
  there from the start). `chart_points` start at `max(window start, baseline_ts)`. When
  the window begins before the baseline the response carries `series_start_reason:
  "all_accounts_recording_from"`, and `sources` lists every account with its
  `first_ts` and a `status`: `recording`, `pending` (connected, no reading yet: reported,
  never waited for) or `disconnected` (left out). The page's *current total* rule is unchanged.
* **Gaps.** A source with no reading within two sampling intervals of a point (10 minutes
  for Condor's 5-minute recorder, two intervals of the range for Hummingbot, at least 15
  minutes; a Hummingbot reading of 0 counts as no reading) makes that point's total
  `null`. The page cuts the line there. Nothing is carried forward. `gaps` lists each
  stretch as `{source, from_ts, to_ts}`, including a source that has stopped recording.
* **Outliers.** A single Hummingbot row that differs from both neighbours by more than
  `max(10%, 2 USD)` while the neighbours agree within 2% is excluded and listed in
  `anomalies` (`source`, `ts`, `value`, `neighbour_value`). The point stays in the series
  as a `null` (a break); no replacement value is made up. A real step (a deposit, or
  neighbours that disagree) is kept.
* **Markers.** `flows` are the confirmed flows of the included native venues inside the
  returned window (`ts`, `venue`, `currency`, signed `amount`, `usd_value_at_time`,
  `source`), drawn as triangles on the line. `pending_flows` counts the unconfirmed ones,
  which are not drawn. Hummingbot-connected accounts have no flow data, so `flows_note`
  says a deposit there is not marked.
* **Trading result.** `result_points` (same timestamps as `chart_points`) is the value
  minus the value at the chart start minus the net confirmed flows since then, each
  account netted over the span between its own readings. `null` buckets stay `null`. It is
  `null` as a whole, with `result_reason` and `result_note`, when a native venue's flows
  are untracked, unreadable or unpriced, or an unconfirmed flow sits in the window (the
  rules of the `change` figure). Hummingbot-connected accounts are included without
  netting and `result_not_netted` names them; a deposit there reads as result.

The page offers *Value | Trading result* (default Value; Result is disabled with the
reason when `result_points` is `null`), a note "Showing from ..., when all N accounts were
recording", and an empty state instead of a line for a single point or when no common
reading exists yet.

## Trading 212: correcting the first sample (one time, on the VM)

Trading 212's first history sample (33.42 USD = 30.00 EUR) left out the 9.11 EUR that the
owner's then-pending MRNA market order had reserved; the fill at 13:30 UTC then made the
account read about 39 EUR and the venue's "trading result since the first sample" +10.2
USD, with nothing earned. The code now counts all account money (see
`docs/trading212.md`), but history is never rewritten, so that sample stays what it was.
The correction is one confirmed manual flow, recording the 9.11 EUR as the owner's own
money that entered the account's value after the first sample:

```
venue    trading212
amount   +9.11        currency EUR      (a deposit)
usd      10.15        (9.11 EUR at the first sample's own rate, 33.42 / 30.00 = 1.114)
ts       one second AFTER the first sample (see below)
note     cash reserved for pending MRNA order, owner deposit
```

**The date matters.** A flow is subtracted only when it is stamped after the first sample
(`start < ts <= end`); a flow stamped at or before it is read as already in that sample
and changes nothing. The MRNA order was created at 11:35:59Z, and the first sample was
taken at or after that, so do not use 11:35:59Z blindly. Read the sample's time and use
one second later:

```
sqlite3 $CONDOR_DATA_DIR/portfolio_history/<user id>.db \
  "select ts, datetime(ts,'unixepoch'), total_usd from samples
   where source='Trading 212' order by ts limit 1"
```

Enter it in the Trade page ("Add deposit/withdrawal", Trading 212: deposit, 9.11, EUR,
that date and time, USD value 10.15, the note above) or with `POST /api/v1/native-flows`
`{"venue":"trading212","amount":9.11,"currency":"EUR","ts":"<first sample ts + 1, ISO>",
"usd_value":10.15,"note":"cash reserved for pending MRNA order, owner deposit"}`.

How it nets out (tested in `tests/test_native_flows.py`): the trading result is
`value now - value at the first sample - confirmed flows after it - the excluded holdings'
change`. With the first sample 33.42 and the account now 43.61 USD (the bot's 29.95 EUR plus
MRNA at 9.20 EUR):

| | trading result |
|---|---|
| no correction | 43.61 - 33.42 = **+10.19** (the phantom gain) |
| the manual flow, dated after the sample | 43.61 - 33.42 - 10.15 = **+0.04** |
| the flow dated at or before the sample | +10.19 (ignored) |

+0.04 is the bot's -0.06 (29.95 against 30.00 EUR) plus MRNA's
+0.09 EUR price move since the fill: the first sample did not record the excluded value, so
that comparison cannot remove MRNA, and its price moves leak into this one figure. They
do not leak into any comparison that starts at a sample recorded after the fix is
deployed (the loop cards' session results, whose start samples are later, are already
right and stay right). Do **not** also treat the 9.11 EUR as excluded by hand: the
excluded value is removed only when both ends recorded it, so the two never overlap.

## Not done / limits

* No venue endpoint was exercised live; see the verification notes above.
* Revolut X and Crypto.com deposits of coins use the account's current price for the USD
  value; a coin the account does not hold cannot be priced and needs a manual USD value.
* Withdrawal fees on Crypto.com may not be included.
* Detection works on balances, so a deposit smaller than the threshold (0.5 USD or 2% of
  the token) is not noticed; enter it by hand if it matters.
* A deposit that is spent on a manual purchase within one reading shows as the holding
  that rose (its cash never moved), not as cash; the history reader covers Trading 212.
* The 30 s loop-card cache means a confirmation shows on the cards within about 30
  seconds.

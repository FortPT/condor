# Trade performance: what each venue earns and loses

Every venue tab on the Trade page (Crypto.com, Revolut X, Trading 212) shows
the same row of cards under its header, a **P/L** column and a totals row in *Bot holdings*, and
a **Result** column in the recent-orders tables. Everything comes from data Condor already
records; nothing is simulated or estimated in the browser, and nothing here places an order or
contacts an exchange beyond the valuation the tab already reads.

The server side is `condor/native_performance.py`, exposed read-only as
`GET /api/v1/<venue>/performance` for `crypto-exchange`, `revolut-x` and
`trading212` (cached for a few seconds). The page asks once a minute.

## The six cards

| Card | What it is | Unit | Source |
| --- | --- | --- | --- |
| **Today** | Whole-account value now minus its value at the start of the UTC day, minus confirmed deposits and withdrawals since then | USD | value history (`portfolio_history`) and flows (`native_flows`), through the same arithmetic as the loop cards (`native_accounts.performance`) |
| **Since start** | The same, from the first recorded value sample | USD | same |
| **Realized** | Profit or loss on sales already made, after fees | venue currency (EUR/USD) | replay of the bot ledger's filled orders |
| **Unrealized** | Open positions at the best bid minus what they cost: `equity - cash - cost of open positions` | venue currency | the venue's own `valuation` (equity) and the ledger |
| **Fees** | Fees charged on filled orders, converted to the venue currency; named an estimate where the order says so | venue currency | the fills' `fee`, `fee_currency`, `fee_source` |
| **Trades (W/L)** | Filled orders, and how many sales made (W) or lost (L) money; win rate; positions closed | count | the replay |

Colours: green for a gain, red for a loss, grey for an unknown figure. Hover a card for its
one-line explanation.

### Two currencies, on purpose

The value history is recorded in **USD** for the whole account, so *Today* and *Since start* are
USD. The ledger is in the venue's own currency (EUR for Revolut X and Trading 212, the chosen
quote for Crypto.com), so *Realized*, *Unrealized* and *Fees* are in
that currency. Each card names its currency; no figure is converted at a guessed rate.

*Today* and *Since start* measure the whole account, not only the bot allocation, with the
owner's excluded holdings (Trading 212) taken out. After a sync the allocation is the account
(minus those exclusions), so the two read alike.

### Deposits are never profit

A confirmed deposit or withdrawal in the window (see `docs/native-flows.md`) is subtracted from
the equity change. Two cases are shown on the card instead of hidden:

* a venue whose flows are not tracked says so (a deposit would read as a gain);
* unconfirmed (pending) movements are not subtracted, and the card says how many there are.

### Missing baselines

* No value samples at all: *Today* and *Since start* are a dash with "No equity history recorded
  yet". They are never zero.
* The history starts after midnight UTC: *Today* is reported "since <first sample>" (partial).
* The nearest sample to midnight is more than three hours away: *Today* is a dash with the reason.
  A sample within three hours but not within fifteen minutes is used and marked estimated.

## Realized profit and the cost basis

The ledger keeps the last 200 filled orders per venue. The replay reads them oldest first,
asset by asset, with **average cost**:

* a buy adds its units and its cost; a fee paid in the quote currency is part of the cost, and a fee
  paid in the coin reduces the units received (the same money);
* a sell realizes `proceeds - average cost x units sold`, where the proceeds are net of a fee paid
  in the quote currency (Revolut X also takes a coin-denominated fee out of the units);
* a partial sell realizes only its share of the cost and leaves the rest at the same average;
* a balance correction the ledger booked (Revolut X) removes units without changing cost, exactly
  as the ledger did.

This mirrors how each service books a fill, line by line, so the replay reproduces the ledger. A
*win* is a sale with a positive result, a *loss* a negative one. A *round trip closed* is a sale
that took a position to zero.

### Positions the orders do not explain

A sync **adopts** the positions already in the account with their cost restarted at the bid at
that moment (the venue does not report what they cost elsewhere), and starts a new order list. A
position can also be older than the oldest order still kept. Neither has orders behind it, so
the replay derives the opening lot from the ledger itself:

* opening units = the ledger's units now minus what the recorded orders changed;
* opening cost is solved from the ledger's cost now (after buys and partial sells the cost is
  linear in the opening cost). It is exact when the ledger's own rounding permits, and not
  solved when under 0.01 % of the opening lot is left.

Each asset reports where its opening lot came from: `adopted_at_sync` (fewer than 200 orders: the
list was not cut) or `before_recorded_orders` (the list is full, so older orders may be gone).
The latter sets `partial_history: true`, and the card says "partial: order history starts at ...".

### Known limits

* If an adopted lot was **sold out completely**, its cost left the ledger with the position and
  cannot be recovered. Those sales show **n/a** (with the reason on hover) and are left out of
  *Realized*, which then says how many sales are missing. Sales after the position was rebuilt
  are exact again. This clears once an order history has a full position cycle after a sync.
* Orders older than the ledger's 200 are not available; *Realized* covers what the ledger kept.
* Realized is after fees. A fee in a currency other than the quote or the coin is not
  converted and is counted in the card's note.
* Fees marked `estimate` (Revolut X before the fee is reported, Trading 212 FX estimates) are
  estimates, and the Fees card says "(est.)".
* *Unrealized* needs a live valuation. When the venue cannot be valued (an unreconciled order,
  an unreachable exchange) the card is a dash with the reason; Realized and Trades still show.
* Orders and positions are not matched to the venue's own history: the venue's history table
  shows a Result only for sales that are also in the bot ledger.

## In the tables

* **Bot holdings**: *P/L* is value at bid minus cost (venue currency, coloured), beside *vs cost*
  (percent). The last row totals cost, **cash + holdings = equity**, and the total unrealized. A
  holding with no live price has no P/L, and the totals row then shows no equity rather than a
  wrong one.
* **Recent orders** (the bot ledger table on every venue, and the venue's order history where the
  order is also in the ledger): *Result* is the realized amount and percent of a sale, a dash for a
  buy and n/a for a sale whose cost is not on record.

## Pause and resume

The Revolut X and Trading 212 headers carry the same button as Crypto.com:
**Pause trading** while enabled, **Enable trading within allocation** while paused. It calls the
existing `PUT /<venue>/enabled` route. Enabling follows the venue's own rules (connected, no
uncertain order, allocation synced; Trading 212 also needs a EUR account) and says why when it is
unavailable; pausing is always allowed. There are no order tickets on these tabs.

import type { UseQueryResult } from "@tanstack/react-query";

import { Change } from "./TradeParts";
import {
  TONE_CLASS, cardsOf, resultFor, signedMoney, toneOf, type HoldingTotals, type Performance, type PerfTrade, type ResultCell as ResultValue,
} from "./performance";
import { money } from "./tradeFormat";

/** A signed amount in the venue currency, green for a gain and red for a loss. */
export function Amount({ value, currency }: { value: number | null | undefined; currency: string }) {
  return <span className={TONE_CLASS[toneOf(value)]}>{signedMoney(value, currency)}</span>;
}

/**
 * What the venue earns and loses, in one row of cards under the header line: Today, Since start, Realized,
 * Unrealized, Fees and Trades. Every card explains itself on hover, and a figure that is missing or only
 * partial says why underneath it. The numbers come from the server's replay of the bot's recorded orders and
 * its value history; nothing is estimated here.
 */
export function PerformanceSummary({ query }: { query: Pick<UseQueryResult<Performance>, "data" | "error" | "isLoading"> }) {
  const perf = query.data;
  return <section aria-label="Performance" className="space-y-2">
    {query.isLoading && <p role="status" className="text-sm">Reading performance…</p>}
    {query.error && !perf && <p className="text-sm text-[var(--color-text-muted)]" data-testid="performance-error">Performance unavailable: {query.error.message}</p>}
    {query.error && perf && <p className="text-xs text-[var(--color-text-muted)]" data-testid="performance-stale">These figures may be out of date: the last refresh failed ({query.error.message}).</p>}
    {perf && perf.available === false && <p className="text-sm text-[var(--color-text-muted)]" data-testid="performance-error">Performance unavailable: {perf.reason ?? "the ledger could not be read"}.</p>}
    {perf && perf.available !== false && <>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        {cardsOf(perf).map((card) => <div key={card.key} data-card={card.key} title={card.hint} className="rounded-lg border border-[var(--color-border)] p-3">
          <p className="text-xs text-[var(--color-text-muted)]">{card.label}</p>
          <p className={`text-base font-semibold ${TONE_CLASS[card.tone]}`} data-tone={card.tone}>{card.value}</p>
          {card.note && <p className="mt-0.5 text-xs text-[var(--color-text-muted)]" data-note={card.key}>{card.note}</p>}
        </div>)}
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">
        Realized, unrealized and fees: the bot allocation in {perf.currency}, after fees. Today and Since start: the whole account in USD, with deposits and withdrawals taken out.
        {/* The first two notes are the standing explanation written out above; the rest are about this report. */}
        {perf.notes && perf.notes.length > 2 ? ` ${perf.notes.slice(2).join(" ")}` : ""}
      </p>
    </>}
  </section>;
}

/** The Result cell of a Recent orders row: the realized P&L of a sale (amount and %), a dash for a buy. */
export function ResultCell({ side, orderId, results, currency, missing }: { side: string; orderId: string; results: Map<string, PerfTrade>; currency: string; missing?: string }) {
  const result: ResultValue = resultFor(side, orderId, results, missing);
  if (result.kind === "none") return <td>—</td>;
  if (result.kind === "unknown") return <td title={result.reason} className="text-[var(--color-text-muted)]">n/a</td>;
  return <td><Amount value={result.pnl} currency={currency} />{result.pct !== null && <span className="ml-1 text-xs"><Change value={String(result.pct)} /></span>}</td>;
}

/** Totals under a Bot holdings table: cash + holdings = equity, and the total unrealized. */
export function HoldingsTotalsRow({ totals, quote, trailing = 0 }: { totals: HoldingTotals; quote: string; trailing?: number }) {
  return <tr data-row="totals" className="border-t border-[var(--color-border)] font-medium" title="Cash plus holdings at the best bid is the equity of the bot allocation.">
    <td>Total (cash + holdings)</td>
    <td></td>
    <td>{totals.cost === null ? "—" : money(totals.cost, quote)}</td>
    <td>{totals.equity === null ? "—" : `${money(totals.equity, quote)} equity`}</td>
    <td>{totals.unrealizedPct === null ? "—" : <Change value={String(totals.unrealizedPct)} />}</td>
    <td title="Total unrealized: holdings at bid minus their cost."><Amount value={totals.unrealized} currency={quote} /></td>
    {Array.from({ length: trailing }, (_, i) => <td key={i}></td>)}
  </tr>;
}

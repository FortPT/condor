/** Types and pure helpers for the earnings/losses overview every venue tab shows (`GET <venue>/performance`). */
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";

/** Today / since-start: the whole account in USD, deposits and withdrawals netted out. */
export type PerfWindow = {
  value: number | null;
  currency: string;
  since: number | null;
  since_label: string | null;
  /** The history starts after the window would, so it is "since <first sample>". */
  partial: boolean;
  estimated: boolean;
  flows_usd: number;
  reason: string | null;
  note: string | null;
};

export type PerfTrade = {
  time: number;
  symbol: string;
  order_id?: string | null;
  client_oid?: string | null;
  pnl: number | null;
  pnl_pct: number | null;
  closed_position: boolean;
  reason: string | null;
};

export type Performance = {
  venue: string;
  label: string;
  as_of: number;
  currency: string;
  connected?: boolean;
  synced?: boolean;
  /** False when the ledger could not be read: nothing else is present then. */
  available: boolean;
  reason?: string | null;
  today?: PerfWindow;
  since_start?: PerfWindow;
  account_equity_usd?: number | null;
  equity?: { now: number | null; cash: number | null; positions_cost: number | null; unrealized: number | null; currency: string; reason: string | null };
  realized?: {
    value: number | null; complete: boolean; reason: string | null; sells: number; unknown_sells: number;
    partial_history: boolean; partial_assets: string[]; history_starts_at: number | null; currency: string;
  };
  unrealized?: { value: number | null; currency: string; reason: string | null };
  fees?: { value: number; estimated: number; is_estimate: boolean; unconverted: number; complete: boolean; currency: string };
  trades?: {
    filled_orders: number; buys: number; sells: number; round_trips_closed: number; wins: number; losses: number; flat: number;
    win_rate: number | null; best: PerfTrade | null; worst: PerfTrade | null;
  };
  orders?: PerfTrade[];
  notes?: string[];
};

/** One query per venue; the cache key matches the venue's other queries so it is shared. */
export function usePerformance(base: string, key: string, enabled = true) {
  return useQuery({
    queryKey: [key, "performance"],
    queryFn: () => apiFetch<Performance>(`${base}/performance`),
    enabled,
    refetchInterval: 60000,
    staleTime: 30000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export type Tone = "up" | "down" | "flat" | "none";

export const toneOf = (value: number | null | undefined): Tone =>
  value === null || value === undefined || !Number.isFinite(value) ? "none" : value > 0 ? "up" : value < 0 ? "down" : "flat";

export const TONE_CLASS: Record<Tone, string> = {
  up: "text-[var(--color-green)]",
  down: "text-[var(--color-red)]",
  flat: "",
  none: "text-[var(--color-text-muted)]",
};

/** Two decimals, four for an amount under a cent so a 0.0042 fee does not read 0.00. */
const digitsOf = (abs: number) => (abs !== 0 && abs < 0.01 ? 4 : 2);
const fixed = (abs: number) => abs.toLocaleString(undefined, { minimumFractionDigits: digitsOf(abs), maximumFractionDigits: digitsOf(abs) });

/** `+1.23 EUR` / `-1.23 EUR` / `0.00 EUR`; an unknown amount is a dash, never a zero. */
export function signedMoney(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${fixed(Math.abs(value))} ${currency}`.trim();
}

/** An unsigned amount with its currency. */
export function plainMoney(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${fixed(Math.abs(value))} ${currency}`.trim();
}

export function utcLabel(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return null;
  const text = new Date(seconds * 1000).toISOString();
  return `${text.slice(0, 10)} ${text.slice(11, 16)} UTC`;
}

export type PerfCard = {
  key: "today" | "since-start" | "realized" | "unrealized" | "fees" | "trades";
  label: string;
  value: string;
  tone: Tone;
  /** What the figure is, shown on hover. */
  hint: string;
  /** Why a figure is missing or only partial; shown under the value, never hidden. */
  note: string | null;
};

const HINTS = {
  today: "The whole account's value now minus its value at the start of the UTC day, in USD, after taking out confirmed deposits and withdrawals so money you add is never profit.",
  sinceStart: "The whole account's value now minus its first recorded value, in USD, after taking out confirmed deposits and withdrawals.",
  realized: "Profit or loss on sales already made: proceeds minus the average cost of the units sold, after fees, replayed from the bot's recorded orders.",
  unrealized: "What the open positions would make or lose if sold now at the best bid: value at bid minus what they cost.",
  fees: "Fees the venue charged on the bot's filled orders, converted to the venue currency. Already included in Realized.",
  trades: "Filled orders recorded by the bot, and its sales that made (W) or lost (L) money.",
} as const;

function windowNote(win: PerfWindow | undefined, fallbackLabel: string): string | null {
  if (!win) return null;
  if (win.value === null) return win.reason ?? "Not available";
  const parts: string[] = [];
  parts.push(win.partial ? `history starts at ${win.since_label ?? "—"}` : win.since_label ? `since ${win.since_label}` : fallbackLabel);
  if (win.estimated) parts.push("estimated from the nearest sample");
  if (win.note?.includes("not tracked")) parts.push("deposits are not tracked here, so a deposit would read as a gain");
  if (win.note?.includes("unconfirmed")) parts.push("unconfirmed deposits are not subtracted");
  return parts.join(" · ");
}

/** The six cards. A missing figure says why instead of reading as zero. */
export function cardsOf(perf: Performance): PerfCard[] {
  const currency = perf.currency;
  const realized = perf.realized;
  const unrealized = perf.unrealized;
  const fees = perf.fees;
  const trades = perf.trades;
  const history = realized?.history_starts_at ? utcLabel(realized.history_starts_at) : null;
  const realizedNote = !realized ? null
    : realized.value === null ? (realized.reason ?? "Not available")
    : !realized.complete ? (realized.reason ?? "Some sales have no recorded cost")
    : realized.partial_history ? `partial: order history starts at ${history ?? "—"}`
    : realized.sells === 0 ? "no sales yet" : null;
  const feeNote = !fees ? null
    : [fees.is_estimate ? "includes estimated fees" : null, fees.unconverted ? `${fees.unconverted} fee(s) in another currency not counted` : null].filter(Boolean).join(" · ") || null;
  const decided = (trades?.wins ?? 0) + (trades?.losses ?? 0);
  return [
    { key: "today", label: "Today", value: signedMoney(perf.today?.value, perf.today?.currency ?? "USD"), tone: toneOf(perf.today?.value), hint: HINTS.today, note: windowNote(perf.today, "since 00:00 UTC") },
    { key: "since-start", label: "Since start", value: signedMoney(perf.since_start?.value, perf.since_start?.currency ?? "USD"), tone: toneOf(perf.since_start?.value), hint: HINTS.sinceStart, note: windowNote(perf.since_start, "since the first sample") },
    { key: "realized", label: "Realized", value: signedMoney(realized?.value, currency), tone: toneOf(realized?.value), hint: HINTS.realized, note: realizedNote },
    { key: "unrealized", label: "Unrealized", value: signedMoney(unrealized?.value, currency), tone: toneOf(unrealized?.value), hint: HINTS.unrealized, note: unrealized?.value === null || unrealized?.value === undefined ? (unrealized?.reason ?? "Not available") : null },
    { key: "fees", label: "Fees", value: fees ? `${plainMoney(fees.value, currency)}${fees.is_estimate ? " (est.)" : ""}` : "—", tone: "none", hint: HINTS.fees, note: feeNote },
    {
      key: "trades", label: "Trades (W/L)",
      value: trades ? `${trades.filled_orders} · ${trades.wins}W / ${trades.losses}L` : "—", tone: "none", hint: HINTS.trades,
      note: trades ? [decided ? `win rate ${Math.round((trades.win_rate ?? 0) * 100)}%` : "no closed sale yet", `${trades.round_trips_closed} position(s) closed`].join(" · ") : null,
    },
  ];
}

/** The key the ledger's order rows and the performance rows share. */
export const orderKey = (order: { order_id?: string | null; client_oid?: string | null; time: number }) =>
  `${order.order_id ?? order.client_oid ?? ""}-${order.time}`;

/** The realized result of each sale, by `orderKey`. */
export function resultsOf(perf: Performance | undefined): Map<string, PerfTrade> {
  const map = new Map<string, PerfTrade>();
  for (const row of perf?.orders ?? []) map.set(orderKey(row), row);
  return map;
}

/** The realized result of each sale, by the venue's order id (what the venue's own order history rows carry). */
export function resultsByOrderId(perf: Performance | undefined): Map<string, PerfTrade> {
  const map = new Map<string, PerfTrade>();
  for (const row of perf?.orders ?? []) if (row.order_id) map.set(row.order_id, row);
  return map;
}

export type ResultCell = { kind: "none" } | { kind: "unknown"; reason: string } | { kind: "known"; pnl: number; pct: number | null };

/** What the Result column shows for one ledger order: nothing for a buy, the realized P&L of a sale. */
export function resultFor(side: string, key: string, results: Map<string, PerfTrade>, missing = "Not available (the performance report has not loaded)"): ResultCell {
  if (side.toLowerCase() !== "sell") return { kind: "none" };
  const row = results.get(key);
  if (!row) return { kind: "unknown", reason: missing };
  if (row.pnl === null) return { kind: "unknown", reason: row.reason ?? "No recorded cost for this sale" };
  return { kind: "known", pnl: row.pnl, pct: row.pnl_pct };
}

export type HoldingLike = { cost: number | null; value: number | null };

/** P/L of one holding in the venue currency: value at bid minus cost. */
export const holdingPnl = (row: HoldingLike): number | null => (row.value !== null && row.cost !== null ? row.value - row.cost : null);

export type HoldingTotals = {
  cost: number | null;
  value: number | null;
  /** Cash plus every holding at the bid; null when any holding has no live price. */
  equity: number | null;
  unrealized: number | null;
  unrealizedPct: number | null;
};

/** Totals under a Bot holdings table: cash + holdings = equity, and the total unrealized. */
export function totalsOf(cash: number | null, rows: HoldingLike[]): HoldingTotals {
  const priced = rows.every((row) => row.value !== null && row.cost !== null);
  if (!priced) {
    const cost = rows.every((row) => row.cost !== null) ? rows.reduce((sum, row) => sum + (row.cost ?? 0), 0) : null;
    return { cost, value: null, equity: null, unrealized: null, unrealizedPct: null };
  }
  const cost = rows.reduce((sum, row) => sum + (row.cost ?? 0), 0);
  const value = rows.reduce((sum, row) => sum + (row.value ?? 0), 0);
  return {
    cost, value, equity: cash === null ? null : cash + value, unrealized: value - cost,
    unrealizedPct: cost > 0 ? (value / cost - 1) * 100 : null,
  };
}

/** Types and pure helpers for the deposits/withdrawals panel (`NativeFlowsPanel`). */

export type FlowStatus = "confirmed" | "pending" | "dismissed";
export type FlowSource = "api" | "detected" | "manual";

export type NativeFlow = {
  id: string;
  venue: string;
  /** Unix seconds. */
  ts: number;
  currency: string;
  /** Signed decimal string: positive = deposit, negative = withdrawal. */
  amount: string;
  usd_value_at_time: number | null;
  source: FlowSource;
  status: FlowStatus;
  note: string;
  external_id: string | null;
};

export type FlowVenueSummary = {
  venue: string;
  label: string;
  recorded_net_flows_usd: number;
  confirmed_count: number;
  pending_count: number;
  pending_net_usd: number;
  unpriced_count: number;
  /** The venue's flows are recorded, so loop PnL is a trading result. */
  tracked: boolean;
  fetch: { ts: number; ok: boolean; reason: string; count: number } | null;
  /** The visible "unconfirmed cash movement" line, empty when nothing is pending. */
  note: string;
};

export type FlowsResponse = { flows: NativeFlow[]; venues: FlowVenueSummary[] };

export type SyncResponse = {
  results: { venue: string; ok: boolean; reason: string; new?: number; detected?: number }[];
};

export const FLOWS_URL = "/api/v1/native-flows";
export const flowsQueryKey = (venue: string) => ["native-flows", venue] as const;

/** The server's venue key for a Trade-page venue key (`revolut-x` -> `revolut_x`; `trading212` stays as it is). */
export const flowVenueKey = (tradeKey: string) => tradeKey.replace(/-/g, "_");

export type FlowKind = "deposit" | "withdrawal";

const POSITIVE_DECIMAL = /^\d{1,18}(\.\d{1,18})?$/;

/**
 * The signed amount to send for a deposit or withdrawal typed as a positive number,
 * or `null` when it is not a positive plain decimal.
 */
export function signedAmount(kind: FlowKind, text: string): string | null {
  const value = text.trim();
  if (!POSITIVE_DECIMAL.test(value) || Number(value) <= 0) return null;
  return kind === "withdrawal" ? `-${value}` : value;
}

export const flowKind = (flow: Pick<NativeFlow, "amount">): FlowKind =>
  flow.amount.trim().startsWith("-") ? "withdrawal" : "deposit";

export const SOURCE_LABEL: Record<FlowSource, string> = {
  api: "Venue history",
  detected: "Detected",
  manual: "Entered by hand",
};

/** `+$4.27` / `-$3.00`, or an em dash when the flow has no USD value. */
export function signedUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "+";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

/** One sentence on whether the venue's own transfer history could be read. */
export function historyLine(summary: FlowVenueSummary | undefined): string {
  if (!summary) return "";
  const fetch = summary.fetch;
  if (!fetch) return "The venue's transfer history has not been read yet.";
  if (fetch.ok) {
    return `The venue's transfer history was read ${new Date(fetch.ts * 1000).toLocaleString()}.`;
  }
  return `The venue's transfer history could not be read (${fetch.reason || "unavailable"}). Detection and manual entry still work.`;
}

/** The date input's value (`2026-10-28`) as the `ts` to send, or `undefined` for now. */
export const dateTs = (value: string): string | undefined => (value.trim() ? value.trim() : undefined);

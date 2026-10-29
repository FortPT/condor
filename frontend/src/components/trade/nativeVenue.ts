/** Configuration and pure helpers for the read-only Revolut X and Trading 212 Trade panels. */
import { useQuery } from "@tanstack/react-query";

import type { ExchangeLedger } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";
import { num } from "./tradeFormat";

export type NativeVenueKey = "revolut-x" | "trading212";

export type NativeVenue = {
  key: NativeVenueKey;
  /** The name on the tab and the panel heading. */
  title: string;
  /** The API prefix; the Settings tab and the status hook use the same key. */
  base: string;
  /** The ledger quote currency until the status says otherwise. */
  quote: string;
  /** What the venue lists, for empty states and the scorecard text. */
  noun: "coins" | "stocks";
  /** The key that holds the market rows in the `/coins` answer. */
  listKey: "coins" | "stocks";
  changeKey: string;
  changeLabel: string;
  volumeKey: string;
  volumeLabel: string;
  /** The route under `base` that lists the markets (`/coins` or `/stocks`). */
  marketsPath: "coins" | "stocks";
  /** The currency the market rows are priced in; it differs from `quote` when they are only a reference. */
  priceQuote: string;
  /** Set when the market rows are a REFERENCE feed, not the venue's own quotes: who supplies them. */
  referenceLabel?: string;
  /** The ledger is valued by the venue's own `/valuation`, read on load, not from the market rows. */
  autoValuation: boolean;
};

/** Said above the markets table of a venue whose rows are not its own quotes. */
export const REFERENCE_NOTE =
  "Reference prices in USD from Kraken xStocks public data (a tokenized copy of the stock), not Trading 212 quotes; Trading 212 has no quote endpoint. Orders fill at Trading 212's own price.";

export const NATIVE_VENUES: Record<NativeVenueKey, NativeVenue> = {
  "revolut-x": {
    key: "revolut-x",
    title: "Revolut X",
    base: "/api/v1/revolut-x",
    quote: "EUR",
    noun: "coins",
    listKey: "coins",
    changeKey: "change_24h_pct",
    changeLabel: "24h",
    volumeKey: "volume_24h_eur",
    volumeLabel: "24h volume",
    marketsPath: "coins",
    priceQuote: "EUR",
    autoValuation: false,
  },
  trading212: {
    key: "trading212",
    title: "Trading 212",
    base: "/api/v1/trading212",
    quote: "EUR",
    noun: "stocks",
    listKey: "stocks",
    changeKey: "change_since_open_pct",
    changeLabel: "Since open",
    volumeKey: "volume_24h_usd",
    volumeLabel: "24h volume",
    marketsPath: "stocks",
    priceQuote: "USD",
    referenceLabel: "Kraken xStocks reference",
    autoValuation: true,
  },
};

export const isNativeVenueKey = (value: string | null): value is NativeVenueKey =>
  value === "revolut-x" || value === "trading212";

/** What both native services answer on `/status` (a subset of their own status types). */
export type NativeStatus = {
  connected: boolean;
  enabled: boolean;
  quote?: string;
  /** Revolut X: whether the signing key exists (the API key is saved next). */
  has_signing_key?: boolean;
  /** Trading 212: the account currency read at the last sync; trading needs EUR. */
  account_currency?: string | null;
  ledger: ExchangeLedger;
  market?: { open: boolean; new_york_time?: string; minutes_to_close?: number; note?: string };
};

/** Same key and URL as the Settings hooks, so the cache is shared and the poll runs once. */
export function useNativeStatus(venue: NativeVenue) {
  return useQuery({
    queryKey: [`${venue.key}-status`],
    queryFn: () => apiFetch<NativeStatus>(`${venue.base}/status`),
    refetchInterval: 10000,
    retry: false,
  });
}

/**
 * Why trading cannot be enabled right now, or "" when it can. Pausing is always allowed. These are the
 * rules the venue's Settings tab applies to the same button, so the two never disagree.
 */
export function toggleBlock(venue: NativeVenue, data: NativeStatus | undefined): string {
  if (!data) return "waiting for the status to load";
  if (!data.connected) return venue.key === "revolut-x" ? "generate a signing key and save the API key first" : "save an API key first";
  if (data.enabled) return "";
  const currency = data.account_currency ?? (data.ledger as { account_currency?: string | null } | undefined)?.account_currency ?? null;
  if (venue.key === "trading212" && !!currency && currency.toUpperCase() !== "EUR") return "the account currency is not EUR";
  if (data.ledger?.uncertain_order) return "reconcile the uncertain order first";
  if (!data.ledger?.synced) return "sync the allocation first";
  return "";
}

export type VenueRow = {
  symbol: string;
  price?: string;
  bid?: string;
  ask?: string;
  spread_pct?: string;
  change_pct: string | null;
  volume: string | number | null;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown) =>
  typeof value === "string" ? value : typeof value === "number" && Number.isFinite(value) ? String(value) : undefined;

/** The market rows of a `/coins` answer, whichever key the venue puts them under. */
export function rowsOf(venue: NativeVenue, payload: unknown): VenueRow[] {
  const list = isRecord(payload) ? payload[venue.listKey] : undefined;
  if (!Array.isArray(list)) return [];
  return list.filter(isRecord).flatMap((row) => {
    const symbol = typeof row.symbol === "string" ? row.symbol : "";
    if (!symbol) return [];
    const volume = row[venue.volumeKey];
    return [{
      symbol,
      price: text(row.price),
      bid: text(row.bid),
      ask: text(row.ask),
      spread_pct: text(row.spread_pct),
      change_pct: text(row[venue.changeKey]) ?? null,
      volume: typeof volume === "string" || typeof volume === "number" ? volume : null,
    }];
  });
}

export type HistoryRow = {
  id: string;
  /** Milliseconds, or null when the venue gave no usable time. */
  time: number | null;
  pair: string;
  side: string;
  status: string;
  quantity: string;
  average_price: string;
  value: string;
  fee: string;
};

/** The final orders of `/history`, in one shape (Revolut X field names; Trading 212 follows Revolut X). */
export function historyOf(payload: unknown): HistoryRow[] {
  if (!Array.isArray(payload)) return [];
  return payload.filter(isRecord).map((row, index) => {
    const when = num(text(row.updated));
    const fee = text(row.total_fee ?? row.fee);
    const feeCurrency = text(row.fee_currency);
    return {
      id: text(row.order_id) ?? `${index}`,
      time: when,
      pair: text(row.pair) ?? "—",
      side: text(row.side) ?? "—",
      status: text(row.status) ?? "—",
      quantity: text(row.filled_quantity) ?? "—",
      average_price: text(row.average_fill_price) ?? "—",
      value: text(row.filled_amount) ?? "—",
      fee: fee === undefined ? "—" : `${fee}${feeCurrency ? ` ${feeCurrency}` : ""}`,
    };
  }).sort((a, b) => (b.time ?? 0) - (a.time ?? 0)).slice(0, 10);
}

export type AccountRow = { currency: string; balance: string; available: string; value_eur?: string };

export function accountsOf(payload: unknown): AccountRow[] {
  const list = isRecord(payload) ? payload.accounts : undefined;
  if (!Array.isArray(list)) return [];
  return list.filter(isRecord).flatMap((row) => {
    const currency = text(row.currency);
    return currency ? [{ currency, balance: text(row.balance) ?? "0", available: text(row.available) ?? "0", value_eur: text(row.value_eur) }] : [];
  });
}

export const isQuoteCurrency = (venue: NativeVenue, currency: string) => currency.toUpperCase() === venue.quote;

/** A balance in the quote currency at the best bid, or null when the asset has no live price here. */
export function accountValue(venue: NativeVenue, currency: string, balance: string, known: Map<string, VenueRow>) {
  const amount = num(balance);
  if (amount === null) return null;
  if (isQuoteCurrency(venue, currency)) return amount;
  const upper = currency.toUpperCase();
  const bid = num(known.get(upper)?.bid);
  return bid === null ? null : amount * bid;
}

/** The value of an account row: the venue's own EUR value when it gives one, else the bid-based value. */
export function accountRowValue(venue: NativeVenue, row: AccountRow, known: Map<string, VenueRow>) {
  if (!venue.autoValuation) return accountValue(venue, row.currency, row.balance, known);
  // The reference rows are USD: a balance is valued only from the venue's own EUR figure.
  return isQuoteCurrency(venue, row.currency) ? num(row.balance) : num(row.value_eur);
}

export type HoldingRow = { symbol: string; units: string; cost: number | null; value: number | null; pnl: number | null };

/** The bot's positions valued at the best bid, as the Crypto.com panel does. */
export function holdingsOf(ledger: ExchangeLedger | undefined, known: Map<string, VenueRow>): HoldingRow[] {
  return Object.entries(ledger?.positions ?? {}).map(([symbol, position]) => {
    const units = num(position.units);
    const bid = num(known.get(symbol)?.bid);
    const value = units !== null && bid !== null ? units * bid : null;
    const cost = num(position.cost);
    return { symbol, units: position.units, cost, value, pnl: value !== null && cost ? (value / cost - 1) * 100 : null };
  });
}

/** Cash plus every position at the best bid; null when any holding has no live price. */
export function equityOf(ledger: ExchangeLedger | undefined, rows: HoldingRow[]): number | null {
  const cash = num(ledger?.cash);
  if (cash === null || rows.some((row) => row.value === null)) return null;
  return rows.reduce((sum, row) => sum + (row.value ?? 0), cash);
}

/** What `/valuation` answers for a venue that values its own ledger (positions at its own EUR prices). */
export type ValuationPayload = {
  equity?: string | number | null;
  positions?: { symbol?: string; units?: string; cost_eur?: string | number | null; value_eur?: string | number | null }[];
};

/** The bot's positions at the venue's own prices; a held stock the valuation did not price shows no value. */
export function holdingsFromValuation(ledger: ExchangeLedger | undefined, payload: unknown): HoldingRow[] {
  const priced = new Map<string, Record<string, unknown>>();
  const list = isRecord(payload) ? payload.positions : undefined;
  if (Array.isArray(list)) for (const item of list) if (isRecord(item) && typeof item.symbol === "string") priced.set(item.symbol, item);
  return Object.entries(ledger?.positions ?? {}).map(([symbol, position]) => {
    const row = priced.get(symbol);
    const value = num(text(row?.value_eur));
    const cost = num(text(row?.cost_eur)) ?? num(position.cost);
    return { symbol, units: position.units, cost, value, pnl: value !== null && cost ? (value / cost - 1) * 100 : null };
  });
}

/** The valuation's own equity, or cash plus the priced holdings, or null when a holding has no price. */
export function equityFromValuation(ledger: ExchangeLedger | undefined, payload: unknown, rows: HoldingRow[]): number | null {
  const given = isRecord(payload) ? num(text(payload.equity)) : null;
  return given !== null ? given : equityOf(ledger, rows);
}

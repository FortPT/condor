/** Configuration and pure helpers for the read-only Kraken xStocks and Revolut X Trade panels. */
import { useQuery } from "@tanstack/react-query";

import type { ExchangeLedger } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";
import { num } from "./tradeFormat";

export type NativeVenueKey = "kraken-xstocks" | "revolut-x";

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
};

export const NATIVE_VENUES: Record<NativeVenueKey, NativeVenue> = {
  "kraken-xstocks": {
    key: "kraken-xstocks",
    title: "Kraken xStocks",
    base: "/api/v1/kraken-xstocks",
    quote: "USD",
    noun: "stocks",
    listKey: "stocks",
    changeKey: "change_since_open_pct",
    changeLabel: "Since open",
    volumeKey: "volume_24h_usd",
    volumeLabel: "24h volume",
  },
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
  },
};

export const isNativeVenueKey = (value: string | null): value is NativeVenueKey =>
  value === "kraken-xstocks" || value === "revolut-x";

/** What both native services answer on `/status` (a subset of their own status types). */
export type NativeStatus = {
  connected: boolean;
  enabled: boolean;
  quote?: string;
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

/** The final orders of `/history`, in one shape (Revolut X and Kraken name their fields differently). */
export function historyOf(venue: NativeVenue, payload: unknown): HistoryRow[] {
  if (!Array.isArray(payload)) return [];
  return payload.filter(isRecord).map((row, index) => {
    const revolut = venue.key === "revolut-x";
    const when = num(text(revolut ? row.updated : row.closetm));
    const fee = text(row.total_fee ?? row.fee);
    const feeCurrency = text(row.fee_currency);
    return {
      id: text(row.order_id) ?? `${index}`,
      time: when === null ? null : revolut ? when : when * 1000,
      pair: text(row.pair) ?? "—",
      side: text(row.side) ?? "—",
      status: text(row.status) ?? "—",
      quantity: text(revolut ? row.filled_quantity : row.vol_exec) ?? "—",
      average_price: text(revolut ? row.average_fill_price : row.avg_price) ?? "—",
      value: text(revolut ? row.filled_amount : row.cost) ?? "—",
      fee: fee === undefined ? "—" : `${fee}${feeCurrency ? ` ${feeCurrency}` : ""}`,
    };
  }).sort((a, b) => (b.time ?? 0) - (a.time ?? 0)).slice(0, 10);
}

export type AccountRow = { currency: string; balance: string; available: string };

export function accountsOf(payload: unknown): AccountRow[] {
  const list = isRecord(payload) ? payload.accounts : undefined;
  if (!Array.isArray(list)) return [];
  return list.filter(isRecord).flatMap((row) => {
    const currency = text(row.currency);
    return currency ? [{ currency, balance: text(row.balance) ?? "0", available: text(row.available) ?? "0" }] : [];
  });
}

/** Kraken names its dollar balance ZUSD. */
export const isQuoteCurrency = (venue: NativeVenue, currency: string) => {
  const upper = currency.toUpperCase();
  return upper === venue.quote || (venue.key === "kraken-xstocks" && upper === "ZUSD");
};

/** A balance in the quote currency at the best bid, or null when the asset has no live price here. */
export function accountValue(venue: NativeVenue, currency: string, balance: string, known: Map<string, VenueRow>) {
  const amount = num(balance);
  if (amount === null) return null;
  if (isQuoteCurrency(venue, currency)) return amount;
  const upper = currency.toUpperCase();
  // Kraken writes a tokenised stock as AAPLx.
  const row = known.get(upper) ?? (venue.key === "kraken-xstocks" && upper.endsWith("X") ? known.get(upper.slice(0, -1)) : undefined);
  const bid = num(row?.bid);
  return bid === null ? null : amount * bid;
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

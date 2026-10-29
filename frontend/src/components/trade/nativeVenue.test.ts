import { describe, expect, it } from "vitest";

import {
  NATIVE_VENUES, accountRowValue, accountValue, accountsOf, equityFromValuation, equityOf, historyOf, holdingsFromValuation, holdingsOf,
  isNativeVenueKey, rowsOf, type VenueRow,
} from "./nativeVenue";

const revolut = NATIVE_VENUES["revolut-x"];
const kraken = NATIVE_VENUES["kraken-xstocks"];
const t212 = NATIVE_VENUES.trading212;
const row = (symbol: string, bid: string): VenueRow => ({ symbol, bid, change_pct: null, volume: null });

describe("native venue helpers", () => {
  it("recognises only the native workspaces", () => {
    expect(["revolut-x", "kraken-xstocks", "trading212"].every(isNativeVenueKey)).toBe(true);
    expect(["crypto-exchange", "hummingbot", "", null].some(isNativeVenueKey)).toBe(false);
  });

  it("reads each venue's market rows under its own keys and drops bad rows", () => {
    expect(rowsOf(revolut, { coins: [
      { symbol: "BTC", price: "1", bid: "0.9", ask: "1.1", spread_pct: "0.2", change_24h_pct: "1.5", volume_24h_eur: "100" },
      { symbol: 5 }, "junk", null,
    ] })).toEqual([{ symbol: "BTC", price: "1", bid: "0.9", ask: "1.1", spread_pct: "0.2", change_pct: "1.5", volume: "100" }]);
    expect(rowsOf(kraken, { stocks: [{ symbol: "AAPL", bid: "2", change_since_open_pct: "0.5", volume_24h_usd: 12 }] })[0])
      .toMatchObject({ symbol: "AAPL", change_pct: "0.5", volume: 12 });
    expect(rowsOf(kraken, { coins: [{ symbol: "AAPL" }] })).toEqual([]);
    expect(rowsOf(revolut, null)).toEqual([]);
  });

  it("normalises history from both venues, newest first, with the time in milliseconds", () => {
    const rows = historyOf(revolut, [
      { order_id: "a", pair: "BTC-EUR", side: "buy", status: "filled", filled_quantity: "1", average_fill_price: "10", filled_amount: "10", total_fee: "0.01", fee_currency: "EUR", updated: 1000 },
      { order_id: "b", pair: "ETH-EUR", side: "sell", status: "filled", updated: 2000 },
    ]);
    expect(rows.map((r) => r.id)).toEqual(["b", "a"]);
    expect(rows[1]).toMatchObject({ time: 1000, quantity: "1", average_price: "10", value: "10", fee: "0.01 EUR" });
    expect(rows[0].fee).toBe("—");
    expect(historyOf(kraken, [{ order_id: "k", pair: "AAPLxUSD", side: "buy", status: "closed", vol_exec: "2", avg_price: "5", cost: "10", fee: "0.04", closetm: "1793219991.5" }])[0])
      .toMatchObject({ time: 1_793_219_991_500, quantity: "2", average_price: "5", value: "10", fee: "0.04" });
    expect(historyOf(revolut, { not: "a list" })).toEqual([]);
  });

  it("keeps account rows and values them at the bid, treating the quote currency as itself", () => {
    expect(accountsOf({ accounts: [{ currency: "EUR", balance: "40", available: "30" }, { balance: "1" }] }))
      .toEqual([{ currency: "EUR", balance: "40", available: "30" }]);
    const known = new Map([["SUI", row("SUI", "3")], ["AAPL", row("AAPL", "270")]]);
    expect(accountValue(revolut, "EUR", "40", known)).toBe(40);
    expect(accountValue(revolut, "SUI", "5", known)).toBe(15);
    expect(accountValue(revolut, "XYZ", "5", known)).toBeNull();
    expect(accountValue(kraken, "ZUSD", "40", known)).toBe(40);
    expect(accountValue(kraken, "AAPLx", "2", known)).toBe(540);
    expect(accountValue(revolut, "SUI", "n/a", known)).toBeNull();
  });

  it("values the bot's positions and its equity, and refuses a total with an unpriced holding", () => {
    const ledger = { quote: "EUR", cash: "40", positions: { SUI: { units: "5", cost: "12" }, XYZ: { units: "1", cost: "1" } }, uncertain_order: null, synced: true };
    const known = new Map([["SUI", row("SUI", "3")]]);
    const holdings = holdingsOf(ledger, known);
    expect(holdings[0]).toMatchObject({ symbol: "SUI", value: 15, cost: 12 });
    expect(holdings[0].pnl).toBeCloseTo(25);
    expect(holdings[1]).toMatchObject({ symbol: "XYZ", value: null, pnl: null });
    expect(equityOf(ledger, holdings)).toBeNull();
    expect(equityOf(ledger, holdings.slice(0, 1))).toBe(55);
    expect(equityOf(undefined, [])).toBeNull();
  });

  it("describes Trading 212 as a EUR ledger over USD reference rows that it values itself", () => {
    expect(t212).toMatchObject({ key: "trading212", base: "/api/v1/trading212", quote: "EUR", priceQuote: "USD", marketsPath: "stocks", autoValuation: true });
    expect(t212.referenceLabel).toContain("Kraken xStocks");
    expect(rowsOf(t212, { stocks: [{ symbol: "AAPL", bid: "2", change_since_open_pct: "0.5", volume_24h_usd: 12 }] })[0])
      .toMatchObject({ symbol: "AAPL", change_pct: "0.5", volume: 12 });
    expect(historyOf(t212, [{ order_id: "t", pair: "AAPL_US_EQ", side: "buy", status: "FILLED", filled_quantity: "0.05", average_fill_price: "270", filled_amount: "11.6", total_fee: "0.02", fee_currency: "EUR", updated: 5000 }])[0])
      .toMatchObject({ time: 5000, pair: "AAPL_US_EQ", quantity: "0.05", average_price: "270", value: "11.6", fee: "0.02 EUR" });
  });

  it("values Trading 212 balances and holdings from its own EUR figures, never from USD reference bids", () => {
    const rows = accountsOf({ accounts: [{ currency: "EUR", balance: "40", available: "30" }, { currency: "AAPL", balance: "0.05", available: "0.05", value_eur: "11.6" }, { currency: "TSLA", balance: "1", available: "1" }] });
    const known = new Map([["AAPL", row("AAPL", "270")]]);
    expect(rows[1].value_eur).toBe("11.6");
    expect(rows.map((r) => accountRowValue(t212, r, known))).toEqual([40, 11.6, null]);
    expect(accountRowValue(kraken, { currency: "AAPLx", balance: "2", available: "2" }, known)).toBe(540);
    const ledger = { quote: "EUR", cash: "30", positions: { AAPL: { units: "0.05", cost: "10" }, TSLA: { units: "1", cost: "5" } }, uncertain_order: null, synced: true };
    const payload = { equity: "41.6", positions: [{ symbol: "AAPL", units: "0.05", cost_eur: "10", price_eur: "232", value_eur: "11.6", price_usd: "270" }] };
    const holdings = holdingsFromValuation(ledger, payload);
    expect(holdings[0]).toMatchObject({ symbol: "AAPL", cost: 10, value: 11.6 });
    expect(holdings[0].pnl).toBeCloseTo(16);
    expect(holdings[1]).toMatchObject({ symbol: "TSLA", cost: 5, value: null, pnl: null });
    expect(equityFromValuation(ledger, payload, holdings)).toBe(41.6);
    expect(equityFromValuation(ledger, undefined, holdings)).toBeNull();
    expect(holdingsFromValuation(ledger, "junk").every((h) => h.value === null)).toBe(true);
  });
});

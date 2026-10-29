/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeVenueTrade } from "./NativeVenueTrade";
import { NATIVE_VENUES } from "./nativeVenue";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), setData: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  createChart: () => ({
    addSeries: () => ({ setData: mocks.setData }),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
}));

const summary = { total: 0, resolved: 0, open: 0, hit_target: 0, stopped: 0, expired: 0, avg_gross_pct: null, avg_net_pct: null, beat_costs: 0 };
const scorecard = {
  assumptions: { round_trip_cost_pct: 0.35, basis: "default" }, prices: { snapshots: 40, coins: 12, days: 1.5 },
  trade_calls: { ...summary, total: 2, resolved: 1, open: 1, hit_target: 1, avg_net_pct: 0.9, beat_costs: 1 },
  pass_calls: { ...summary }, by_cost_multiple: [{ multiple: 1, min_target_pct: 0.4, resolved: 1, avg_net_pct: 0.9, win_rate: 1 }],
  baseline_gross_pct: 0.1, edge_vs_baseline_pct: 0.8, enough_data: false,
  recent: [],
};
const candles = { pair: "X", timeframe: "1h", candles: [
  { t: 200000, o: "2", h: "3", l: "1", c: "2.5", v: "1" },
  { t: 100000, o: "1", h: "2", l: "0.5", c: "2", v: "1" },
] };

/** What each venue's service really answers, in its own field names. */
const fixtures = {
  "revolut-x": {
    quote: "EUR",
    market: undefined,
    coinsPayload: (rows: unknown[]) => ({ quote: "EUR", coins: rows }),
    rows: [
      { symbol: "BTC", pair: "BTC-EUR", price: "72649", bid: "72643", ask: "72650", spread_pct: "0.01", change_24h_pct: "1.5", range_24h_pct: "3", volume_24h_eur: "9442733" },
      { symbol: "SUI", pair: "SUI-EUR", price: "3.001", bid: "3", ask: "3.003", spread_pct: "0.1", change_24h_pct: "-2.25", volume_24h_eur: "500000" },
    ],
    accounts: [{ currency: "EUR", balance: "40", available: "40" }, { currency: "SUI", balance: "5", available: "5" }],
    history: [{ order_id: "h1", pair: "BTC-EUR", side: "buy", status: "filled", filled_quantity: "0.0001", average_fill_price: "72000", filled_amount: "7.2", total_fee: "0.01", fee_currency: "EUR", updated: 1_793_219_991_000 }],
    historyPair: "BTC-EUR",
    changes: ["+1.50%", "-2.25%"],
    held: "SUI",
    heldValue: /15[.,]00 EUR/,
    cashText: /Cash 40[.,]00 EUR/,
    equityText: /Equity 55[.,]00 EUR/,
    accountValue: /15[.,]00 EUR/,
  },
  "kraken-xstocks": {
    quote: "USD",
    market: { open: true, new_york_time: "2026-10-28 10:00", minutes_to_close: 360 },
    coinsPayload: (rows: unknown[]) => ({ quote: "USD", market: { open: true }, stocks: rows }),
    rows: [
      { symbol: "AAPL", pair: "AAPLxUSD", price: "270", bid: "269.9", ask: "270.1", spread_pct: "0.074", volume_24h_usd: 9_000_000, range_24h_pct: "2", change_since_open_pct: "0.8" },
      { symbol: "TSLA", pair: "TSLAxUSD", price: "300", bid: "300", ask: "300.4", spread_pct: "0.133", volume_24h_usd: 5_000_000, range_24h_pct: "3", change_since_open_pct: "-1.2" },
    ],
    accounts: [{ currency: "ZUSD", balance: "40", available: "40" }, { currency: "TSLAx", balance: "5", available: "5" }],
    history: [{ order_id: "h1", pair: "AAPLxUSD", side: "buy", status: "closed", vol_exec: "0.1", avg_price: "270", cost: "27", fee: "0.04", closetm: "1793219991.5" }],
    historyPair: "AAPLxUSD",
    changes: ["+0.80%", "-1.20%"],
    held: "TSLA",
    heldValue: /1,?500[.,]00 USD/,
    cashText: /Cash 40[.,]00 USD/,
    equityText: /Equity 1,?540[.,]00 USD/,
    accountValue: /1,?500[.,]00 USD/,
  },
} as const;
type Key = keyof typeof fixtures;

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
let status: Record<string, unknown>;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
const calls = () => mocks.fetch.mock.calls as [string, { method?: string } | undefined][];
const paths = () => calls().map(([path]) => path);

function build(key: Key, overrides: Partial<{ connected: boolean; positions: Record<string, { units: string; cost: string }>; uncertain: unknown }> = {}) {
  const f = fixtures[key];
  const positions = overrides.positions ?? (key === "revolut-x" ? { SUI: { units: "5", cost: "12" } } : { TSLA: { units: "5", cost: "1400" } });
  status = {
    connected: overrides.connected ?? true, venue: NATIVE_VENUES[key].title, quote: f.quote, enabled: true,
    ledger: {
      quote: f.quote, cash: "40", positions, uncertain_order: overrides.uncertain ?? null, enabled: true, floor: "2", synced: true,
      orders: [{ time: 1_793_219_000, side: "buy", symbol: f.held, order_id: "o1", status: "filled", quantity: "5", value: "12", fee: "0.01", fee_currency: f.quote }],
    },
    ...(f.market ? { market: f.market } : {}),
  };
  mocks.fetch.mockReset().mockImplementation((path: string) => {
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/scorecard")) return Promise.resolve(scorecard);
    if (path.includes("/coins?keyword=SU")) return Promise.resolve(f.coinsPayload([f.rows[1]]));
    if (path.includes("/coins")) return Promise.resolve(f.coinsPayload([...f.rows]));
    if (path.includes("/candles")) return Promise.resolve(candles);
    if (path.endsWith("/accounts")) return Promise.resolve({ accounts: f.accounts });
    if (path.endsWith("/history")) return Promise.resolve(f.history);
    if (path.endsWith("/valuation")) return Promise.resolve({ equity: "55", quote: f.quote, minimum_cash_reserve: "0" });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}
async function mount(key: Key) {
  await act(async () => root.render(<QueryClientProvider client={qc}><NativeVenueTrade venue={NATIVE_VENUES[key]} /></QueryClientProvider>));
  // The status resolves first and only then do the private reads start.
  await wait();
  await wait();
}
const button = (label: string | RegExp) => Array.from(host.querySelectorAll("button")).find((b) =>
  typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? ""))!;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  mocks.setData.mockReset();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
});

describe.each(["revolut-x", "kraken-xstocks"] as const)("%s panel", (key) => {
  const f = fixtures[key];
  const venue = NATIVE_VENUES[key];

  it("shows the bot allocation, the markets, the balances and the history of a connected account", async () => {
    build(key);
    await mount(key);
    const text = host.textContent!;
    expect(host.querySelector("h1")!.textContent).toBe(venue.title);
    expect(text).toContain("Connected");
    expect(text).toMatch(f.cashText);
    expect(text).toMatch(f.equityText);
    // markets: bid, ask, spread and the change, whichever way the venue names them
    const markets = host.querySelector('section[aria-label="Markets"]')!;
    expect(markets.textContent).toContain(f.rows[0].symbol);
    expect(markets.textContent).toContain(f.rows[0].spread_pct + "%");
    expect(markets.textContent).toMatch(venue.changeLabel);
    for (const change of f.changes) expect(markets.textContent).toContain(change);
    expect(markets.textContent).toContain("held");
    // the bot's positions are valued at the live best bid
    const holdings = host.querySelector('section[aria-label="Holdings"]')!.textContent!;
    expect(holdings).toContain(f.held);
    expect(holdings).toMatch(f.heldValue);
    // the whole account, with values where a price is known
    const balances = host.querySelector('section[aria-label="Account balances"]')!.textContent!;
    expect(balances).toContain(f.accounts[1].currency);
    expect(balances).toMatch(f.accountValue);
    // the order history, normalised from this venue's own field names
    const recent = host.querySelector('section[aria-label="Recent orders"]')!.textContent!;
    expect(recent).toContain(f.historyPair);
    expect(recent).toContain("buy");
    expect(recent).toContain("Bot ledger");
    // the scorecard route is used
    expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain(`real ${venue.title} prices`);
    if (key === "kraken-xstocks") expect(text).toContain("US market open");
    expect(host.querySelector("[data-state='not-connected']")).toBeNull();
  });

  it("is read-only: no order or preview control, and only GET reads", async () => {
    build(key);
    await mount(key);
    const labels = Array.from(host.querySelectorAll("button")).map((b) => b.textContent ?? "");
    expect(labels.filter((label) => /\b(buy|sell|order|place|preview|confirm|enable|pause|reconcile)\b/i.test(label))).toEqual([]);
    expect(host.querySelector("form")).toBeNull();
    await act(async () => button("Value allocation").click());
    await wait();
    expect(host.textContent).toContain("55");
    expect(calls().every(([path, init]) => path.startsWith(venue.base) && !init)).toBe(true);
    expect(paths()).toContain(`${venue.base}/valuation`);
    expect(paths().some((path) => /\/(order|preview|enabled|sync|reconcile|credentials)\b/.test(path))).toBe(false);
  });

  it("reads each endpoint once on mount, so it adds no per-second polling", async () => {
    build(key);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await mount(key);
      const before = paths().length;
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      // status polls every 10 s and the markets every 30 s: nothing refires within 5 s
      expect(paths().length).toBe(before);
      for (const suffix of ["/status", "/accounts", "/history", "/scorecard"]) {
        expect(paths().filter((path) => path.endsWith(suffix)).length).toBe(1);
      }
      expect(paths().filter((path) => path.includes("/coins?keyword=")).length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("charts the picked market and filters by search", async () => {
    build(key);
    await mount(key);
    const second = f.rows[1].symbol;
    await act(async () => button(second).click());
    await wait();
    expect(paths().some((path) => path.includes(`/candles?symbol=${second}&timeframe=1h`))).toBe(true);
    expect(host.querySelector('[data-testid="exchange-chart"]')).not.toBeNull();
    const search = host.querySelector("input")! as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(search, "su");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await wait();
    expect(paths().some((path) => path.includes("/coins?keyword=SU"))).toBe(true);
  });

  it("says plainly that it is not connected and points to Settings instead of going blank", async () => {
    build(key, { connected: false, positions: {} });
    await mount(key);
    const notice = host.querySelector("[data-state='not-connected']")!;
    expect(notice.textContent).toContain("Not connected - open Settings");
    expect(notice.querySelector("a")!.getAttribute("href")).toBe(`/settings?tab=${key}`);
    expect(host.textContent).toContain("not connected");
    expect(host.querySelector('section[aria-label="Account balances"]')!.textContent).toContain("Not connected - open Settings");
    expect(host.querySelector('section[aria-label="Recent orders"]')!.textContent).toContain("Not connected - open Settings");
    // no private read is attempted, public markets still show
    expect(paths().some((path) => path.endsWith("/accounts") || path.endsWith("/history"))).toBe(false);
    expect(host.querySelector('section[aria-label="Markets"]')!.textContent).toContain(f.rows[0].symbol);
    expect(button("Refresh balances").disabled).toBe(true);
    expect(button("Value allocation").disabled).toBe(true);
    expect(host.querySelector("[role='alert']")).toBeNull();
  });

  it("shows an empty bot allocation and an empty market list without breaking", async () => {
    build(key, { positions: {} });
    mocks.fetch.mockImplementation((path: string) => {
      if (path.endsWith("/status")) return Promise.resolve(status);
      if (path.includes("/coins")) return Promise.resolve(f.coinsPayload([]));
      if (path.endsWith("/accounts")) return Promise.resolve({ accounts: [] });
      if (path.endsWith("/history")) return Promise.resolve([]);
      return Promise.reject(new Error("no data"));
    });
    await mount(key);
    expect(host.textContent).toContain("The bot holds no positions.");
    expect(host.textContent).toContain(`No ${venue.noun} are listed on ${venue.title} right now.`);
    expect(host.textContent).toContain(`The ${venue.title} account holds no funded balances.`);
    expect(host.textContent).toContain("No final orders in the history window.");
    expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain("Scorecard unavailable");
  });

  it("shows the error when the status cannot be read, and when a private read fails", async () => {
    build(key);
    mocks.fetch.mockImplementation((path: string) => {
      if (path.endsWith("/status")) return Promise.reject(new Error("Server unreachable"));
      if (path.includes("/coins")) return Promise.reject(new Error("markets are down"));
      return Promise.reject(new Error("nope"));
    });
    await mount(key);
    expect(host.querySelector("[role='alert']")!.textContent).toContain("Server unreachable");
    expect(host.textContent).toContain("Markets unavailable: markets are down");
    expect(host.querySelector("h1")!.textContent).toBe(venue.title);
    await act(async () => root.unmount());
    qc.clear();
    root = createRoot(host);
    build(key);
    mocks.fetch.mockImplementation((path: string) => {
      if (path.endsWith("/status")) return Promise.resolve(status);
      if (path.endsWith("/accounts")) return Promise.reject(new Error("Revolut refused the read"));
      if (path.includes("/coins")) return Promise.resolve(f.coinsPayload([...f.rows]));
      if (path.endsWith("/history")) return Promise.resolve([]);
      return Promise.reject(new Error("nope"));
    });
    await mount(key);
    expect(host.querySelector("[role='alert']")!.textContent).toContain("Revolut refused the read");
    expect(host.querySelector('section[aria-label="Account balances"]')!.textContent).toContain("Balances unavailable");
    expect(host.textContent).toMatch(f.cashText);
  });

  it("tells the owner when the allocation is unsynced or an order needs reconciling", async () => {
    build(key, { uncertain: { client_oid: "x" } });
    (status.ledger as { synced: boolean }).synced = false;
    await mount(key);
    expect(host.textContent).toContain(`The bot allocation has not been synced from the ${venue.title} account`);
    expect(host.querySelector("[role='alert']")!.textContent).toContain("needs reconciliation");
  });
});

/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeVenueTrade } from "./NativeVenueTrade";
import { NATIVE_VENUES, type NativeVenueKey } from "./nativeVenue";
import type { Performance } from "./performance";
import { makePerformance } from "./performance.fixture";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  createChart: () => ({ addSeries: () => ({ setData: vi.fn() }), timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() }),
}));

/** What each venue needs to show one SUI/TSLA holding at a known price. */
const CASES = {
  "revolut-x": { quote: "EUR", symbol: "SUI", positions: { SUI: { units: "5", cost: "12" } }, cash: "40", equity: /55[.,]00 EUR equity/, pnl: /\+3[.,]00 EUR/, losing: { SUI: { units: "5", cost: "20" } }, loss: /-5[.,]00 EUR/ },
  trading212: { quote: "EUR", symbol: "TSLA", positions: { TSLA: { units: "0.05", cost: "10" } }, cash: "30", equity: /41[.,]60 EUR equity/, pnl: /\+1[.,]60 EUR/, losing: { TSLA: { units: "0.05", cost: "20" } }, loss: /-8[.,]40 EUR/ },
} as const;

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
let status: Record<string, unknown>;
let perf: Performance | Error;
let put: ((body: { enabled: boolean }) => Promise<unknown>) | null;
let history: unknown[];
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
const calls = () => mocks.fetch.mock.calls as [string, { method?: string; body?: string } | undefined][];
const paths = () => calls().map(([path]) => path);

function setup(key: NativeVenueKey, opts: {
  connected?: boolean; enabled?: boolean; synced?: boolean; uncertain?: boolean; currency?: string;
  positions?: Record<string, { units: string; cost: string }>; orders?: Record<string, unknown>[]; valuation?: unknown;
} = {}) {
  const c = CASES[key];
  const venue = NATIVE_VENUES[key];
  status = {
    connected: opts.connected ?? true, has_signing_key: true, venue: venue.title, quote: c.quote, enabled: opts.enabled ?? true,
    account_currency: opts.currency ?? (key === "trading212" ? "EUR" : undefined),
    market: key === "revolut-x" ? undefined : { open: true, minutes_to_close: 100 },
    ledger: {
      quote: c.quote, cash: c.cash, positions: opts.positions ?? c.positions, uncertain_order: opts.uncertain ? { client_oid: "x" } : null,
      enabled: true, floor: "2", synced: opts.synced ?? true, orders: opts.orders ?? [],
    },
  };
  perf = makePerformance(c.quote);
  put = (body) => Promise.resolve({ ...status, enabled: body.enabled });
  history = [];
  const row = { symbol: c.symbol, bid: "3", ask: "3.1", price: "3", spread_pct: "0.1", change_24h_pct: "1", change_since_open_pct: "1", volume_24h_eur: 1, volume_24h_usd: 1 };
  mocks.fetch.mockReset().mockImplementation((path: string, init?: { method?: string; body?: string }) => {
    if (init?.method === "PUT" && path.endsWith("/enabled")) return put!(JSON.parse(init.body!));
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/performance")) return perf instanceof Error ? Promise.reject(perf) : Promise.resolve(perf);
    if (path.includes("/coins")) return Promise.resolve({ quote: c.quote, coins: [row], stocks: [row] });
    if (path.includes("/stocks")) return Promise.resolve({ quote: c.quote, stocks: [row] });
    if (path.includes("/candles")) return Promise.resolve({ pair: "X", timeframe: "1h", candles: [] });
    if (path.endsWith("/accounts")) return Promise.resolve({ accounts: [] });
    if (path.endsWith("/history")) return Promise.resolve(history);
    if (path.endsWith("/valuation")) {
      return Promise.resolve("valuation" in opts ? opts.valuation : {
        equity: "41.6", quote: c.quote,
        positions: [{ symbol: "TSLA", units: "0.05", cost_eur: ((opts.positions ?? c.positions) as Record<string, { cost: string }>).TSLA?.cost ?? "10", value_eur: "11.6" }],
      });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
  return venue;
}
async function mount(key: NativeVenueKey) {
  await act(async () => root.render(<QueryClientProvider client={qc}><NativeVenueTrade venue={NATIVE_VENUES[key]} /></QueryClientProvider>));
  await wait();
  await wait();
}
const section = (label: string) => host.querySelector(`section[aria-label="${label}"]`)!;
const switchButton = () => Array.from(host.querySelectorAll("button")).find((b) => /^(Pause trading|Enable trading within allocation|Pausing…|Enabling…)$/.test(b.textContent ?? ""))!;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

describe.each(["revolut-x", "trading212"] as const)("%s earnings overview", (key) => {
  const c = CASES[key];

  it("shows the six performance cards directly under the header line", async () => {
    setup(key);
    await mount(key);
    const summary = section("Performance");
    expect(Array.from(summary.querySelectorAll("[data-card]")).map((card) => card.getAttribute("data-card")))
      .toEqual(["today", "since-start", "realized", "unrealized", "fees", "trades"]);
    const heading = host.querySelector("h1")!;
    const markets = section("Markets");
    expect(heading.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.compareDocumentPosition(markets) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.querySelector('[data-card="today"]')!.textContent).toMatch(/\+1[.,]50 USD/);
    expect(summary.querySelector('[data-card="realized"]')!.textContent).toMatch(/\+4[.,]50 (EUR|USD)/);
    expect(paths().filter((path) => path.endsWith("/performance")).length).toBe(1);
  });

  it("adds a P/L amount beside vs cost and a totals row with equity and the total unrealized", async () => {
    setup(key);
    await mount(key);
    const holdings = section("Holdings");
    const header = Array.from(holdings.querySelectorAll("thead th")).map((th) => th.textContent);
    expect(header.slice(-2)).toEqual(["vs cost", "P/L"]);
    const row = Array.from(holdings.querySelectorAll("tbody tr")).find((tr) => tr.textContent!.startsWith(c.symbol))!;
    expect(row.textContent).toMatch(c.pnl);
    expect(row.querySelector("td:last-child span")!.className).toContain("--color-green");
    const totals = holdings.querySelector('tr[data-row="totals"]')!;
    expect(totals.textContent).toMatch(c.equity);
    expect(totals.textContent).toMatch(c.pnl);
  });

  it("colours a losing holding red and shows no totals rather than a wrong one for an unpriced holding", async () => {
    setup(key, { positions: c.losing });
    await mount(key);
    const row = Array.from(section("Holdings").querySelectorAll("tbody tr")).find((tr) => tr.textContent!.startsWith(c.symbol))!;
    expect(row.textContent).toMatch(c.loss);
    expect(row.querySelector("td:last-child span")!.className).toContain("--color-red");
    expect(section("Holdings").querySelector('tr[data-row="totals"]')!.textContent).toMatch(c.loss);
    await act(async () => root.unmount());
    qc.clear();
    root = createRoot(host);
    setup(key, key === "trading212" ? { valuation: { equity: null, positions: [] } } : { positions: { ZZZ: { units: "1", cost: "5" } } });
    await mount(key);
    const totals = section("Holdings").querySelector('tr[data-row="totals"]')!;
    expect(totals.textContent).toContain("Total (cash + holdings)");
    expect(totals.textContent).not.toMatch(/equity/);
    expect(totals.querySelector("td:last-child")!.textContent).toBe("—");
  });

  it("gives every sale in the ledger a Result and leaves buys and unknown sales honest", async () => {
    setup(key, {
      orders: [
        { time: 1_793_219_000, side: "buy", symbol: c.symbol, order_id: "b1", status: "filled", quantity: "5", value: "12", fee: "0.01", fee_currency: c.quote },
        { time: 1_793_219_100, side: "sell", symbol: c.symbol, order_id: "s1", status: "filled", quantity: "2", value: "6", fee: "0.01", fee_currency: c.quote },
        { time: 1_793_219_200, side: "sell", symbol: c.symbol, order_id: "s2", status: "filled", quantity: "1", value: "3", fee: "0.01", fee_currency: c.quote },
      ],
    });
    perf = makePerformance(c.quote, {
      orders: [
        { time: 1_793_219_100, symbol: c.symbol, order_id: "s1", client_oid: null, pnl: 1.2, pnl_pct: 25, closed_position: false, reason: null },
        { time: 1_793_219_200, symbol: c.symbol, order_id: "s2", client_oid: null, pnl: null, pnl_pct: null, closed_position: false, reason: "cost of the position held before the first recorded order is not on record" },
      ],
    });
    history = [{ order_id: "s1", pair: "X", side: "sell", status: "filled", filled_quantity: "2", average_fill_price: "3", filled_amount: "6", filled_quantity_: "2", vol_exec: "2", avg_price: "3", cost: "6", closetm: "1793219100", updated: 1_793_219_100_000 }];
    await mount(key);
    const recent = section("Recent orders");
    const tables = recent.querySelectorAll("table");
    const ledger = tables[tables.length - 1];
    expect(Array.from(ledger.querySelectorAll("thead th")).map((th) => th.textContent).at(-1)).toBe("Result");
    const cells = Array.from(ledger.querySelectorAll("tbody tr")).map((tr) => tr.querySelector("td:last-child")!);
    // newest first: s2, s1, b1
    expect(cells[0].textContent).toBe("n/a");
    expect(cells[0].getAttribute("title")).toContain("not on record");
    expect(cells[1].textContent).toMatch(/\+1[.,]20/);
    expect(cells[1].textContent).toMatch(/\+25[.,]00%/);
    expect(cells[2].textContent).toBe("—");
    // the venue's own history rows are matched by order id
    const historyCell = tables[0].querySelector("tbody tr td:last-child")!;
    expect(historyCell.textContent).toMatch(/\+1[.,]20/);
  });

  it("says why a figure is unavailable instead of showing zero, and keeps the page working", async () => {
    setup(key);
    perf = makePerformance(c.quote, {
      today: { value: null, currency: "USD", since: null, since_label: null, partial: false, estimated: false, flows_usd: 0, reason: "No equity history recorded yet", note: null },
      unrealized: { value: null, currency: c.quote, reason: "Reconcile the uncertain order before valuing" },
      realized: { value: 2, complete: false, reason: "1 sale(s) have no recorded cost and are not counted: x", sells: 3, unknown_sells: 1, partial_history: false, partial_assets: [], history_starts_at: null, currency: c.quote },
    });
    await mount(key);
    const summary = section("Performance");
    expect(summary.querySelector('[data-card="today"]')!.textContent).toContain("—");
    expect(summary.querySelector('[data-note="today"]')!.textContent).toBe("No equity history recorded yet");
    expect(summary.querySelector('[data-note="unrealized"]')!.textContent).toContain("Reconcile the uncertain order");
    expect(summary.querySelector('[data-note="realized"]')!.textContent).toContain("have no recorded cost");
    expect(section("Holdings").textContent).toContain(c.symbol);
  });

  it("names a failed performance read without hiding the venue", async () => {
    setup(key);
    perf = new Error("boom");
    await mount(key);
    expect(section("Performance").textContent).toContain("Performance unavailable: boom");
    expect(host.querySelector("[role='alert']")).toBeNull();
    expect(host.querySelector("h1")!.textContent).toBe(NATIVE_VENUES[key].title);
  });
});

describe.each(["revolut-x", "trading212"] as const)("%s pause and enable", (key) => {
  const base = NATIVE_VENUES[key].base;
  const puts = () => calls().filter(([, init]) => init?.method === "PUT");

  it("pauses through the existing PUT route and re-reads the status", async () => {
    setup(key);
    await mount(key);
    expect(switchButton().textContent).toBe("Pause trading");
    expect(switchButton().disabled).toBe(false);
    const before = paths().filter((path) => path.endsWith("/status")).length;
    await act(async () => switchButton().click());
    await wait();
    expect(puts().map(([path, init]) => [path, init!.body])).toEqual([[`${base}/enabled`, JSON.stringify({ enabled: false })]]);
    expect(paths().filter((path) => path.endsWith("/status")).length).toBeGreaterThan(before);
  });

  it("offers enabling only when the venue's own rules allow it", async () => {
    setup(key, { enabled: false });
    await mount(key);
    expect(switchButton().textContent).toBe("Enable trading within allocation");
    expect(switchButton().disabled).toBe(false);
    expect(host.querySelector("[data-testid='toggle-block']")).toBeNull();
    await act(async () => switchButton().click());
    await wait();
    expect(puts().map(([, init]) => init!.body)).toEqual([JSON.stringify({ enabled: true })]);
  });

  it("will not enable an allocation that has not been synced, and says so", async () => {
    setup(key, { enabled: false, synced: false });
    await mount(key);
    expect(switchButton().disabled).toBe(true);
    expect(host.querySelector("[data-testid='toggle-block']")!.textContent).toContain("sync the allocation first");
    await act(async () => switchButton().click());
    expect(puts()).toEqual([]);
  });

  it("will not enable while an order needs reconciling, but still allows a pause", async () => {
    setup(key, { enabled: false, uncertain: true });
    await mount(key);
    expect(switchButton().disabled).toBe(true);
    expect(host.querySelector("[data-testid='toggle-block']")!.textContent).toContain("reconcile the uncertain order first");
    await act(async () => root.unmount());
    qc.clear();
    root = createRoot(host);
    setup(key, { enabled: true, uncertain: true });
    await mount(key);
    expect(switchButton().textContent).toBe("Pause trading");
    expect(switchButton().disabled).toBe(false);
  });

  it("is unavailable while not connected", async () => {
    setup(key, { connected: false, enabled: false });
    await mount(key);
    expect(switchButton().disabled).toBe(true);
    expect(puts()).toEqual([]);
  });

  it("shows a pending state while the change is in flight and the failure when it is refused", async () => {
    setup(key);
    put = () => new Promise(() => {});
    await mount(key);
    await act(async () => switchButton().click());
    await wait();
    expect(switchButton().textContent).toBe("Pausing…");
    expect(switchButton().getAttribute("aria-busy")).toBe("true");
    expect(switchButton().disabled).toBe(true);
    await act(async () => root.unmount());
    qc.clear();
    root = createRoot(host);
    setup(key, { enabled: false });
    put = () => Promise.reject(new Error("Sync the allocation before enabling"));
    await mount(key);
    await act(async () => switchButton().click());
    await wait();
    expect(host.querySelector("[role='alert']")!.textContent).toContain("Sync the allocation before enabling");
  });
});

describe("trading212 pause and enable", () => {
  it("will not enable an account that is not in EUR", async () => {
    setup("trading212", { enabled: false, currency: "USD" });
    await mount("trading212");
    expect(switchButton().disabled).toBe(true);
    expect(host.querySelector("[data-testid='toggle-block']")!.textContent).toContain("the account currency is not EUR");
  });
});

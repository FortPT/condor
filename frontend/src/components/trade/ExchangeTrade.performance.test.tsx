/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ExchangeTrade } from "./ExchangeTrade";
import type { Performance } from "./performance";
import { makePerformance } from "./performance.fixture";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  createChart: () => ({ addSeries: () => ({ setData: vi.fn() }), timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() }),
}));

const coin = (symbol: string, last: string, bid = last) => ({
  symbol, pair: `${symbol}_USD`, price: last, bid, ask: last, spread_pct: "0.02", change_24h_pct: "1.5", volume_24h: "1000000", min_quantity_step: "0.001",
});
const sell = { time: 1_793_219_100, side: "sell", symbol: "CRO", order_id: "s1", status: "FILLED", quantity: "10", value: "0.6", fee: "0.001", fee_currency: "USD" };
const unknownSell = { ...sell, time: 1_793_219_200, order_id: "s2" };
const buy = { ...sell, time: 1_793_219_000, side: "buy", order_id: "b1" };

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
let status: Record<string, unknown>;
let perf: Performance;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });

function ledger(extra: Record<string, unknown> = {}) {
  return { quote: "USD", cash: "26", positions: { CRO: { units: "216", cost: "9.98" } }, uncertain_order: null, enabled: true, orders: [buy, sell, unknownSell], floor: "2", synced: true, ...extra };
}
async function mount(next?: { enabled?: boolean; synced?: boolean; connected?: boolean }) {
  status = { connected: next?.connected ?? true, environment: "production", enabled: next?.enabled ?? true, ledger: ledger({ synced: next?.synced ?? true }) };
  perf = makePerformance("USD", {
    orders: [
      { time: 1_793_219_100, symbol: "CRO", order_id: "s1", client_oid: null, pnl: -0.25, pnl_pct: -4.1, closed_position: false, reason: null },
      { time: 1_793_219_200, symbol: "CRO", order_id: "s2", client_oid: null, pnl: null, pnl_pct: null, closed_position: false, reason: "cost of the position held before the first recorded order is not on record" },
    ],
  });
  mocks.fetch.mockReset().mockImplementation((path: string, init?: { method?: string; body?: string }) => {
    if (init?.method === "PUT") return Promise.resolve({});
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/performance")) return Promise.resolve(perf);
    if (path.includes("/coins")) return Promise.resolve({ quote: "USD", coins: [coin("BTC", "73000"), coin("CRO", "0.05", "0.0499")] });
    if (path.includes("/candles")) return Promise.resolve({ pair: "BTC_USD", timeframe: "1h", candles: [] });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
  await act(async () => root.render(<QueryClientProvider client={qc}><ExchangeTrade /></QueryClientProvider>));
  await wait();
  await wait();
}
const section = (label: string) => host.querySelector(`section[aria-label="${label}"]`)!;
const toggle = () => Array.from(host.querySelectorAll("button")).find((b) => /^(Pause trading|Enable trading within allocation)$/.test(b.textContent ?? "")) as HTMLButtonElement;

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

describe("Crypto.com earnings overview", () => {
  it("puts the six performance cards directly under the header line", async () => {
    await mount();
    const summary = section("Performance");
    expect(Array.from(summary.querySelectorAll("[data-card]")).map((card) => card.getAttribute("data-card")))
      .toEqual(["today", "since-start", "realized", "unrealized", "fees", "trades"]);
    expect(host.querySelector("h1")!.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.compareDocumentPosition(section("Markets")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.querySelector('[data-card="since-start"]')!.textContent).toMatch(/-3[.,]25 USD/);
    expect(summary.querySelector('[data-card="since-start"] p[data-tone]')!.className).toContain("--color-red");
    expect(summary.querySelector('[data-card="today"] p[data-tone]')!.className).toContain("--color-green");
    expect(summary.querySelector('[data-card="trades"]')!.textContent).toContain("1W / 1L");
    expect(summary.querySelector('[data-card="today"]')!.getAttribute("title")).toContain("deposits and withdrawals");
  });

  it("adds a coloured P/L amount beside vs cost and a totals row", async () => {
    await mount();
    const holdings = section("Holdings");
    const header = Array.from(holdings.querySelectorAll("thead th")).map((th) => th.textContent);
    expect(header).toEqual(["Asset", "Units", "Cost", "Value at bid", "vs cost", "P/L", ""]);
    const row = Array.from(holdings.querySelectorAll("tbody tr")).find((tr) => tr.textContent!.startsWith("CRO"))!;
    // 216 x 0.0499 = 10.7784 against a cost of 9.98
    expect(row.textContent).toMatch(/\+0[.,]80 USD/);
    expect(row.querySelectorAll("td")[5].querySelector("span")!.className).toContain("--color-green");
    const totals = holdings.querySelector('tr[data-row="totals"]')!;
    expect(totals.textContent).toMatch(/36[.,]78 USD equity/);
    expect(totals.textContent).toMatch(/\+0[.,]80 USD/);
    expect(totals.querySelectorAll("td")).toHaveLength(7);
  });

  it("shows each sale's realized result in a Result column, a dash for a buy and n/a with the reason when unknown", async () => {
    await mount();
    const orders = section("Bot orders");
    const header = Array.from(orders.querySelectorAll("thead th")).map((th) => th.textContent);
    expect(header).toEqual(["Time", "Side", "Asset", "Status", "Filled", "Value", "Fee", "Result"]);
    const cells = Array.from(orders.querySelectorAll("tbody tr")).map((tr) => tr.querySelector("td:last-child")!);
    // newest first: s2 (unknown), s1 (-0.25), b1 (buy)
    expect(cells[0].textContent).toBe("n/a");
    expect(cells[0].getAttribute("title")).toContain("not on record");
    expect(cells[1].textContent).toMatch(/-0[.,]25 USD/);
    expect(cells[1].textContent).toMatch(/-4[.,]10%/);
    expect(cells[1].querySelector("span")!.className).toContain("--color-red");
    expect(cells[2].textContent).toBe("—");
  });

  it("keeps working and says so when the performance report cannot be read", async () => {
    await mount();
    mocks.fetch.mockImplementation((path: string) => path.endsWith("/performance") ? Promise.reject(new Error("history locked"))
      : path.endsWith("/status") ? Promise.resolve(status) : path.includes("/coins") ? Promise.resolve({ quote: "USD", coins: [coin("BTC", "73000")] }) : Promise.reject(new Error("x")));
    await act(async () => { await qc.refetchQueries({ queryKey: ["crypto-exchange", "performance"] }); });
    await wait();
    // the last good figures stay, and the failed refresh is named
    expect(section("Performance").textContent).toContain("These figures may be out of date");
    expect(section("Performance").textContent).toContain("history locked");
    expect(section("Holdings").textContent).toContain("CRO");
  });
});

describe("Crypto.com pause and enable", () => {
  it("offers a pause while trading is enabled", async () => {
    await mount({ enabled: true });
    expect(toggle().textContent).toBe("Pause trading");
    expect(toggle().disabled).toBe(false);
    await act(async () => toggle().click());
    await wait();
    const put = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(put[0]).toBe("/api/v1/crypto-exchange/enabled");
    expect(JSON.parse(put[1].body)).toEqual({ enabled: false });
  });

  it("offers enabling only when the allocation is synced", async () => {
    await mount({ enabled: false, synced: true });
    expect(toggle().textContent).toBe("Enable trading within allocation");
    expect(toggle().disabled).toBe(false);
    await act(async () => root.unmount());
    qc.clear();
    root = createRoot(host);
    await mount({ enabled: false, synced: false });
    expect(toggle().disabled).toBe(true);
  });
});

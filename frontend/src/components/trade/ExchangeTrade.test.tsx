/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ExchangeTrade } from "./ExchangeTrade";

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

const limits = {
  floor: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
  guards_apply_from: "1000", max_asset_fraction_below_threshold: "0.4", default_slippage_pct: "0.3",
};
const summary = { total: 0, resolved: 0, open: 0, hit_target: 0, stopped: 0, expired: 0, avg_gross_pct: null, avg_net_pct: null, beat_costs: 0 };
let scorecard: Record<string, unknown>;
let status: Record<string, unknown>;
const coin = (symbol: string, last: string, bid = last) => ({
  symbol, pair: `${symbol}_USD`, price: last, bid, ask: last, spread_pct: "0.02", change_24h_pct: "1.5",
  volume_24h: "1000000", min_quantity_step: "0.001",
});
const top = [coin("BTC", "73000"), coin("SOL", "92"), coin("CRO", "0.05", "0.0499")];
const preview = {
  pair: "SOL_USD", side: "buy", quantity: "0.054", limit_price: "92.3", bid: "91.9", ask: "92",
  spread_pct: "0.108", estimated_average_price: "92.01", estimated_impact_pct: "0.011", book_covers_order: true,
};
const order = { order: { order_id: "o1", instrument_name: "SOL_USD", side: "BUY", status: "FILLED", quantity: "0.054",
  cumulative_quantity: "0.054", cumulative_value: "4.97", avg_price: "92.01", cumulative_fee: "0.01", fee_instrument_name: "USD" } };

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status = {
    connected: true, environment: "production", enabled: true, limits,
    ledger: { quote: "USD", cash: "26", positions: { CRO: { units: "216", cost: "9.98" } }, uncertain_order: null, enabled: true, orders: [], floor: "2", synced: true },
  };
  scorecard = {
    assumptions: { round_trip_cost_pct: 1, basis: "measured" }, prices: { snapshots: 120, coins: 17, days: 2.5 },
    trade_calls: { ...summary, total: 3, resolved: 2, open: 1, hit_target: 1, stopped: 1, avg_net_pct: 1.5, beat_costs: 1 },
    pass_calls: { ...summary, total: 4, resolved: 4, avg_net_pct: -2, expired: 4 },
    by_cost_multiple: [
      { multiple: 1, min_target_pct: 1, resolved: 3, avg_net_pct: 1.2, win_rate: 0.5 },
      { multiple: 2, min_target_pct: 2, resolved: 1, avg_net_pct: null, win_rate: null },
    ],
    baseline_gross_pct: -0.4, edge_vs_baseline_pct: 3.1, enough_data: false,
    recent: [{ ts: 1, symbol: "SOL", verdict: "trade", target_pct: 10, stop_pct: 5, horizon_h: 24, outcome: "target", net_pct: 5 }],
  };
  mocks.setData.mockReset();
  mocks.fetch.mockReset().mockImplementation((path: string) => {
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/scorecard")) return Promise.resolve(scorecard);
    if (path.includes("/coins?keyword=CRO")) return Promise.resolve({ quote: "USD", coins: [coin("CRO", "0.05", "0.0499")] });
    if (path.includes("/coins")) return Promise.resolve({ quote: "USD", coins: top });
    if (path.includes("/candles")) return Promise.resolve({ pair: "SOL_USD", timeframe: "1h", candles: [
      { t: 200000, o: "2", h: "3", l: "1", c: "2.5", v: "1" },
      { t: 100000, o: "1", h: "2", l: "0.5", c: "2", v: "1" },
      { t: 100000, o: "1", h: "2", l: "0.5", c: "2", v: "1" },
    ] });
    if (path.endsWith("/preview")) return Promise.resolve(preview);
    if (path.endsWith("/order")) return Promise.resolve({ ...order, status });
    return Promise.resolve({});
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={qc}><ExchangeTrade /></QueryClientProvider>));
  await wait();
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  vi.restoreAllMocks();
});

const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent === label) as HTMLButtonElement;
async function click(label: string) {
  await act(async () => button(label).click());
  await wait();
}
async function type(field: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const amountField = () => host.querySelector('input[inputmode="decimal"]') as HTMLInputElement;
async function requestPreview(amount: string) {
  await type(amountField(), amount);
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
}
const pickCoin = async (symbol: string) => {
  const row = Array.from(host.querySelectorAll('section[aria-label="Markets"] button'))
    .find((b) => b.textContent?.startsWith(symbol)) as HTMLButtonElement;
  await act(async () => row.click());
  await wait();
};
const posts = () => mocks.fetch.mock.calls.filter(([, init]) => init);
const callsTo = (suffix: string) => mocks.fetch.mock.calls.filter(([path]) => String(path).includes(suffix));

it("loads read-only: lists the top coins, never posts or enables anything", () => {
  expect(posts()).toEqual([]);
  expect(host.textContent).toContain("BTC");
  expect(host.textContent).toContain("SOL");
});

it("shows held coins first, tagged held, valued at the live bid in the quote currency", () => {
  const rows = Array.from(host.querySelectorAll('section[aria-label="Markets"] button')).map((b) => b.textContent);
  expect(rows[0]).toContain("CRO");
  expect(rows[0]).toContain("held");
  expect(host.querySelector('section[aria-label="Holdings"]')!.textContent).toMatch(/10[.,]78 USD/);
  expect(host.textContent).not.toContain("€");
});

it("describes the limits the server reports", () => {
  expect(host.textContent).toContain("no single coin may exceed 40% of bot equity");
  expect(host.textContent).not.toContain("up to three positions and one sixth");
});

it("charts the selected coin from the Exchange candles, oldest first without duplicates", async () => {
  await pickCoin("SOL");
  const [path] = callsTo("/candles").at(-1)!;
  expect(path).toBe("/api/v1/crypto-exchange/candles?symbol=SOL&timeframe=1h&count=200");
  const data = mocks.setData.mock.calls.at(-1)![0] as { time: number }[];
  expect(data.map((point) => point.time)).toEqual([100, 200]);
  await click("4h");
  expect(callsTo("/candles").at(-1)![0]).toContain("timeframe=4h");
});

it("says so when there is no chart, and trading still works", async () => {
  mocks.fetch.mockImplementation((path: string) => path.includes("/candles")
    ? Promise.reject(new Error("Unknown pair"))
    : path.endsWith("/status") ? Promise.resolve(status) : path.endsWith("/scorecard") ? Promise.resolve(scorecard) : Promise.resolve({ quote: "USD", coins: top }));
  await pickCoin("CRO");
  expect(host.textContent).toContain("Unknown pair");
  expect(host.textContent).toContain("Trading this coin still works without a chart");
});

it("previews a buy first (nothing is placed), sending an exact decimal amount", async () => {
  await pickCoin("SOL");
  await requestPreview("5");
  const call = callsTo("/preview")[0];
  expect(JSON.parse(call[1].body)).toEqual({ side: "buy", symbol: "SOL", amount: "5" });
  expect(host.textContent).toContain("Buy up to 0.054 SOL on SOL_USD");
  expect(host.textContent).toContain("limit price 92.3 USD");
  expect(host.textContent).toContain("Estimated average price 92.01 USD");
  expect(host.textContent).toContain("price impact 0.011%");
  expect(host.textContent).toContain("Immediate-or-cancel");
  expect(callsTo("/order")).toHaveLength(0);
});

it("sends the max slippage only when it is entered", async () => {
  await pickCoin("SOL");
  const slippage = Array.from(host.querySelectorAll('input[inputmode="decimal"]'))[1] as HTMLInputElement;
  await type(slippage, "0.5");
  await requestPreview("5");
  expect(JSON.parse(callsTo("/preview")[0][1].body)).toEqual({ side: "buy", symbol: "SOL", amount: "5", max_slippage_pct: "0.5" });
});

it("places the order only after the confirmation dialog, with the previewed request", async () => {
  await pickCoin("SOL");
  await requestPreview("5");
  await click("Place order…");
  expect(host.querySelector('[role="dialog"]')!.textContent).toContain("Buy up to 0.054 SOL");
  expect(callsTo("/order")).toHaveLength(0);
  await click("Confirm and place order");
  const placed = callsTo("/order");
  expect(placed).toHaveLength(1);
  expect(JSON.parse(placed[0][1].body)).toEqual({ side: "buy", symbol: "SOL", amount: "5" });
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(host.textContent).toContain("Order result");
  expect(host.textContent).toContain("FILLED");
});

it("cancelling the confirmation places nothing", async () => {
  await pickCoin("SOL");
  await requestPreview("5");
  await click("Place order…");
  await click("Cancel");
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(callsTo("/order")).toHaveLength(0);
});

it("editing the amount discards the preview so a stale one can never be placed", async () => {
  await pickCoin("SOL");
  await requestPreview("5");
  expect(button("Place order…")).toBeDefined();
  await type(amountField(), "6");
  expect(button("Place order…")).toBeUndefined();
});

it("warns when the book cannot fill the whole order", async () => {
  mocks.fetch.mockImplementation((path: string) => path.endsWith("/preview")
    ? Promise.resolve({ ...preview, book_covers_order: false })
    : path.endsWith("/status") ? Promise.resolve(status) : path.endsWith("/scorecard") ? Promise.resolve(scorecard) : Promise.resolve({ quote: "USD", coins: top }));
  await pickCoin("SOL");
  await requestPreview("5");
  expect(host.textContent).toContain("too thin to fill the whole order");
});

it("rejects malformed amounts before asking for a preview", async () => {
  await pickCoin("SOL");
  await type(amountField(), "1e3");
  expect(button("Preview order").disabled).toBe(true);
});

it("sells a holding with its exact ledger units", async () => {
  await act(async () => (Array.from(host.querySelectorAll('section[aria-label="Holdings"] button')).find((b) => b.textContent === "Sell…") as HTMLButtonElement).click());
  await wait();
  await click("Max");
  expect(amountField().value).toBe("216");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  expect(JSON.parse(callsTo("/preview").at(-1)![1].body)).toEqual({ side: "sell", symbol: "CRO", amount: "216" });
});

it("cannot place an order while trading is paused", async () => {
  status = { ...status, enabled: false };
  await act(async () => { await qc.invalidateQueries({ queryKey: ["crypto-exchange-status"] }); });
  await wait();
  await pickCoin("SOL");
  await requestPreview("5");
  expect(button("Place order…").disabled).toBe(true);
  expect(host.textContent).toContain("Trading is paused");
});

it("does not allow a preview before the allocation has been synced", async () => {
  status = { ...status, enabled: false, ledger: { ...(status.ledger as object), synced: false, enabled: false } };
  await act(async () => { await qc.invalidateQueries({ queryKey: ["crypto-exchange-status"] }); });
  await wait();
  await pickCoin("SOL");
  await type(amountField(), "5");
  expect(button("Preview order").disabled).toBe(true);
  expect(host.textContent).toContain("has not been synced");
  expect(button("Enable trading within allocation").disabled).toBe(true);
});

it("an order of unknown outcome refreshes the status and offers reconcile, trading stays paused", async () => {
  await pickCoin("SOL");
  await requestPreview("5");
  const paused = { ...status, enabled: false, ledger: { ...(status.ledger as object), enabled: false, uncertain_order: { client_oid: "c1", side: "buy", symbol: "SOL" } } };
  mocks.fetch.mockImplementation((path: string) => path.endsWith("/order")
    ? Promise.reject(new Error("Order outcome needs reconciliation. Trading paused; do not resubmit."))
    : path.endsWith("/status") ? Promise.resolve(paused) : path.endsWith("/scorecard") ? Promise.resolve(scorecard)
      : path.endsWith("/reconcile") ? Promise.resolve({ ...status, enabled: false }) : Promise.resolve({ quote: "USD", coins: top }));
  await click("Place order…");
  await click("Confirm and place order");
  await wait();
  expect(host.textContent).toContain("Order outcome needs reconciliation");
  expect(host.textContent).toContain("An order needs reconciliation");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  await click("Reconcile from Exchange order history");
  expect(callsTo("/reconcile")).toHaveLength(1);
  expect(callsTo("/reconcile")[0][1].method).toBe("POST");
});

it("shows the paper scorecard: calls, outcomes after costs, baseline and which cost multiple worked", () => {
  const panel = host.querySelector('section[aria-label="Scorecard"]')!.textContent!;
  expect(panel).toContain("1% round-trip cost");
  expect(panel).toContain("real Exchange prices");
  expect(panel).toContain("Tracking 17 coins · 120 price snapshots over 2.5 days");
  expect(panel).toContain("6 of 30 resolved calls needed");
  expect(panel).toContain("Trade calls");
  expect(panel).toMatch(/Pass calls/);
  expect(panel).toContain("+1.50%");
  expect(panel).toContain("-2.00%");
  expect(panel).toContain("Baseline (average of all listed coins over the same windows): -0.40%");
  expect(panel).toContain("Edge vs baseline: +3.10%");
  expect(panel).toContain("2× cost (2.0%)");
  expect(panel).toContain("SOL · trade · target +10% / stop −5% in 24h · target (+5.00% net)");
});

it("a scorecard that is down never breaks trading", async () => {
  await act(async () => root.unmount());
  qc.clear();
  mocks.fetch.mockImplementation((path: string) => path.endsWith("/scorecard")
    ? Promise.reject(new Error("boom"))
    : path.endsWith("/status") ? Promise.resolve(status) : Promise.resolve({ quote: "USD", coins: top }));
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={qc}><ExchangeTrade /></QueryClientProvider>));
  await wait();
  expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain("Scorecard unavailable");
  await pickCoin("SOL");
  expect(host.textContent).toContain("Buy SOL with USD");
});

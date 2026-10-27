/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { AppTrade } from "./AppTrade";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), candles: vi.fn(), setData: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch, api: { getCandles: mocks.candles } }));
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
  floor_eur: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
  guards_apply_from_eur: "1000", max_asset_fraction_below_threshold: "0.4",
};
const summary = { total: 0, resolved: 0, open: 0, hit_target: 0, stopped: 0, expired: 0, avg_gross_pct: null, avg_net_pct: null, beat_costs: 0 };
let scorecard: Record<string, unknown>;
let status: Record<string, unknown>;
const coin = (rank: number, symbol: string, amount: string, tradable = true) => ({
  rank, symbol, name: symbol, is_tradable: tradable, price_native: { amount, currency: "EUR" },
  percent_change_24h_native: "1.5", percent_change_1w_native: "-2", percent_change_30d_native: "-5",
});
const top = [coin(1, "BTC", "73000"), coin(7, "SOL", "92"), coin(8, "TRX", "0.29", false)];
const quote = { id: "q1", countdown: 15,
  from_amount: { amount: "5", currency: "EUR" }, to_amount: { amount: "0.0545", currency: "SOL" } };

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status = {
    connected: true, enabled: true, limits,
    ledger: { cash_eur: "26", positions: { CRO: { units: "216", cost_eur: "9.98" } }, uncertain_order: null },
  };
  scorecard = {
    assumptions: { round_trip_cost_pct: 5, basis: "measured" }, prices: { snapshots: 120, coins: 17, days: 2.5 },
    trade_calls: { ...summary, total: 3, resolved: 2, open: 1, hit_target: 1, stopped: 1, avg_net_pct: 1.5, beat_costs: 1 },
    pass_calls: { ...summary, total: 4, resolved: 4, avg_net_pct: -2, expired: 4 },
    by_cost_multiple: [
      { multiple: 1, min_target_pct: 5, resolved: 3, avg_net_pct: 1.2, win_rate: 0.5 },
      { multiple: 2, min_target_pct: 10, resolved: 1, avg_net_pct: null, win_rate: null },
    ],
    baseline_gross_pct: -0.4, edge_vs_baseline_pct: 3.1, enough_data: false,
    recent: [{ ts: 1, symbol: "SOL", verdict: "trade", target_pct: 10, stop_pct: 5, horizon_h: 24, outcome: "target", net_pct: 5 }],
  };
  mocks.setData.mockReset();
  mocks.candles.mockReset().mockResolvedValue([
    { timestamp: 200, open: 2, high: 3, low: 1, close: 2.5, volume: 1 },
    { timestamp: 100, open: 1, high: 2, low: 0.5, close: 2, volume: 1 },
    { timestamp: 100, open: 1, high: 2, low: 0.5, close: 2, volume: 1 },
  ]);
  mocks.fetch.mockReset().mockImplementation((path: string) => {
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/scorecard")) return Promise.resolve(scorecard);
    if (path.endsWith("/coins?keyword=")) return Promise.resolve({ coins: top });
    if (path.endsWith("/coins?keyword=CRO")) return Promise.resolve({ coins: [coin(40, "CRO", "0.05")] });
    if (path.endsWith("/quote")) return Promise.resolve(quote);
    return Promise.resolve({ transaction: { id: "t1", status: "completed" } });
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <ServerContext value={{ server: "test", setServer: () => {} }}><AppTrade /></ServerContext>
  </QueryClientProvider>));
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
async function requestQuote(amount: string) {
  await type(host.querySelector('input[inputmode="decimal"]') as HTMLInputElement, amount);
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

it("loads read-only: lists the top coins, never posts or enables anything", () => {
  expect(posts()).toEqual([]);
  expect(host.textContent).toContain("BTC");
  expect(host.textContent).toContain("SOL");
  expect(host.textContent).toContain("not tradable");
});

it("shows held coins first, tagged held, with listing prices for holdings outside the top list", async () => {
  const rows = () => Array.from(host.querySelectorAll('section[aria-label="Markets"] button')).map((b) => b.textContent);
  expect(rows()[0]).toContain("CRO");
  expect(rows()[0]).toContain("held");
  for (let attempt = 0; attempt < 20 && !/€10[.,]80/.test(host.textContent ?? ""); attempt += 1) await wait();
  expect(host.querySelector('section[aria-label="Holdings"]')!.textContent).toMatch(/€10[.,]80/);
  expect(rows()[0]).toMatch(/€0[.,]05/);
});

it("describes the limits the server reports", () => {
  expect(host.textContent).toContain("no single coin may exceed 40% of bot equity");
  expect(host.textContent).not.toContain("up to three positions and one sixth");
});

it("charts the selected coin from public candles, oldest first without duplicates", async () => {
  await pickCoin("SOL");
  const [, connector, pair, interval, limit, start, end] = mocks.candles.mock.calls.at(-1)!;
  expect([connector, pair, interval, limit]).toEqual(["binance", "SOL-USDT", "1h", 200]);
  expect(end - start).toBe(200 * 3600);
  const data = mocks.setData.mock.calls.at(-1)![0] as { time: number }[];
  expect(data.map((point) => point.time)).toEqual([100, 200]);
});

it("falls back to OKX when Binance has no candles, and says so when neither does", async () => {
  mocks.candles.mockImplementation((_server: string, connector: string) =>
    connector === "okx" ? Promise.resolve([{ timestamp: 1, open: 1, high: 1, low: 1, close: 1, volume: 1 }]) : Promise.reject(new Error("no")));
  await pickCoin("CRO");
  expect(mocks.candles.mock.calls.map((call) => call[1])).toContain("okx");
  mocks.candles.mockRejectedValue(new Error("no"));
  await pickCoin("SOL");
  expect(host.textContent).toContain("No chart data for SOL");
  expect(host.textContent).toContain("Trading this coin still works without a chart");
});

it("requests an exact decimal source amount for a buy and shows the cost against the listed price", async () => {
  await pickCoin("SOL");
  await requestQuote("5");
  const call = mocks.fetch.mock.calls.find(([path]) => path.endsWith("/quote"))!;
  expect(JSON.parse(call[1].body)).toEqual({ kind: "purchase", source: "EUR", target: "SOL", amount: "5" });
  expect(host.textContent).toContain("Spend 5 EUR → receive 0.0545 SOL");
  expect(host.textContent).toContain("saves 0.28% vs listed");
});

it("rejects malformed amounts before asking for a quote", async () => {
  await pickCoin("SOL");
  await type(host.querySelector('input[inputmode="decimal"]') as HTMLInputElement, "1e3");
  expect(button("Get quotation").disabled).toBe(true);
});

it("expired quotations are never submitted", async () => {
  await pickCoin("SOL");
  const start = Date.now();
  await requestQuote("5");
  vi.spyOn(Date, "now").mockReturnValue(start + 60000);
  await act(async () => button("Confirm & submit order").click());
  expect(mocks.fetch.mock.calls.some(([path]) => path.endsWith("/execute"))).toBe(false);
});

it("submits only the quoted id once the order is confirmed", async () => {
  await pickCoin("SOL");
  await requestQuote("5");
  await act(async () => button("Confirm & submit order").click());
  await wait();
  const execute = mocks.fetch.mock.calls.filter(([path]) => path.endsWith("/execute"));
  expect(execute).toHaveLength(1);
  expect(JSON.parse(execute[0][1].body)).toEqual({ quotation_id: "q1" });
  expect(host.textContent).not.toContain("Confirm & submit order");
});

it("sells a holding with its exact ledger units", async () => {
  await act(async () => (Array.from(host.querySelectorAll('section[aria-label="Holdings"] button')).find((b) => b.textContent === "Sell…") as HTMLButtonElement).click());
  await wait();
  await click("Max");
  expect((host.querySelector('input[inputmode="decimal"]') as HTMLInputElement).value).toBe("216");
  mocks.fetch.mockImplementation((path: string) => Promise.resolve(path.endsWith("/status") ? status : path.endsWith("/quote")
    ? { id: "q2", countdown: 15, from_amount: { amount: "216", currency: "CRO" }, to_amount: { amount: "9.99", currency: "EUR" } } : { coins: top }));
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  const call = mocks.fetch.mock.calls.filter(([path]) => path.endsWith("/quote")).at(-1)!;
  expect(JSON.parse(call[1].body)).toEqual({ kind: "sale", source: "CRO", target: "EUR", amount: "216" });
  expect(host.textContent).toContain("costs 7.5");
});

it("cannot submit while trading is paused", async () => {
  status = { ...status, enabled: false };
  await act(async () => { await qc.invalidateQueries({ queryKey: ["crypto-com-status"] }); });
  await wait();
  await pickCoin("SOL");
  await requestQuote("5");
  expect(button("Confirm & submit order").disabled).toBe(true);
  expect(host.textContent).toContain("Trading is paused");
});

it("refuses to quote a coin the App cannot trade", async () => {
  await pickCoin("TRX");
  await type(host.querySelector('input[inputmode="decimal"]') as HTMLInputElement, "5");
  expect(button("Get quotation").disabled).toBe(true);
  expect(host.textContent).toContain("TRX is not tradable in the App");
});

it("shows the paper scorecard: calls, outcomes after costs, baseline and which cost multiple worked", () => {
  const panel = host.querySelector('section[aria-label="Scorecard"]')!.textContent!;
  expect(panel).toContain("5% round-trip cost");
  expect(panel).toContain("Tracking 17 coins · 120 price snapshots over 2.5 days");
  expect(panel).toContain("6 of 30 resolved calls needed");
  expect(panel).toContain("Trade calls");
  expect(panel).toMatch(/Pass calls/);
  expect(panel).toContain("+1.50%");
  expect(panel).toContain("−2.00%".replace("−", "-"));
  expect(panel).toContain("Baseline (average of all listed coins over the same windows): -0.40%");
  expect(panel).toContain("Edge vs baseline: +3.10%");
  expect(panel).toContain("2× cost (10.0%)");
  expect(panel).toContain("SOL · trade · target +10% / stop −5% in 24h · target (+5.00% net)");
});

it("a scorecard that is down never breaks trading, and a refresh failure keeps the last good numbers", async () => {
  mocks.fetch.mockImplementation((path: string) => path.endsWith("/scorecard")
    ? Promise.reject(new Error("boom"))
    : path.endsWith("/status") ? Promise.resolve(status) : Promise.resolve({ coins: top }));
  await act(async () => { await qc.invalidateQueries({ queryKey: ["crypto-com-scorecard"] }); });
  await wait();
  expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain("Tracking 17 coins");
  await act(async () => root.unmount());
  qc.clear();
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <ServerContext value={{ server: "test", setServer: () => {} }}><AppTrade /></ServerContext>
  </QueryClientProvider>));
  await wait();
  expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain("Scorecard unavailable");
  await pickCoin("SOL");
  expect(host.textContent).toContain("Buy SOL with EUR");
});

/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeVenueTrade } from "./NativeVenueTrade";
import { NATIVE_VENUES, PAPER_BANNER, isNativeVenueKey } from "./nativeVenue";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), setData: vi.fn(), admin: { value: true } }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.admin.value }));
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  createChart: () => ({
    addSeries: () => ({ setData: mocks.setData }),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
}));

const venue = NATIVE_VENUES["xstocks-paper"];
const summary = { total: 0, resolved: 0, open: 0, hit_target: 0, stopped: 0, expired: 0, avg_gross_pct: null, avg_net_pct: null, beat_costs: 0 };
const scorecard = {
  assumptions: { round_trip_cost_pct: 0.85, basis: "default" }, prices: { snapshots: 40, coins: 12, days: 1.5 },
  trade_calls: { ...summary, total: 2, resolved: 1, open: 1, hit_target: 1, avg_net_pct: 0.9, beat_costs: 1 },
  pass_calls: { ...summary }, by_cost_multiple: [], baseline_gross_pct: 0.1, edge_vs_baseline_pct: 0.8, enough_data: false, recent: [],
};
const candles = { pair: "AAPLxUSD", timeframe: "1h", candles: [{ t: 100000, o: "1", h: "2", l: "0.5", c: "2", v: "1" }] };
const rows = [
  { symbol: "AAPL", pair: "AAPLxUSD", price: "270", bid: "269.9", ask: "270.1", spread_pct: "0.074", volume_24h_usd: 9_000_000, change_since_open_pct: "0.8" },
  { symbol: "TSLA", pair: "TSLAxUSD", price: "300", bid: "300", ask: "300.4", spread_pct: "0.133", volume_24h_usd: 5_000_000, change_since_open_pct: "-1.2" },
];
const assumptions = { taker_fee_pct: "0.08", slippage_allowance_pct: "0.02", start_cash: "100" };
const openedStatus = {
  connected: true, paper: true, simulated: true, quote: "USD", enabled: true, market: { open: true, new_york_time: "Wed 11:00", minutes_to_close: 300 },
  assumptions,
  ledger: {
    quote: "USD", cash: "60", positions: { AAPL: { units: "0.1", cost: "27" } }, uncertain_order: null, enabled: true, floor: "2", synced: true,
    orders: [{ time: 1_793_219_000, side: "buy", symbol: "AAPL", order_id: "paper-1", status: "closed", quantity: "0.1", value: "26.99", fee: "0.02", fee_currency: "USD" }],
  },
};
const unopenedStatus = { ...openedStatus, connected: false, enabled: false, ledger: { ...openedStatus.ledger, cash: "100", positions: {}, orders: [] } };

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
let status: Record<string, unknown>;
let resetBehaviour: () => Promise<unknown>;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
const calls = () => mocks.fetch.mock.calls as [string, { method?: string } | undefined][];
const paths = () => calls().map(([path]) => path);
const button = (label: string | RegExp) => Array.from(host.querySelectorAll("button")).find((b) =>
  typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? ""));

function build(next: Record<string, unknown>) {
  status = next;
  resetBehaviour = () => Promise.resolve({ reset: { equity_after: 100 } });
  mocks.fetch.mockReset().mockImplementation((path: string, init?: { method?: string }) => {
    if (path.endsWith("/status")) return Promise.resolve(status);
    if (path.endsWith("/scorecard")) return Promise.resolve(scorecard);
    if (path.includes("/coins")) return Promise.resolve({ quote: "USD", stocks: rows });
    if (path.includes("/candles")) return Promise.resolve(candles);
    if (path.endsWith("/accounts")) return Promise.resolve({ accounts: [{ currency: "USD", balance: "60", available: "60" }, { currency: "AAPLx", balance: "0.1", available: "0.1" }] });
    if (path.endsWith("/history")) return Promise.resolve([{ order_id: "paper-1", pair: "AAPLxUSD", side: "buy", status: "closed", vol_exec: "0.1", avg_price: "269.9", cost: "26.99", fee: "0.02", fee_currency: "USD", closetm: 1_793_219_000, simulated: true }]);
    if (path.endsWith("/valuation")) return Promise.resolve({ equity: "86.99", quote: "USD", minimum_cash_reserve: "0" });
    if (path.endsWith("/reset") && init?.method === "POST") return resetBehaviour();
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}
async function mount() {
  await act(async () => root.render(<QueryClientProvider client={qc}><NativeVenueTrade venue={venue} /></QueryClientProvider>));
  await wait();
  await wait();
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  mocks.setData.mockReset();
  mocks.admin.value = true;
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

describe("Kraken xStocks (paper) panel", () => {
  it("is a native workspace on the paper routes", () => {
    expect(isNativeVenueKey("xstocks-paper")).toBe(true);
    expect(venue).toMatchObject({ title: "Kraken xStocks (paper)", base: "/api/v1/xstocks-paper", paper: true, quote: "USD" });
    expect(NATIVE_VENUES["kraken-xstocks"].paper).toBeUndefined();
  });

  it("always shows the SIMULATED banner with the assumed fee and slippage", async () => {
    build(openedStatus);
    await mount();
    const banner = host.querySelector("[data-paper-banner]")!;
    expect(banner.textContent).toContain(PAPER_BANNER);
    expect(PAPER_BANNER).toBe("SIMULATED - no real money");
    expect(banner.textContent).toContain("0.08%");
    expect(banner.textContent).toContain("0.02%");
    expect(banner.textContent).toContain("not part of any real total");
    expect(host.querySelector("h1")!.textContent).toBe("Kraken xStocks (paper)");
  });

  it("shows the simulated cash, positions, equity, orders, markets and scorecard", async () => {
    build(openedStatus);
    await mount();
    const text = host.textContent!;
    expect(text).toContain("Account opened");
    expect(text).toMatch(/Cash 60[.,]00 USD/);
    // 60 cash + 0.1 AAPL at the 269.9 best bid
    expect(text).toMatch(/Equity 86[.,]99 USD/);
    expect(host.querySelector('section[aria-label="Markets"]')!.textContent).toContain("AAPL");
    const holdings = host.querySelector('section[aria-label="Holdings"]')!.textContent!;
    expect(holdings).toContain("AAPL");
    expect(holdings).toMatch(/26[.,]99 USD/);
    const recent = host.querySelector('section[aria-label="Recent orders"]')!.textContent!;
    expect(recent).toContain("AAPLxUSD");
    expect(recent).toContain("Bot ledger");
    const balances = host.querySelector('section[aria-label="Account balances"]')!.textContent!;
    expect(balances).toContain("AAPLx");
    expect(balances).toMatch(/26[.,]99 USD/);
    expect(host.querySelector('section[aria-label="Scorecard"]')!.textContent).toContain("real Kraken xStocks (paper) prices");
    expect(text).toContain("US market open");
  });

  it("has no order ticket, no Settings link and reads only from the paper routes", async () => {
    build(openedStatus);
    await mount();
    const labels = Array.from(host.querySelectorAll("button")).map((b) => b.textContent ?? "");
    expect(labels.filter((label) => /\b(buy|sell|place|preview|enable|pause|reconcile|sync)\b/i.test(label))).toEqual([]);
    expect(host.querySelector("form")).toBeNull();
    expect(host.querySelector('a[href^="/settings"]')).toBeNull();
    expect(calls().every(([path]) => path.startsWith(venue.base))).toBe(true);
    expect(calls().every(([, init]) => !init)).toBe(true);
    await act(async () => button("Value allocation")!.click());
    await wait();
    expect(paths()).toContain(`${venue.base}/valuation`);
  });

  it("says the account is not opened yet, can still be valued, and never sends the user to Settings", async () => {
    build(unopenedStatus);
    await mount();
    expect(host.querySelector("[data-state='not-opened']")!.textContent).toContain("has not been opened yet");
    expect(host.textContent).toContain("Not opened yet");
    expect(host.querySelector('a[href^="/settings"]')).toBeNull();
    expect(paths().some((path) => path.endsWith("/accounts") || path.endsWith("/history"))).toBe(false);
    expect((button("Value allocation") as HTMLButtonElement).disabled).toBe(false);
    // the first valuation opens it, so the status is read again
    status = openedStatus;
    await act(async () => button("Value allocation")!.click());
    await wait();
    await wait();
    expect(host.textContent).toContain("Account opened");
  });

  it("warns when the paper account is paused at its equity floor", async () => {
    build({ ...openedStatus, enabled: false });
    await mount();
    expect(host.querySelector("[role=alert]")!.textContent).toContain("paused");
    expect(host.textContent).toContain("Trading: paused");
  });

  describe("reset", () => {
    it("is offered to the owner and needs a second, explicit confirmation before it calls the route", async () => {
      build(openedStatus);
      await mount();
      expect(button("Confirm reset")).toBeUndefined();
      await act(async () => button("Reset paper account")!.click());
      expect(host.textContent).toContain("Archive the simulated ledger");
      expect(calls().some(([path]) => path.endsWith("/reset"))).toBe(false);
      await act(async () => button("Cancel")!.click());
      expect(button("Reset paper account")).toBeDefined();
      expect(calls().some(([path]) => path.endsWith("/reset"))).toBe(false);
    });

    it("posts to the reset route once confirmed, shows it is pending, then refreshes the account", async () => {
      build(openedStatus);
      await mount();
      let finish: (value: unknown) => void = () => {};
      resetBehaviour = () => new Promise((resolve) => { finish = resolve; });
      await act(async () => button("Reset paper account")!.click());
      await act(async () => button("Confirm reset")!.click());
      expect(calls().filter(([path, init]) => path === `${venue.base}/reset` && init?.method === "POST")).toHaveLength(1);
      await wait();
      expect(host.textContent).toContain("Resetting the paper account");
      expect((button(/Resetting/) as HTMLButtonElement).disabled).toBe(true);
      const statusReads = paths().filter((path) => path.endsWith("/status")).length;
      status = unopenedStatus;
      await act(async () => finish({ reset: {} }));
      await wait();
      expect(host.textContent).toContain("The paper account was reset to 100 USD.");
      expect(paths().filter((path) => path.endsWith("/status")).length).toBeGreaterThan(statusReads);
      expect(host.querySelector("[role=alert]")).toBeNull();
    });

    it("shows the error when the reset fails and does not claim it worked", async () => {
      build(openedStatus);
      await mount();
      resetBehaviour = () => Promise.reject(new Error("Admin access required"));
      await act(async () => button("Reset paper account")!.click());
      await act(async () => button("Confirm reset")!.click());
      await wait();
      expect(host.querySelector("[role=alert]")!.textContent).toContain("Reset failed: Admin access required");
      expect(host.textContent).not.toContain("was reset");
    });

    it("is hidden from anyone who is not an admin", async () => {
      mocks.admin.value = false;
      build(openedStatus);
      await mount();
      expect(host.querySelector("[data-paper-reset]")).toBeNull();
      expect(button(/reset/i)).toBeUndefined();
    });

    it("offers to open an account that does not exist yet", async () => {
      build(unopenedStatus);
      await mount();
      await act(async () => button("Open paper account")!.click());
      expect(host.textContent).toContain("Open the simulated account with 100 USD?");
      await act(async () => button("Confirm open")!.click());
      await wait();
      expect(calls().some(([path, init]) => path.endsWith("/reset") && init?.method === "POST")).toBe(true);
    });
  });
});

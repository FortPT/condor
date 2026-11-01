/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { SolanaTrade } from "./SolanaTrade";
import { allowedTokens, limitRows, parseDecisions, signedUsd, walletRows } from "./solana";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("./TradeParts", () => ({ CandleChart: ({ rows }: { rows: unknown[] }) => <div data-testid="chart" data-rows={rows.length} /> }));

const portfolio = {
  connectors: [
    { connector: "kraken", balances: [{ token: "BTC", total: 1, usd_value: 99999 }] },
    {
      connector: "solana-mainnet-beta",
      balances: [
        { token: "USDC", total: 5, usd_value: 5 },
        { token: "SOL", total: 0.3294, usd_value: 31.4 },
        { token: "DUST", total: 0, usd_value: 0 },
      ],
    },
  ],
};
const strategy = {
  status: "running",
  config: {
    frequency_sec: 900,
    risk_limits: {
      max_position_size_quote: 15, max_swap_quote: 12, max_swap_slippage_pct: 1.0,
      swap_default_wallet_only: true, min_swap_sol_reserve: 0.02, allowed_swap_tokens: "SOL,USDC,JUP,WIF",
    },
  },
  instances: [{ session_num: 2, tick_count: 3 }],
};
const swap = {
  status: "CONFIRMED", side: "sell", trading_pair: "SOL-USDC", input_amount: 0.05, output_amount: 4.7,
  price: 94, transaction_hash: "5abcdefghijkl", timestamp: "2026-11-01T00:10:00Z",
};
const overview = {
  address: "85maohASGbekdraAsBkX9XLXppwmFDpF9nnboKPCSqbn", value_usd: 31.4, deposits_usd: 31.0, since_start_usd: 0.4, today_usd: -0.1,
  deposits: [{ signature: "depositsig1234", ts: 1_793_489_183, usd: 29.75 }, { signature: "withdrawsig5678", ts: 1_793_485_199, usd: -1.2 }],
};
const quotes = {
  quotes: [
    { token: "SOL", price: 95.6, price_impact_pct: 0, error: null },
    { token: "JUP", price: 0.258938, price_impact_pct: 0.07, error: null },
    { token: "WIF", price: null, price_impact_pct: null, error: "not quotable" },
  ],
};
const journal = {
  content: "# Journal\n\n## Summary\nx\n\n## Decisions\n- **#1** (00:40) Tick 1: HOLD, no swap. No cash.\n- **#2** (00:56) Tick 2: HOLD, no swap. Still no cash.\n\n## Ticks\n- tick#1 | other\n",
};
const candles = [{ timestamp: 1, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 }, { timestamp: 2, open: 1.5, high: 2, low: 1, close: 1.8, volume: 11 }];

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
type Overrides = Partial<Record<"strategy" | "swaps" | "portfolio" | "overview" | "quotes" | "journal" | "candles", unknown>>;

function respond(over: Overrides = {}) {
  const pick = (key: keyof Overrides, fallback: unknown) => (over[key] instanceof Error ? Promise.reject(over[key]) : Promise.resolve(key in over ? over[key] : fallback));
  mocks.fetch.mockReset().mockImplementation((path: string) => {
    if (path.includes("/journal")) return pick("journal", journal);
    if (path.includes("/strategies/")) return pick("strategy", strategy);
    if (path.includes("/dex/swaps")) return pick("swaps", { swaps: [swap] });
    if (path.includes("/dex/quotes")) return pick("quotes", quotes);
    if (path.includes("/solana/overview")) return pick("overview", overview);
    if (path.includes("/market/candles")) return pick("candles", candles);
    if (path.endsWith("/portfolio")) return pick("portfolio", portfolio);
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}
const paths = () => mocks.fetch.mock.calls.map(([path]) => String(path));
const text = () => host.textContent ?? "";

async function render(server: string | null = "FortPT VM-08") {
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <ServerContext value={{ server, setServer: () => {} }}><SolanaTrade /></ServerContext>
  </QueryClientProvider>));
  await wait();
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
});

describe("helpers", () => {
  it("walletRows reads only the Solana connector, drops empty balances and orders by value", () => {
    const { rows, total } = walletRows(portfolio);
    expect(rows.map((r) => r.token)).toEqual(["SOL", "USDC"]);
    expect(total).toBeCloseTo(36.4);
    expect(rows[0].share).toBeCloseTo(86.3, 1);
    expect(walletRows(undefined)).toEqual({ rows: [], total: 0 });
  });
  it("limitRows states the saved limits and dashes what is not set", () => {
    const rows = Object.fromEntries(limitRows(strategy.config));
    expect(rows["Allowed tokens"]).toBe("SOL, USDC, JUP, WIF");
    expect(rows["Slippage limit"]).toBe("1%");
    expect(rows["SOL always kept for fees"]).toBe("0.02 SOL");
    expect(Object.fromEntries(limitRows({}))["Largest swap"]).toBe("—");
  });
  it("allowedTokens is upper case, once each, without cash", () => {
    expect(allowedTokens(strategy.config)).toEqual(["SOL", "JUP", "WIF"]);
    expect(allowedTokens({ risk_limits: { allowed_swap_tokens: ["sol", "SOL", " jup ", "usdc"] } })).toEqual(["SOL", "JUP"]);
    expect(allowedTokens({})).toEqual([]);
  });
  it("parseDecisions reads the Decisions section only, newest first", () => {
    const rows = parseDecisions(journal.content);
    expect(rows.map((r) => r.tick)).toEqual(["2", "1"]);
    expect(rows[0]).toEqual({ tick: "2", time: "00:56", text: "Tick 2: HOLD, no swap. Still no cash." });
    expect(parseDecisions("## Ticks\n- **#1** (00:40) not a decision")).toEqual([]);
    expect(parseDecisions(undefined)).toEqual([]);
    expect(parseDecisions(journal.content, 1)).toHaveLength(1);
  });
  it("signedUsd shows the sign and a dash for nothing", () => {
    expect(signedUsd(1.234)).toBe("+$1.23");
    expect(signedUsd(-1.234)).toBe("−$1.23");
    expect(signedUsd(0.001)).toBe("$0.00");
    expect(signedUsd(undefined)).toBe("—");
    expect(signedUsd(Number.NaN)).toBe("—");
  });
});

describe("SolanaTrade", () => {
  it("shows the summary, chart, prices, wallet, decisions, limits, swaps and deposits, and only reads", async () => {
    respond();
    await render();
    expect(host.querySelector("h1")?.textContent).toBe("Solana");
    expect(text()).toContain("Loop: running");
    expect(text()).toContain("Session 2 · 3 ticks · every 15 min");
    const metric = (label: string) => host.querySelector(`[data-metric="${label}"]`)?.textContent ?? "";
    expect(metric("Wallet value")).toMatch(/31[.,]40 USD/);
    expect(metric("Wallet value")).toContain("85ma…Sqbn");
    expect(metric("Deposited")).toMatch(/31[.,]00 USD/);
    expect(metric("Result since start")).toContain("+$0.40");
    expect(metric("Result since start")).toContain("+1.29% of deposits");
    expect(metric("Today")).toContain("−$0.10");
    expect(host.querySelector("[data-testid=chart]")?.getAttribute("data-rows")).toBe("2");
    expect(host.querySelector('[data-quote="JUP"]')?.textContent).toMatch(/\$0[.,]2589/);
    expect(host.querySelector('[data-quote="WIF"]')?.textContent).toContain("not quotable");
    expect(host.querySelector('[data-quote="USDC"]')).toBeNull();
    expect(host.querySelector('[data-token="SOL"]')?.textContent).toMatch(/\$95[.,]6/);
    expect(host.querySelector('[data-token="DUST"]')).toBeNull();
    expect(host.querySelector('[data-decision="2"]')?.textContent).toContain("Still no cash");
    expect(text()).toContain("session 2");
    expect(text()).toContain("SOL, USDC, JUP, WIF");
    expect(text()).toContain("SELL");
    expect(text()).toContain("Confirmed");
    expect(host.querySelector("a[href='https://solscan.io/tx/5abcdefghijkl']")).not.toBeNull();
    expect(host.querySelector('[data-deposit="depositsig1234"]')?.textContent).toContain("+$29.75");
    expect(host.querySelector('[data-deposit="withdrawsig5678"]')?.textContent).toContain("−$1.20");
    expect(mocks.fetch.mock.calls.every(([, init]) => !init)).toBe(true);
    expect(paths()).toEqual(expect.arrayContaining([
      "/api/v1/agents/solana_dex_trader/strategies/swing",
      "/api/v1/agents/solana_dex_trader/strategies/swing/sessions/2/journal",
      "/api/v1/servers/FortPT%20VM-08/portfolio",
      "/api/v1/servers/FortPT%20VM-08/solana/overview",
      "/api/v1/servers/FortPT%20VM-08/dex/swaps?limit=25",
      "/api/v1/servers/FortPT%20VM-08/dex/quotes?tokens=SOL%2CJUP%2CWIF",
    ]));
  });

  it("asks for the SOL chart by its mint and re-reads it when the interval changes", async () => {
    respond();
    await render();
    const chartCalls = () => paths().filter((p) => p.includes("/market/candles"));
    expect(chartCalls()).toHaveLength(1);
    expect(chartCalls()[0]).toContain("connector=solana-mainnet-beta");
    expect(chartCalls()[0]).toContain("trading_pair=So11111111111111111111111111111111111111112-USDC");
    expect(chartCalls()[0]).toContain("interval=1h");
    const four = Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "4h")!;
    await act(async () => four.click());
    await wait();
    expect(chartCalls()).toHaveLength(2);
    expect(chartCalls()[1]).toContain("interval=4h");
    expect(four.getAttribute("aria-pressed")).toBe("true");
  });

  it("says plainly when there is nothing yet: no swaps, deposits, decisions or chart", async () => {
    respond({ swaps: { swaps: [] }, overview: { address: "x", value_usd: 0, deposits: [] }, journal: { content: "## Decisions\n" }, candles: [] });
    await render();
    expect(host.querySelector('[data-state="no-swaps"]')?.textContent).toBe("No swaps yet.");
    expect(host.querySelector('[data-state="no-deposits"]')).not.toBeNull();
    expect(host.querySelector('[data-state="no-decisions"]')).not.toBeNull();
    expect(host.querySelector('[data-state="no-chart"]')).not.toBeNull();
  });

  it("labels a pending swap and a failed one", async () => {
    respond({ swaps: { swaps: [{ ...swap, status: "SUBMITTED", transaction_hash: "a" }, { ...swap, status: "FAILED", transaction_hash: "b" }] } });
    await render();
    expect(text()).toContain("Pending");
    expect(text()).toContain("Failed");
  });

  it("warns when the loop has no server pinned", async () => {
    respond({ strategy: { ...strategy, unavailable: "no_server" } });
    await render();
    expect(text()).toContain("no server pinned");
  });

  it("does not read a journal when the loop has no session", async () => {
    respond({ strategy: { status: "stopped", config: strategy.config, instances: [] } });
    await render();
    expect(text()).toContain("The loop is not running, so there is no current session.");
    expect(paths().some((p) => p.includes("/journal"))).toBe(false);
  });

  it("shows an error when a read fails, and still shows what loaded", async () => {
    respond({ swaps: new Error("Gateway is down") });
    await render();
    expect(host.querySelector("[role=alert]")?.textContent).toContain("Gateway is down");
    expect(host.querySelector('[data-token="SOL"]')).not.toBeNull();
  });

  it("asks for a server instead of reading anything server-scoped when none is selected", async () => {
    respond();
    await render(null);
    expect(text()).toContain("Select a server");
    expect(paths().some((p) => p.includes("/servers/"))).toBe(false);
  });
});

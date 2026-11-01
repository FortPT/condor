/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { SolanaTrade, limitRows, walletRows } from "./SolanaTrade";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.fetch }));

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
      swap_default_wallet_only: true, min_swap_sol_reserve: 0.02, allowed_swap_tokens: "SOL,USDC,JUP",
    },
  },
  instances: [{ session_num: 2, tick_count: 3 }],
};
const swap = {
  status: "CONFIRMED", side: "sell", trading_pair: "SOL-USDC", input_amount: 0.05, output_amount: 4.7,
  price: 94, transaction_hash: "5abcdefghijkl", timestamp: "2026-11-01T00:10:00Z",
};

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });

function respond(overrides: { strategy?: unknown; swaps?: unknown; portfolio?: unknown } = {}) {
  mocks.fetch.mockReset().mockImplementation((path: string) => {
    if (path.includes("/strategies/")) return overrides.strategy instanceof Error ? Promise.reject(overrides.strategy) : Promise.resolve(overrides.strategy ?? strategy);
    if (path.includes("/dex/swaps")) return overrides.swaps instanceof Error ? Promise.reject(overrides.swaps) : Promise.resolve(overrides.swaps ?? { swaps: [swap] });
    if (path.endsWith("/portfolio")) return Promise.resolve(overrides.portfolio ?? portfolio);
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

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

describe("walletRows", () => {
  it("reads only the Solana connector, drops empty balances and orders by value", () => {
    const { rows, total } = walletRows(portfolio);
    expect(rows.map((r) => r.token)).toEqual(["SOL", "USDC"]);
    expect(total).toBeCloseTo(36.4);
    expect(rows[0].share).toBeCloseTo(86.3, 1);
  });
  it("is empty without a portfolio or without the connector", () => {
    expect(walletRows(undefined)).toEqual({ rows: [], total: 0 });
    expect(walletRows({ connectors: [{ connector: "kraken", balances: [] }] }).rows).toEqual([]);
  });
});

describe("limitRows", () => {
  it("states the saved limits in words", () => {
    const rows = Object.fromEntries(limitRows(strategy.config));
    expect(rows["Allowed tokens"]).toBe("SOL, USDC, JUP");
    expect(rows["Largest swap"]).toContain("USDC");
    expect(rows["Slippage limit"]).toBe("1%");
    expect(rows["SOL always kept for fees"]).toBe("0.02 SOL");
    expect(rows["Signs with"]).toBe("the default wallet only");
  });
  it("shows a dash for a limit that is not set rather than inventing one", () => {
    const rows = Object.fromEntries(limitRows({}));
    expect(rows["Allowed tokens"]).toBe("—");
    expect(rows["Largest swap"]).toBe("—");
    expect(rows["Slippage limit"]).toBe("—");
    expect(rows["Signs with"]).toBe("any wallet");
  });
  it("reads the token list from an array too", () => {
    expect(Object.fromEntries(limitRows({ risk_limits: { allowed_swap_tokens: ["SOL", "JUP"] } }))["Allowed tokens"]).toBe("SOL, JUP");
  });
});

describe("SolanaTrade", () => {
  it("shows the loop, the wallet, the limits and the swaps, and only reads", async () => {
    respond();
    await render();
    expect(host.querySelector("h1")?.textContent).toBe("Solana");
    expect(host.textContent).toContain("Loop: running");
    expect(host.textContent).toContain("Session 2 · 3 ticks · every 15 min");
    expect(host.querySelector('[data-token="SOL"]')).not.toBeNull();
    expect(host.querySelector('[data-token="DUST"]')).toBeNull();
    expect(host.textContent).toContain("SOL, USDC, JUP");
    expect(host.textContent).toContain("SOL-USDC");
    expect(host.textContent).toContain("SELL");
    expect(host.textContent).toContain("Confirmed");
    expect(host.querySelector("a[href='https://solscan.io/tx/5abcdefghijkl']")).not.toBeNull();
    expect(host.querySelectorAll("button")).toHaveLength(0);
    expect(mocks.fetch.mock.calls.every(([, init]) => !init)).toBe(true);
    expect(mocks.fetch.mock.calls.map(([path]) => path)).toEqual(expect.arrayContaining([
      "/api/v1/agents/solana_dex_trader/strategies/swing",
      "/api/v1/servers/FortPT%20VM-08/portfolio",
      "/api/v1/servers/FortPT%20VM-08/dex/swaps?limit=25",
    ]));
  });

  it("says plainly when there are no swaps yet", async () => {
    respond({ swaps: { swaps: [] } });
    await render();
    expect(host.querySelector('[data-state="no-swaps"]')?.textContent).toBe("No swaps yet.");
  });

  it("labels a pending swap and a failed one", async () => {
    respond({ swaps: { swaps: [{ ...swap, status: "SUBMITTED", transaction_hash: "a" }, { ...swap, status: "FAILED", transaction_hash: "b" }] } });
    await render();
    expect(host.textContent).toContain("Pending");
    expect(host.textContent).toContain("Failed");
  });

  it("warns when the loop has no server pinned", async () => {
    respond({ strategy: { ...strategy, status: "running", unavailable: "no_server" } });
    await render();
    expect(host.textContent).toContain("no server pinned");
  });

  it("shows an error when a read fails, and still shows what loaded", async () => {
    respond({ swaps: new Error("Gateway is down") });
    await render();
    expect(host.querySelector("[role=alert]")?.textContent).toContain("Gateway is down");
    expect(host.querySelector('[data-token="SOL"]')).not.toBeNull();
  });

  it("asks for a server instead of reading the wallet when none is selected", async () => {
    respond();
    await render(null);
    expect(host.textContent).toContain("Select a server");
    expect(mocks.fetch.mock.calls.some(([path]) => String(path).includes("/portfolio"))).toBe(false);
  });
});

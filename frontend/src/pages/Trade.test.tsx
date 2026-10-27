/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { Trade } from "./Trade";

const request = vi.hoisted(() => ({ fetch: vi.fn(), credentials: vi.fn(), wallets: vi.fn() }));
vi.mock("@/pages/CreateExecutor", () => ({ CreateExecutor: () => {
  const location = useLocation();
  return <div data-workspace="exchange">Exchange workspace{location.search}</div>;
} }));
vi.mock("@/components/ConnectKeysOverlay", () => ({ ConnectKeysOverlay: () => <div data-gate>Connect Exchange keys</div> }));

const appStatus = { connected: true, enabled: false, ledger: { cash_eur: "26", positions: {}, uncertain_order: null } };
const onlyReads = (calls: unknown[][]) => calls.every(([path, init]) =>
  (path === "/api/v1/crypto-com/status" || String(path).startsWith("/api/v1/crypto-com/coins?")) && !init);
vi.mock("@/lib/api", async () => ({
  apiFetch: request.fetch,
  api: { getCredentials: request.credentials, getGatewayWallets: request.wallets, getCandles: vi.fn().mockRejectedValue(new Error("no candles")) },
}));
let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  request.fetch.mockReset().mockImplementation((path: string) => Promise.resolve(
    path.includes("/coins") ? { coins: [] } : appStatus));
  request.credentials.mockReset().mockResolvedValue({ credentials: [] });
  request.wallets.mockReset().mockResolvedValue({ wallets: [] });
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
async function render(url = "/trade") {
  await act(async () => root.render(<QueryClientProvider client={qc}>
    <ServerContext value={{ server: "test", setServer: () => {} }}>
      <MemoryRouter initialEntries={[url]}><Trade /></MemoryRouter>
    </ServerContext>
  </QueryClientProvider>));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
}
async function choose(label: string) {
  const button = Array.from(host.querySelectorAll("button")).find((b) => b.textContent === label)!;
  await act(async () => button.click());
}
it("opens the connected main App without Exchange keys and only reads status", async () => {
  await render();
  expect(host.textContent).toContain("Get quotation");
  expect(host.textContent).toMatch(/Cash €26[.,]00/);
  expect(host.textContent).not.toContain("€30");
  expect(host.textContent).not.toContain("€5 initially");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.querySelector("[data-workspace]")).toBeNull();
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});
it("keeps Exchange-only accounts in Hummingbot", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "crypto_com" }] });
  request.fetch.mockResolvedValue({ connected: false, enabled: false, ledger: { cash_eur: "0", positions: {}, uncertain_order: null } });
  await render();
  expect(host.querySelector("[data-workspace]")).not.toBeNull();
  expect(host.querySelector("[data-gate]")).toBeNull();
});
it("defaults to App with a Gateway wallet but no Exchange keys", async () => {
  request.wallets.mockResolvedValue({ wallets: [{ walletAddresses: ["wallet"] }] });
  await render();
  expect(host.textContent).toContain("Get quotation");
  expect(host.querySelector("[data-gate]")).toBeNull();
});
it.each(["?type=grid", "?type=order", "?workspace=hummingbot", "?connector=crypto_com&pair=BTC-USDT"])("preserves explicit Exchange URL %s", async (search) => {
  await render("/trade" + search);
  expect(host.querySelector("[data-workspace]")?.textContent).toContain(search);
  expect(host.querySelector("[data-gate]")).not.toBeNull();
});
it("keeps Exchange gating when switching and allows returning to App", async () => {
  await render();
  await choose("Hummingbot / Exchange");
  expect(host.querySelector("[data-gate]")).not.toBeNull();
  await choose("Crypto.com App");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.textContent).toContain("Get quotation");
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});
it("offers explicit App even with Exchange keys without automatically enabling it", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "binance" }] });
  await render("/trade?workspace=app");
  expect(host.textContent).toContain("Get quotation");
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});

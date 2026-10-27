/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { Trade } from "./Trade";

const request = vi.hoisted(() => ({ fetch: vi.fn(), credentials: vi.fn(), wallets: vi.fn() }));
vi.mock("@/lib/api", () => ({
  apiFetch: request.fetch,
  api: { getCredentials: request.credentials, getGatewayWallets: request.wallets },
}));
vi.mock("@/pages/CreateExecutor", () => ({ CreateExecutor: () => {
  const location = useLocation();
  return <div data-workspace="exchange">Exchange workspace{location.search}</div>;
} }));
vi.mock("@/components/ConnectKeysOverlay", () => ({ ConnectKeysOverlay: () => <div data-gate>Connect Exchange keys</div> }));

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  request.fetch.mockReset().mockResolvedValue({
    connected: true, enabled: false, ledger: { cash_eur: "26", positions: {}, uncertain_order: null },
  });
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
  expect(host.textContent).toContain("Quote an order");
  expect(host.textContent).toContain("Allocated cash: €26");
  expect(host.textContent).not.toContain("€30");
  expect(host.textContent).not.toContain("€5 initially");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.querySelector("[data-workspace]")).toBeNull();
  expect(request.fetch.mock.calls.every(([path, init]) => path === "/api/v1/crypto-com/status" && !init)).toBe(true);
});
it("keeps Exchange-only accounts in Hummingbot", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "crypto_com" }] });
  request.fetch.mockResolvedValue({ connected: false });
  await render();
  expect(host.querySelector("[data-workspace]")).not.toBeNull();
  expect(host.querySelector("[data-gate]")).toBeNull();
});
it("defaults to App with a Gateway wallet but no Exchange keys", async () => {
  request.wallets.mockResolvedValue({ wallets: [{ walletAddresses: ["wallet"] }] });
  await render();
  expect(host.textContent).toContain("Quote an order");
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
  expect(host.textContent).toContain("Quote an order");
  expect(request.fetch.mock.calls.every(([path, init]) => path.endsWith("/status") && !init)).toBe(true);
});
it("offers explicit App even with Exchange keys without automatically enabling it", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "binance" }] });
  await render("/trade?workspace=app");
  expect(host.textContent).toContain("Quote an order");
  expect(request.fetch.mock.calls.every(([path, init]) => path.endsWith("/status") && !init)).toBe(true);
});

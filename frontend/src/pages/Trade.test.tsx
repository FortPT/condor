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

const exchangeStatus = { connected: true, environment: "production", enabled: false, ledger: { quote: "USD", cash: "26", positions: {}, uncertain_order: null, synced: true } };
// The deposits/withdrawals record under the venue is read too (a GET, never a write).
const flowsRead = (path: unknown) => String(path).startsWith("/api/v1/native-flows?venue=");
const onlyReads = (calls: unknown[][]) => calls.every(([path, init]) =>
  (flowsRead(path) || path === "/api/v1/crypto-exchange/status" || path === "/api/v1/crypto-exchange/scorecard" ||
    String(path).startsWith("/api/v1/crypto-exchange/coins?") || String(path).startsWith("/api/v1/crypto-exchange/candles?")) && !init);
vi.mock("@/lib/api", async () => ({
  apiFetch: request.fetch,
  api: { getCredentials: request.credentials, getGatewayWallets: request.wallets },
}));
let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  request.fetch.mockReset().mockImplementation((path: string) => Promise.resolve(
    path.includes("/coins") ? { quote: "USD", coins: [] } : path.includes("/candles") ? { pair: "BTC_USD", timeframe: "1h", candles: [] } : path.endsWith("/scorecard") ? {} : exchangeStatus));
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
it("opens the connected Crypto.com Exchange without Hummingbot keys and only reads", async () => {
  await render();
  expect(host.textContent).toContain("Preview order");
  expect(host.textContent).toMatch(/Cash 26[.,]00 USD/);
  expect(host.textContent).not.toContain("€");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.querySelector("[data-workspace]")).toBeNull();
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});
it("keeps Hummingbot-only accounts in Hummingbot", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "crypto_com" }] });
  request.fetch.mockResolvedValue({ connected: false, environment: "production", enabled: false, ledger: { quote: "USD", cash: "0", positions: {}, uncertain_order: null, synced: false } });
  await render();
  expect(host.querySelector("[data-workspace]")).not.toBeNull();
  expect(host.querySelector("[data-gate]")).toBeNull();
});
it("defaults to the Exchange with a Gateway wallet but no Hummingbot keys", async () => {
  request.wallets.mockResolvedValue({ wallets: [{ walletAddresses: ["wallet"] }] });
  await render();
  expect(host.textContent).toContain("Preview order");
  expect(host.querySelector("[data-gate]")).toBeNull();
});
it.each(["?type=grid", "?type=order", "?workspace=hummingbot", "?connector=crypto_com&pair=BTC-USDT"])("preserves explicit Hummingbot URL %s", async (search) => {
  await render("/trade" + search);
  expect(host.querySelector("[data-workspace]")?.textContent).toContain(search);
  expect(host.querySelector("[data-gate]")).not.toBeNull();
});
it("keeps Hummingbot gating when switching and allows returning to the Crypto.com Exchange", async () => {
  await render();
  await choose("Hummingbot");
  expect(host.querySelector("[data-gate]")).not.toBeNull();
  await choose("Crypto.com Exchange");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.textContent).toContain("Preview order");
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});
it("offers the explicit Crypto.com Exchange even with Hummingbot keys, without enabling it", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "binance" }] });
  await render("/trade?workspace=crypto-exchange");
  expect(host.textContent).toContain("Preview order");
  expect(onlyReads(request.fetch.mock.calls)).toBe(true);
});
it("keeps the retired workspace=app link working", async () => {
  request.credentials.mockResolvedValue({ credentials: [{ connector: "binance" }] });
  await render("/trade?workspace=app");
  expect(host.textContent).toContain("Preview order");
});
it.each([["revolut-x", "Revolut X"], ["kraken-xstocks", "Kraken xStocks"]])("opens the read-only %s panel from its URL without Hummingbot keys", async (key, title) => {
  await render(`/trade?workspace=${key}`);
  expect(host.querySelector("h1")?.textContent).toBe(title);
  expect(host.querySelector(`[data-venue="${key}"]`)).not.toBeNull();
  expect(host.textContent).not.toContain("Preview order");
  expect(host.querySelector("[data-gate]")).toBeNull();
  expect(host.querySelector("[data-workspace]")).toBeNull();
  const pressed = Array.from(host.querySelectorAll("button[aria-pressed='true']")).map((b) => b.textContent);
  expect(pressed).toContain(title);
  expect(pressed).not.toContain("Hummingbot");
  expect(pressed).not.toContain("Crypto.com Exchange");
  // the native panel only reads its own routes (and the Crypto.com status the page itself polls)
  expect(request.fetch.mock.calls.every(([path, init]) => !init && (String(path).startsWith(`/api/v1/${key}/`) || String(path).startsWith("/api/v1/crypto-exchange/") || flowsRead(path)))).toBe(true);
});
it.each([
  ["/trade?workspace=revolut-x", "revolut_x"],
  ["/trade?workspace=kraken-xstocks", "kraken_xstocks"],
  ["/trade?workspace=crypto-exchange", "crypto_com_exchange"],
])("shows the deposits and withdrawals record under %s for its own venue", async (url, venue) => {
  await render(url);
  expect(host.querySelector(`[data-native-flows="${venue}"]`)).not.toBeNull();
  expect(request.fetch.mock.calls.some(([path]) => path === `/api/v1/native-flows?venue=${venue}`)).toBe(true);
});
it("shows no deposits panel on the Hummingbot tab", async () => {
  await render("/trade?workspace=hummingbot");
  expect(host.querySelector("[data-native-flows]")).toBeNull();
});
it("switches between the Crypto.com Exchange, Kraken xStocks, Revolut X and Hummingbot tabs", async () => {
  await render();
  expect(host.textContent).toContain("Preview order");
  await choose("Revolut X");
  expect(host.querySelector("h1")?.textContent).toBe("Revolut X");
  expect(host.textContent).not.toContain("Preview order");
  await choose("Kraken xStocks");
  expect(host.querySelector("h1")?.textContent).toBe("Kraken xStocks");
  await choose("Hummingbot");
  expect(host.querySelector("[data-gate]")).not.toBeNull();
  expect(host.querySelector("h1")).toBeNull();
  await choose("Crypto.com Exchange");
  expect(host.textContent).toContain("Preview order");
  expect(host.querySelector("[data-venue]")).toBeNull();
});
it("never makes Kraken xStocks or Revolut X the default account", async () => {
  await render("/trade");
  expect(host.querySelector("[data-venue]")).toBeNull();
  expect(host.textContent).toContain("Preview order");
});

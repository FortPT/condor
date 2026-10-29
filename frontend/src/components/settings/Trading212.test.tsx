/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { failureText } from "./failureText";
import { Trading212 } from "./Trading212";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ apiFetch: request }));

const limits = {
  floor: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
  guards_apply_from: "1000", max_asset_fraction_below_threshold: "0.4", default_slippage_pct: "0.3",
  fx_fee_pct_each_way: "0.15", max_buy_round_trip_pct: "1.0", min_order_eur: "1",
};
let status: Record<string, unknown>;
let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;
const wait = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent === label) as HTMLButtonElement;
async function type(field: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const calls = (method?: string) => request.mock.calls.filter(([, init]) => (method ? init?.method === method : !init));
const alerts = () => Array.from(host.querySelectorAll('[role="alert"]')).map((node) => node.textContent);

async function mount() {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={qc}><Trading212 /></QueryClientProvider>); });
  await wait();
}
async function remount(next: Record<string, unknown>) {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  status = next;
  await mount();
}
const ledgerOf = (extra: object) => ({ ...(status.ledger as object), ...extra });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status = {
    connected: true, has_secret: true, account_currency: "EUR", venue: "Trading 212", quote: "EUR", enabled: false, limits,
    market: { open: true, new_york_time: "Wed 11:00", minutes_to_close: 300 },
    ledger: { quote: "EUR", cash: "30", positions: {}, uncertain_order: null, enabled: false, orders: [], floor: "2", synced: true },
  };
  request.mockReset();
  request.mockImplementation((path: string) => Promise.resolve(path.endsWith("/status") ? status : {}));
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  vi.restoreAllMocks();
});

it("does not enable execution, sync or order on page load", async () => {
  await mount();
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/trading212/status"]);
});

it("describes the server's limits and the EUR-only, session-only rule", async () => {
  await mount();
  expect(host.textContent).toContain("Only US stocks listed on Trading 212 are traded, in a EUR account");
  expect(host.textContent).toContain("regular US session");
  expect(host.textContent).toContain("no single stock may exceed 40% of bot equity");
  expect(host.textContent).toContain("From 1000 EUR: 50% of bot equity stays in cash, at most 3 positions");
  expect(host.textContent).toContain("US market: open (300 min to the close)");
  expect(host.textContent).toContain("0.15% currency-conversion fee each way");
  expect(host.textContent).toContain("above 1.0% is refused");
  expect(host.textContent).toContain("Account currency: EUR");
  expect(host.textContent).toContain("first real order must be tiny");
  expect(host.textContent).toContain("no withdrawal endpoint and no withdrawal permission");
  expect(host.querySelectorAll("form")).toHaveLength(1);
  expect(host.textContent).not.toContain("Preview order");
});

it("takes the key and secret, never echoes them and sends nothing else", async () => {
  await mount();
  const fields = Array.from(host.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
  expect(fields).toHaveLength(2);
  await type(fields[0], "the-key");
  await type(fields[1], "the-secret");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  const [path, init] = calls("PUT")[0];
  expect(path).toBe("/api/v1/trading212/credentials");
  expect(JSON.parse(init.body)).toEqual({ api_key: "the-key", api_secret: "the-secret" });
  expect(fields[0].value).toBe("");
  expect(fields[1].value).toBe("");
  expect(host.textContent).not.toContain("the-secret");
  expect(host.textContent).toContain("API key saved. Trading is paused");
});

it("syncs the allocation and shows cash, positions and skipped assets", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => Promise.resolve(path.endsWith("/sync")
    ? { ...status, ledger: ledgerOf({ cash: "42.5", positions: { AAPL: { units: "0.1", cost: "34.5" } }, synced: true }), skipped: ["XXBT"] }
    : init ? {} : status));
  await act(async () => button("Sync allocation from Trading 212 account").click());
  await wait();
  const sync = calls("POST").find(([path]) => path.endsWith("/sync"))!;
  expect(sync[0]).toBe("/api/v1/trading212/sync");
  const text = host.querySelector('[role="status"]')!.textContent!;
  expect(text).toContain("cash 42.5 EUR");
  expect(text).toContain("AAPL 0.1");
  expect(text).toContain("skipped (not tradable here or no price): XXBT");
});

it("shows a visible pending state while a sync is running", async () => {
  await mount();
  let finish: (value: unknown) => void = () => {};
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/sync")
    ? new Promise((resolve) => { finish = resolve; })
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Sync allocation from Trading 212 account").click());
  expect(button("Syncing…").disabled).toBe(true);
  expect(button("Syncing…").getAttribute("aria-busy")).toBe("true");
  await act(async () => finish({ ...status, skipped: [] }));
  await wait();
  expect(button("Sync allocation from Trading 212 account")).toBeDefined();
});

it("never lets a failed sync or toggle pass silently, even with an empty reason", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => {
    if (path.endsWith("/sync")) return Promise.reject(new Error("Trading 212 unavailable"));
    if (path.endsWith("/enabled")) return Promise.reject(new Error(""));
    return Promise.resolve(init ? {} : status);
  });
  await act(async () => button("Sync allocation from Trading 212 account").click());
  await wait();
  expect(alerts()).toContain("Sync allocation failed: Trading 212 unavailable");
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(alerts().some((text) => text!.startsWith("Enable/pause trading failed: the server gave no reason"))).toBe(true);
  expect(failureText(undefined)).toContain("no reason");
  expect(failureText("plain text")).toBe("plain text");
});

it("shows a failed status load instead of an empty page", async () => {
  request.mockImplementation(() => Promise.reject(new Error("Request failed: 500")));
  await mount();
  expect(alerts()).toContain("Loading status failed: Request failed: 500");
  expect(button("Sync allocation from Trading 212 account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: waiting for the status to load");
});

it("says why a button is disabled instead of doing nothing", async () => {
  const healthy = status;
  await mount();
  await remount({ ...healthy, connected: false });
  expect(button("Sync allocation from Trading 212 account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: save an API key first");
  expect(host.textContent).toContain("Enabling is unavailable: save an API key first");
  expect(button("Disconnect").disabled).toBe(true);
  await remount({ ...healthy, enabled: true, ledger: { ...(healthy.ledger as object), enabled: true } });
  expect(button("Sync allocation from Trading 212 account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: pause trading first");
  expect(button("Pause trading").disabled).toBe(false);
  await remount({ ...healthy, ledger: { ...(healthy.ledger as object), synced: false } });
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(host.textContent).toContain("Enabling is unavailable: sync the allocation first");
  expect(host.textContent).toContain("has not been synced");
});

it("toggles trading and confirms it, or shows the server's refusal text", async () => {
  await mount();
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(host.textContent).toContain("Trading enabled within the allocation.");
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/enabled")
    ? Promise.reject(new Error("Sync the bot allocation from the Trading 212 account first"))
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(alerts().join(" ")).toContain("first");
});

it("offers reconcile only for an uncertain order and keeps trading paused", async () => {
  await mount();
  expect(button("Reconcile from Trading 212 order history")).toBeUndefined();
  await remount({ ...status, ledger: ledgerOf({ uncertain_order: { client_oid: "c1", side: "buy", symbol: "AAPL" } }) });
  expect(alerts().join(" ")).toContain("needs reconciliation");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(button("Sync allocation from Trading 212 account").disabled).toBe(true);
  await act(async () => button("Reconcile from Trading 212 order history").click());
  await wait();
  expect(calls("POST").some(([path]) => path.endsWith("/reconcile"))).toBe(true);
  expect(host.textContent).toContain("Reconciled from Trading 212's pending orders and order history");
});

it("disconnects through DELETE /credentials", async () => {
  await mount();
  await act(async () => button("Disconnect").click());
  await wait();
  expect(request.mock.calls.some(([path, init]) => path.endsWith("/credentials") && init?.method === "DELETE")).toBe(true);
  expect(host.textContent).toContain("Disconnected.");
});

it("saves a legacy single key with an empty secret and says what that means", async () => {
  await mount();
  const fields = Array.from(host.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
  expect(fields[1].required).toBe(false);
  expect(fields[1].placeholder).toContain("legacy key");
  await type(fields[0], "legacy-key");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  expect(JSON.parse(calls("PUT")[0][1].body)).toEqual({ api_key: "legacy-key", api_secret: "" });
  expect(fields[0].value).toBe("");
});

it("refuses to enable a non-EUR account and says why", async () => {
  await mount();
  await remount({ ...status, account_currency: "USD" });
  expect(host.textContent).toContain("Account currency: USD");
  expect(alerts().join(" ")).toContain("not EUR");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(host.textContent).toContain("Enabling is unavailable: the account currency is not EUR");
});

it("shows the key-creation instruction with the minimum permissions and no way to read the secret back", async () => {
  await mount();
  const text = host.textContent!;
  expect(text).toContain("Settings → API");
  expect(text).toContain("orders read and orders execute");
  expect(text).toContain("history transactions");
  expect(text).toContain("never in chat");
  expect(request.mock.calls.every(([path]) => path === "/api/v1/trading212/status")).toBe(true);
});

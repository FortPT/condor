/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { failureText } from "./failureText";
import { RevolutX } from "./RevolutX";

const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ apiFetch: request }));

const PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAQO/MSVsLMhpGZzlwjs4xbVvFPUsqBFr+7wsmTVVPgo0=\n-----END PUBLIC KEY-----\n";
const limits = {
  floor: "2", reserve_fraction: "0.5", position_fraction: "1/6", max_positions: 3,
  guards_apply_from: "1000", max_asset_fraction_below_threshold: "0.4", default_slippage_pct: "0.3",
  taker_fee_pct_estimate: "0.09",
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
  await act(async () => { root.render(<QueryClientProvider client={qc}><RevolutX /></QueryClientProvider>); });
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
    connected: true, has_signing_key: true, has_api_key: true, public_key: PUBLIC_KEY,
    venue: "Revolut X", quote: "EUR", enabled: false, limits,
    ledger: { quote: "EUR", cash: "30", positions: {}, uncertain_order: null, enabled: false, orders: [], floor: "2", synced: true, observed_taker_fee_pct: null },
  };
  request.mockReset();
  request.mockImplementation((path: string) => Promise.resolve(path.endsWith("/status") ? status : {}));
});
afterEach(async () => {
  await act(async () => root.unmount());
  qc.clear();
  host.remove();
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
});

it("does not enable execution, sync or order on page load", async () => {
  await mount();
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/api/v1/revolut-x/status"]);
});

it("describes the EUR-only, around-the-clock rule, the limits and that the fee is an estimate", async () => {
  await mount();
  expect(host.textContent).toContain("Only Revolut X pairs quoted in EUR are traded, around the clock");
  expect(host.textContent).toContain("no single coin may exceed 40% of bot equity");
  expect(host.textContent).toContain("From 1000 EUR: 50% of bot equity stays in cash, at most 3 positions");
  expect(host.textContent).toContain("Allocated cash: 30 EUR");
  expect(host.textContent).toContain("ESTIMATE");
  expect(host.textContent).toContain("immediate-or-cancel");
  expect(host.textContent).not.toContain("US market");
  expect(host.textContent).not.toContain("Preview order");
  await remount({ ...status, ledger: ledgerOf({ observed_taker_fee_pct: "0.0876" }) });
  expect(host.textContent).toContain("Taker fee: 0.0876% (reported by the last real fill)");
  expect(host.textContent).not.toContain("ESTIMATE");
});

it("generates the signing key on the server and shows only the public key to register", async () => {
  await remount({ ...status, connected: false, has_signing_key: false, has_api_key: false, public_key: null });
  expect(host.querySelector("pre")).toBeNull();
  request.mockImplementation((path: string, init?: { method?: string }) => {
    if (path.endsWith("/signing-key")) {
      status = { ...status, has_signing_key: true, public_key: PUBLIC_KEY };
      return Promise.resolve(status);
    }
    return Promise.resolve(init ? {} : status);
  });
  await act(async () => button("Generate signing key").click());
  await wait();
  const [path, init] = calls("POST")[0];
  expect(path).toBe("/api/v1/revolut-x/signing-key");
  expect(init.body).toBeUndefined();
  expect(host.querySelector("pre")!.textContent).toBe(PUBLIC_KEY);
  expect(host.textContent).toContain("API keys");
  expect(host.textContent).toContain("Signing key created");
  expect(host.textContent).not.toContain("PRIVATE");
  expect(button("Replace signing key")).toBeDefined();
  expect(host.textContent).toContain("Account: signing key created, API key missing");
});

it("copies the public key and says so, or shows why it could not", async () => {
  await mount();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  await act(async () => button("Copy public key").click());
  await wait();
  expect(writeText).toHaveBeenCalledWith(PUBLIC_KEY);
  expect(host.textContent).toContain("Public key copied.");
  writeText.mockRejectedValue(new Error("denied"));
  await act(async () => button("Copy public key").click());
  await wait();
  expect(alerts()).toContain("Copy public key failed: denied");
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  await act(async () => button("Copy public key").click());
  await wait();
  expect(alerts().join(" ")).toContain("copy it by hand");
});

it("takes the API key in a password field, never echoes it and sends nothing else", async () => {
  await mount();
  const fields = Array.from(host.querySelectorAll('input[type="password"]')) as HTMLInputElement[];
  expect(fields).toHaveLength(1);
  await type(fields[0], "the-api-key");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  const [path, init] = calls("PUT")[0];
  expect(path).toBe("/api/v1/revolut-x/api-key");
  expect(JSON.parse(init.body)).toEqual({ api_key: "the-api-key" });
  expect(fields[0].value).toBe("");
  expect(host.textContent).not.toContain("the-api-key");
  expect(host.textContent).toContain("API key saved. Trading is paused");
});

it("does not offer to save an API key before a signing key exists", async () => {
  await remount({ ...status, connected: false, has_signing_key: false, has_api_key: false, public_key: null });
  expect(button("Save API key").disabled).toBe(true);
  expect(host.textContent).toContain("Saving is unavailable: generate a signing key first");
  expect(host.textContent).toContain("Account: not connected");
});

it("syncs the allocation and shows cash, positions and skipped assets", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => Promise.resolve(path.endsWith("/sync")
    ? { ...status, ledger: ledgerOf({ cash: "42.5", positions: { SUI: { units: "5", cost: "15" } }, synced: true }), skipped: ["XYZ"] }
    : init ? {} : status));
  await act(async () => button("Sync allocation from Revolut X account").click());
  await wait();
  const sync = calls("POST").find(([path]) => path.endsWith("/sync"))!;
  expect(sync[0]).toBe("/api/v1/revolut-x/sync");
  const text = host.querySelector('[role="status"]')!.textContent!;
  expect(text).toContain("cash 42.5 EUR");
  expect(text).toContain("SUI 5");
  expect(text).toContain("skipped (no EUR pair or no price): XYZ");
});

it("shows a visible pending state while a key is generating or a sync is running", async () => {
  await mount();
  let finish: (value: unknown) => void = () => {};
  request.mockImplementation((path: string, init?: { method?: string }) => (path.endsWith("/sync") || path.endsWith("/signing-key"))
    ? new Promise((resolve) => { finish = resolve; })
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Sync allocation from Revolut X account").click());
  expect(button("Syncing…").disabled).toBe(true);
  expect(button("Syncing…").getAttribute("aria-busy")).toBe("true");
  await act(async () => finish({ ...status, skipped: [] }));
  await wait();
  await act(async () => button("Replace signing key").click());
  expect(button("Generating…").disabled).toBe(true);
  expect(button("Generating…").getAttribute("aria-busy")).toBe("true");
  await act(async () => finish(status));
  await wait();
});

it("never lets a failed generate, sync or toggle pass silently, even with an empty reason", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => {
    if (path.endsWith("/signing-key")) return Promise.reject(new Error("Reconcile the uncertain order before replacing the signing key"));
    if (path.endsWith("/sync")) return Promise.reject(new Error("Revolut X unavailable"));
    if (path.endsWith("/enabled")) return Promise.reject(new Error(""));
    return Promise.resolve(init ? {} : status);
  });
  await act(async () => button("Replace signing key").click());
  await wait();
  expect(alerts()).toContain("Generate signing key failed: Reconcile the uncertain order before replacing the signing key");
  await act(async () => button("Sync allocation from Revolut X account").click());
  await wait();
  expect(alerts()).toContain("Sync allocation failed: Revolut X unavailable");
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(JSON.parse(calls("PUT").at(-1)![1].body)).toEqual({ enabled: true });
  expect(alerts().some((text) => text!.startsWith("Enable/pause trading failed: the server gave no reason"))).toBe(true);
  expect(failureText(undefined)).toContain("no reason");
  expect(failureText("plain text")).toBe("plain text");
});

it("shows a rejected API key as text and keeps the field for another try", async () => {
  await mount();
  request.mockImplementation((path: string, init?: { method?: string }) => path.endsWith("/api-key")
    ? Promise.reject(new Error("Invalid API key: Revolut X keys are 64 letters and digits"))
    : Promise.resolve(init ? {} : status));
  const field = host.querySelector('input[type="password"]') as HTMLInputElement;
  await type(field, "short");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await wait();
  expect(alerts()).toContain("Save API key failed: Invalid API key: Revolut X keys are 64 letters and digits");
});

it("shows a failed status load instead of an empty page", async () => {
  request.mockImplementation(() => Promise.reject(new Error("Request failed: 500")));
  await mount();
  expect(alerts()).toContain("Loading status failed: Request failed: 500");
  expect(button("Sync allocation from Revolut X account").disabled).toBe(true);
  expect(button("Generate signing key").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: waiting for the status to load");
});

it("says why a button is disabled instead of doing nothing", async () => {
  const healthy = status;
  await mount();
  await remount({ ...healthy, connected: false, has_api_key: false });
  expect(button("Sync allocation from Revolut X account").disabled).toBe(true);
  expect(host.textContent).toContain("Sync is unavailable: generate a signing key and save the API key first");
  expect(host.textContent).toContain("Enabling is unavailable: generate a signing key and save the API key first");
  await remount({ ...healthy, connected: false, has_signing_key: false, has_api_key: false, public_key: null });
  expect(button("Disconnect").disabled).toBe(true);
  await remount({ ...healthy, enabled: true, ledger: { ...(healthy.ledger as object), enabled: true } });
  expect(button("Sync allocation from Revolut X account").disabled).toBe(true);
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
    ? Promise.reject(new Error("Sync the bot allocation from the Revolut X account first"))
    : Promise.resolve(init ? {} : status));
  await act(async () => button("Enable trading within allocation").click());
  await wait();
  expect(alerts().join(" ")).toContain("first");
});

it("offers reconcile only for an uncertain order and keeps trading paused", async () => {
  await mount();
  expect(button("Reconcile from Revolut X order history")).toBeUndefined();
  await remount({ ...status, ledger: ledgerOf({ uncertain_order: { client_oid: "c1", side: "buy", symbol: "BTC" } }) });
  expect(alerts().join(" ")).toContain("needs reconciliation");
  expect(button("Enable trading within allocation").disabled).toBe(true);
  expect(button("Sync allocation from Revolut X account").disabled).toBe(true);
  expect(button("Replace signing key").disabled).toBe(true);
  await act(async () => button("Reconcile from Revolut X order history").click());
  await wait();
  expect(calls("POST").some(([path]) => path.endsWith("/reconcile"))).toBe(true);
  expect(host.textContent).toContain("Reconciled from Revolut X's order history");
});

it("disconnects through DELETE /credentials", async () => {
  await mount();
  await act(async () => button("Disconnect").click());
  await wait();
  expect(request.mock.calls.some(([path, init]) => path.endsWith("/credentials") && init?.method === "DELETE")).toBe(true);
  expect(host.textContent).toContain("Disconnected.");
});

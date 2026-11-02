/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DisplaySettings } from "@/components/settings/DisplaySettings";
import { HIDE_HUMMINGBOT_PAGES, useHideHummingbot } from "./useUiPreferences";

const request = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch: request.fetch }));

let root: Root;
let host: HTMLDivElement;
let qc: QueryClient;

function Probe() {
  const { hidden, loading } = useHideHummingbot();
  return <p data-probe>{loading ? "loading" : hidden ? "hidden" : "shown"}</p>;
}

async function render(node: React.ReactNode) {
  await act(async () => root.render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
}
const probe = () => host.querySelector("[data-probe]")?.textContent;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  request.fetch.mockReset();
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

describe("useHideHummingbot", () => {
  it("hides only when the stored preference is exactly true", async () => {
    request.fetch.mockResolvedValue({ [HIDE_HUMMINGBOT_PAGES]: true });
    await render(<Probe />);
    expect(probe()).toBe("hidden");
    expect(request.fetch).toHaveBeenCalledWith("/api/v1/auth/preferences");
  });

  it("shows everything when the preference is off, missing or not a boolean", async () => {
    for (const stored of [{}, { [HIDE_HUMMINGBOT_PAGES]: false }, { [HIDE_HUMMINGBOT_PAGES]: "yes" }]) {
      qc.clear();
      request.fetch.mockResolvedValue(stored);
      await render(<Probe />);
      expect(probe()).toBe("shown");
    }
  });

  it("shows everything when the preference cannot be read", async () => {
    request.fetch.mockRejectedValue(new Error("down"));
    await render(<Probe />);
    expect(probe()).toBe("shown");
  });
});

describe("DisplaySettings", () => {
  it("shows the switch off, and turning it on saves the preference and updates every reader", async () => {
    request.fetch.mockImplementation((_path: string, init?: RequestInit) =>
      Promise.resolve(init?.method === "PATCH" ? { [HIDE_HUMMINGBOT_PAGES]: true } : {}));
    await render(<><Probe /><DisplaySettings /></>);
    const box = host.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(probe()).toBe("shown");
    await act(async () => box.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    const patch = request.fetch.mock.calls.find(([, init]) => init?.method === "PATCH")!;
    expect(patch[0]).toBe("/api/v1/auth/preferences");
    expect(JSON.parse(String(patch[1].body))).toEqual({ updates: { [HIDE_HUMMINGBOT_PAGES]: true } });
    expect(probe()).toBe("hidden");
    expect((host.querySelector("input[type=checkbox]") as HTMLInputElement).checked).toBe(true);
  });

  it("says when the setting could not be saved", async () => {
    request.fetch.mockImplementation((_path: string, init?: RequestInit) =>
      init?.method === "PATCH" ? Promise.reject(new Error("nope")) : Promise.resolve({}));
    await render(<DisplaySettings />);
    await act(async () => (host.querySelector("input[type=checkbox]") as HTMLInputElement).click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(host.querySelector("[role=alert]")?.textContent).toContain("Could not save");
  });
});

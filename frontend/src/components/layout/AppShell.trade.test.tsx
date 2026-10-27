/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";

vi.mock("@/hooks/useCredentials", () => ({ useCredentials: () => ({ hasKeys: false, isLoading: false }) }));
vi.mock("@/hooks/useServer", () => ({ useServer: () => ({ server: "test" }) }));
vi.mock("@/hooks/useTheme", () => ({ useTheme: () => ({ theme: "dark", toggleTheme: vi.fn() }) }));
vi.mock("@/hooks/useChat", () => ({ ChatProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/hooks/usePrefetchData", () => ({ usePrefetchData: vi.fn() }));
vi.mock("@/lib/viewFacts", () => ({ useViewFacts: vi.fn() }));
vi.mock("@/components/chat/ChatBubble", () => ({ ChatBubble: () => null }));
vi.mock("@/components/RelaunchBanner", () => ({ RelaunchBanner: () => null }));
vi.mock("@/components/TelemetryConsentBanner", () => ({ TelemetryConsentBanner: () => null }));
vi.mock("@/components/ReportIssueDialog", () => ({ ReportIssueDialog: () => null }));
vi.mock("./ServerSelector", () => ({ ServerSelector: () => null }));
vi.mock("./CurrencySelector", () => ({ CurrencySelector: () => null }));
vi.mock("./NotificationBell", () => ({ NotificationBell: () => null }));

it.each([
  ["/trade", false], ["/crypto-com", false], ["/bots", true], ["/dex", true],
])("keeps shell onboarding scoped on %s", async (path, gated) => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const qc = new QueryClient();
  try {
    await act(async () => root.render(<QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route element={<AppShell />}><Route path={path} element={<p>Workspace body</p>} /></Route></Routes>
      </MemoryRouter>
    </QueryClientProvider>));
    expect(host.textContent).toContain("Workspace body");
    expect(host.textContent?.includes("Connect an Exchange or Wallet")).toBe(gated);
  } finally {
    await act(async () => root.unmount());
    qc.clear();
    host.remove();
  }
});

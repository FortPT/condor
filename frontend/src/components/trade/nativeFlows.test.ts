import { describe, expect, it } from "vitest";

import {
  dateTs, flowKind, flowVenueKey, historyLine, signedAmount, signedUsd,
  type FlowVenueSummary,
} from "./nativeFlows";

const summary = (over: Partial<FlowVenueSummary> = {}): FlowVenueSummary => ({
  venue: "revolut_x", label: "Revolut X", recorded_net_flows_usd: 0, confirmed_count: 0,
  pending_count: 0, pending_net_usd: 0, unpriced_count: 0, tracked: false, fetch: null, note: "", ...over,
});

describe("flowVenueKey", () => {
  it("maps the Trade page's venue keys to the server's", () => {
    expect(flowVenueKey("revolut-x")).toBe("revolut_x");
    expect(flowVenueKey("trading212")).toBe("trading212");
    expect(flowVenueKey("crypto_com_exchange")).toBe("crypto_com_exchange");
  });
});

describe("signedAmount", () => {
  it("sends a deposit positive and a withdrawal negative", () => {
    expect(signedAmount("deposit", "4.27")).toBe("4.27");
    expect(signedAmount("withdrawal", " 4.27 ")).toBe("-4.27");
  });

  it.each(["", "0", "0.00", "-3", "+3", "1e5", "1,5", "abc", "4.", ".5"])("refuses %j", (text) => {
    expect(signedAmount("deposit", text)).toBeNull();
  });
});

describe("flowKind and signedUsd", () => {
  it("reads the type from the amount's sign", () => {
    expect(flowKind({ amount: "5" })).toBe("deposit");
    expect(flowKind({ amount: "-5" })).toBe("withdrawal");
  });

  it("prints a sign and two decimals, or a dash without a USD value", () => {
    expect(signedUsd(4.7)).toBe("+$4.70");
    expect(signedUsd(-3)).toBe("-$3.00");
    expect(signedUsd(null)).toBe("—");
    expect(signedUsd(undefined)).toBe("—");
  });
});

describe("historyLine", () => {
  it("says the history was never read, read, or could not be read and why", () => {
    expect(historyLine(undefined)).toBe("");
    expect(historyLine(summary())).toContain("has not been read yet");
    expect(historyLine(summary({ fetch: { ts: 1_793_000_000, ok: true, reason: "", count: 2 } }))).toContain(
      "transfer history was read",
    );
    const failed = historyLine(summary({ fetch: { ts: 1, ok: false, reason: "unavailable: denied", count: 0 } }));
    expect(failed).toContain("could not be read (unavailable: denied)");
    expect(failed).toContain("Detection and manual entry still work");
  });
});

describe("dateTs", () => {
  it("sends the date as typed, or nothing for now", () => {
    expect(dateTs("2026-10-28")).toBe("2026-10-28");
    expect(dateTs("  ")).toBeUndefined();
  });
});

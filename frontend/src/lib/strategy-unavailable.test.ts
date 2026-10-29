import { describe, expect, it } from "vitest";

import { pnlBasisTitle, unavailableLabel, unconfirmedLine } from "./strategy-unavailable";

describe("unavailableLabel", () => {
  it("names every reason a loop's figures can be missing", () => {
    expect(unavailableLabel("no_server")).toBe("No server configured");
    expect(unavailableLabel("no_access")).toBe("Server access unavailable for this loop");
    expect(unavailableLabel("unreachable")).toBe("Server unreachable");
  });

  it("says a native crew has no equity recorded yet", () => {
    expect(unavailableLabel("no_history")).toBe("No equity history yet");
  });

  it("says a paper-only crew holds no account", () => {
    expect(unavailableLabel("no_ledger")).toBe("Paper test only: no account equity");
  });

  it("is empty for a priced loop", () => {
    expect(unavailableLabel("")).toBe("");
    expect(unavailableLabel(undefined)).toBe("");
  });
});

describe("pnlBasisTitle", () => {
  it("is absent for an ordinary executor figure", () => {
    expect(pnlBasisTitle("", "")).toBeUndefined();
    expect(pnlBasisTitle(undefined, "anything")).toBeUndefined();
  });

  it("says a native crew's figure is the change in account equity", () => {
    expect(pnlBasisTitle("account_equity", "")).toBe(
      "Change in account equity since this session started (native crew)",
    );
  });

  it("appends the note that flags an estimated boundary", () => {
    expect(pnlBasisTitle("account_equity", "Session start estimated.")).toBe(
      "Change in account equity since this session started (native crew). Session start estimated.",
    );
  });
});

describe("pnlBasisTitle with recorded deposits and withdrawals", () => {
  it("calls the figure a trading result when the venue's flows are tracked", () => {
    expect(pnlBasisTitle("account_equity", "", { tracked: true })).toBe(
      "Trading result since this session started: change in account equity net of recorded deposits and withdrawals (native crew)",
    );
  });

  it("keeps the older wording when they are not tracked", () => {
    expect(pnlBasisTitle("account_equity", "", { tracked: false })).toBe(
      "Change in account equity since this session started (native crew)",
    );
  });

  it("says unconfirmed cash is not subtracted, after the estimate note", () => {
    const title = pnlBasisTitle("account_equity", "Session start estimated.", {
      tracked: true,
      unconfirmedCount: 1,
      unconfirmedUsd: 4.74,
    })!;
    expect(title).toContain("Session start estimated.");
    expect(title).toContain("Unconfirmed cash movement of +$4.74 is not subtracted");
  });

  it("states a negative unconfirmed movement with its sign and none when nothing is pending", () => {
    expect(unconfirmedLine({ unconfirmedCount: 1, unconfirmedUsd: -3 })).toBe(
      "Unconfirmed cash movement of -$3.00",
    );
    expect(unconfirmedLine({ unconfirmedCount: 0, unconfirmedUsd: 5 })).toBe("");
    expect(unconfirmedLine(undefined)).toBe("");
  });
});

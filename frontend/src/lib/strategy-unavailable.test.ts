import { describe, expect, it } from "vitest";

import { pnlBasisTitle, unavailableLabel } from "./strategy-unavailable";

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

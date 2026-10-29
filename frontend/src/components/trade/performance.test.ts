import { describe, expect, it } from "vitest";

import { NATIVE_VENUES, toggleBlock, type NativeStatus } from "./nativeVenue";
import {
  cardsOf, holdingPnl, orderKey, resultFor, resultsByOrderId, resultsOf, signedMoney, toneOf, totalsOf, utcLabel,
} from "./performance";
import { makePerformance } from "./performance.fixture";

const card = (perf: ReturnType<typeof makePerformance>, key: string) => cardsOf(perf).find((c) => c.key === key)!;

describe("money and tone", () => {
  it("signs gains and losses, never turns an unknown into zero, and keeps tiny fees readable", () => {
    expect(signedMoney(1.5, "EUR")).toMatch(/^\+1[.,]50 EUR$/);
    expect(signedMoney(-0.04, "EUR")).toMatch(/^-0[.,]04 EUR$/);
    expect(signedMoney(0, "USD")).toMatch(/^0[.,]00 USD$/);
    expect(signedMoney(null, "USD")).toBe("—");
    expect(signedMoney(undefined, "USD")).toBe("—");
    expect(signedMoney(0.0042, "USD")).toMatch(/^\+0[.,]0042 USD$/);
  });

  it("maps the sign to a tone", () => {
    expect([toneOf(2), toneOf(-2), toneOf(0), toneOf(null), toneOf(undefined)]).toEqual(["up", "down", "flat", "none", "none"]);
  });

  it("formats a UTC moment", () => {
    expect(utcLabel(1_793_145_600)).toBe("2026-10-28 00:00 UTC");
    expect(utcLabel(null)).toBeNull();
  });
});

describe("cardsOf", () => {
  it("makes the six cards with a hover explanation each", () => {
    const cards = cardsOf(makePerformance("EUR"));
    expect(cards.map((c) => c.label)).toEqual(["Today", "Since start", "Realized", "Unrealized", "Fees", "Trades (W/L)"]);
    expect(cards.every((c) => c.hint.length > 20)).toBe(true);
    expect(card(makePerformance("EUR"), "today").value).toMatch(/\+1[.,]50 USD/);
    expect(card(makePerformance("EUR"), "realized").value).toMatch(/\+4[.,]50 EUR/);
    expect(card(makePerformance("EUR"), "trades").value).toBe("5 · 1W / 1L");
    expect(card(makePerformance("EUR"), "since-start").tone).toBe("down");
  });

  it("says why a figure is missing instead of showing zero", () => {
    const perf = makePerformance("EUR", {
      today: { value: null, currency: "USD", since: null, since_label: null, partial: false, estimated: false, flows_usd: 0, reason: "No equity history recorded yet", note: null },
      unrealized: { value: null, currency: "EUR", reason: "Reconcile the uncertain order" },
    });
    expect(card(perf, "today")).toMatchObject({ value: "—", tone: "none", note: "No equity history recorded yet" });
    expect(card(perf, "unrealized")).toMatchObject({ value: "—", note: "Reconcile the uncertain order" });
  });

  it("flags a partial today, estimated windows, untracked deposits and partial or incomplete realized figures", () => {
    const base = makePerformance("USD").today!;
    const perf = makePerformance("USD", {
      today: { ...base, partial: true, since: 1_793_164_000, since_label: "2026-10-28 05:00 UTC", estimated: true, note: "Deposits and withdrawals are not tracked for this venue: a deposit would read as a gain. 1 unconfirmed deposit/withdrawal(s) are not subtracted." },
      realized: { ...makePerformance("USD").realized!, partial_history: true, history_starts_at: 1_793_100_000 },
    });
    const today = card(perf, "today").note!;
    expect(today).toContain("history starts at 2026-10-28 05:00 UTC");
    expect(today).toContain("estimated");
    expect(today).toContain("deposits are not tracked here");
    expect(today).toContain("unconfirmed deposits are not subtracted");
    expect(card(perf, "realized").note).toContain("partial: order history starts at 2026-10-27");
  });

  it("names an incomplete realized total and unconverted or estimated fees", () => {
    const perf = makePerformance("USD", {
      realized: { ...makePerformance("USD").realized!, complete: false, reason: "1 sale(s) have no recorded cost and are not counted: x", unknown_sells: 1 },
      fees: { value: 0.5, estimated: 0.2, is_estimate: true, unconverted: 2, complete: false, currency: "USD" },
    });
    expect(card(perf, "realized").note).toContain("no recorded cost");
    expect(card(perf, "fees").value).toMatch(/0[.,]50 USD \(est\.\)/);
    expect(card(perf, "fees").note).toContain("includes estimated fees");
    expect(card(perf, "fees").note).toContain("2 fee(s) in another currency not counted");
  });

  it("shows no sales yet as a note, not an error", () => {
    const perf = makePerformance("USD", { realized: { ...makePerformance("USD").realized!, value: 0, sells: 0 }, trades: { ...makePerformance("USD").trades!, wins: 0, losses: 0, win_rate: null } });
    expect(card(perf, "realized").note).toBe("no sales yet");
    expect(card(perf, "trades").note).toContain("no closed sale yet");
  });
});

describe("results by order", () => {
  const perf = makePerformance("USD", {
    orders: [
      { time: 10, symbol: "A", order_id: "x1", client_oid: null, pnl: 2, pnl_pct: 20, closed_position: true, reason: null },
      { time: 20, symbol: "B", order_id: null, client_oid: "c1", pnl: null, pnl_pct: null, closed_position: false, reason: "no recorded cost for this sale" },
    ],
  });

  it("keys a ledger order the way the table's rows are keyed", () => {
    expect(orderKey({ order_id: "x1", time: 10 })).toBe("x1-10");
    expect(orderKey({ client_oid: "c1", time: 20 })).toBe("c1-20");
    expect(Array.from(resultsOf(perf).keys())).toEqual(["x1-10", "c1-20"]);
    expect(Array.from(resultsByOrderId(perf).keys())).toEqual(["x1"]);
  });

  it("gives a sale its amount, a buy nothing, and an unknown sale its reason", () => {
    const results = resultsOf(perf);
    expect(resultFor("sell", "x1-10", results)).toEqual({ kind: "known", pnl: 2, pct: 20 });
    expect(resultFor("SELL", "c1-20", results)).toEqual({ kind: "unknown", reason: "no recorded cost for this sale" });
    expect(resultFor("buy", "x1-10", results)).toEqual({ kind: "none" });
    expect(resultFor("sell", "zzz", results).kind).toBe("unknown");
    expect(resultFor("sell", "zzz", resultsOf(undefined), "custom")).toEqual({ kind: "unknown", reason: "custom" });
  });
});

describe("holdings P/L and totals", () => {
  it("is value minus cost, and unknown when either is missing", () => {
    expect(holdingPnl({ cost: 10, value: 12.5 })).toBe(2.5);
    expect(holdingPnl({ cost: 10, value: 7 })).toBe(-3);
    expect(holdingPnl({ cost: null, value: 7 })).toBeNull();
    expect(holdingPnl({ cost: 10, value: null })).toBeNull();
  });

  it("totals cash + holdings = equity and the total unrealized", () => {
    const totals = totalsOf(40, [{ cost: 12, value: 15 }, { cost: 10, value: 8 }]);
    expect(totals).toMatchObject({ cost: 22, value: 23, equity: 63, unrealized: 1 });
    expect(totals.unrealizedPct).toBeCloseTo((23 / 22 - 1) * 100);
  });

  it("states no equity rather than a wrong one when any holding has no price", () => {
    expect(totalsOf(40, [{ cost: 12, value: null }])).toEqual({ cost: 12, value: null, equity: null, unrealized: null, unrealizedPct: null });
    expect(totalsOf(40, [])).toMatchObject({ cost: 0, equity: 40, unrealized: 0 });
    expect(totalsOf(null, [{ cost: 1, value: 2 }]).equity).toBeNull();
  });
});

describe("toggleBlock (the same rules as each venue's Settings tab)", () => {
  const status = (over: Partial<NativeStatus> = {}, ledger: Record<string, unknown> = {}): NativeStatus => ({
    connected: true, enabled: false, ledger: { quote: "EUR", cash: "1", positions: {}, uncertain_order: null, synced: true, ...ledger }, ...over,
  } as NativeStatus);
  const revolut = NATIVE_VENUES["revolut-x"];
  const kraken = NATIVE_VENUES["kraken-xstocks"];
  const t212 = NATIVE_VENUES.trading212;

  it("waits for the status and for a connection", () => {
    expect(toggleBlock(kraken, undefined)).toBe("waiting for the status to load");
    expect(toggleBlock(kraken, status({ connected: false }))).toBe("save an API key first");
    expect(toggleBlock(revolut, status({ connected: false }))).toBe("generate a signing key and save the API key first");
  });

  it("needs a synced allocation and no uncertain order to enable, in that order of reasons", () => {
    expect(toggleBlock(kraken, status())).toBe("");
    expect(toggleBlock(kraken, status({}, { synced: false }))).toBe("sync the allocation first");
    expect(toggleBlock(kraken, status({}, { uncertain_order: { client_oid: "x" }, synced: false }))).toBe("reconcile the uncertain order first");
  });

  it("always allows a pause once connected", () => {
    expect(toggleBlock(kraken, status({ enabled: true }, { synced: false, uncertain_order: { client_oid: "x" } }))).toBe("");
    expect(toggleBlock(t212, status({ enabled: true, account_currency: "USD" }))).toBe("");
  });

  it("needs a EUR account for Trading 212", () => {
    expect(toggleBlock(t212, status({ account_currency: "USD" }))).toBe("the account currency is not EUR");
    expect(toggleBlock(t212, status({ account_currency: "EUR" }))).toBe("");
    expect(toggleBlock(kraken, status({ account_currency: "USD" }))).toBe("");
  });
});

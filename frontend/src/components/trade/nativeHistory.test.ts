import { describe, expect, it } from "vitest";

import { withRealFigures, type LedgerOrder } from "./nativeHistory";
import { NATIVE_VENUES, historyOf } from "./nativeVenue";

const revolut = NATIVE_VENUES["revolut-x"];
const t212 = NATIVE_VENUES.trading212;

const listed = (id: string, extra: Record<string, unknown> = {}) => ({
  order_id: id, pair: "HYPE-EUR", side: "sell", status: "filled", filled_quantity: "0.0001",
  average_fill_price: "72368.42", filled_amount: "7.24", updated: 2000, ...extra,
});
const ledgerOrder = (id: string, extra: Partial<LedgerOrder> = {}): LedgerOrder => ({
  time: 2, side: "sell", symbol: "BTC", order_id: id, status: "filled", quantity: "0.0001", value: "7.24",
  fee: "0.01", fee_currency: "EUR", ...extra,
});

describe("the real figures of a Revolut X order", () => {
  it("derives the average price from the fills' value and quantity, not the rounded one", () => {
    const [row] = withRealFigures(revolut, historyOf([listed("a")]), [ledgerOrder("a", { fills_value: "7.2776" })]);
    expect(row.average_price).toBe("72776");
    expect(row.rounded).toBe(false);
  });

  it("shows the fee with its currency from the order record when the list has none", () => {
    const [row] = withRealFigures(revolut, historyOf([listed("a")]), [ledgerOrder("a", { fills_value: "7.2776" })]);
    expect(row.fee).toBe("0.01 EUR");
    const [estimated] = withRealFigures(revolut, historyOf([listed("a")]), [ledgerOrder("a", { fee_source: "estimate" })]);
    expect(estimated.fee).toBe("0.01 EUR (estimate)");
  });

  it("keeps the exchange's figure for an older order and says it is rounded", () => {
    const [noRecord] = withRealFigures(revolut, historyOf([listed("old")]), []);
    expect(noRecord).toMatchObject({ average_price: "72368.42", rounded: true, fee: "—" });
    const [noFills] = withRealFigures(revolut, historyOf([listed("old")]), [ledgerOrder("old")]);
    expect(noFills).toMatchObject({ average_price: "72368.42", rounded: true, fee: "0.01 EUR" });
  });

  it("keeps a fee the list already carries, and flags nothing on an order with no price", () => {
    const [withFee] = withRealFigures(revolut, historyOf([listed("a", { total_fee: "0.02", fee_currency: "EUR" })]), [ledgerOrder("a")]);
    expect(withFee.fee).toBe("0.02 EUR");
    const [cancelled] = withRealFigures(revolut, historyOf([{ order_id: "c", status: "cancelled", updated: 1 }]), []);
    expect(cancelled.rounded).toBe(false);
  });

  it("leaves other venues alone", () => {
    const rows = historyOf([{ order_id: "k", pair: "AAPL_US_EQ", side: "buy", status: "FILLED", filled_quantity: "2", average_fill_price: "5", filled_amount: "10", total_fee: "0.04", updated: 5000 }]);
    const [row] = withRealFigures(t212, rows, [ledgerOrder("k", { fills_value: "99" })]);
    expect(row).toMatchObject({ average_price: "5", rounded: false, fee: "0.04" });
  });
});

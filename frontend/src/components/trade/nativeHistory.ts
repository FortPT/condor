/**
 * What the Recent orders table shows for a Revolut X order: the real average price and the fee.
 *
 * Revolut X reports a cent-rounded `filled_amount` and `average_fill_price` on the order list
 * (a sale at 72776 reads 72368.42), and no fee there. The service keeps the truth for orders it
 * settled since the fills were read: the fills' value (`fills_value`) and the fee on the ledger
 * entry. Those are used when the ledger has them; an older order keeps the stored figure and is
 * flagged `rounded`, so a rounded price is never presented as the real one.
 */
import type { ExchangeLedger } from "@/hooks/useCredentials";
import { digitsFor, num } from "./tradeFormat";
import type { HistoryRow, NativeVenue } from "./nativeVenue";

export type LedgerOrder = NonNullable<ExchangeLedger["orders"]>[number];
export type ShownHistoryRow = HistoryRow & {
  /** True when the average price is the exchange's cent-rounded figure, not derived from fills. */
  rounded: boolean;
};

export function withRealFigures(venue: NativeVenue, rows: HistoryRow[], orders: LedgerOrder[] | undefined): ShownHistoryRow[] {
  const revolut = venue.key === "revolut-x";
  const byId = new Map<string, LedgerOrder>();
  for (const order of orders ?? []) if (order.order_id) byId.set(order.order_id, order);
  return rows.map((row) => {
    const order = revolut ? byId.get(row.id) : undefined;
    let { average_price: average, fee } = row;
    let rounded = revolut && average !== "—";
    if (order) {
      const units = num(order.quantity);
      const fills = num(order.fills_value);
      if (units !== null && units > 0 && fills !== null && fills > 0) {
        const price = fills / units;
        average = price.toLocaleString(undefined, { maximumFractionDigits: Math.max(digitsFor(price), 2), useGrouping: false });
        rounded = false;
      }
      if (fee === "—" && order.fee !== undefined && order.fee !== null && order.fee !== "") {
        fee = `${order.fee}${order.fee_currency ? ` ${order.fee_currency}` : ""}${order.fee_source === "estimate" ? " (estimate)" : ""}`;
      }
    }
    return { ...row, average_price: average, fee, rounded };
  });
}

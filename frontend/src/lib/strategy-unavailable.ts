import type { StrategyUnavailable } from "@/lib/api";

/**
 * The one line that says why a loop's figures are missing (CORR-430), or
 * `""` when they are not — the reader's cue to render exactly as before.
 *
 * Without it every no-client state (CORR-706) reads as a session that traded
 * nothing: an empty executor table, no vitals, a run that filters to nothing.
 */
export function unavailableLabel(reason: StrategyUnavailable | undefined): string {
  switch (reason) {
    case "no_access":
      return "Server access unavailable for this loop";
    case "unreachable":
      return "Server unreachable";
    case "no_server":
      return "No server configured";
    case "no_history":
      return "No equity history yet";
    case "no_ledger":
      return "Paper test only: no account equity";
    default:
      return "";
  }
}

/** The PnL basis of a native crew: the change in its venue's account equity. */
export const ACCOUNT_EQUITY = "account_equity";

/** What a native crew's card knows about the venue's deposits and withdrawals. */
export interface PnlFlows {
  /** The venue's flows are recorded, so the figure is a trading result. */
  tracked?: boolean;
  /** Detected cash movements the owner has not confirmed (never subtracted). */
  unconfirmedCount?: number;
  unconfirmedUsd?: number;
}

/** `+$9.00` / `-$3.00`. */
function signedDollars(value: number): string {
  return `${value < 0 ? "-" : "+"}$${Math.abs(value).toFixed(2)}`;
}

/**
 * The visible line for unconfirmed cash movements, or `""` when there are none:
 * the figure may be wrong by exactly that amount until the owner decides.
 */
export function unconfirmedLine(flows: PnlFlows | undefined): string {
  const count = flows?.unconfirmedCount ?? 0;
  if (!count) return "";
  return `Unconfirmed cash movement of ${signedDollars(flows?.unconfirmedUsd ?? 0)}`;
}

/**
 * What a loop card's PnL figure is, for its hover, or `undefined` for an
 * ordinary executor figure. A native crew has no executors, so its number is
 * the change in the account's value, net of the deposits and withdrawals
 * recorded for it when they are tracked (a trading result), and otherwise the
 * plain equity change; the note, when there is one, says that a session boundary
 * was estimated from the nearest recorded sample.
 */
export function pnlBasisTitle(
  basis: string | undefined,
  note: string | undefined,
  flows?: PnlFlows,
): string | undefined {
  if (basis !== ACCOUNT_EQUITY) return undefined;
  const base = flows?.tracked
    ? "Trading result since this session started: change in account equity net of recorded deposits and withdrawals (native crew)"
    : "Change in account equity since this session started (native crew)";
  const parts = [base];
  if (note) parts.push(note);
  const unconfirmed = unconfirmedLine(flows);
  if (unconfirmed) {
    parts.push(`${unconfirmed} is not subtracted until you confirm it on the Trade page`);
  }
  return parts.join(". ");
}

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

/**
 * What a loop card's PnL figure is, for its hover, or `undefined` for an
 * ordinary executor figure. A native crew has no executors, so its number is
 * the change in the account's value; the note, when there is one, says that a
 * session boundary was estimated from the nearest recorded sample.
 */
export function pnlBasisTitle(
  basis: string | undefined,
  note: string | undefined,
): string | undefined {
  if (basis !== ACCOUNT_EQUITY) return undefined;
  const base = "Change in account equity since this session started (native crew)";
  return note ? `${base}. ${note}` : base;
}

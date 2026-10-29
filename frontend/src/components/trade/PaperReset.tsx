import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useIsAdmin } from "@/hooks/useIsAdmin";
import { apiFetch } from "@/lib/api";
import type { NativeVenue } from "./nativeVenue";

const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

/**
 * The owner's "Reset paper account" control. Admin only: the button is hidden for anyone
 * else (a convenience; `POST /reset` re-checks the role on the server). It never resets on
 * one click: the first click asks, the second one calls the REST route, which archives the
 * simulated ledger and starts again from the starting cash. The agents have no such action.
 */
export function PaperReset({ venue, opened, startCash = "100" }: { venue: NativeVenue; opened: boolean; startCash?: string }) {
  const admin = useIsAdmin();
  const queryClient = useQueryClient();
  const [asking, setAsking] = useState(false);
  const reset = useMutation({
    mutationFn: () => apiFetch<unknown>(`${venue.base}/reset`, { method: "POST" }),
    retry: false,
    onSuccess: () => {
      setAsking(false);
      // The status, balances, history, scorecard and loop cards all change with the ledger.
      void queryClient.invalidateQueries({ predicate: (query) => String(query.queryKey[0]).startsWith(venue.key) });
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  if (!admin) return null;
  const label = opened ? "Reset paper account" : "Open paper account";
  return <div className="space-y-2" data-paper-reset>
    {!asking && <button type="button" className={button} disabled={reset.isPending} onClick={() => { reset.reset(); setAsking(true); }}>{label}</button>}
    {asking && <div role="group" aria-label="Confirm reset" className="flex flex-wrap items-center gap-2 text-sm">
      <span>{opened
        ? `Archive the simulated ledger and start again with ${startCash} USD? The positions and order history are kept in the archive; the reset is not counted as a gain or a loss.`
        : `Open the simulated account with ${startCash} USD?`}</span>
      <button type="button" className={button} disabled={reset.isPending} onClick={() => reset.mutate()}>{reset.isPending ? "Resetting…" : opened ? "Confirm reset" : "Confirm open"}</button>
      <button type="button" className={button} disabled={reset.isPending} onClick={() => setAsking(false)}>Cancel</button>
    </div>}
    {reset.isPending && <p role="status" className="text-sm">Resetting the paper account…</p>}
    {reset.isError && <p role="alert" className="text-sm text-[var(--color-red)]">Reset failed: {reset.error.message}</p>}
    {reset.isSuccess && !asking && <p role="status" className="text-sm">The paper account was reset to {startCash} USD.</p>}
  </div>;
}

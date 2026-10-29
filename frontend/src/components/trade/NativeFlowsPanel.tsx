import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { apiFetch } from "@/lib/api";
import {
  FLOWS_URL, SOURCE_LABEL, dateTs, flowKind, flowsQueryKey, historyLine, signedAmount, signedUsd,
  type FlowKind, type FlowsResponse, type NativeFlow, type SyncResponse,
} from "./nativeFlows";

const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";
const card = "rounded-lg border border-[var(--color-border)] p-4";

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

/**
 * The owner's record of deposits and withdrawals for one native venue, so the loop
 * cards show a trading result and not a transfer. Lists what is recorded, the cash
 * movements Condor detected but could not explain (Confirm or Dismiss; nothing is
 * subtracted until confirmed), and a form to add one by hand. Records only: no money
 * moves here.
 */
export function NativeFlowsPanel({
  venue,
  title,
  defaultCurrency,
}: {
  /** The server's venue key: `crypto_com_exchange`, `kraken_xstocks`, `revolut_x` or `trading212`. */
  venue: string;
  title: string;
  defaultCurrency: string;
}) {
  const queryClient = useQueryClient();
  const key = flowsQueryKey(venue);
  const flows = useQuery({
    queryKey: key,
    queryFn: () => apiFetch<FlowsResponse>(`${FLOWS_URL}?venue=${encodeURIComponent(venue)}`),
    staleTime: 30000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  // A change here moves the loop cards' PnL, so those lists refresh too.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: key });
    void queryClient.invalidateQueries({ queryKey: ["agents"] });
  };

  const confirm = useMutation({
    mutationFn: (id: string) => apiFetch<NativeFlow>(`${FLOWS_URL}/${id}/confirm`, { method: "POST" }),
    onSuccess: refresh,
  });
  const dismiss = useMutation({
    mutationFn: (id: string) => apiFetch<NativeFlow>(`${FLOWS_URL}/${id}/dismiss`, { method: "POST" }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => apiFetch(`${FLOWS_URL}/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
  const sync = useMutation({
    mutationFn: () => apiFetch<SyncResponse>(`${FLOWS_URL}/sync`, { method: "POST", body: JSON.stringify({ venue }) }),
    onSuccess: refresh,
  });

  const [kind, setKind] = useState<FlowKind>("deposit");
  const [amountText, setAmountText] = useState("");
  const [currency, setCurrency] = useState(defaultCurrency);
  const [date, setDate] = useState("");
  const [note, setNote] = useState("");
  const [usd, setUsd] = useState("");
  const amount = signedAmount(kind, amountText);
  const add = useMutation({
    mutationFn: () =>
      apiFetch<NativeFlow>(FLOWS_URL, {
        method: "POST",
        body: JSON.stringify({
          venue,
          amount,
          currency: currency.trim().toUpperCase(),
          ts: dateTs(date),
          note: note.trim(),
          usd_value: usd.trim() || undefined,
        }),
      }),
    onSuccess: () => {
      setAmountText("");
      setNote("");
      setUsd("");
      refresh();
    },
  });

  const summary = flows.data?.venues?.[0];
  const rows = flows.data?.flows ?? [];
  const pending = rows.filter((row) => row.status === "pending");
  const recorded = rows.filter((row) => row.status === "confirmed");
  const syncLine = sync.data?.results[0];
  const error = flows.error || confirm.error || dismiss.error || remove.error || sync.error;

  return <section className={`${card} space-y-3`} aria-label="Deposits and withdrawals" data-native-flows={venue}>
    <div className="flex flex-wrap items-center gap-3">
      <h2 className="font-semibold">Deposits and withdrawals</h2>
      <button type="button" className={`${button} ml-auto`} disabled={sync.isPending} onClick={() => sync.mutate()}>
        {sync.isPending ? "Reading history…" : "Re-read venue history"}
      </button>
    </div>
    <p className="text-xs text-[var(--color-text-muted)]">
      {summary?.tracked
        ? `Loop PnL for ${title} is a trading result: the change in account equity net of the deposits and withdrawals confirmed below.`
        : `Nothing is recorded for ${title} yet, so its loop PnL is the plain equity change and a deposit reads as a gain. Add or confirm the deposits and withdrawals below.`}
    </p>
    {flows.isLoading && <p role="status" className="text-sm">Reading recorded flows…</p>}
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    {summary && <p className="text-xs text-[var(--color-text-muted)]" data-flows-history>{historyLine(summary)}</p>}
    {syncLine && <p role="status" className="text-sm" data-flows-sync>
      {syncLine.ok
        ? `History read: ${syncLine.new ?? 0} new, ${syncLine.detected ?? 0} detected.`
        : `History not read: ${syncLine.reason || "unavailable"}${syncLine.detected ? `; ${syncLine.detected} detected` : ""}.`}
    </p>}
    {summary && summary.confirmed_count > 0 && <p className="text-sm" data-flows-net>
      Recorded net flows {signedUsd(summary.recorded_net_flows_usd)} across {summary.confirmed_count} confirmed {summary.confirmed_count === 1 ? "entry" : "entries"}
      {summary.unpriced_count > 0 ? `; ${summary.unpriced_count} without a USD value are not subtracted` : ""}.
    </p>}

    {pending.length > 0 && <div className="rounded border border-amber-500/40 p-3" data-flows-pending>
      <p className="mb-2 text-sm font-medium">{summary?.note || "Unconfirmed cash movement"}</p>
      <ul className="space-y-2">
        {pending.map((row) => <li key={row.id} className="flex flex-wrap items-center gap-2 text-sm">
          <span>{when(row.ts)} · {flowKind(row) === "deposit" ? "Deposit" : "Withdrawal"} {row.amount} {row.currency} ({signedUsd(row.usd_value_at_time)})</span>
          <span className="text-xs text-[var(--color-text-muted)]">{SOURCE_LABEL[row.source]}{row.note ? ` · ${row.note}` : ""}</span>
          <span className="ml-auto flex gap-2">
            <button type="button" className={button} disabled={confirm.isPending || dismiss.isPending} onClick={() => confirm.mutate(row.id)}>
              {confirm.isPending && confirm.variables === row.id ? "Confirming…" : "Confirm"}
            </button>
            <button type="button" className={button} disabled={confirm.isPending || dismiss.isPending} onClick={() => dismiss.mutate(row.id)}>
              {dismiss.isPending && dismiss.variables === row.id ? "Dismissing…" : "Dismiss"}
            </button>
          </span>
        </li>)}
      </ul>
      <p className="mt-2 text-xs text-[var(--color-text-muted)]">Not subtracted from PnL until you confirm. Dismiss a trade you made yourself or an internal conversion.</p>
    </div>}

    {recorded.length > 0 && <table className="w-full text-left text-sm">
      <thead><tr><th>Time</th><th>Type</th><th>Amount</th><th>USD at the time</th><th>Source</th><th>Note</th><th /></tr></thead>
      <tbody>{recorded.map((row) => <tr key={row.id}>
        <td>{when(row.ts)}</td>
        <td>{flowKind(row) === "deposit" ? "Deposit" : "Withdrawal"}</td>
        <td>{row.amount} {row.currency}</td>
        <td>{signedUsd(row.usd_value_at_time)}</td>
        <td>{SOURCE_LABEL[row.source]}</td>
        <td>{row.note}</td>
        <td>{row.source === "manual"
          ? <button type="button" className={button} disabled={remove.isPending} onClick={() => remove.mutate(row.id)}>
            {remove.isPending && remove.variables === row.id ? "Deleting…" : "Delete"}
          </button>
          : <button type="button" className={button} disabled={dismiss.isPending} onClick={() => dismiss.mutate(row.id)}>
            {dismiss.isPending && dismiss.variables === row.id ? "Dismissing…" : "Dismiss"}
          </button>}
        </td>
      </tr>)}</tbody>
    </table>}
    {!flows.isLoading && !flows.isError && !rows.length && <p className="text-sm">No deposits or withdrawals recorded.</p>}

    <form className="space-y-2" aria-label="Add deposit or withdrawal" onSubmit={(event) => { event.preventDefault(); if (amount) add.mutate(); }}>
      <h3 className="text-sm font-medium">Add deposit or withdrawal</h3>
      <div className="grid gap-2 sm:grid-cols-[8rem_1fr_6rem_10rem]">
        <select className={input} aria-label="Type" value={kind} onChange={(event) => setKind(event.target.value as FlowKind)}>
          <option value="deposit">Deposit</option>
          <option value="withdrawal">Withdrawal</option>
        </select>
        <input className={input} inputMode="decimal" placeholder="Amount" aria-label="Amount" value={amountText} onChange={(event) => setAmountText(event.target.value)} />
        <input className={input} placeholder="Currency" aria-label="Currency" value={currency} onChange={(event) => setCurrency(event.target.value)} />
        <input className={input} type="date" aria-label="Date" value={date} onChange={(event) => setDate(event.target.value)} />
      </div>
      <div className="grid gap-2 sm:grid-cols-[1fr_12rem]">
        <input className={input} placeholder="Note (optional)" aria-label="Note" value={note} onChange={(event) => setNote(event.target.value)} />
        <input className={input} inputMode="decimal" placeholder="USD value (optional)" aria-label="USD value" value={usd} onChange={(event) => setUsd(event.target.value)} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className={button} disabled={!amount || !currency.trim() || add.isPending}>
          {add.isPending ? "Saving…" : "Add entry"}
        </button>
        <span className="text-xs text-[var(--color-text-muted)]">Leave the date empty for now. USD value is only needed for a currency Condor cannot price; the rate at entry time is stored.</span>
      </div>
      {amountText.trim() !== "" && !amount && <p className="text-sm text-[var(--color-red)]">Enter the amount as a positive number, without a sign or separators.</p>}
      {add.isError && <p role="alert" className="text-sm text-[var(--color-red)]">{add.error.message}</p>}
      {add.isSuccess && <p role="status" className="text-sm">Saved.</p>}
    </form>
  </section>;
}

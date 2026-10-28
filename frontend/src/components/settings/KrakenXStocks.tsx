import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useKrakenXStocksStatus, type KrakenXStocksStatus } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";

const base = "/api/v1/kraken-xstocks";
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

type SyncResult = KrakenXStocksStatus & { skipped: string[] };

/** A failure is always shown with some text: an empty message must never read as "nothing happened". */
export function failureText(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "the server gave no reason (check the browser console and the Condor log)";
}

function limitsSummary(limits: KrakenXStocksStatus["limits"]): string {
  if (!limits?.guards_apply_from) return "";
  const cap = Math.round(Number(limits.max_asset_fraction_below_threshold) * 100);
  const reserve = Math.round(Number(limits.reserve_fraction) * 100);
  return `Only Kraken xStocks pairs quoted in USD are traded, and only during the regular US session (Mon-Fri 09:30-16:00 New York). Below ${limits.guards_apply_from} USD total bot equity no single stock may exceed ${cap}% of bot equity. From ${limits.guards_apply_from} USD: ${reserve}% of bot equity stays in cash, at most ${limits.max_positions} positions and ${limits.position_fraction} of equity per position. New exposure stops at ${limits.floor} USD total bot equity.`;
}

export function KrakenXStocks() {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [synced, setSynced] = useState<SyncResult | null>(null);
  const [notice, setNotice] = useState("");
  const status = useKrakenXStocksStatus();
  const refresh = () => qc.invalidateQueries({ queryKey: ["kraken-xstocks-status"] });
  const save = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", {
      method: "PUT", body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }),
    }),
    onMutate: () => setNotice(""),
    onSuccess: () => {
      setApiKey(""); setApiSecret(""); setSynced(null);
      setNotice("API key saved. Trading is paused: sync the allocation, then enable trading.");
      refresh();
    },
    retry: false,
  });
  const disconnect = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", { method: "DELETE" }),
    onMutate: () => setNotice(""),
    onSuccess: () => { setSynced(null); setNotice("Disconnected. The API key was removed and trading is paused."); refresh(); },
    retry: false,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(base + "/enabled", { method: "PUT", body: JSON.stringify({ enabled }) }),
    onMutate: () => setNotice(""),
    onSuccess: (_next, enabled) => setNotice(enabled ? "Trading enabled within the allocation." : "Trading paused."),
    onSettled: () => refresh(),
    retry: false,
  });
  const sync = useMutation({
    mutationFn: () => apiFetch<SyncResult>(base + "/sync", { method: "POST" }),
    onMutate: () => { setNotice(""); setSynced(null); },
    onSuccess: (next) => { setSynced(next); refresh(); },
    retry: false,
  });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(base + "/reconcile", { method: "POST" }),
    onMutate: () => setNotice(""),
    onSuccess: () => setNotice("Reconciled from Kraken's order history. Trading stays paused until you enable it again."),
    onSettled: () => refresh(),
    retry: false,
  });
  const failures: [string, boolean, unknown][] = [
    ["Loading status", status.isError, status.error],
    ["Save API key", save.isError, save.error],
    ["Disconnect", disconnect.isError, disconnect.error],
    ["Enable/pause trading", toggle.isError, toggle.error],
    ["Sync allocation", sync.isError, sync.error],
    ["Reconcile", reconcile.isError, reconcile.error],
  ];
  const data = status.data;
  const ledger = data?.ledger;
  const uncertain = !!ledger?.uncertain_order;
  const held = Object.keys(ledger?.positions ?? {});
  const enabled = !!data?.enabled;

  // A disabled button gives no feedback of its own, so every disabled state says why.
  const syncBlock = !data ? "waiting for the status to load"
    : !data.connected ? "save an API key first"
    : enabled ? "pause trading first"
    : uncertain ? "reconcile the uncertain order first"
    : "";
  const toggleBlock = !data ? "waiting for the status to load"
    : !data.connected ? "save an API key first"
    : !enabled && uncertain ? "reconcile the uncertain order first"
    : !enabled && !ledger?.synced ? "sync the allocation first"
    : "";

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Kraken xStocks</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">Tokenized US stocks on Kraken, traded in USD through your Kraken API key. Claude and agent loops use this same account and its enforced allocation.</p>
    </div>
    {status.isLoading && <p role="status" className="text-sm">Loading status…</p>}
    {failures.filter(([, failed]) => failed).map(([label, , error]) =>
      <p key={label} role="alert" className="text-sm text-[var(--color-red)]">{label} failed: {failureText(error)}</p>)}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">{limitsSummary(data?.limits)}</p>
      <p className="text-sm">Account: {data?.connected ? "API key saved" : "not connected"} · Trading: {enabled ? "enabled" : "paused"}{data?.market && ` · US market: ${data.market.open ? `open (${data.market.minutes_to_close} min to the close)` : "closed"}`}</p>
      {ledger && (ledger.synced
        ? <p className="text-sm">Allocated cash: {ledger.cash} USD · Open positions: {held.length}{held.length > 0 && ` (${held.join(", ")})`}</p>
        : <p className="text-sm">The allocation has not been synced from the Kraken account yet. Sync it before enabling trading.</p>)}
      {uncertain && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check the Kraken order history before any new submission.</p>}
      {uncertain && <button className={button} disabled={reconcile.isPending} aria-busy={reconcile.isPending} onClick={() => reconcile.mutate()}>{reconcile.isPending ? "Reconciling…" : "Reconcile from Kraken order history"}</button>}
      <div className="flex flex-wrap items-center gap-2">
        <button className={button} disabled={!!syncBlock || sync.isPending} aria-busy={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? "Syncing…" : "Sync allocation from Kraken account"}</button>
        <button className={button} disabled={!!toggleBlock || toggle.isPending} aria-busy={toggle.isPending} onClick={() => toggle.mutate(!enabled)}>
          {toggle.isPending ? (enabled ? "Pausing…" : "Enabling…") : enabled ? "Pause trading" : "Enable trading within allocation"}
        </button>
      </div>
      {syncBlock && <p className="text-xs text-[var(--color-text-muted)]">Sync is unavailable: {syncBlock}.</p>}
      {toggleBlock && <p className="text-xs text-[var(--color-text-muted)]">Enabling is unavailable: {toggleBlock}.</p>}
      {synced && <p role="status" className="text-sm">Allocation synced: cash {synced.ledger.cash} USD
        {" · "}positions: {Object.entries(synced.ledger.positions).map(([symbol, position]) => `${symbol} ${position.units}`).join(", ") || "none"}
        {synced.skipped.length > 0 && <> · skipped (not an online xStock or no price): {synced.skipped.join(", ")}</>}</p>}
      <p className="text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. Account holdings outside the bot allocation are excluded. Orders are immediate-or-cancel limits, so a fast market can leave them partly filled or unfilled. The signing and order fields are verified only by the first real order: make it tiny.</p>
    </section>
    <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <h2 className="font-semibold">Connect a Kraken API key</h2>
      <p className="text-sm">On Kraken open Security → API and create a key with query-funds, query-orders and create/modify-orders permission only (no withdrawal), restricted to this server's IP address. The key and secret are stored on this Condor host and never returned to the browser or model. Saving or replacing them pauses trading.</p>
      <label className="block text-sm">API key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={input} required /></label>
      <label className="block text-sm">API secret<input type="password" autoComplete="off" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} className={input} required /></label>
      <div className="flex flex-wrap items-center gap-3">
        <button className={button} disabled={save.isPending} aria-busy={save.isPending}>{save.isPending ? "Saving…" : "Save API key"}</button>
        <button type="button" className={button} disabled={!data?.connected || disconnect.isPending} aria-busy={disconnect.isPending} onClick={() => disconnect.mutate()}>{disconnect.isPending ? "Disconnecting…" : "Disconnect"}</button>
      </div>
      {!data?.connected && <p className="text-xs text-[var(--color-text-muted)]">Disconnect is unavailable: no API key is saved.</p>}
    </form>
  </div>;
}

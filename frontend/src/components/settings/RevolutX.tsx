import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useRevolutXStatus, type RevolutXStatus } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";
import { failureText } from "@/components/settings/failureText";

const base = "/api/v1/revolut-x";
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

type SyncResult = RevolutXStatus & { skipped: string[] };

function limitsSummary(limits: RevolutXStatus["limits"]): string {
  if (!limits?.guards_apply_from) return "";
  const cap = Math.round(Number(limits.max_asset_fraction_below_threshold) * 100);
  const reserve = Math.round(Number(limits.reserve_fraction) * 100);
  return `Only Revolut X pairs quoted in EUR are traded, around the clock. Below ${limits.guards_apply_from} EUR total bot equity no single coin may exceed ${cap}% of bot equity. From ${limits.guards_apply_from} EUR: ${reserve}% of bot equity stays in cash, at most ${limits.max_positions} positions and ${limits.position_fraction} of equity per position. New exposure stops at ${limits.floor} EUR total bot equity.`;
}

function feeSummary(data: RevolutXStatus | undefined): string {
  if (!data) return "";
  const observed = data.ledger?.observed_taker_fee_pct;
  if (observed) return `Taker fee: ${observed}% (reported by the last real fill).`;
  const estimate = data.limits?.taker_fee_pct_estimate;
  return estimate
    ? `Taker fee: about ${estimate}% is an ESTIMATE (Revolut X's published rate). The real fee is known only after the first real fill.`
    : "";
}

export function RevolutX() {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [synced, setSynced] = useState<SyncResult | null>(null);
  const [notice, setNotice] = useState("");
  const status = useRevolutXStatus();
  const refresh = () => qc.invalidateQueries({ queryKey: ["revolut-x-status"] });
  const generate = useMutation({
    mutationFn: () => apiFetch<RevolutXStatus>(base + "/signing-key", { method: "POST" }),
    onMutate: () => setNotice(""),
    onSuccess: () => {
      setApiKey(""); setSynced(null);
      setNotice("Signing key created. Register the public key below in the Revolut X web app, then paste the API key it gives you. Trading is paused.");
      refresh();
    },
    retry: false,
  });
  const save = useMutation({
    mutationFn: () => apiFetch(base + "/api-key", { method: "PUT", body: JSON.stringify({ api_key: apiKey }) }),
    onMutate: () => setNotice(""),
    onSuccess: () => {
      setApiKey(""); setSynced(null);
      setNotice("API key saved. Trading is paused: sync the allocation, then enable trading.");
      refresh();
    },
    retry: false,
  });
  const disconnect = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", { method: "DELETE" }),
    onMutate: () => setNotice(""),
    onSuccess: () => { setSynced(null); setNotice("Disconnected. The signing key and API key were removed and trading is paused."); refresh(); },
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
    onSuccess: () => setNotice("Reconciled from Revolut X's order history. Trading stays paused until you enable it again."),
    onSettled: () => refresh(),
    retry: false,
  });
  const copy = useMutation({
    mutationFn: async (text: string) => {
      if (!navigator.clipboard) throw new Error("the browser blocked clipboard access; select the key and copy it by hand");
      await navigator.clipboard.writeText(text);
    },
    onMutate: () => setNotice(""),
    onSuccess: () => setNotice("Public key copied."),
    retry: false,
  });
  const failures: [string, boolean, unknown][] = [
    ["Loading status", status.isError, status.error],
    ["Generate signing key", generate.isError, generate.error],
    ["Save API key", save.isError, save.error],
    ["Disconnect", disconnect.isError, disconnect.error],
    ["Enable/pause trading", toggle.isError, toggle.error],
    ["Sync allocation", sync.isError, sync.error],
    ["Reconcile", reconcile.isError, reconcile.error],
    ["Copy public key", copy.isError, copy.error],
  ];
  const data = status.data;
  const ledger = data?.ledger;
  const uncertain = !!ledger?.uncertain_order;
  const held = Object.keys(ledger?.positions ?? {});
  const enabled = !!data?.enabled;
  const publicKey = data?.public_key ?? null;

  // A disabled button gives no feedback of its own, so every disabled state says why.
  const generateBlock = !data ? "waiting for the status to load"
    : uncertain ? "reconcile the uncertain order first"
    : "";
  const saveBlock = !data ? "waiting for the status to load"
    : !data.has_signing_key ? "generate a signing key first"
    : "";
  const syncBlock = !data ? "waiting for the status to load"
    : !data.connected ? "generate a signing key and save the API key first"
    : enabled ? "pause trading first"
    : uncertain ? "reconcile the uncertain order first"
    : "";
  const toggleBlock = !data ? "waiting for the status to load"
    : !data.connected ? "generate a signing key and save the API key first"
    : !enabled && uncertain ? "reconcile the uncertain order first"
    : !enabled && !ledger?.synced ? "sync the allocation first"
    : "";
  const account = !data ? "" : data.connected ? "connected" : data.has_signing_key ? "signing key created, API key missing" : "not connected";

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Revolut X</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">Crypto on the Revolut X exchange, traded in EUR through an API key whose signing key Condor creates on this server. Claude and agent loops use this same account and its enforced allocation.</p>
      <a className="mt-1 inline-block text-sm underline" href="/trade?workspace=revolut-x">View Revolut X on the Trade page</a>
    </div>
    {status.isLoading && <p role="status" className="text-sm">Loading status…</p>}
    {failures.filter(([, failed]) => failed).map(([label, , error]) =>
      <p key={label} role="alert" className="text-sm text-[var(--color-red)]">{label} failed: {failureText(error)}</p>)}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">{limitsSummary(data?.limits)}</p>
      <p className="text-sm">Account: {account} · Trading: {enabled ? "enabled" : "paused"}</p>
      {data && <p className="text-xs text-[var(--color-text-muted)]">{feeSummary(data)}</p>}
      {ledger && (ledger.synced
        ? <p className="text-sm">Allocated cash: {ledger.cash} EUR · Open positions: {held.length}{held.length > 0 && ` (${held.join(", ")})`}</p>
        : <p className="text-sm">The allocation has not been synced from the Revolut X account yet. Sync it before enabling trading.</p>)}
      {uncertain && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check the Revolut X order history before any new submission.</p>}
      {uncertain && <button className={button} disabled={reconcile.isPending} aria-busy={reconcile.isPending} onClick={() => reconcile.mutate()}>{reconcile.isPending ? "Reconciling…" : "Reconcile from Revolut X order history"}</button>}
      <div className="flex flex-wrap items-center gap-2">
        <button className={button} disabled={!!syncBlock || sync.isPending} aria-busy={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? "Syncing…" : "Sync allocation from Revolut X account"}</button>
        <button className={button} disabled={!!toggleBlock || toggle.isPending} aria-busy={toggle.isPending} onClick={() => toggle.mutate(!enabled)}>
          {toggle.isPending ? (enabled ? "Pausing…" : "Enabling…") : enabled ? "Pause trading" : "Enable trading within allocation"}
        </button>
      </div>
      {syncBlock && <p className="text-xs text-[var(--color-text-muted)]">Sync is unavailable: {syncBlock}.</p>}
      {toggleBlock && <p className="text-xs text-[var(--color-text-muted)]">Enabling is unavailable: {toggleBlock}.</p>}
      {synced && <p role="status" className="text-sm">Allocation synced: cash {synced.ledger.cash} EUR
        {" · "}positions: {Object.entries(synced.ledger.positions).map(([symbol, position]) => `${symbol} ${position.units}`).join(", ") || "none"}
        {synced.skipped.length > 0 && <> · skipped (no EUR pair or no price): {synced.skipped.join(", ")}</>}</p>}
      <p className="text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. Account holdings outside the bot allocation are excluded. Orders are immediate-or-cancel limits, so a fast market can leave them partly filled or unfilled. The signing, order and fee fields are verified only by the first real order: make it tiny.</p>
    </section>
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">1. Signing key</h2>
      <p className="text-sm">Condor creates an Ed25519 key pair on this server. The private key stays on this host (it is never shown, returned to the browser or given to a model); only the public key below leaves it. Replacing the key removes the saved API key and pauses trading.</p>
      <button className={button} disabled={!!generateBlock || generate.isPending} aria-busy={generate.isPending} onClick={() => generate.mutate()}>
        {generate.isPending ? "Generating…" : data?.has_signing_key ? "Replace signing key" : "Generate signing key"}
      </button>
      {generateBlock && <p className="text-xs text-[var(--color-text-muted)]">Generating is unavailable: {generateBlock}.</p>}
      {publicKey && <div className="space-y-2">
        <p className="text-sm">In the Revolut X web app open <strong>API keys</strong>, add a key and paste this public key. Revolut X then shows the 64-character API key.</p>
        <pre aria-label="Public key" className="overflow-x-auto rounded border border-[var(--color-border)] p-2 text-xs">{publicKey}</pre>
        <button className={button} disabled={copy.isPending} aria-busy={copy.isPending} onClick={() => copy.mutate(publicKey)}>{copy.isPending ? "Copying…" : "Copy public key"}</button>
      </div>}
    </section>
    <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <h2 className="font-semibold">2. API key</h2>
      <p className="text-sm">Paste the API key Revolut X gave you. Give it trading access without any withdrawal right, restricted to this server's IP address if Revolut X offers that. It is stored on this Condor host and never returned to the browser or model. Saving a key pauses trading.</p>
      <label className="block text-sm">API key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={input} required /></label>
      <div className="flex flex-wrap items-center gap-3">
        <button className={button} disabled={!!saveBlock || save.isPending} aria-busy={save.isPending}>{save.isPending ? "Saving…" : "Save API key"}</button>
        <button type="button" className={button} disabled={(!data?.has_signing_key && !data?.has_api_key) || disconnect.isPending} aria-busy={disconnect.isPending} onClick={() => disconnect.mutate()}>{disconnect.isPending ? "Disconnecting…" : "Disconnect"}</button>
      </div>
      {saveBlock && <p className="text-xs text-[var(--color-text-muted)]">Saving is unavailable: {saveBlock}.</p>}
      {!data?.has_signing_key && !data?.has_api_key && <p className="text-xs text-[var(--color-text-muted)]">Disconnect is unavailable: nothing is saved.</p>}
    </form>
  </div>;
}

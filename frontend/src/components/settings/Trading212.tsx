import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useTrading212Status, type Trading212Status } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";
import { failureText } from "@/components/settings/failureText";

const base = "/api/v1/trading212";
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

type SyncResult = Trading212Status & { skipped: string[] };

function limitsSummary(limits: Trading212Status["limits"]): string {
  if (!limits?.guards_apply_from) return "";
  const cap = Math.round(Number(limits.max_asset_fraction_below_threshold) * 100);
  const reserve = Math.round(Number(limits.reserve_fraction) * 100);
  return `Only US stocks listed on Trading 212 are traded, in a EUR account, and only during the regular US session (Mon-Fri 09:30-16:00 New York). Below ${limits.guards_apply_from} EUR total bot equity no single stock may exceed ${cap}% of bot equity. From ${limits.guards_apply_from} EUR: ${reserve}% of bot equity stays in cash, at most ${limits.max_positions} positions and ${limits.position_fraction} of equity per position. New exposure stops at ${limits.floor} EUR total bot equity.`;
}

function costSummary(limits: Trading212Status["limits"]): string {
  if (!limits?.fx_fee_pct_each_way) return "";
  const guard = limits.max_buy_round_trip_pct ? ` A buy whose estimated round trip is above ${limits.max_buy_round_trip_pct}% is refused.` : "";
  const minimum = limits.min_order_eur ? ` Minimum order ${limits.min_order_eur} EUR.` : "";
  return `Costs: no commission, but a ${limits.fx_fee_pct_each_way}% currency-conversion fee each way on US stocks (Trading 212's published figure; spread and slippage are ESTIMATES until real fills show them).${guard}${minimum}`;
}

export function Trading212() {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [synced, setSynced] = useState<SyncResult | null>(null);
  const [notice, setNotice] = useState("");
  const [excludedText, setExcludedText] = useState<string | null>(null);
  const status = useTrading212Status();
  const refresh = () => qc.invalidateQueries({ queryKey: ["trading212-status"] });
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
    onSuccess: () => { setSynced(null); setNotice("Disconnected. The API credentials were removed and trading is paused."); refresh(); },
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
    onSuccess: () => setNotice("Reconciled from Trading 212's pending orders and order history. Trading stays paused until you enable it again."),
    onSettled: () => refresh(),
    retry: false,
  });
  const exclude = useMutation({
    mutationFn: (tickers: string[]) => apiFetch(base + "/excluded", { method: "PUT", body: JSON.stringify({ tickers }) }),
    onMutate: () => setNotice(""),
    onSuccess: () => {
      setExcludedText(null);
      setNotice("Exclusion list saved. Trading is paused: sync the allocation, then enable trading.");
      refresh();
    },
    retry: false,
  });
  const failures: [string, boolean, unknown][] = [
    ["Loading status", status.isError, status.error],
    ["Save API key", save.isError, save.error],
    ["Disconnect", disconnect.isError, disconnect.error],
    ["Enable/pause trading", toggle.isError, toggle.error],
    ["Sync allocation", sync.isError, sync.error],
    ["Reconcile", reconcile.isError, reconcile.error],
    ["Save exclusions", exclude.isError, exclude.error],
  ];
  const data = status.data;
  const ledger = data?.ledger;
  const uncertain = !!ledger?.uncertain_order;
  const held = Object.keys(ledger?.positions ?? {});
  const enabled = !!data?.enabled;
  const excluded = data?.excluded_tickers ?? ledger?.excluded_tickers ?? [];
  const check = data?.account_check;
  const outside = (check?.outside_allocation ?? []).map((row) => `${row.symbol} ${row.units}${row.excluded ? " (excluded)" : ""}`);
  const currency = data?.account_currency ?? ledger?.account_currency ?? null;
  const wrongCurrency = !!currency && currency.toUpperCase() !== "EUR";

  // A disabled button gives no feedback of its own, so every disabled state says why.
  const syncBlock = !data ? "waiting for the status to load"
    : !data.connected ? "save an API key first"
    : enabled ? "pause trading first"
    : uncertain ? "reconcile the uncertain order first"
    : "";
  const toggleBlock = !data ? "waiting for the status to load"
    : !data.connected ? "save an API key first"
    : !enabled && wrongCurrency ? "the account currency is not EUR"
    : !enabled && uncertain ? "reconcile the uncertain order first"
    : !enabled && !ledger?.synced ? "sync the allocation first"
    : "";

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Trading 212</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">US stocks on your live Trading 212 Invest account (EUR), traded through your Trading 212 API key. Claude and agent loops use this same account and its enforced allocation. There is no demo mode: every order is real money.</p>
      <a className="mt-1 inline-block text-sm underline" href="/trade?workspace=trading212">View Trading 212 on the Trade page</a>
    </div>
    {status.isLoading && <p role="status" className="text-sm">Loading status…</p>}
    {failures.filter(([, failed]) => failed).map(([label, , error]) =>
      <p key={label} role="alert" className="text-sm text-[var(--color-red)]">{label} failed: {failureText(error)}</p>)}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">{limitsSummary(data?.limits)}</p>
      <p className="text-sm">{costSummary(data?.limits)}</p>
      <p className="text-sm">Account: {data?.connected ? "API key saved" : "not connected"} · Trading: {enabled ? "enabled" : "paused"}{data?.market && ` · US market: ${data.market.open ? `open (${data.market.minutes_to_close} min to the close)` : "closed"}`}</p>
      <p className="text-sm">Account currency: {currency ?? "not read yet (sync the allocation to read it)"}</p>
      {wrongCurrency && <p role="alert" className="text-sm text-[var(--color-red)]">The Trading 212 account currency is {currency}, not EUR. This module refuses to trade a non-EUR account because its ledger and cost model are in euros.</p>}
      {ledger && (ledger.synced
        ? <p className="text-sm">Allocated cash: {ledger.cash} EUR · Open positions: {held.length}{held.length > 0 && ` (${held.join(", ")})`}</p>
        : <p className="text-sm">The allocation has not been synced from the Trading 212 account yet. Sync it before enabling trading.</p>)}
      {check && !check.ledger_matches_account && <p role="alert" className="text-sm text-[var(--color-red)]">The bot ledger and the Trading 212 account disagree ({check.mismatch.map((m) => m.message).join("; ")}). Trading cannot be enabled until you pause trading and sync the allocation.</p>}
      {outside.length > 0 && <p className="text-xs text-[var(--color-text-muted)]">Your own holdings outside the bot allocation (never traded by the bot): {outside.join(", ")}.</p>}
      {uncertain && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check the Trading 212 order history before any new submission.</p>}
      {uncertain && <button className={button} disabled={reconcile.isPending} aria-busy={reconcile.isPending} onClick={() => reconcile.mutate()}>{reconcile.isPending ? "Reconciling…" : "Reconcile from Trading 212 order history"}</button>}
      <div className="flex flex-wrap items-center gap-2">
        <button className={button} disabled={!!syncBlock || sync.isPending} aria-busy={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? "Syncing…" : "Sync allocation from Trading 212 account"}</button>
        <button className={button} disabled={!!toggleBlock || toggle.isPending} aria-busy={toggle.isPending} onClick={() => toggle.mutate(!enabled)}>
          {toggle.isPending ? (enabled ? "Pausing…" : "Enabling…") : enabled ? "Pause trading" : "Enable trading within allocation"}
        </button>
      </div>
      {syncBlock && <p className="text-xs text-[var(--color-text-muted)]">Sync is unavailable: {syncBlock}.</p>}
      {toggleBlock && <p className="text-xs text-[var(--color-text-muted)]">Enabling is unavailable: {toggleBlock}.</p>}
      {synced && <p role="status" className="text-sm">Allocation synced: cash {synced.ledger.cash} EUR
        {" · "}positions: {Object.entries(synced.ledger.positions).map(([symbol, position]) => `${symbol} ${position.units}`).join(", ") || "none"}
        {synced.skipped.length > 0 && <> · skipped (not tradable here or no price): {synced.skipped.join(", ")}</>}</p>}
      <p className="text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. Account holdings outside the bot allocation are excluded. Trading 212 has no quote endpoint, so prices the agents see are reference prices from Kraken xStocks public data; real fills and fees come from Trading 212. Authenticated behaviour (order body, limit vs market, error and rate-limit headers, fees) is not yet verified against a real account: the first real order must be tiny and approved by you.</p>
    </section>
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Holdings kept out of the bot</h2>
      <p className="text-sm">Stocks you hold yourself in this account (for example one you bought by hand) that the bot must never trade. Syncing adopts every other position into the bot allocation; the symbols listed here are skipped, and the order tool refuses them. Changing the list pauses trading: sync the allocation, then enable trading again.</p>
      <label className="block text-sm">Excluded stock symbols, separated by commas
        <input value={excludedText ?? excluded.join(", ")} onChange={(event) => setExcludedText(event.target.value)} className={input} placeholder="for example MRNA" /></label>
      <button className={button} disabled={exclude.isPending || excludedText === null} aria-busy={exclude.isPending}
        onClick={() => exclude.mutate((excludedText ?? "").split(/[\s,]+/).filter(Boolean))}>{exclude.isPending ? "Saving…" : "Save exclusions"}</button>
      {excludedText === null && <p className="text-xs text-[var(--color-text-muted)]">Save exclusions is unavailable: change the list first.</p>}
    </section>
    <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <h2 className="font-semibold">Connect a Trading 212 API key</h2>
      <p className="text-sm">In the Trading 212 app open Settings → API and create a key for the live Invest account with the minimum permissions: account data, portfolio, orders read and orders execute, plus history orders and history transactions (deposit and withdrawal tracking) and metadata (the instrument list). The Trading 212 API has no withdrawal endpoint and no withdrawal permission (its scopes are account, portfolio, metadata, orders read, orders execute, history orders, history transactions, history dividends and pies), so none can be granted. Optionally restrict the key to this server's IP address. The app shows the key and secret once: enter them here only, never in chat. They are stored on this Condor host and never returned to the browser or model. Saving or replacing them pauses trading.</p>
      <label className="block text-sm">API key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={input} required /></label>
      <label className="block text-sm">API secret<input type="password" autoComplete="off" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} className={input} placeholder="Leave empty only for a legacy key without a secret" /></label>
      <p className="text-xs text-[var(--color-text-muted)]">Current keys come as a key plus a secret (HTTP Basic). An empty secret means a legacy single key sent on its own.{data?.connected && !data.has_secret ? " The saved credentials have no secret." : ""}</p>
      <div className="flex flex-wrap items-center gap-3">
        <button className={button} disabled={save.isPending} aria-busy={save.isPending}>{save.isPending ? "Saving…" : "Save API key"}</button>
        <button type="button" className={button} disabled={!data?.connected || disconnect.isPending} aria-busy={disconnect.isPending} onClick={() => disconnect.mutate()}>{disconnect.isPending ? "Disconnecting…" : "Disconnect"}</button>
      </div>
      {!data?.connected && <p className="text-xs text-[var(--color-text-muted)]">Disconnect is unavailable: no API key is saved.</p>}
    </form>
  </div>;
}

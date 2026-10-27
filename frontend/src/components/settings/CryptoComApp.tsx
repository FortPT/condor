import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type Status = {
  connected: boolean;
  enabled: boolean;
  ledger: {
    cash_eur: string;
    positions: Record<string, { units: string; cost_eur: string }>;
    uncertain_order: unknown;
  };
};
type Quote = {
  id: string;
  countdown: number;
  from_amount: { amount: string; currency: string };
  to_amount: { amount: string; currency: string };
};
const base = "/api/v1/crypto-com";
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

export function CryptoComApp({ settings = false }: { settings?: boolean }) {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [kind, setKind] = useState("purchase");
  const [source, setSource] = useState("EUR");
  const [target, setTarget] = useState("BTC");
  const [amount, setAmount] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [expiresAt, setExpiresAt] = useState(0);
  const [result, setResult] = useState<unknown>(null);
  const status = useQuery({ queryKey: ["crypto-com-status"], queryFn: () => apiFetch<Status>(base + "/status"), refetchInterval: 10000, retry: false });
  const refresh = () => qc.invalidateQueries({ queryKey: ["crypto-com-status"] });
  const save = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", { method: "PUT", body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }) }),
    onSuccess: () => { setApiKey(""); setApiSecret(""); setQuote(null); refresh(); },
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(base + "/enabled", { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSuccess: () => { setQuote(null); refresh(); },
  });
  const inspect = useMutation({
    mutationFn: (path: string) => apiFetch(base + path),
    onSuccess: setResult,
  });
  const requestQuote = useMutation({
    mutationFn: () => apiFetch<Quote>(base + "/quote", { method: "POST", body: JSON.stringify({ kind, source, target, amount }) }),
    onSuccess: (next) => { setQuote(next); setExpiresAt(Date.now() + next.countdown * 1000); },
  });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(base + "/reconcile", { method: "POST" }),
    onSuccess: () => refresh(),
    retry: false,
  });
  const execute = useMutation({
    mutationFn: (quotationId: string) => apiFetch(base + "/execute", { method: "POST", body: JSON.stringify({ quotation_id: quotationId }) }),
    onSuccess: (next) => { setResult(next); refresh(); },
    onSettled: () => setQuote(null),
    retry: false,
  });
  const error = status.error || save.error || toggle.error || inspect.error || requestQuote.error || execute.error || reconcile.error;
  const data = status.data;

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Crypto.com App</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">Spot trading through your App Agent Key. Claude and agent loops use this same account and its enforced allocation.</p>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">Starts with €30. Keeps half of current bot equity in cash, up to three positions and one sixth of equity per position (€5 initially). New exposure stops at €2 total bot equity. Profits can increase position sizes.</p>
      <p className="text-sm">Account: {data?.connected ? "Agent Key saved" : "not connected"} · Trading: {data?.enabled ? "enabled" : "paused"}</p>
      {data?.ledger && <p className="text-sm">Allocated cash: €{data.ledger.cash_eur} · Open positions: {Object.keys(data.ledger.positions).length}</p>}
      {!!data?.ledger.uncertain_order && <p role="alert" className="text-sm text-[var(--color-red)]">An order needs reconciliation. Trading stays paused. Check Crypto.com history before any new submission.</p>}
      {!!data?.ledger.uncertain_order && <button className={button} disabled={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile from provider history</button>}
      <div className="flex flex-wrap gap-2">
        <button className={button} disabled={!data?.connected || toggle.isPending || !!data?.ledger.uncertain_order} onClick={() => toggle.mutate(!data?.enabled)}>{data?.enabled ? "Pause trading" : "Enable trading within allocation"}</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/accounts")}>Check connection & balances</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/history")}>Transaction history</button>
        <button className={button} disabled={!data?.connected || inspect.isPending} onClick={() => inspect.mutate("/valuation")}>Value bot allocation</button>
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">Enabling execution does not start a loop or place an order. Account holdings outside the bot allocation are excluded. A stop is not a guaranteed exit price.</p>
    </section>
    {settings ? <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <h2 className="font-semibold">Connect an Agent Key</h2>
      <p className="text-sm">In the Crypto.com App, open More → Agent Key. Enable Execute trades and choose the provider's limits. Enter the key and secret here; they are stored on this Condor host and never returned to the browser or model.</p>
      <label className="block text-sm">Agent Key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={input} required /></label>
      <label className="block text-sm">Agent Secret<input type="password" autoComplete="off" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} className={input} required /></label>
      <button className={button} disabled={save.isPending}>Save Agent Key</button>
      <Link className="ml-3 text-sm underline" to="/crypto-com">Open trading</Link>
    </form> : <>
      {!data?.connected && <Link className="text-sm underline" to="/settings?tab=crypto-com">Connect Crypto.com App</Link>}
      <form className="space-y-3 rounded-lg border border-[var(--color-border)] p-4" onChange={() => setQuote(null)} onSubmit={(event) => { event.preventDefault(); setQuote(null); requestQuote.mutate(); }}>
        <h2 className="font-semibold">Quote an order</h2>
        <label className="block text-sm">Operation<select className={input} value={kind} onChange={(event) => setKind(event.target.value)}><option value="purchase">Buy with cash</option><option value="sale">Sell to cash</option><option value="exchange">Exchange crypto</option></select></label>
        <div className="grid grid-cols-2 gap-3">
          <label className="text-sm">From<input className={input} value={source} onChange={(event) => setSource(event.target.value.toUpperCase())} required /></label>
          <label className="text-sm">To<input className={input} value={target} onChange={(event) => setTarget(event.target.value.toUpperCase())} required /></label>
        </div>
        <label className="block text-sm">Amount to spend ({source})<input className={input} value={amount} inputMode="decimal" onChange={(event) => setAmount(event.target.value)} required /></label>
        <button className={button} disabled={!data?.connected || requestQuote.isPending || execute.isPending}>Get quotation</button>
      </form>
      {quote && <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
        <p>Spend {quote.from_amount.amount} {quote.from_amount.currency} → receive {quote.to_amount.amount} {quote.to_amount.currency}</p>
        <p className="text-sm">Quotation expires after {quote.countdown} seconds. Expired quotes are rejected by the server.</p>
        <button className={button} disabled={!data?.enabled || execute.isPending} onClick={() => {
          if (Date.now() >= expiresAt) { setQuote(null); return; }
          execute.mutate(quote.id);
        }}>Confirm & submit order</button>
      </section>}
    </>}
    {result !== null && <pre aria-label="Crypto.com result" className="max-h-96 overflow-auto rounded-lg border border-[var(--color-border)] p-4 text-xs">{JSON.stringify(result, null, 2)}</pre>}
  </div>;
}

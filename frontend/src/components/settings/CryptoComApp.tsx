import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { limitsSummary } from "@/components/settings/cryptoComLimits";
import { useCryptoComStatus } from "@/hooks/useCredentials";
import { apiFetch } from "@/lib/api";

const base = "/api/v1/crypto-com";
const input = "w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-sm";
const button = "rounded border border-[var(--color-border)] px-3 py-2 text-sm disabled:opacity-40";

export function CryptoComApp({ settings = false }: { settings?: boolean }) {
  const qc = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const status = useCryptoComStatus();
  const refresh = () => qc.invalidateQueries({ queryKey: ["crypto-com-status"] });
  const save = useMutation({
    mutationFn: () => apiFetch(base + "/credentials", { method: "PUT", body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }) }),
    onSuccess: () => { setApiKey(""); setApiSecret(""); refresh(); },
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(base + "/enabled", { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSuccess: () => refresh(),
  });
  const inspect = useMutation({
    mutationFn: (path: string) => apiFetch(base + path),
    onSuccess: setResult,
  });
  const reconcile = useMutation({
    mutationFn: () => apiFetch(base + "/reconcile", { method: "POST" }),
    onSuccess: () => refresh(),
    retry: false,
  });
  const error = status.error || save.error || toggle.error || inspect.error || reconcile.error;
  const data = status.data;

  return <div className="mx-auto max-w-3xl space-y-5">
    <div>
      <h1 className="text-xl font-semibold">Crypto.com App</h1>
      <p className="mt-1 text-sm text-[var(--color-text-muted)]">Spot trading through your App Agent Key. Claude and agent loops use this same account and its enforced allocation.</p>
    </div>
    {error && <p role="alert" className="text-sm text-[var(--color-red)]">{error.message}</p>}
    <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4">
      <h2 className="font-semibold">Bot allocation</h2>
      <p className="text-sm">{limitsSummary(data?.limits)}</p>
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
      <Link className="ml-3 text-sm underline" to="/trade?workspace=app">Open trading</Link>
    </form> : !data?.connected && <Link className="text-sm underline" to="/settings?tab=crypto-com">Connect Crypto.com App</Link>}
    {result !== null && <AppResult value={result} />}
  </div>;
}

type Money = { amount?: string; currency?: string };
type Wallet = { currency: string; amount?: Money; balance?: Money; available?: Money };
type Transaction = { id: string; status?: string; from_amount?: Money; to_amount?: Money };

function money(value?: Money) {
  return value?.amount === undefined ? "—" : `${value.amount} ${value.currency ?? ""}`.trim();
}

export function AppResult({ value }: { value: unknown }) {
  const data = value as {
    equity_eur?: string; new_position_limit_eur?: string; minimum_cash_reserve_eur?: string;
    cash?: Wallet[]; crypto?: Wallet[]; transaction?: Transaction;
  };
  if (data.equity_eur !== undefined) return <section className="rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="mb-2 font-semibold">Bot allocation at current exit quotations</h2>
    <p>Total: €{data.equity_eur}</p>
    <p>Cash reserve: €{data.minimum_cash_reserve_eur}</p>
    <p>Maximum new position: €{data.new_position_limit_eur}</p>
  </section>;
  if (data.cash || data.crypto) return <section className="space-y-3 rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="font-semibold">Account balances</h2>
    <p className="text-xs text-[var(--color-text-muted)]">Whole account holdings; only the bot allocation may be traded.</p>
    {[{ name: "Cash", rows: data.cash ?? [] }, { name: "Crypto", rows: data.crypto ?? [] }].map(({ name, rows }) => <div key={name}>
      <h3 className="mb-1 font-medium">{name}</h3>
      {rows.length ? <div className="max-h-64 overflow-auto"><table className="w-full text-left"><thead><tr><th>Currency</th><th>Balance</th><th>Available</th></tr></thead><tbody>
        {rows.map((row, index) => <tr key={`${row.currency}-${index}`}><td>{row.currency}</td><td>{money(row.amount ?? row.balance)}</td><td>{money(row.available ?? row.amount)}</td></tr>)}
      </tbody></table></div> : <p>No holdings</p>}
    </div>)}
  </section>;
  const transactions: Transaction[] = Array.isArray(value) ? value : data.transaction ? [data.transaction] : [];
  return <section className="overflow-auto rounded-lg border border-[var(--color-border)] p-4 text-sm">
    <h2 className="mb-2 font-semibold">Transactions</h2>
    {transactions.length ? <table className="w-full text-left"><thead><tr><th>Reference</th><th>Status</th><th>From</th><th>To</th></tr></thead><tbody>
      {transactions.map((transaction) => <tr key={transaction.id}><td>{transaction.id}</td><td>{transaction.status ?? "Not reported"}</td><td>{money(transaction.from_amount)}</td><td>{money(transaction.to_amount)}</td></tr>)}
    </tbody></table> : <p>No recent transactions</p>}
  </section>;
}

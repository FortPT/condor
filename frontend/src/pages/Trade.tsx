import { Navigate, useSearchParams } from "react-router-dom";

import { ConnectKeysOverlay } from "@/components/ConnectKeysOverlay";
import { CryptoComApp } from "@/components/settings/CryptoComApp";
import { useCredentials, useCryptoComStatus } from "@/hooks/useCredentials";
import { useServer } from "@/hooks/useServer";
import { CreateExecutor } from "@/pages/CreateExecutor";

export function Trade() {
  const [params, setParams] = useSearchParams();
  const { server } = useServer();
  const { hasKeys, credentials, isLoading: keysLoading } = useCredentials();
  const status = useCryptoComStatus();
  const workspace = params.get("workspace");
  const explicitExchange = workspace === "hummingbot" || workspace === "exchange" ||
    params.has("type") || params.has("connector") || params.has("pair");
  const appWorkspace = workspace === "app" ||
    (!explicitExchange && !!status.data?.connected && credentials.length === 0);
  const loading = !workspace && !explicitExchange && (keysLoading || status.isLoading);

  // CreateExecutor consumes connector/pair; retain the account choice afterward.
  if (!workspace && !params.has("type") && (params.has("connector") || params.has("pair"))) {
    const nextParams = new URLSearchParams(params);
    nextParams.set("workspace", "hummingbot");
    return <Navigate to={{ pathname: "/trade", search: `?${nextParams}` }} replace />;
  }

  function selectWorkspace(next: "app" | "hummingbot") {
    const nextParams = new URLSearchParams(params);
    nextParams.set("workspace", next);
    if (next === "app") {
      nextParams.delete("type");
      nextParams.delete("connector");
      nextParams.delete("pair");
    }
    setParams(nextParams);
  }

  return <div className="-m-6 flex h-[calc(100%+3rem)] flex-col">
    <div className="flex shrink-0 gap-2 border-b border-[var(--color-border)] p-3" role="group" aria-label="Trading account">
      <button className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && appWorkspace} onClick={() => selectWorkspace("app")}>Crypto.com App</button>
      <button className="rounded border border-[var(--color-border)] px-3 py-2 text-sm aria-pressed:bg-[var(--color-surface-hover)]" aria-pressed={!loading && !appWorkspace} onClick={() => selectWorkspace("hummingbot")}>Hummingbot / Exchange</button>
    </div>
    {loading ? <p role="status" className="p-6 text-sm">Loading trading accounts…</p> :
      appWorkspace ? <div className="min-h-0 flex-1 overflow-auto p-6"><CryptoComApp /></div> :
        <div className="relative min-h-0 flex-1 p-6">
          <CreateExecutor />
          {server && !keysLoading && !hasKeys && <ConnectKeysOverlay />}
        </div>}
  </div>;
}

import type { ConnectorBalance } from "@/lib/api";

/**
 * The connectors that hold real money. A simulated (paper) account is listed on the
 * Portfolio page, but it is never part of the total, the token count, the top holdings or
 * any allocation share: the server leaves it out of `total_usd` and so must the page.
 */
export function realConnectors(connectors: ConnectorBalance[]): ConnectorBalance[] {
  return connectors.filter((connector) => !connector.paper);
}

/** A connector's share (percent) of the real total; a simulated account has none. */
export function realShare(connector: ConnectorBalance, totalUsd: number): number {
  if (connector.paper || totalUsd <= 0) return 0;
  return (connector.total_usd / totalUsd) * 100;
}

/** The simulated (paper) connectors, listed apart and never counted. */
export function paperConnectors(connectors: ConnectorBalance[]): ConnectorBalance[] {
  return connectors.filter((connector) => connector.paper);
}

/**
 * What is held per token, summed over the real connectors only (a simulated
 * account's balances are not holdings). Zero and negative values are left out.
 */
export function realTokenTotals(connectors: ConnectorBalance[]): Map<string, number> {
  const totals = new Map<string, number>();
  for (const connector of realConnectors(connectors)) {
    for (const balance of connector.balances ?? []) {
      const usd = balance.usd_value;
      if (!balance.token || !Number.isFinite(usd) || usd <= 0) continue;
      totals.set(balance.token, (totals.get(balance.token) ?? 0) + usd);
    }
  }
  return totals;
}

/** How many token balances the real connectors report (the "assets" count). */
export function realTokenCount(connectors: ConnectorBalance[]): number {
  return realConnectors(connectors).reduce((n, c) => n + (c.balances?.length ?? 0), 0);
}

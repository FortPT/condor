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

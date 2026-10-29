/**
 * Top Holdings: what is held per token, summed over every connector.
 *
 * The same asset can sit on several connectors (Kraken USD and Crypto.com USD cash are two
 * accounts but one holding), so listing the rows as they come printed "USD" twice. The Assets
 * table keeps one row per connector; this is the by-token view.
 */
export type HoldingSource = {
  connector: string;
  balances: { token: string; usd_value: number }[];
};

export type TokenHolding = { token: string; usd_value: number; connector: string };

export function topHoldings(connectors: HoldingSource[], limit = 8): TokenHolding[] {
  const byToken = new Map<string, { usd: number; venues: string[] }>();
  for (const source of connectors) {
    for (const balance of source.balances) {
      const entry = byToken.get(balance.token) ?? { usd: 0, venues: [] };
      entry.usd += Number.isFinite(balance.usd_value) ? balance.usd_value : 0;
      if (!entry.venues.includes(source.connector)) entry.venues.push(source.connector);
      byToken.set(balance.token, entry);
    }
  }
  return [...byToken.entries()]
    .map(([token, entry]) => ({ token, usd_value: entry.usd, connector: entry.venues.join(", ") }))
    .sort((a, b) => b.usd_value - a.usd_value)
    .slice(0, limit);
}

def edit(path, pairs):
    t = open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in t else "\n"
    t = t.replace("\r\n", "\n")
    for old, new in pairs:
        assert t.count(old) == 1, (path, t.count(old), old[:90])
        t = t.replace(old, new)
    open(path, "w", encoding="utf-8", newline="").write(t.replace("\n", nl))

edit("lib/api.ts", [(
"export interface ConnectorBalance {\n  connector: string;\n  balances: BalanceItem[];\n  total_usd: number;\n  note?: string | null;\n}",
"/** A holding the owner keeps outside the bot (Trading 212): its value and result against what was paid. */\nexport interface HoldingItem {\n  token: string;\n  quantity: number;\n  value_usd: number;\n  cost_usd?: number | null;\n  gain_usd?: number | null;\n  gain_pct?: number | null;\n}\n\nexport interface ConnectorBalance {\n  connector: string;\n  balances: BalanceItem[];\n  total_usd: number;\n  note?: string | null;\n  holdings?: HoldingItem[];\n}")])

edit("pages/Portfolio.tsx", [
("      {expanded &&\n        connector.balances.map((b) => (",
"""      {expanded &&
        (connector.holdings ?? []).map((h) => (
          <tr key={`holding-${h.token}`} className="border-b border-[var(--color-border)]/30" data-holding={h.token}>
            <td colSpan={5} className="px-4 py-2 pl-10 text-xs">
              <span className="text-[var(--color-text-muted)]">Your holding {h.token}: </span>
              <span className="tabular-nums">{formatCurrency(convertFromUsd(h.value_usd), currencySymbol)}</span>
              {h.gain_usd === null || h.gain_usd === undefined ? (
                <span className="text-[var(--color-text-muted)]"> · result against cost not available</span>
              ) : (
                <>
                  <span className="text-[var(--color-text-muted)]"> · since you bought it </span>
                  <span
                    className="font-medium tabular-nums"
                    style={{ color: Math.abs(h.gain_usd) < 0.005 ? undefined : h.gain_usd > 0 ? "var(--color-green)" : "var(--color-red)" }}
                  >
                    {h.gain_usd >= 0.005 ? "+" : h.gain_usd <= -0.005 ? "−" : ""}
                    {formatCurrency(convertFromUsd(Math.abs(h.gain_usd)), currencySymbol)}
                    {h.gain_pct === null || h.gain_pct === undefined ? "" : ` (${h.gain_pct >= 0 ? "+" : "−"}${Math.abs(h.gain_pct).toFixed(1)}%)`}
                  </span>
                </>
              )}
            </td>
          </tr>
        ))}
      {expanded &&
        connector.balances.map((b) => ("""),
])

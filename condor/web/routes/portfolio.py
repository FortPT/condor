from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from condor import (
    native_accounts,
    portfolio_change,
    portfolio_chart,
    portfolio_history,
)
from condor.fetchers.portfolio import (
    PORTFOLIO_HISTORY_RANGES,
    UNIFIED_ACCOUNT_NOTE,
    balance_value,
    dedupe_unified_accounts,
)
from condor.server_data_service import ServerDataType, get_server_data_service
from condor.web.auth import get_current_user, require_server_access
from condor.web.models import (
    BalanceItem,
    ConnectorBalance,
    PortfolioHistoryChange,
    PortfolioHistoryPoint,
    PortfolioHistoryResponse,
    PortfolioResponse,
    WebUser,
)
from condor.web.routes._errors import upstream_error
from config_manager import get_config_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio"])


@router.get("/servers/{name}/portfolio", response_model=PortfolioResponse)
async def get_portfolio(
    name: str,
    refresh: bool = Query(False),
    user: WebUser = Depends(require_server_access),
):
    cm = get_config_manager()

    try:
        if refresh:
            # Bypass SDS cache — force exchange re-fetch via Hummingbot
            from condor.fetchers.portfolio import fetch_portfolio_refreshed

            client = await cm.get_client(name)
            state = await fetch_portfolio_refreshed(client)
            # Update SDS cache so subsequent non-refresh reads get fresh data
            get_server_data_service().put(name, ServerDataType.PORTFOLIO, state)
        else:
            state = await get_server_data_service().get_or_fetch(
                name, ServerDataType.PORTFOLIO
            )
    except Exception as e:
        logger.exception("Portfolio fetch failed for '%s'", name)
        raise upstream_error("Failed to get portfolio", e)

    if state is None:
        # Check if fetch is registered
        sds = get_server_data_service()
        registered = ServerDataType.PORTFOLIO in sds._fetch_registry
        logger.warning(
            "Portfolio returned None for %s (fetch registered: %s, health: %s)",
            name,
            registered,
            (
                sds.get_server_health(name).status.value
                if name in sds._health
                else "unknown"
            ),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get portfolio: server '{name}' returned no data"
            + ("" if registered else " (fetch not registered — try restarting)"),
        )

    # Parse portfolio state into structured response
    # API returns: {account_name: {connector_name: [balance_dicts]}}
    state, unified = dedupe_unified_accounts(state)
    connectors: list[ConnectorBalance] = []
    total_usd = 0.0

    if isinstance(state, dict):
        for account_name, account_data in state.items():
            if not isinstance(account_data, dict):
                continue

            for connector_name, connector_balances in account_data.items():
                if not isinstance(connector_balances, list):
                    continue

                balances: list[BalanceItem] = []
                connector_total = 0.0

                for item in connector_balances:
                    if not isinstance(item, dict):
                        continue
                    token = item.get("token", item.get("asset", ""))
                    total_bal = float(item.get("units", item.get("total_balance", 0)))
                    available = float(
                        item.get(
                            "available_units", item.get("available_balance", total_bal)
                        )
                    )
                    usd_val = balance_value(item)

                    if not token:
                        continue

                    balances.append(
                        BalanceItem(
                            token=token,
                            total=total_bal,
                            available=available,
                            usd_value=usd_val,
                        )
                    )
                    connector_total += usd_val

                # Filter out zero-value tokens and sort by value descending
                balances = [b for b in balances if b.usd_value >= 0.01]
                balances.sort(key=lambda b: b.usd_value, reverse=True)

                connectors.append(
                    ConnectorBalance(
                        connector=connector_name,
                        balances=balances,
                        total_usd=connector_total,
                        note=(
                            UNIFIED_ACCOUNT_NOTE
                            if (account_name, connector_name) in unified
                            else None
                        ),
                    )
                )

    # Hummingbot's kraken connector already values that account; adding the
    # Kraken xStocks view as well would count the same balances twice.
    skip = (
        ("kraken_xstocks",) if any(c.connector == "kraken" for c in connectors) else ()
    )
    native = await native_accounts.collect_views(user.id, skip=skip)
    connectors.extend(ConnectorBalance(**view) for view in native.views)
    notes = list(native.notes)
    total_usd = sum(c.total_usd for c in connectors)
    return PortfolioResponse(
        server=name, connectors=connectors, total_usd=total_usd, notes=notes
    )


@router.get("/portfolio/native")
async def get_native_portfolio(user: WebUser = Depends(get_current_user)):
    """Crypto.com, Kraken xStocks and Revolut X for the caller, read-only.

    What the chat agent's ``native_portfolio`` tool returns: Hummingbot cannot see
    these accounts, so its own portfolio tool reads as empty without them.
    """
    return await native_accounts.overview(user.id)


@router.get(
    "/servers/{name}/portfolio/history", response_model=PortfolioHistoryResponse
)
async def get_portfolio_history(
    name: str,
    range: str = Query("1D", pattern="^(1D|1W|1M|3M)$"),
    breakdown: bool = Query(False),
    user: WebUser = Depends(require_server_access),
):

    # The raw snapshots are the same regardless of *breakdown* (parsing happens
    # below), so the cache key is the range alone. SDS coalesces concurrent
    # reads of the same expired key into one backend fetch.
    try:
        history = await get_server_data_service().get_or_fetch(
            name, ServerDataType.PORTFOLIO_HISTORY, range_key=range
        )
    except Exception as e:
        logger.warning("Failed to get portfolio history: %s", e)
        history = None

    _, interval = PORTFOLIO_HISTORY_RANGES[range]

    logger.debug("Portfolio history response shape: %s", type(history))
    if isinstance(history, dict):
        logger.debug("Portfolio history keys: %s", list(history.keys()))

    entries, keyed = _extract_snapshot_entries(history)

    # Forward-fill: track per-connector totals so missing exchanges
    # carry forward their last known value instead of dropping to 0.
    points: list[PortfolioHistoryPoint] = []
    prev_connector_totals: dict[str, float] = {}
    # What Hummingbot's own kraken connector contributed to each point: Kraken is also
    # recorded natively, and the chart prefers that source (see below).
    kraken_parts: dict[float, float] = {}
    for ts, snapshot in entries:
        total = (
            0 if keyed else snapshot.get("total_value", snapshot.get("total_usd", 0))
        )
        if total == 0:
            # Sum token values from nested structure
            # API returns {timestamp, state: {account: {connector: [balances]}}}
            cur_totals = _extract_connector_totals(_snapshot_state(snapshot, keyed))
            if cur_totals and prev_connector_totals:
                # Forward-fill: for connectors seen before but missing now, use previous value
                for key, prev_val in prev_connector_totals.items():
                    if key not in cur_totals:
                        cur_totals[key] = prev_val
            if cur_totals:
                prev_connector_totals = cur_totals
            total = sum(cur_totals.values())
        else:
            cur_totals = _extract_connector_totals(_snapshot_state(snapshot, keyed))
        if keyed and total <= 0:
            # Dict-keyed payloads historically drop zero-total points
            continue
        points.append(PortfolioHistoryPoint(timestamp=ts, total_usd=float(total)))
        kraken_parts[ts] = sum(
            value
            for key, value in cur_totals.items()
            if key.rsplit(":", 1)[-1] == "kraken"
        )

    points.sort(key=lambda p: p.timestamp)

    top_tokens: list[str] = []
    if breakdown and points:
        top_tokens = _build_token_breakdown(entries, keyed, points)

    # Add the accounts Condor reads itself (history starts when recording began).
    # Kraken xStocks is skipped when Hummingbot's own kraken connector already
    # counts that account.
    hummingbot_counts_kraken = bool(
        entries
        and any(
            key.rsplit(":", 1)[-1] == "kraken"
            for key in _extract_connector_totals(_snapshot_state(entries[-1][1], keyed))
        )
    )
    now = time.time()
    since = now - PORTFOLIO_HISTORY_RANGES[range][0]
    interval_seconds = portfolio_history.INTERVAL_SECONDS.get(interval, 3600)
    skip = (portfolio_history.KRAKEN,) if hummingbot_counts_kraken else ()
    hummingbot_points = list(points)
    native: dict = {}
    # The series (not the current total) prefers Condor's own Kraken xStocks readings to
    # Hummingbot's kraken connector when it has them: the same account, but Hummingbot's
    # history of it has had gaps and a bad row. Other connectors are used as they are.
    connected = portfolio_chart.connected_sources(user.id)
    try:
        chart_native = portfolio_history.series(
            user.id, since=since - portfolio_chart.EDGE_MARGIN_SEC
        )
    except Exception as error:  # noqa: BLE001 - never lose the Hummingbot chart
        logger.warning("Native portfolio history skipped (%s)", type(error).__name__)
        chart_native = {}
    native_kraken = portfolio_chart.native_kraken_preferred(
        chart_native, now, connected
    )
    chart_hummingbot, chart_skip = hummingbot_points, skip
    if native_kraken:
        chart_hummingbot = portfolio_chart.without_part(hummingbot_points, kraken_parts)
        chart_skip = ()
    try:
        native = portfolio_history.series(user.id, since=since)
        merge_points, merge_skip = points, skip
        if native_kraken:
            # Hummingbot's kraken stands in until Condor's own reading begins.
            first, _ = portfolio_history.bounds(user.id, portfolio_history.KRAKEN)
            native_from = float(first[0]) if first else since
            merge_points = portfolio_chart.without_part(
                points, kraken_parts, from_ts=native_from
            )
            merge_skip = ()
        points = portfolio_history.merge(
            merge_points, native, interval_seconds, skip=merge_skip
        )
    except Exception as error:  # noqa: BLE001 - never lose the Hummingbot chart
        logger.warning("Native portfolio history skipped (%s)", type(error).__name__)

    # The merged series starts when its youngest source does and holds deposits, so
    # its first and last point are not a gain: state the change only when every
    # source can be read at the window start and its flows are known.
    try:
        change = PortfolioHistoryChange(
            **portfolio_change.compute(
                user.id,
                hummingbot_points,
                native,
                since,
                interval_seconds,
                skip=skip,
                now=now,
            )
        )
    except Exception as error:  # noqa: BLE001 - never lose the chart over a figure
        logger.warning("Portfolio change skipped (%s)", type(error).__name__)
        change = PortfolioHistoryChange(**portfolio_change.unavailable("error"))

    # The series the chart draws: one baseline where every account is recording, gaps
    # left as breaks, deposit markers and the net-of-flows result. Additive: a failure
    # leaves ``chart_points`` empty and the page falls back to ``points``.
    chart: dict[str, Any] = {}
    try:
        chart = portfolio_chart.build(
            user.id,
            chart_hummingbot,
            chart_native,
            since,
            now,
            interval_seconds,
            skip=chart_skip,
            connected=connected,
        )
    except Exception as error:  # noqa: BLE001 - never lose the chart over its notes
        logger.warning("Portfolio chart series skipped (%s)", type(error).__name__)

    return PortfolioHistoryResponse(
        server=name,
        points=points,
        interval=interval,
        top_tokens=top_tokens,
        change=change,
        **chart,
    )


_SNAPSHOT_LIST_KEYS = ("data", "snapshots", "history", "points", "results")


def _snapshot_state(snapshot: Any, keyed: bool) -> Any:
    """Return the portfolio state within a snapshot entry."""
    return snapshot if keyed else snapshot.get("state", snapshot)


def _extract_snapshot_entries(history: Any) -> tuple[list[tuple[float, Any]], bool]:
    """Normalize a history response into sorted (timestamp, snapshot) entries.

    Handles three shapes: a bare list of snapshots, a dict with the snapshot
    list under a common key, or a dict mapping timestamps to portfolio states.
    Returns (entries, keyed); keyed=True means the dict-keyed-timestamp shape,
    where each entry holds a raw portfolio state instead of a snapshot dict.
    """
    snapshots = None
    if isinstance(history, list):
        snapshots = history
    elif isinstance(history, dict):
        # Try common keys
        for key in _SNAPSHOT_LIST_KEYS:
            if key in history and isinstance(history[key], list):
                snapshots = history[key]
                break
        if snapshots is None:
            # The dict itself maps timestamps → portfolio states
            keyed_entries: list[tuple[float, Any]] = [
                (_parse_timestamp(ts_key), snapshot_data)
                for ts_key, snapshot_data in history.items()
            ]
            keyed_entries.sort(key=lambda x: x[0])
            return keyed_entries, True

    entries: list[tuple[float, Any]] = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        ts = snapshot.get("timestamp", snapshot.get("time", snapshot.get("t", 0)))
        if not ts:
            continue
        entries.append((_parse_timestamp(ts), snapshot))
    entries.sort(key=lambda x: x[0])
    return entries, False


def _build_token_breakdown(
    entries: list[tuple[float, Any]],
    keyed: bool,
    points: list[PortfolioHistoryPoint],
) -> list[str]:
    """Populate each point's per-token values, collapsing beyond the top 8 into "Other".

    Mutates ``point.tokens`` in place and returns the top token names.
    """
    # Build token values per timestamp (with forward-fill for missing exchanges)
    ts_token_map: dict[float, dict[str, float]] = {}
    prev_token_vals: dict[str, float] = {}
    for ts, snapshot in entries:
        token_vals = _extract_token_values(_snapshot_state(snapshot, keyed))
        if not token_vals:
            continue
        # Forward-fill: tokens present before but missing now keep previous value
        if prev_token_vals:
            for tk, tv in prev_token_vals.items():
                if tk not in token_vals:
                    token_vals[tk] = tv
        prev_token_vals = token_vals
        ts_token_map[ts] = token_vals

    if not ts_token_map:
        return []

    # Determine top 8 tokens by aggregate value
    agg: dict[str, float] = {}
    for tv in ts_token_map.values():
        for token, val in tv.items():
            agg[token] = agg.get(token, 0) + val
    top_tokens = sorted(agg, key=lambda t: agg[t], reverse=True)[:8]
    top_set = set(top_tokens)

    # Populate token breakdown on each point, collapsing rest into "Other"
    for point in points:
        tv = ts_token_map.get(point.timestamp, {})
        if not tv:
            continue
        tokens_out: dict[str, float] = {}
        other = 0.0
        for token, val in tv.items():
            if token in top_set:
                tokens_out[token] = val
            else:
                other += val
        if other > 0:
            tokens_out["Other"] = other
        point.tokens = tokens_out

    if "Other" in {t for p in points for t in p.tokens} and "Other" not in top_tokens:
        top_tokens.append("Other")
    return top_tokens


def _parse_timestamp(val: object) -> float:
    """Parse a timestamp that may be numeric or ISO 8601 string."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return 0.0


def _extract_token_values(data: object) -> dict[str, float]:
    """Extract per-token USD values from a portfolio snapshot."""
    tokens: dict[str, float] = {}
    if not isinstance(data, dict):
        return tokens
    for val in data.values():
        if isinstance(val, dict):
            for inner in val.values():
                if isinstance(inner, list):
                    for item in inner:
                        if isinstance(item, dict):
                            token = item.get("token", item.get("asset", ""))
                            usd = balance_value(item)
                            if token and usd > 0:
                                tokens[token] = tokens.get(token, 0) + usd
    return tokens


def _extract_connector_totals(data: object) -> dict[str, float]:
    """Extract per-connector USD totals from a portfolio snapshot."""
    totals: dict[str, float] = {}
    if not isinstance(data, dict):
        return totals
    for account, val in data.items():
        if isinstance(val, dict):
            for connector, inner in val.items():
                key = f"{account}:{connector}"
                if isinstance(inner, list):
                    s = 0.0
                    for item in inner:
                        if isinstance(item, dict):
                            s += balance_value(item)
                    totals[key] = s
                elif isinstance(inner, (int, float)):
                    totals[key] = float(inner)
        elif isinstance(val, (int, float)):
            totals[account] = float(val)
    return totals

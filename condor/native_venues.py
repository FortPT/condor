"""The venues Condor trades itself, outside Hummingbot, in one small registry.

A native venue has a service module (per-user ledger, ``status`` and, for a real
account, an async ``portfolio``), a crew of agents that trade it, and a value
history. Everything that has to know "which venues are there" reads this table:
the dashboard portfolio, the value recorder, the loop cards' PnL and the chat
agent's ``native_portfolio`` tool. Adding a venue is one ``NativeVenue(...)`` line
below; nothing else enumerates them.

A leaf module on purpose (no service import at load time): the web routes, the
recorder and the MCP server all import it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeVenue:
    #: Folder under ``CONDOR_DATA_DIR`` and the name the tools use.
    key: str
    #: The connector name the dashboard shows, and the value-history source.
    label: str
    #: Module holding the module-level ``service`` singleton.
    module: str
    #: Slugs of the agents whose loops trade this venue.
    agents: tuple[str, ...] = ()
    #: A real account Condor can value in USD. ``False`` for a paper-only venue,
    #: which has no ledger, no balance and therefore no equity.
    account: bool = True
    #: Shown on the dashboard when the account could not be valued.
    unvalued_note: str = ""

    @property
    def service(self) -> Any:
        return importlib.import_module(self.module).service


VENUES: tuple[NativeVenue, ...] = (
    NativeVenue(
        key="crypto_com_exchange",
        label="Crypto.com Exchange",
        module="condor.crypto_com_exchange",
        agents=("crypto_com_exchange_trader",),
        unvalued_note=(
            "Crypto.com App could not be valued. Its holdings are excluded from "
            "this total; check the Crypto.com page and retry."
        ),
    ),
    NativeVenue(
        key="kraken_xstocks",
        label="Kraken xStocks",
        module="condor.kraken_xstocks",
        agents=("kraken_xstocks_trader",),
        unvalued_note=(
            "Kraken xStocks could not be valued. Its holdings are excluded from "
            "this total; check the Kraken xStocks settings and retry."
        ),
    ),
    NativeVenue(
        key="xstocks_paper",
        label="xStocks paper",
        module="condor.xstocks_paper",
        agents=("xstocks_paper_trader",),
        account=False,
    ),
)


def get(key: str) -> NativeVenue:
    for venue in VENUES:
        if venue.key == key:
            return venue
    raise KeyError(key)


def account_venues() -> tuple[NativeVenue, ...]:
    """The venues that hold real money Condor can value."""
    return tuple(v for v in VENUES if v.account)


def for_agent(agent_slug: str) -> NativeVenue | None:
    """The venue a native crew's agent trades, or ``None`` for any other agent."""
    for venue in VENUES:
        if agent_slug in venue.agents:
            return venue
    return None

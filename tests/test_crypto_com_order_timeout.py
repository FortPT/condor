"""Order calls must outlast the default MCP tool budget."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers.condor import server


@pytest.mark.parametrize("action", ["execute", "reconcile"])
def test_order_calls_get_a_long_timeout(action):
    with patch(
        "mcp_servers.condor.condor_client.call_main_api",
        new=AsyncMock(return_value={}),
    ) as call:
        asyncio.run(server.crypto_com_app(action=action, quotation_id="q1"))
    assert call.await_args.kwargs["timeout"] >= 60

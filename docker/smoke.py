"""Container packaging smoke check; no login, models, agents or external APIs."""

import asyncio
import os
from pathlib import Path

from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from condor.runtime.toolsets import get_project_dir
from condor.web.app import create_app

assert Path(get_project_dir()) == Path.cwd(), "MCP and parent must share config cwd"
assert Path("frontend/dist/index.html").is_file(), "Native frontend bundle is missing"


async def check_mcp_children():
    # Match the agent's uv command, not the parent's explicit --no-sync launch.
    for module, flags, expected in (
        ("condor", ["--profile", "tick", "--user-id", "1"], "crypto_com_app"),
        ("hummingbot_api", ["--profile", "tick"], "get_prices"),
    ):
        parameters = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", f"mcp_servers.{module}", *flags],
            env=dict(os.environ),
            cwd=str(Path.cwd()),
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert expected in {tool.name for tool in tools.tools}, module


asyncio.run(asyncio.wait_for(check_mcp_children(), timeout=30))
with TestClient(create_app()) as client:
    for route in (
        "/",
        "/portfolio",
        "/bots",
        "/trade",
        "/dex",
        "/routines",
        "/settings",
    ):
        response = client.get(route)
        assert response.status_code == 200, route
        assert "text/html" in response.headers["content-type"], route
    response = client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403), "Native auth must remain enabled"
assert not Path("config.yml").exists(), "Smoke check must not initialize accounts"
print("Native container packaging smoke check passed")

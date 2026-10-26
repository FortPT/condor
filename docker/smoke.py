"""Container packaging smoke check; no login, models, agents or external APIs."""

from pathlib import Path

from fastapi.testclient import TestClient

from condor.runtime.toolsets import get_project_dir
from condor.web.app import create_app

assert Path(get_project_dir()) == Path.cwd(), "MCP and parent must share config cwd"
assert Path("frontend/dist/index.html").is_file(), "Native frontend bundle is missing"
with TestClient(create_app()) as client:
    for route in ("/", "/portfolio", "/bots", "/trade", "/dex", "/routines", "/settings"):
        response = client.get(route)
        assert response.status_code == 200, route
        assert "text/html" in response.headers["content-type"], route
    response = client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403), "Native auth must remain enabled"
assert not Path("config.yml").exists(), "Smoke check must not initialize accounts"
print("Native container packaging smoke check passed")

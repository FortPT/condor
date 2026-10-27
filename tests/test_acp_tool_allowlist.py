"""An ACP helper's explicit tool list also bounds its native host tools."""

import asyncio

import pytest

from condor.runtime.llm_client import build_llm_client

OPTIONS = [
    {"kind": "allow_always", "optionId": "always"},
    {"kind": "allow_once", "optionId": "once"},
    {"kind": "reject_once", "optionId": "reject"},
]
ALLOW = {"outcome": {"outcome": "selected", "optionId": "once"}}
REJECT = {"outcome": {"outcome": "selected", "optionId": "reject"}}
CANCEL = {"outcome": {"outcome": "cancelled"}}


def decide(allowed, tool, *, callback=None, options=OPTIONS):
    client = build_llm_client(
        "claude-acp:sonnet", allowed_tools=allowed, permission_callback=callback
    )
    assert client.permission_callback is not None
    return asyncio.run(client.permission_callback(tool, options))


@pytest.mark.parametrize(
    "name", ["Bash", "Read", "Write", "Task", "mcp__condor__crypto_com_app"]
)
def test_market_analyst_cannot_escape_its_read_only_tools(name):
    result = decide(
        ["get_prices", "get_market_data", "trading_agent_journal_read"],
        {"tool": name, "input": {"action": "execute", "quotation_id": "stub"}},
    )
    assert result["outcome"] == REJECT["outcome"]


def test_risk_reviewer_can_only_read_the_journal():
    allowed = ["trading_agent_journal_read"]
    assert decide(allowed, {"tool": "mcp__condor__trading_agent_journal_read"}) == ALLOW
    assert (
        decide(allowed, {"tool": "mcp__mcp-hummingbot__get_prices"})["outcome"]
        == REJECT["outcome"]
    )


def test_coordinator_execution_retains_original_risk_decision_and_arguments():
    calls = []
    outcome = {**CANCEL, "reason": "dry run cannot execute"}
    tool = {
        "tool": "mcp__condor__crypto_com_app",
        "input": {"action": "execute", "quotation_id": "stub"},
    }

    async def risk(tool_call, options):
        calls.append((tool_call, options))
        return outcome

    result = decide(["crypto_com_app"], tool, callback=risk)
    assert result is outcome
    assert calls[0][0] is tool
    assert calls[0][1] is OPTIONS
    denied = decide(["crypto_com_app"], {"tool": "Bash"}, callback=risk)
    assert denied["outcome"] == REJECT["outcome"]
    assert len(calls) == 1


def test_native_tool_search_discovery_is_available():
    assert (
        decide(["get_prices"], {"tool": "ToolSearch", "input": {"query": "get_prices"}})
        == ALLOW
    )
    assert (
        decide(["get_prices"], {"tool": "mcp__unknown__ToolSearch"})["outcome"]
        == REJECT["outcome"]
    )


@pytest.mark.parametrize(
    "allowed,name",
    [
        (["get_prices"], "mcp__mcp-hummingbot__get_prices"),
        (["mcp__mcp-hummingbot__get_prices"], "mcp__mcp-hummingbot__get_prices"),
        (["get_prices"], "get_prices"),
        (["manage_strategies"], "mcp__condor__manage_loops"),
        (["mcp__condor__manage_strategies"], "mcp__condor__manage_loops"),
    ],
)
def test_short_full_and_renamed_tool_names_keep_existing_semantics(allowed, name):
    assert decide(allowed, {"tool": name}) == ALLOW


def test_a_full_allowlisted_name_does_not_allow_another_server_with_same_suffix():
    result = decide(["mcp__condor__get_prices"], {"tool": "mcp__other__get_prices"})
    assert result["outcome"] == REJECT["outcome"]


@pytest.mark.parametrize(
    "tool",
    [{}, {"tool": None}, {"tool": 4}, {"tool": ["get_prices"]}, {"tool": "unknown"}],
)
def test_unknown_and_unreadable_names_fail_closed(tool):
    assert decide(["get_prices"], tool)["outcome"] == REJECT["outcome"]
    assert decide(["get_prices"], tool, options=[]) == CANCEL


def test_without_a_callback_only_allow_once_is_selected():
    assert decide(["get_prices"], {"title": "mcp__mcp-hummingbot__get_prices"}) == ALLOW
    assert decide(["get_prices"], {"tool": "get_prices"}, options=OPTIONS[:1]) == CANCEL


@pytest.mark.parametrize("allowed", [None, []])
def test_empty_allowlist_keeps_unrestricted_acp_callback_identity(allowed):
    async def callback(tool, options):
        return ALLOW

    assert (
        build_llm_client("claude-code", allowed_tools=allowed).permission_callback
        is None
    )
    assert (
        build_llm_client(
            "claude-code", allowed_tools=allowed, permission_callback=callback
        ).permission_callback
        is callback
    )


def test_pydantic_ai_filter_and_callback_are_unchanged():
    async def callback(tool, options):
        return CANCEL

    client = build_llm_client(
        "ollama:test", allowed_tools=["get_prices"], permission_callback=callback
    )
    assert client.permission_callback is callback
    assert client.allowed_tools == {"get_prices"}


def test_restricted_claude_session_removes_native_tools_before_provider_autoapproval():
    client = build_llm_client(
        "claude-acp:sonnet", allowed_tools=["trading_agent_journal_read"]
    )
    options = client._session_new_params()["_meta"]["claudeCode"]["options"]
    assert options["tools"] == ["ToolSearch"]
    assert options["allowDangerouslySkipPermissions"] is False
    assert options["settingSources"] == []
    assert options["settings"]["permissions"]["ask"] == ["mcp__*"]
    assert (
        "allowedTools" not in options
    )  # SDK pre-approval would bypass our risk callback.


def test_restricted_session_preserves_its_system_prompt_metadata():
    client = build_llm_client(
        "claude-acp:sonnet",
        allowed_tools=["get_prices"],
        system_prompt="Analyst identity",
    )
    meta = client._session_new_params()["_meta"]
    assert meta["systemPrompt"] == {"append": "Analyst identity"}
    assert meta["claudeCode"]["options"]["tools"] == ["ToolSearch"]


@pytest.mark.parametrize("allowed", [None, []])
def test_unrestricted_session_keeps_original_provider_options(allowed):
    client = build_llm_client("claude-acp:sonnet", allowed_tools=allowed)
    assert "_meta" not in client._session_new_params()

"""Modern-era (MCP 2026-07-28) request handling.

The 2026-07-28 revision drops the initialize handshake: every request
carries its protocol version, client identity and capabilities in
``params._meta`` and is served statelessly; ``server/discover`` replaces
``initialize``; every result carries ``resultType``. These tests pin the
dual-era behaviour -- modern requests served per that revision, legacy
requests exactly as before -- and the Streamable HTTP rules that go with
it (header/body validation, 400/404 status codes, no sessions).
"""

import base64
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.interfaces import ToolResult
from core.mcp_server import (
    ERA_LEGACY,
    ERA_MODERN,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    MCPServer,
)
from core.plugin_manager import PluginManager

MODERN = "2026-07-28"
TOOL = "anchorage_parcels__find_parcel"


def _server(config=None, tools=None):
    plugin_manager = MagicMock(spec=PluginManager)
    plugin_manager.config = config or {}
    plugin_manager.get_all_tools.return_value = tools or []
    plugin_manager.has_tool.return_value = True
    plugin_manager.execute_tool = AsyncMock(
        return_value=ToolResult(success=True, content=[{"type": "text", "text": "ok"}])
    )
    return MCPServer(plugin_manager)


def _meta(version=MODERN, capabilities=None, client=None):
    meta = {META_PROTOCOL_VERSION: version}
    if capabilities is not None:
        meta[META_CLIENT_CAPABILITIES] = capabilities
    if client is not None:
        meta[META_CLIENT_INFO] = client
    return meta


def _request(method, params=None, rid=1, **meta_kwargs):
    params = dict(params or {})
    params["_meta"] = _meta(**meta_kwargs)
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _headers(method, name=None, version=MODERN):
    headers = {"mcp-protocol-version": version, "mcp-method": method}
    if name is not None:
        headers["mcp-name"] = name
    return headers


# The exact request claude.ai was observed sending (2026-09-17 on the GIS
# fork; the same probe opens every session against this server), minus
# the trace context: the probe that used to get a 400 on every start.
CLAUDE_AI_DISCOVER = {
    "jsonrpc": "2.0",
    "id": 592859513,
    "method": "server/discover",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "Anthropic/ClaudeAI",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {
                    "io.modelcontextprotocol/ui": {
                        "mimeTypes": ["text/html;profile=mcp-app"]
                    }
                }
            },
        }
    },
}


class TestEraClassification:
    def test_initialize_is_legacy_even_with_modern_header(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        assert (
            MCPServer.request_era(request, {"mcp-protocol-version": MODERN})
            == ERA_LEGACY
        )

    def test_modern_meta_is_modern(self):
        assert MCPServer.request_era(CLAUDE_AI_DISCOVER, {}) == ERA_MODERN

    def test_modern_header_without_meta_is_modern(self):
        """A body with no _meta under a modern header is a malformed
        modern request, not a legacy one to quietly serve."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (
            MCPServer.request_era(request, {"mcp-protocol-version": MODERN})
            == ERA_MODERN
        )

    def test_no_meta_no_header_is_legacy(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert MCPServer.request_era(request, {}) == ERA_LEGACY
        assert (
            MCPServer.request_era(request, {"mcp-protocol-version": "2025-11-25"})
            == ERA_LEGACY
        )

    def test_version_lists_are_consistent(self):
        """SUPPORTED is modern + legacy, newest first, and initialize's
        default is the newest LEGACY one (it is the legacy handshake)."""
        assert MCPServer.SUPPORTED_PROTOCOL_VERSIONS[0] == MODERN
        assert MCPServer.SUPPORTED_PROTOCOL_VERSIONS == (
            MCPServer.MODERN_PROTOCOL_VERSIONS + MCPServer.LEGACY_PROTOCOL_VERSIONS
        )
        assert MCPServer.DEFAULT_PROTOCOL_VERSION == "2025-11-25"
        assert MCPServer.DEFAULT_PROTOCOL_VERSION in MCPServer.LEGACY_PROTOCOL_VERSIONS


class TestDiscover:
    @pytest.mark.asyncio
    async def test_claude_ai_probe_is_served(self):
        server = _server(
            config={
                "server_name": "Anchorage Parcels MCP",
                "server_version": "2.3.0",
                "instructions": "Start with find_parcel.",
            }
        )

        response = await server.handle_request(CLAUDE_AI_DISCOVER)

        assert response["id"] == 592859513
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"][0] == MODERN
        assert "2025-11-25" in result["supportedVersions"]
        assert result["capabilities"] == {"tools": {}}
        assert result["instructions"] == "Start with find_parcel."
        assert result["_meta"][META_SERVER_INFO] == {
            "name": "Anchorage Parcels MCP",
            "version": "2.3.0",
        }
        assert result["ttlMs"] == MCPServer.DISCOVER_TTL_MS
        assert result["cacheScope"] == "public"

    @pytest.mark.asyncio
    async def test_discover_omits_instructions_when_unset(self):
        response = await _server().handle_request(CLAUDE_AI_DISCOVER)
        assert "instructions" not in response["result"]


class TestModernToolsList:
    @pytest.mark.asyncio
    async def test_tools_list_is_cacheable_and_stamped(self):
        tools = [{"name": TOOL, "description": "d", "inputSchema": {}}]
        server = _server(tools=tools)

        response = await server.handle_request(_request("tools/list", capabilities={}))

        result = response["result"]
        assert result["tools"] == tools
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == MCPServer.TOOLS_LIST_TTL_MS
        assert result["cacheScope"] == "public"
        assert result["_meta"][META_SERVER_INFO]["name"] == "OpenContext"

    @pytest.mark.asyncio
    async def test_legacy_tools_list_is_unchanged(self):
        """Legacy clients keep getting exactly what they got before: no
        resultType, no cache hints, no _meta."""
        tools = [{"name": TOOL, "description": "d", "inputSchema": {}}]
        server = _server(tools=tools)

        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        assert response["result"] == {"tools": tools}


class TestModernToolsCall:
    @pytest.mark.asyncio
    async def test_tools_call_result_is_stamped(self):
        server = _server()

        response = await server.handle_request(
            _request(
                "tools/call",
                {"name": TOOL, "arguments": {"parcel_id": "002-151-32"}},
                capabilities={},
                client={"name": "claude-code", "version": "2.1.274"},
            )
        )

        server.plugin_manager.execute_tool.assert_awaited_once_with(
            TOOL, {"parcel_id": "002-151-32"}
        )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["content"] == [{"type": "text", "text": "ok"}]
        assert META_SERVER_INFO in result["_meta"]

    @pytest.mark.asyncio
    async def test_tool_execution_error_is_a_complete_result(self):
        """isError results are still resultType complete -- the spec's
        tool-execution-error example carries both."""
        server = _server()
        server.plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                success=False, content=[], error_message="bad field"
            )
        )

        response = await server.handle_request(
            _request("tools/call", {"name": TOOL}, capabilities={})
        )

        result = response["result"]
        assert result["isError"] is True
        assert result["resultType"] == "complete"

    @pytest.mark.asyncio
    async def test_legacy_tools_call_is_unchanged(self):
        server = _server()

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": {}},
            }
        )

        assert response["result"] == {"content": [{"type": "text", "text": "ok"}]}


class TestModernValidation:
    @pytest.mark.asyncio
    async def test_missing_client_capabilities_is_invalid_params(self):
        response = await _server().handle_request(
            _request("tools/list")  # no clientCapabilities
        )

        assert response["error"]["code"] == -32602
        assert META_CLIENT_CAPABILITIES in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_unknown_modern_version_is_32022(self):
        response = await _server().handle_request(
            _request("tools/list", capabilities={}, version="2027-01-01")
        )

        error = response["error"]
        assert error["code"] == -32022
        assert error["data"]["requested"] == "2027-01-01"
        assert error["data"]["supported"] == list(MCPServer.SUPPORTED_PROTOCOL_VERSIONS)

    @pytest.mark.asyncio
    async def test_legacy_version_in_meta_is_32022(self):
        """Our legacy versions are only served via initialize; declared
        as per-request metadata they are unsupported."""
        response = await _server().handle_request(
            _request("tools/list", capabilities={}, version="2025-11-25")
        )

        assert response["error"]["code"] == -32022

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method", ["ping", "initialize", "resources/list", "subscriptions/listen"]
    )
    async def test_methods_not_in_this_era_are_method_not_found(self, method):
        """ping is gone from this revision; initialize is the OTHER era's
        handshake; resources are not served; and with no listChanged
        capability there is nothing to listen for."""
        request = _request(method, capabilities={})
        # `initialize` classifies as legacy from the body alone, so force
        # the era the way handle_http_request would for a modern header.
        response = await _server().handle_request(request, era=ERA_MODERN)

        assert response["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_modern_notification_is_dropped(self):
        request = _request("notifications/anything", capabilities={})
        del request["id"]

        assert await _server().handle_request(request) is None

    @pytest.mark.asyncio
    async def test_modern_errors_log_at_warning_without_traceback(self, caplog):
        """The caller-error invariant holds for the new error classes too:
        a bad version or a missing _meta field is the client's mistake."""
        caplog.set_level(logging.WARNING, logger="core.mcp_server")

        await _server().handle_request(
            _request("tools/list", capabilities={}, version="2027-01-01")
        )
        await _server().handle_request(_request("tools/list"))

        assert caplog.records, "expected WARNING records"
        assert all(r.levelno == logging.WARNING for r in caplog.records)
        assert all(r.exc_info is None for r in caplog.records)


class TestStreamableHTTP:
    """handle_http_request: header validation and status codes."""

    async def _post(self, server, request, headers):
        return await server.handle_http_request(json.dumps(request), headers)

    @pytest.mark.asyncio
    async def test_claude_ai_probe_end_to_end(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, _headers("server/discover")
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["result"]["supportedVersions"][0] == MODERN

    @pytest.mark.asyncio
    async def test_missing_mcp_method_header_is_header_mismatch(self):
        response = await self._post(
            _server(),
            CLAUDE_AI_DISCOVER,
            {"mcp-protocol-version": MODERN},
        )

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["id"] == 592859513
        assert body["error"]["code"] == -32020
        assert "Mcp-Method" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_version_header_on_modern_body_is_header_mismatch(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, {"mcp-method": "server/discover"}
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32020

    @pytest.mark.asyncio
    async def test_version_header_body_disagreement_is_header_mismatch(self):
        response = await self._post(
            _server(),
            CLAUDE_AI_DISCOVER,
            _headers("server/discover", version="2025-11-25"),
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32020

    @pytest.mark.asyncio
    async def test_mcp_method_disagreement_is_header_mismatch(self):
        response = await self._post(
            _server(), CLAUDE_AI_DISCOVER, _headers("tools/list")
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32020

    @pytest.mark.asyncio
    async def test_tools_call_requires_matching_mcp_name(self):
        server = _server()
        request = _request(
            "tools/call", {"name": TOOL, "arguments": {}}, capabilities={}
        )

        missing = await self._post(server, request, _headers("tools/call"))
        assert missing["statusCode"] == 400
        assert json.loads(missing["body"])["error"]["code"] == -32020

        wrong = await self._post(
            server, request, _headers("tools/call", name="anchorage_parcels__other")
        )
        assert wrong["statusCode"] == 400

        ok = await self._post(server, request, _headers("tools/call", name=TOOL))
        assert ok["statusCode"] == 200

    @pytest.mark.asyncio
    async def test_mcp_name_base64_sentinel_is_decoded(self):
        server = _server()
        encoded = "=?base64?" + base64.b64encode(TOOL.encode()).decode() + "?="
        request = _request(
            "tools/call", {"name": TOOL, "arguments": {}}, capabilities={}
        )

        response = await self._post(
            server, request, _headers("tools/call", name=encoded)
        )

        assert response["statusCode"] == 200

    @pytest.mark.asyncio
    async def test_tools_call_without_name_falls_through_to_invalid_params(self):
        """A missing params.name is the body's fault, not the header's:
        the caller gets the -32602 that names the missing field, not a
        header-mismatch complaint about a header it could not have set.
        A bad tool argument is not a malformed envelope, so it stays
        HTTP 200 like every other tool-level -32602."""
        request = _request("tools/call", {"arguments": {}}, capabilities={})

        response = await self._post(_server(), request, _headers("tools/call"))

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_unknown_modern_method_is_404(self):
        response = await self._post(
            _server(),
            _request("resources/list", capabilities={}),
            _headers("resources/list"),
        )

        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_modern_ping_is_404(self):
        response = await self._post(
            _server(), _request("ping", capabilities={}), _headers("ping")
        )

        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_unknown_legacy_method_stays_200(self):
        """Legacy Streamable HTTP puts JSON-RPC errors in a 200, and the
        server/discover probe from an older-core-era client must keep
        looking like a legacy refusal, not a modern 404."""
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"mcp-protocol-version": "2025-11-25"},
        )

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_legacy_ping_still_returns_empty_object(self):
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            {"mcp-protocol-version": "2025-11-25", "mcp-session-id": "s-1"},
        )

        assert response["statusCode"] == 200
        assert json.loads(response["body"]) == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {},
        }

    @pytest.mark.asyncio
    async def test_malformed_meta_is_400(self):
        request = _request("tools/list")  # no clientCapabilities

        response = await self._post(_server(), request, _headers("tools/list"))

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_unsupported_modern_version_is_400(self):
        request = _request("tools/list", capabilities={}, version="2027-01-01")

        response = await self._post(
            _server(), request, _headers("tools/list", version="2027-01-01")
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32022

    @pytest.mark.asyncio
    async def test_modern_header_with_legacy_body_is_header_mismatch(self):
        """The half-migrated case: header says 2026-07-28, body carries no
        _meta. Rejected as a modern request (the client is told which
        header/body pair disagrees) rather than silently served as legacy."""
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"mcp-protocol-version": MODERN},
        )

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32020

    @pytest.mark.asyncio
    async def test_modern_notification_is_202(self):
        request = _request("notifications/anything", capabilities={})
        del request["id"]

        response = await self._post(
            _server(), request, _headers("notifications/anything")
        )

        assert response["statusCode"] == 202
        assert response["body"] == ""

    @pytest.mark.asyncio
    async def test_legacy_notification_is_202(self):
        response = await self._post(
            _server(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {},
        )

        assert response["statusCode"] == 202
        assert response["body"] == ""

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self):
        response = await _server().handle_http_request("[1, 2]", {})

        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32600


class TestLogging:
    @pytest.mark.asyncio
    async def test_client_identity_logged_for_both_eras(self, caplog):
        caplog.set_level(logging.INFO, logger="core.mcp_server")
        server = _server()

        await server.handle_request(CLAUDE_AI_DISCOVER)
        await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "claude-code", "version": "2.1.274"},
                },
            }
        )

        received = [
            r for r in caplog.records if r.getMessage() == "JSON-RPC request received"
        ]
        assert [r.mcp_era for r in received] == [ERA_MODERN, ERA_LEGACY]
        assert [r.mcp_client_name for r in received] == [
            "Anthropic/ClaudeAI",
            "claude-code",
        ]
        assert [r.mcp_protocol_version for r in received] == [
            MODERN,
            "2025-11-25",
        ]

    @pytest.mark.asyncio
    async def test_era_is_on_the_response_log_line_too(self, caplog):
        caplog.set_level(logging.INFO, logger="core.mcp_server")

        await _server().handle_request(CLAUDE_AI_DISCOVER)

        processed = [
            r
            for r in caplog.records
            if r.getMessage() == "JSON-RPC request processed successfully"
        ]
        assert [r.mcp_era for r in processed] == [ERA_MODERN]

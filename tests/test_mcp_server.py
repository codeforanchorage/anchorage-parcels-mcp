"""Comprehensive tests for MCP Server.

These tests verify JSON-RPC protocol handling, request routing,
error handling, and HTTP request processing.
"""

import pytest
import json
import logging
from unittest.mock import AsyncMock, MagicMock

from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from core.interfaces import ToolResult


class TestInitialize:
    """Test initialize method handling."""

    @pytest.mark.asyncio
    async def test_initialize_returns_correct_response(self):
        """Test that initialize returns correct protocol version and capabilities."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.config = {}
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        # No protocolVersion requested -> server answers with the newest
        # version it supports.
        assert response["result"]["protocolVersion"] == "2025-11-25"
        assert "capabilities" in response["result"]
        assert "serverInfo" in response["result"]
        # Empty config -> default server name.
        assert response["result"]["serverInfo"]["name"] == "OpenContext"
        assert response["result"]["serverInfo"]["version"] == "1.0.0"
        # No instructions configured -> key omitted.
        assert "instructions" not in response["result"]

    @pytest.mark.asyncio
    async def test_initialize_negotiates_version_and_uses_config(self):
        """Initialize echoes a supported requested version and pulls
        serverInfo name + instructions from config."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.config = {
            "server_name": "Anchorage GIS MCP",
            "instructions": "Start with find_gis_content.",
        }
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }

        response = await server.handle_request(request)

        result = response["result"]
        # Requested version is supported -> echoed back.
        assert result["protocolVersion"] == "2025-06-18"
        assert result["serverInfo"]["name"] == "Anchorage GIS MCP"
        assert result["instructions"] == "Start with find_gis_content."

    @pytest.mark.asyncio
    async def test_initialize_echoes_2025_11_25(self):
        """The 2025-11-25 revision is supported and echoed back."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.config = {}
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }

        response = await server.handle_request(request)

        assert response["result"]["protocolVersion"] == "2025-11-25"

    @pytest.mark.asyncio
    async def test_initialize_unsupported_version_falls_back(self):
        """An unrecognized requested version (including ones newer than we
        support, e.g. 2026-07-28) gets the newest supported version back,
        per the spec's version-negotiation rule."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.config = {}
        server = MCPServer(plugin_manager)

        for requested in ("1999-01-01", "2026-07-28"):
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": requested},
            }

            response = await server.handle_request(request)

            assert response["result"]["protocolVersion"] == "2025-11-25", requested

    @pytest.mark.asyncio
    async def test_initialize_notification_returns_none(self):
        """Test that initialize notification (no id) returns None."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.config = {}
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
            # No "id" field - this is a notification
        }

        response = await server.handle_request(request)

        assert response is None


class TestToolsList:
    """Test tools/list method handling."""

    @pytest.mark.asyncio
    async def test_tools_list_returns_all_tools(self):
        """Test that tools/list returns all registered tools."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.return_value = [
            {
                "name": "ckan__search_datasets",
                "description": "Search datasets",
                "inputSchema": {},
            },
            {
                "name": "ckan__get_dataset",
                "description": "Get dataset",
                "inputSchema": {},
            },
        ]
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        assert "tools" in response["result"]
        assert len(response["result"]["tools"]) == 2
        assert response["result"]["tools"][0]["name"] == "ckan__search_datasets"

    @pytest.mark.asyncio
    async def test_tools_list_empty_when_no_tools(self):
        """Test that tools/list returns empty list when no tools registered."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.return_value = []
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["result"]["tools"] == []


class TestToolsCall:
    """Test tools/call method handling."""

    @pytest.mark.asyncio
    async def test_tools_call_succeeds_with_valid_tool(self):
        """Test that tools/call succeeds with valid tool."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "Tool executed successfully"}],
                success=True,
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ckan__search_datasets",
                "arguments": {"query": "test", "limit": 10},
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        assert "content" in response["result"]
        assert len(response["result"]["content"]) > 0
        assert "isError" not in response["result"]
        plugin_manager.execute_tool.assert_called_once_with(
            "ckan__search_datasets",
            {"query": "test", "limit": 10},
        )

    @pytest.mark.asyncio
    async def test_tools_call_returns_error_when_tool_fails(self):
        """Test that tools/call returns error when tool execution fails."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "Error occurred"}],
                success=False,
                error_message="Tool execution failed",
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ckan__search_datasets",
                "arguments": {},
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        assert "result" in response
        assert response["result"]["isError"] is True
        assert "error" in response["result"]
        assert response["result"]["error"] == "Tool execution failed"

    @pytest.mark.asyncio
    async def test_tools_call_returns_non_empty_error_when_error_message_is_none(self):
        """Test that tools/call returns a non-empty error when error_message is None."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[],
                success=False,
                error_message=None,
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ckan__search_datasets",
                "arguments": {},
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        assert "result" in response
        assert response["result"]["isError"] is True
        assert "error" in response["result"]
        assert response["result"]["error"]
        assert "unknown" in response["result"]["error"].lower()
        # Error should also be in content so LLM clients (e.g. Claude) receive it
        assert response["result"]["content"]
        assert response["result"]["content"][0]["text"] == "An unknown error occurred"

    @pytest.mark.asyncio
    async def test_tools_call_puts_error_in_content_when_content_empty(self):
        """Test that error message is in content when tool fails with empty content."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[],
                success=False,
                error_message="Resource 'xyz' not found (HTTP 404)",
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ckan__query_data",
                "arguments": {"resource_id": "xyz"},
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["result"]["isError"] is True
        assert response["result"]["error"] == "Resource 'xyz' not found (HTTP 404)"
        # Content must include error so all clients (Claude, Inspector, curl) receive it
        assert len(response["result"]["content"]) == 1
        assert response["result"]["content"][0]["type"] == "text"
        assert (
            response["result"]["content"][0]["text"]
            == "Resource 'xyz' not found (HTTP 404)"
        )

    @pytest.mark.asyncio
    async def test_tools_call_missing_tool_name_is_invalid_params(self):
        """A missing params.name is a caller error -> -32602, not -32603."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "arguments": {},
                # Missing "name" field
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        assert "error" in response
        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Invalid params"
        assert "params.name is required" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_tools_call_handles_missing_arguments(self):
        """Test that tools/call handles missing arguments gracefully."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "Success"}],
                success=True,
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ckan__search_datasets",
                # Missing "arguments" field
            },
        }

        response = await server.handle_request(request)

        assert response is not None
        # Should use empty dict as default
        plugin_manager.execute_tool.assert_called_once_with(
            "ckan__search_datasets",
            {},
        )


class TestPing:
    """Test ping method handling."""

    @pytest.mark.asyncio
    async def test_ping_returns_empty_result(self):
        """Spec: ping's result MUST be an empty object."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        # The liveness signal is the response itself, not its body.
        assert response["result"] == {}


class TestNotifications:
    """Test notification handling."""

    @pytest.mark.asyncio
    async def test_notifications_initialized_returns_none(self):
        """Test that notifications/initialized returns None."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
            # No "id" field - this is a notification
        }

        response = await server.handle_request(request)

        assert response is None

    @pytest.mark.asyncio
    async def test_unknown_notification_returns_none(self):
        """Test that unknown notification method returns None."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "method": "notifications/unknown",
            "params": {},
            # No "id" field - this is a notification
        }

        response = await server.handle_request(request)

        assert response is None


class TestUnknownMethods:
    """Test handling of unknown methods."""

    @pytest.mark.asyncio
    async def test_unknown_method_returns_method_not_found(self):
        """Spec: an unrecognized method is -32601, not -32603.

        Clients probe for optional methods; -32603 would read as a server
        fault and hide the fact that the method simply is not implemented.
        """
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "unknown/method",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert "error" in response
        assert response["error"]["code"] == -32601
        assert response["error"]["message"] == "Method not found"
        assert "Unknown method" in response["error"]["data"]


class TestMalformedToolsCall:
    """A malformed tools/call is a caller error: -32602, never -32603."""

    @staticmethod
    def _server():
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.has_tool = MagicMock(return_value=True)
        plugin_manager.list_tool_names = MagicMock(return_value=[])
        plugin_manager.execute_tool = AsyncMock()
        return MCPServer(plugin_manager), plugin_manager

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_arguments", ["a string", ["a", "list"], 42, True])
    async def test_non_object_arguments_is_invalid_params(self, bad_arguments):
        """Unvalidated, these reach the plugin and come back as a raw
        Python AttributeError ("'str' object has no attribute 'get'")
        dressed up as a tool result."""
        server, plugin_manager = self._server()

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ckan__search_datasets", "arguments": bad_arguments},
        }

        response = await server.handle_request(request)

        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Invalid params"
        assert "params.arguments must be an object" in response["error"]["data"]
        # The plugin must never see a malformed argument payload.
        plugin_manager.execute_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_null_arguments_is_treated_as_empty(self):
        """`arguments` is optional; an explicit null means "no arguments"."""
        server, plugin_manager = self._server()
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "ok"}], success=True
            )
        )

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ckan__search_datasets", "arguments": None},
        }

        response = await server.handle_request(request)

        assert "error" not in response
        plugin_manager.execute_tool.assert_awaited_once_with(
            "ckan__search_datasets", {}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", [None, "", 0])
    async def test_missing_or_empty_name_is_invalid_params(self, bad_name):
        server, plugin_manager = self._server()

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": bad_name, "arguments": {}},
        }

        response = await server.handle_request(request)

        assert response["error"]["code"] == -32602
        assert "params.name is required" in response["error"]["data"]
        plugin_manager.execute_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_object_params_is_invalid_params(self):
        server, _ = self._server()

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": ["positional", "params"],
        }

        response = await server.handle_request(request)

        assert response["error"]["code"] == -32602
        assert "params must be an object" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_malformed_calls_log_at_warning_without_traceback(self, caplog):
        server, _ = self._server()

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ckan__search_datasets", "arguments": "nope"},
        }

        with caplog.at_level(logging.WARNING, logger="core.mcp_server"):
            await server.handle_request(request)

        records = [r for r in caplog.records if "Invalid params" in r.getMessage()]
        assert records, "expected a WARNING for the malformed call"
        assert all(r.levelno == logging.WARNING for r in records)
        assert all(r.exc_info is None for r in records)


class TestUnknownTool:
    """An unregistered tool name is a protocol error, not a server fault."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_invalid_params(self):
        """Spec (server/tools): unknown tool -> -32602 "Unknown tool: <name>"."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.has_tool = MagicMock(return_value=False)
        plugin_manager.list_tool_names = MagicMock(
            return_value=["ckan__get_dataset", "ckan__search_datasets"]
        )
        plugin_manager.execute_tool = AsyncMock()
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "ckan__no_such_tool", "arguments": {}},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Unknown tool: ckan__no_such_tool"
        assert response["error"]["data"]["available_tools"] == [
            "ckan__get_dataset",
            "ckan__search_datasets",
        ]
        # The plugin must never be reached for a tool that does not exist.
        plugin_manager.execute_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_tool_is_logged_at_warning_without_traceback(self, caplog):
        """A caller naming a missing tool must not log a traceback.

        Tracebacks for caller errors bury genuine server faults in
        CloudWatch, which is exactly how this gap was found.
        """
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.has_tool = MagicMock(return_value=False)
        plugin_manager.list_tool_names = MagicMock(return_value=[])
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }

        with caplog.at_level(logging.WARNING, logger="core.mcp_server"):
            await server.handle_request(request)

        records = [r for r in caplog.records if "Unknown tool" in r.getMessage()]
        assert records, "expected a WARNING naming the unknown tool"
        assert all(r.levelno == logging.WARNING for r in records)
        assert all(r.exc_info is None for r in records)

    @pytest.mark.asyncio
    async def test_known_tool_still_routes_to_plugin(self):
        """The guard must not block tools that do exist."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.has_tool = MagicMock(return_value=True)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "ok"}], success=True
            )
        )
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "ckan__search_datasets", "arguments": {}},
        }

        response = await server.handle_request(request)

        assert response["result"]["content"][0]["text"] == "ok"
        plugin_manager.execute_tool.assert_awaited_once()


class TestErrorHandling:
    """Test error handling."""

    @pytest.mark.asyncio
    async def test_exception_in_handler_returns_error_response(self):
        """Test that exceptions in handler return error response."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.side_effect = Exception("Internal error")
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }

        response = await server.handle_request(request)

        assert response is not None
        assert "error" in response
        assert response["error"]["code"] == -32603
        assert response["error"]["message"] == "Internal error"
        assert "Internal error" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_exception_in_notification_returns_none(self):
        """Test that exceptions in notification handlers return None."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.side_effect = Exception("Internal error")
        server = MCPServer(plugin_manager)

        request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            # No "id" field - this is a notification
        }

        response = await server.handle_request(request)

        # Notifications should return None even on error
        assert response is None


class TestHTTPRequestHandling:
    """Test HTTP request handling."""

    @pytest.mark.asyncio
    async def test_handle_http_request_with_valid_json(self):
        """Test handling HTTP request with valid JSON."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.return_value = []
        server = MCPServer(plugin_manager)

        request_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping",
                "params": {},
            }
        )

        response = await server.handle_http_request(request_body)

        assert response["statusCode"] == 200
        assert response["headers"]["Content-Type"] == "application/json"
        assert "body" in response
        body = json.loads(response["body"])
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1

    @pytest.mark.asyncio
    async def test_handle_http_request_with_invalid_json(self):
        """Test handling HTTP request with invalid JSON."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request_body = "invalid json {"

        response = await server.handle_http_request(request_body)

        assert response["statusCode"] == 400
        assert response["headers"]["Content-Type"] == "application/json"
        body = json.loads(response["body"])
        assert body["error"]["code"] == -32700
        assert body["error"]["message"] == "Parse error"

    @pytest.mark.asyncio
    async def test_handle_http_request_with_notification(self):
        """Test handling HTTP request with notification (no id)."""
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        request_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
                # No "id" field
            }
        )

        response = await server.handle_http_request(request_body)

        assert response["statusCode"] == 200
        assert response["body"] == ""  # Empty body for notifications

    @pytest.mark.asyncio
    async def test_handle_http_request_preserves_headers(self):
        """Test that HTTP request handler accepts optional headers."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.get_all_tools.return_value = []
        server = MCPServer(plugin_manager)

        request_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping",
                "params": {},
            }
        )

        headers = {"X-Custom-Header": "value"}
        response = await server.handle_http_request(request_body, headers)

        assert response["statusCode"] == 200
        # Headers are not modified by handle_http_request
        # They're just passed through for logging purposes

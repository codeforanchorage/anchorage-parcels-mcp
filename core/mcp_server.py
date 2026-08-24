"""MCP Server implementation for OpenContext.

Handles MCP JSON-RPC protocol and integrates with Plugin Manager.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from core.logging_utils import (
    format_jsonrpc_request_log,
    format_jsonrpc_response_log,
)
from core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class JsonRpcError(Exception):
    """A JSON-RPC error that should be returned to the caller verbatim.

    Raised for *caller* mistakes (unknown method, unknown tool, bad
    params) as opposed to server faults. ``handle_request`` turns these
    into the declared error code and logs them at WARNING with no
    traceback -- a client probing for an optional method, or naming a
    tool that does not exist, is not a server incident, and a stack
    trace for it only buries real errors in the logs.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error_object(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class MCPServer:
    """MCP Server that handles JSON-RPC requests and routes to Plugin Manager."""

    def __init__(self, plugin_manager: PluginManager) -> None:
        """Initialize MCP Server with Plugin Manager.

        Args:
            plugin_manager: Initialized Plugin Manager instance
        """
        self.plugin_manager = plugin_manager

    async def handle_request(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Handle a single MCP JSON-RPC request.

        Args:
            request: JSON-RPC request dictionary
            session_id: Optional MCP session ID for log correlation

        Returns:
            JSON-RPC response dictionary, or None for notifications
        """
        start_time = time.perf_counter()
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        # Check if this is a notification (no id field)
        is_notification = request_id is None

        # Log JSON-RPC request
        request_log_data = format_jsonrpc_request_log(
            request_id=request_id,
            method=method,
            params=params,
            is_notification=is_notification,
        )
        if session_id:
            request_log_data["mcp_session_id"] = session_id
        logger.info("JSON-RPC request received", extra=request_log_data)

        try:
            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method == "tools/list":
                result = await self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            elif method == "ping":
                # Spec: the ping response MUST be an empty result object.
                # The liveness signal is the response itself, not its body.
                result = {}
            elif method == "notifications/initialized":
                # MCP notification - no response needed
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None
            else:
                # For notifications with unknown methods, silently ignore
                if is_notification:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    logger.warning(
                        f"Ignoring unknown notification method: {method}",
                        extra={
                            **request_log_data,
                            "duration_ms": round(duration_ms, 2),
                        },
                    )
                    return None
                raise JsonRpcError(
                    -32601,
                    "Method not found",
                    f"Unknown method: {method}",
                )

            # Don't send response for notifications
            if is_notification:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None

            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }

            # Log JSON-RPC response
            duration_ms = (time.perf_counter() - start_time) * 1000
            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                result=result,
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.info(
                "JSON-RPC request processed successfully", extra=response_log_data
            )

            return response

        except JsonRpcError as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": e.to_error_object(),
            }

            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                error=error_response.get("error"),
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            # WARNING, not ERROR, and no exc_info: this is a caller
            # error. Tracebacks here bury genuine server faults.
            logger.warning(
                f"JSON-RPC caller error on {method}: {e.message} ({e.data})",
                extra={**response_log_data, "error_type": type(e).__name__},
            )

            if is_notification:
                return None
            return error_response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": str(e),
                },
            }

            # Log JSON-RPC error response
            response_log_data = format_jsonrpc_response_log(
                request_id=request_id,
                method=method,
                error=error_response.get("error"),
                duration_ms=duration_ms,
            )
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.error(
                f"Error handling JSON-RPC request {method}: {e}",
                extra={**response_log_data, "error_type": type(e).__name__},
                exc_info=True,
            )

            # Don't send error response for notifications
            if is_notification:
                return None
            return error_response

    # MCP protocol revisions this server implements, newest first. The wire
    # format for a tools-only server is compatible across these, so we echo
    # the client's requested version when it's one we recognize. (Previously
    # this was hardcoded, which could make clients on a newer revision --
    # e.g. M365 Copilot -- warn or balk.) 2026-07-28 is NOT listed: its core
    # drops initialize/ping and requires server/discover, resultType, and
    # cacheable list results, which this server does not implement yet.
    SUPPORTED_PROTOCOL_VERSIONS = (
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    )
    # On an unrecognized (or absent) requested version the spec says to
    # answer with the latest version the server supports.
    DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request.

        Negotiates the protocol version against the client's request and
        populates serverInfo / instructions from config so each deployment
        can identify itself and steer the model independently.

        Args:
            params: Initialize parameters

        Returns:
            Initialize response
        """
        config = self.plugin_manager.config or {}

        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version in self.SUPPORTED_PROTOCOL_VERSIONS
            else self.DEFAULT_PROTOCOL_VERSION
        )

        result: Dict[str, Any] = {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": config.get("server_name", "OpenContext"),
                "version": str(config.get("server_version", "1.0.0")),
            },
        }

        # Optional per-deployment guidance string surfaced to the client/model.
        instructions = config.get("instructions")
        if instructions:
            result["instructions"] = instructions

        return result

    async def _handle_tools_list(self) -> Dict[str, Any]:
        """Handle tools/list request.

        Returns:
            List of available tools
        """
        tools = self.plugin_manager.get_all_tools()
        return {"tools": tools}

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request.

        Args:
            params: Tool call parameters (name, arguments)

        Returns:
            Tool execution result
        """
        if not isinstance(params, dict):
            raise JsonRpcError(
                -32602,
                "Invalid params",
                f"params must be an object, got {type(params).__name__}.",
            )

        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        # `arguments` is optional; an explicit null means "no arguments".
        if arguments is None:
            arguments = {}

        # Anything else non-object is a caller error and must be caught
        # HERE. Left unvalidated, a string or list is handed to the plugin
        # and the caller gets a raw Python AttributeError ("'str' object
        # has no attribute 'get'") dressed up as a tool result.
        if not isinstance(arguments, dict):
            raise JsonRpcError(
                -32602,
                "Invalid params",
                (
                    f"params.arguments must be an object, got "
                    f"{type(arguments).__name__}."
                ),
            )

        if not tool_name:
            raise JsonRpcError(
                -32602,
                "Invalid params",
                "params.name is required and must name a registered tool.",
            )

        # Spec (server/tools, Error Handling): an unknown tool name is a
        # *protocol* error -- code -32602, message "Unknown tool: <name>".
        # Without this check it fell through to the generic -32603
        # "Internal error" handler with a full traceback, which both
        # misled clients probing for a tool and polluted the logs.
        if not self.plugin_manager.has_tool(tool_name):
            raise JsonRpcError(
                -32602,
                f"Unknown tool: {tool_name}",
                {"available_tools": self.plugin_manager.list_tool_names()},
            )

        result = await self.plugin_manager.execute_tool(tool_name, arguments)

        if result.success:
            return {
                "content": result.content,
            }
        else:
            error_msg = result.error_message or "An unknown error occurred"
            # Include error in content so all clients (curl, Inspector, Claude) receive it.
            # LLMs read content for context; empty content means they cannot see the error.
            content = (
                result.content
                if result.content
                else [{"type": "text", "text": error_msg}]
            )
            return {
                "content": content,
                "isError": True,
                "error": error_msg,
            }

    async def handle_http_request(
        self, body: str, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Handle HTTP request with MCP JSON-RPC payload.

        This method is used by Lambda handler to process HTTP requests.

        Args:
            body: Request body (JSON string)
            headers: HTTP headers (optional)

        Returns:
            Response dictionary with statusCode and body
        """
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            logger.error(
                f"Invalid JSON in request body: {e}",
                extra={"error_type": "JSONDecodeError"},
                exc_info=True,
            )
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": "Parse error",
                            "data": str(e),
                        },
                    }
                ),
            }

        # Pull MCP session ID from headers (lowercased by the Lambda adapter)
        # so downstream log lines can be grouped by session.
        session_id = None
        if headers:
            session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")

        # Handle the request (logging is done in handle_request)
        response = await self.handle_request(request, session_id=session_id)

        # If response is None, it was a notification - return empty response
        if response is None:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": "",
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response),
        }

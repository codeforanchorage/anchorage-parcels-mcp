"""MCP Server implementation for OpenContext.

Handles MCP JSON-RPC protocol and integrates with Plugin Manager.

This is a DUAL-ERA server (spec 2026-07-28, "Versioning and Compatibility"):

* Legacy era (2025-11-25 and earlier): the client opens with an
  ``initialize`` handshake, the transport mints an ``Mcp-Session-Id`` for
  log correlation, and every later request rides on that session.
* Modern era (2026-07-28 and later): there is no handshake. Every request
  carries its protocol version, client identity and capabilities in
  ``params._meta`` and is served statelessly; ``server/discover`` replaces
  ``initialize`` as the way to learn what the server supports.

The era is chosen per request from how the client opens: an ``initialize``
request is legacy; a request whose ``_meta`` carries
``io.modelcontextprotocol/protocolVersion`` is modern. Both are served on
the same endpoint, which is what lets a dual-era client (claude.ai, Claude
Code) skip the "probe with 2026-07-28, get 400, fall back to initialize"
round trip every session used to start with.
"""

import base64
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from core.logging_utils import (
    format_jsonrpc_request_log,
    format_jsonrpc_response_log,
)
from core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)

# Reserved `_meta` keys (spec 2026-07-28 "General fields > _meta").
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

ERA_LEGACY = "legacy"
ERA_MODERN = "modern"


class JsonRpcError(Exception):
    """A JSON-RPC error that should be returned to the caller verbatim.

    Raised for *caller* mistakes (unknown method, unknown tool, bad
    params) as opposed to server faults. ``handle_request`` turns these
    into the declared error code and logs them at WARNING with no
    traceback -- a client probing for an optional method, or naming a
    tool that does not exist, is not a server incident, and a stack
    trace for it only buries real errors in the logs.

    ``http_status`` is the status the Streamable HTTP transport should
    carry the error on. Legacy-era errors are always 200 (a JSON-RPC
    error is still a successful HTTP exchange); the 2026-07-28 revision
    additionally requires 400 for a malformed or unsupported-version
    request and 404 for an unknown method.
    """

    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
        http_status: int = 200,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status

    def to_error_object(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class MalformedRequestError(JsonRpcError):
    """A modern request missing a required ``_meta`` field.

    Spec: "A request missing any required field is malformed; the server
    MUST reject it with -32602 (Invalid params). On HTTP, the response
    status MUST be 400." Kept distinct from the -32602 a bad tool
    argument gets, which stays HTTP 200 -- a bad argument is not a
    malformed envelope, and a 400 with a non-modern-specific body is what
    makes a dual-era client think it is talking to a legacy server.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(-32602, "Invalid params", detail, http_status=400)


class UnsupportedProtocolVersionError(JsonRpcError):
    """The request declares a protocol version this server does not serve.

    Mapped to -32022 with ``data.supported`` / ``data.requested`` so the
    client can retry with a mutually supported version (spec "Protocol
    Version Negotiation"). HTTP status 400.
    """

    def __init__(self, requested: Optional[str], supported: Tuple[str, ...]):
        super().__init__(
            -32022,
            "Unsupported protocol version",
            {"supported": list(supported), "requested": requested},
            http_status=400,
        )
        self.requested = requested


class MCPServer:
    """MCP Server that handles JSON-RPC requests and routes to Plugin Manager."""

    # Protocol revisions this server implements, newest first.
    #
    # MODERN revisions drop the initialize handshake: version, identity and
    # capabilities ride in every request's `_meta`, `server/discover` is
    # mandatory, `ping`/sessions/GET-SSE are gone, and every result carries
    # `resultType`. LEGACY revisions open with `initialize` and are served
    # exactly as before. The wire format for a tools-only server is
    # otherwise compatible across each group -- the additions each revision
    # brings (icons, tasks, elicitation) are optional and unused here -- so
    # we echo the client's requested version when it's one we recognize.
    MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
    LEGACY_PROTOCOL_VERSIONS = (
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    )
    SUPPORTED_PROTOCOL_VERSIONS = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS
    # On an unrecognized (or absent) requested version `initialize` answers
    # with the latest version the server supports -- the latest LEGACY one,
    # because initialize is itself the legacy handshake (see
    # _handle_initialize).
    DEFAULT_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]

    # Cache hints on modern results (spec "CacheableResult"). The tool set
    # and server identity only change on redeploy, so both are safe to
    # cache; tools/list gets the shorter window so a renamed tool is not
    # stale for long after a deploy.
    TOOLS_LIST_TTL_MS = 300_000
    DISCOVER_TTL_MS = 3_600_000

    # Methods whose Mcp-Name header must mirror `params.name` / `params.uri`.
    _NAMED_METHODS = {
        "tools/call": "name",
        "resources/read": "uri",
        "prompts/get": "name",
    }

    def __init__(self, plugin_manager: PluginManager) -> None:
        """Initialize MCP Server with Plugin Manager.

        Args:
            plugin_manager: Initialized Plugin Manager instance
        """
        self.plugin_manager = plugin_manager

    # ── Era classification ────────────────────────────────────────────

    @staticmethod
    def _request_meta(request: Dict[str, Any]) -> Dict[str, Any]:
        """The request's ``params._meta`` object, or {} if absent/invalid."""
        params = request.get("params")
        if not isinstance(params, dict):
            return {}
        meta = params.get("_meta")
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def request_era(
        cls,
        request: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """Which protocol era a request belongs to.

        Spec ("Backward Compatibility with Initialization-Based Versions"):
        a dual-era server selects its behaviour from how the client opens --
        an ``initialize`` request is legacy, a request carrying modern
        per-request ``_meta`` is modern. A body without ``_meta`` under a
        modern ``MCP-Protocol-Version`` header is a malformed modern
        request, so it is classified modern and rejected downstream rather
        than being quietly served as legacy.
        """
        if request.get("method") == "initialize":
            return ERA_LEGACY
        if META_PROTOCOL_VERSION in cls._request_meta(request):
            return ERA_MODERN
        declared = (headers or {}).get("mcp-protocol-version")
        if declared in cls.MODERN_PROTOCOL_VERSIONS:
            return ERA_MODERN
        return ERA_LEGACY

    # ── Request handling ──────────────────────────────────────────────

    async def handle_request(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
        era: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Handle a single MCP JSON-RPC request.

        Args:
            request: JSON-RPC request dictionary
            session_id: Optional MCP session ID for log correlation
                (legacy era only; modern requests have no session)
            era: ERA_LEGACY / ERA_MODERN, or None to classify from the
                request body alone

        Returns:
            JSON-RPC response dictionary, or None for notifications
        """
        response, _status = await self._handle_request_with_status(
            request, session_id=session_id, era=era
        )
        return response

    async def _handle_request_with_status(
        self,
        request: Dict[str, Any],
        session_id: Optional[str] = None,
        era: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """handle_request plus the HTTP status the response should carry.

        Legacy responses are always 200 (a JSON-RPC error is still a
        successful HTTP exchange). Modern responses follow the 2026-07-28
        Streamable HTTP rules: 400 for an unsupported version, header
        mismatch or malformed ``_meta``, 404 for an unknown method.
        """
        start_time = time.perf_counter()
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if era is None:
            era = self.request_era(request)
        meta = self._request_meta(request)
        # `params` is passed to the dispatchers UNCOERCED so that a
        # non-object params on tools/call still gets its own -32602; this
        # dict view is only for reading the envelope fields below.
        params_view = params if isinstance(params, dict) else {}

        # Check if this is a notification (no id field)
        is_notification = request_id is None

        # Log JSON-RPC request
        request_log_data = format_jsonrpc_request_log(
            request_id=request_id,
            method=method,
            params=params,
            is_notification=is_notification,
        )
        request_log_data["mcp_era"] = era
        # Flatten client identity onto the log line for BOTH eras, so
        # client-family dashboards keep working once claude.ai stops
        # sending initialize (where they used to read clientInfo from).
        client_info = (
            meta.get(META_CLIENT_INFO)
            if era == ERA_MODERN
            else params_view.get("clientInfo")
            if method == "initialize"
            else None
        )
        if isinstance(client_info, dict):
            request_log_data["mcp_client_name"] = client_info.get("name")
            request_log_data["mcp_client_version"] = client_info.get("version")
        declared_version = (
            meta.get(META_PROTOCOL_VERSION)
            if era == ERA_MODERN
            else params_view.get("protocolVersion")
        )
        if declared_version is not None:
            request_log_data["mcp_protocol_version"] = declared_version
        if session_id:
            request_log_data["mcp_session_id"] = session_id
        logger.info("JSON-RPC request received", extra=request_log_data)

        try:
            if era == ERA_MODERN:
                result = await self._dispatch_modern(
                    method, params, meta, is_notification
                )
            else:
                result = await self._dispatch_legacy(method, params, is_notification)

            if result is None or is_notification:
                # Notification (known or unknown): nothing to send back.
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "JSON-RPC notification processed",
                    extra={
                        **request_log_data,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return None, 202

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
            response_log_data["mcp_era"] = era
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.info(
                "JSON-RPC request processed successfully", extra=response_log_data
            )

            return response, 200

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
            response_log_data["mcp_era"] = era
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            # WARNING, not ERROR, and no exc_info: this is a caller
            # error. Tracebacks here bury genuine server faults.
            logger.warning(
                f"JSON-RPC caller error on {method}: {e.message} ({e.data})",
                extra={**response_log_data, "error_type": type(e).__name__},
            )

            if is_notification:
                return None, 202
            return error_response, e.http_status

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
            response_log_data["mcp_era"] = era
            if session_id:
                response_log_data["mcp_session_id"] = session_id
            logger.error(
                f"Error handling JSON-RPC request {method}: {e}",
                extra={**response_log_data, "error_type": type(e).__name__},
                exc_info=True,
            )

            # Don't send error response for notifications
            if is_notification:
                return None, 202
            return error_response, 200

    # ── Legacy era (initialize handshake) ─────────────────────────────

    async def _dispatch_legacy(
        self,
        method: Optional[str],
        params: Any,
        is_notification: bool,
    ) -> Optional[Dict[str, Any]]:
        """Route a legacy-era request. Returns None for notifications."""
        if method == "initialize":
            return await self._handle_initialize(
                params if isinstance(params, dict) else {}
            )
        if method == "tools/list":
            return await self._handle_tools_list()
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method == "ping":
            # Spec: the ping response MUST be an empty result object.
            # The liveness signal is the response itself, not its body.
            return {}
        if method == "notifications/initialized":
            # MCP notification - no response needed
            return None
        if is_notification:
            # For notifications with unknown methods, silently ignore
            logger.warning(f"Ignoring unknown notification method: {method}")
            return None
        raise JsonRpcError(-32601, "Method not found", f"Unknown method: {method}")

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request.

        Negotiates the protocol version against the client's request and
        populates serverInfo / instructions from config so each deployment
        can identify itself and steer the model independently.

        Only LEGACY revisions are negotiable here: ``initialize`` *is* the
        legacy handshake, so a client asking for 2026-07-28 on it is
        confused, and echoing that version back would promise stateless
        semantics on a session-based exchange. It falls back to the
        default like any other unrecognised version.

        Args:
            params: Initialize parameters

        Returns:
            Initialize response
        """
        config = self.plugin_manager.config or {}

        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version in self.LEGACY_PROTOCOL_VERSIONS
            else self.DEFAULT_PROTOCOL_VERSION
        )

        result: Dict[str, Any] = {
            "protocolVersion": protocol_version,
            "capabilities": self._capabilities(),
            "serverInfo": self._server_info(),
        }

        # Optional per-deployment guidance string surfaced to the client/model.
        instructions = config.get("instructions")
        if instructions:
            result["instructions"] = instructions

        return result

    # ── Modern era (per-request _meta, stateless) ─────────────────────

    async def _dispatch_modern(
        self,
        method: Optional[str],
        params: Any,
        meta: Dict[str, Any],
        is_notification: bool,
    ) -> Optional[Dict[str, Any]]:
        """Route a modern-era request. Returns None for notifications.

        Validates the per-request protocol fields first (spec "_meta >
        Per-request protocol fields"): ``protocolVersion`` and
        ``clientCapabilities`` are required on every request; a version we
        do not serve statelessly is -32022; then dispatches and stamps the
        result with ``resultType`` and our ``serverInfo``.
        """
        if is_notification:
            # This revision defines no client-to-server notifications on
            # Streamable HTTP; accept and drop anything that arrives.
            logger.warning(f"Ignoring unknown notification method: {method}")
            return None

        version = meta.get(META_PROTOCOL_VERSION)
        if not isinstance(version, str) or not version:
            raise MalformedRequestError(
                f"Missing required _meta field '{META_PROTOCOL_VERSION}'"
            )
        if version not in self.MODERN_PROTOCOL_VERSIONS:
            # Includes our own legacy versions: those are only served via
            # the initialize handshake, never as per-request metadata.
            raise UnsupportedProtocolVersionError(
                version, self.SUPPORTED_PROTOCOL_VERSIONS
            )
        if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
            raise MalformedRequestError(
                f"Missing required _meta field '{META_CLIENT_CAPABILITIES}'"
            )

        if method == "server/discover":
            result = await self._handle_discover()
        elif method == "tools/list":
            result = await self._handle_tools_list()
            result["ttlMs"] = self.TOOLS_LIST_TTL_MS
            result["cacheScope"] = "public"
        elif method == "tools/call":
            result = await self._handle_tools_call(params)
        else:
            # `ping`, `initialize`, `resources/*`, `subscriptions/listen`
            # (we advertise no listChanged) and anything else: not served
            # in this era. The spec requires HTTP 404 here, where the
            # legacy transport keeps -32601 inside a 200.
            raise JsonRpcError(
                -32601,
                "Method not found",
                f"Unknown method: {method}",
                http_status=404,
            )

        result["resultType"] = "complete"
        result.setdefault("_meta", {})[META_SERVER_INFO] = self._server_info()
        return result

    async def _handle_discover(self) -> Dict[str, Any]:
        """Handle server/discover: our versions, capabilities and identity.

        The modern replacement for ``initialize``'s response. Lists EVERY
        version we implement, newest first, exactly like the spec's own
        example -- a dual-era client picks the newest mutual one; a legacy
        client never calls this.
        """
        config = self.plugin_manager.config or {}
        result: Dict[str, Any] = {
            "supportedVersions": list(self.SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": self._capabilities(),
            "ttlMs": self.DISCOVER_TTL_MS,
            "cacheScope": "public",
        }
        instructions = config.get("instructions")
        if instructions:
            result["instructions"] = instructions
        return result

    # ── Shared ────────────────────────────────────────────────────────

    def _server_info(self) -> Dict[str, str]:
        config = self.plugin_manager.config or {}
        return {
            "name": config.get("server_name", "OpenContext"),
            "version": str(config.get("server_version", "1.0.0")),
        }

    @staticmethod
    def _capabilities() -> Dict[str, Any]:
        # Tools only; no listChanged, so modern clients have no reason to
        # open a subscriptions/listen stream.
        return {"tools": {}}

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
            response: Dict[str, Any] = {"content": result.content}
            if result.structured_content is not None:
                # The spec suggests ALSO serialising the JSON into a text
                # block for backwards compatibility. We deliberately do
                # not: the prose block already in `content` is the
                # human/model-readable rendering of the same data, and
                # duplicating it as raw JSON would roughly double the
                # token cost of every response for a consumer that is
                # usually an LLM.
                response["structuredContent"] = result.structured_content
            return response
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

    # ── Streamable HTTP ───────────────────────────────────────────────

    @staticmethod
    def _decode_header_value(value: str) -> Optional[str]:
        """Undo the ``=?base64?…?=`` sentinel encoding a client MUST use
        for header values that are not plain ASCII (spec "Value
        Encoding"). Returns None if the payload is not valid base64/UTF-8.
        """
        if value.startswith("=?base64?") and value.endswith("?="):
            try:
                return base64.b64decode(
                    value[len("=?base64?") : -len("?=")], validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        return value

    @classmethod
    def _modern_header_mismatch(
        cls,
        request: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Spec "Server Validation": the standard request headers must be
        present and mirror the body. Returns the failure description, or
        None if the request is consistent. Header names are looked up
        lowercased (both adapters normalise them that way).
        """
        method = request.get("method")
        meta = cls._request_meta(request)
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}

        version_header = headers.get("mcp-protocol-version")
        if version_header is None:
            return "required header MCP-Protocol-Version is missing"
        body_version = meta.get(META_PROTOCOL_VERSION)
        if body_version is not None and version_header != body_version:
            return (
                f"MCP-Protocol-Version header value '{version_header}' does "
                f"not match body value '{body_version}'"
            )

        method_header = headers.get("mcp-method")
        if method_header is None:
            return "required header Mcp-Method is missing"
        if method_header != method:
            return (
                f"Mcp-Method header value '{method_header}' does not match "
                f"body value '{method}'"
            )

        name_field = cls._NAMED_METHODS.get(method or "")
        body_name = params.get(name_field) if name_field else None
        if name_field and body_name is not None:
            name_header = headers.get("mcp-name")
            if name_header is None:
                return "required header Mcp-Name is missing"
            decoded = cls._decode_header_value(name_header)
            if decoded != body_name:
                return (
                    f"Mcp-Name header value '{name_header}' does not match "
                    f"body value '{body_name}'"
                )
        return None

    async def handle_http_request(
        self, body: str, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Handle HTTP request with MCP JSON-RPC payload.

        This method is used by Lambda handler to process HTTP requests.

        Args:
            body: Request body (JSON string)
            headers: HTTP headers, lowercased (optional)

        Returns:
            Response dictionary with statusCode and body
        """
        headers = headers or {}
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            # A caller error: they sent something that is not JSON. -32700
            # already tells them so; a traceback of our own json.loads adds
            # nothing and buries genuine faults in the log.
            logger.warning(
                f"Invalid JSON in request body: {e}",
                extra={"error_type": "JSONDecodeError"},
            )
            return self._http_error(400, None, -32700, "Parse error", str(e))
        if not isinstance(request, dict):
            return self._http_error(
                400,
                None,
                -32600,
                "Invalid Request",
                "Request body must be a single JSON-RPC object",
            )

        era = self.request_era(request, headers)

        session_id = None
        if era == ERA_MODERN:
            # Modern transport: the mirrored headers must agree with the
            # body before we trust either (spec "Server Validation").
            # Sessions do not exist in this era; an Mcp-Session-Id sent by
            # a confused client is ignored, never echoed.
            mismatch = self._modern_header_mismatch(request, headers)
            if mismatch:
                logger.warning(
                    f"400 error: header mismatch: {mismatch}",
                    extra={
                        "error_type": "HeaderMismatchError",
                        "jsonrpc_method": request.get("method"),
                        "mcp_era": era,
                    },
                )
                return self._http_error(
                    400,
                    request.get("id"),
                    -32020,
                    f"Header mismatch: {mismatch}",
                )
        else:
            # Pull MCP session ID from headers (lowercased by the adapter)
            # so downstream log lines can be grouped by session.
            session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")

        # Handle the request (logging is done in handle_request)
        response, status_code = await self._handle_request_with_status(
            request, session_id=session_id, era=era
        )

        # A notification gets 202 Accepted with no body (both eras).
        if response is None:
            return {
                "statusCode": 202,
                "headers": {"Content-Type": "application/json"},
                "body": "",
            }

        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response),
        }

    @staticmethod
    def _http_error(
        status: int,
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}),
        }

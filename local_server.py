# run_local_server.py
"""Run OpenContext MCP server locally for testing (no Lambda needed)."""

import asyncio
import json
import uuid

from aiohttp import web

from server import http_handler
from server.http_handler import UniversalHTTPHandler

# Shared handler instance -- the SAME class the Lambda adapter drives, so
# this entry point gets the Origin allowlist and MCP-Protocol-Version
# checks instead of a parallel path that skips them.
_handler = UniversalHTTPHandler()


async def init_server():
    """Initialize server on startup."""
    print("🚀 Initializing OpenContext MCP Server locally...")

    await http_handler._initialize_server()
    plugin_manager = http_handler._plugin_manager

    print("✅ Server initialized successfully")
    print(f"Loaded plugins: {list(plugin_manager.plugins.keys())}")
    print(f"Available tools: {len(plugin_manager.get_all_tools())}")


async def handle_mcp_request(request):
    """Handle MCP JSON-RPC request."""
    try:
        body = await request.text()
        # Lowercased to match what the Lambda adapter normalizes to.
        headers = {k.lower(): v for k, v in request.headers.items()}

        # This entry point has historically also served "/"; the shared
        # handler only routes MCP paths, so map it onto /mcp.
        path = "/mcp" if request.path == "/" else request.path

        status_code, response_headers, response_body = await _handler.handle_request(
            method=request.method,
            path=path,
            body=body,
            headers=headers,
            request_id=str(uuid.uuid4()),
        )

        return web.Response(
            text=response_body,
            status=status_code,
            headers=response_headers,
        )

    except Exception as e:
        return web.Response(
            text=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(e)},
                }
            ),
            status=500,
            headers={"Content-Type": "application/json"},
        )


async def start_server():
    """Start local HTTP server."""
    await init_server()

    app = web.Application()
    app.router.add_post("/", handle_mcp_request)
    app.router.add_post("/mcp", handle_mcp_request)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8000)
    await site.start()

    print("\n" + "=" * 50)
    print("🌐 Local MCP Server running!")
    print("=" * 50)
    print(f"URL: http://localhost:8000")
    print("\nTest with:")
    print("  opencontext-client http://localhost:8000")
    print("\nPress Ctrl+C to stop")
    print("=" * 50 + "\n")

    # Keep running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        if http_handler._plugin_manager is not None:
            await http_handler._plugin_manager.shutdown()


if __name__ == "__main__":
    asyncio.run(start_server())

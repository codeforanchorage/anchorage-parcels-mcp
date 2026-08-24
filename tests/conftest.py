"""Shared pytest fixtures.

The HTTP handler keeps its plugin manager, MCP server and config in
module-level globals so a warm Lambda container can reuse them across
invocations. That lifetime is wrong for tests: whatever one test leaves
behind is what the next test starts with, which turns an ordering change
into a mystery failure. Snapshot and restore them around every test.
"""

import pytest

from server import http_handler

_HANDLER_GLOBALS = ("_plugin_manager", "_mcp_server", "_config")


@pytest.fixture(autouse=True)
def restore_http_handler_globals():
    """Snapshot server.http_handler's warm-start globals, restore after."""
    saved = {name: getattr(http_handler, name) for name in _HANDLER_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(http_handler, name, value)

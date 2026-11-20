"""Shared pytest fixtures (like a jest setup file)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
DEMO_SERVER = ROOT / "examples" / "demo_server.py"


@pytest.fixture
def proxied_demo():
    """Factory: StdioServerParameters that start the demo server *through* the proxy.

    The test client plays the role of Claude Desktop.
    """

    def make(block: str = "") -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_guard", "proxy", "--name", "demo", "--", sys.executable, str(DEMO_SERVER)],
            env={**os.environ, "MCP_GUARD_BLOCK": block},
        )

    return make

"""Logging setup.

Everything goes to stderr. In proxy mode stdout IS the JSON-RPC channel to
Claude Desktop; one stray print() there corrupts the protocol. Claude Desktop
captures each server's stderr into its MCP log files, so stderr is also where
you'll find these logs on macOS: ~/Library/Logs/Claude/mcp-server-<name>.log
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
    root = logging.getLogger("mcp_guard")
    root.handlers[:] = [handler]
    root.setLevel(level)

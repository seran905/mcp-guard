"""Command-line entry point: one program, several modes.

    mcp-guard proxy --name demo -- uv run python examples/demo_server.py

`pyproject.toml` maps the `mcp-guard` command to `main()` here.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from mcp_guard import __version__
from mcp_guard.log import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block MCP tool calls.")
    parser.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    proxy = sub.add_parser("proxy", help="wrap a stdio MCP server (used from claude_desktop_config.json)")
    proxy.add_argument("--name", required=True, help="server name shown in the approval popup")
    proxy.add_argument("server_command", nargs=argparse.REMAINDER, help="-- <command> [args...]")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()

    if args.command == "proxy":
        from mcp_guard.proxy.stdio_proxy import StdioProxy  # lazy import: keeps other modes fast

        command = list(args.server_command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("proxy needs the real server command after --")
        return asyncio.run(StdioProxy(args.name, command).run())

    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())

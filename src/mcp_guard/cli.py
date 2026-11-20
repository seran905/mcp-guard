"""Command-line entry point: one program, several modes.

    mcp-guard proxy --name demo -- uv run python examples/demo_server.py

`pyproject.toml` maps the `mcp-guard` command to `main()` here.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from mcp_guard import __version__
from mcp_guard.log import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block MCP tool calls.")
    parser.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="run the approval daemon (shows popups)")
    daemon.add_argument(
        "--ui",
        choices=["qt", "allow", "block"],
        default="qt",
        help="qt = popups (default); allow/block = headless, for testing",
    )

    for cmd, text in (
        ("install", "guard every stdio server in claude_desktop_config.json"),
        ("uninstall", "restore claude_desktop_config.json to the original servers"),
        ("status", "show which Claude Desktop servers are guarded"),
    ):
        p = sub.add_parser(cmd, help=text)
        p.add_argument("--config", type=Path, default=None, help="path to claude_desktop_config.json")
        if cmd != "status":
            p.add_argument("--dry-run", action="store_true", help="show changes without writing")
        if cmd == "install":
            p.add_argument("--add-demo", action="store_true", help="also register examples/demo_server.py")

    proxy = sub.add_parser("proxy", help="wrap a stdio MCP server (used from claude_desktop_config.json)")
    proxy.add_argument("--name", required=True, help="server name shown in the approval popup")
    proxy.add_argument("server_command", nargs=argparse.REMAINDER, help="-- <command> [args...]")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()

    if args.command == "daemon":
        return _run_daemon(args.ui)

    if args.command in ("install", "uninstall", "status"):
        return _run_installer(args)

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


def _run_installer(args: argparse.Namespace) -> int:
    from mcp_guard.installer import claude_desktop as cd

    config = args.config or cd.default_config_path()
    try:
        if args.command == "install":
            demo = Path(__file__).resolve().parents[2] / "examples" / "demo_server.py" if args.add_demo else None
            changes = cd.install(config, demo_server=demo, dry_run=args.dry_run)
        elif args.command == "uninstall":
            changes = cd.uninstall(config, dry_run=args.dry_run)
        else:
            changes = cd.status(config)
    except cd.InstallError as exc:
        print(f"error: {exc}")
        return 1

    print(f"config: {config}")
    for change in changes or [cd.Change("(none)", "no MCP servers found")]:
        print(f"  {change.server:<24} {change.status}")
    if args.command != "status" and not getattr(args, "dry_run", False):
        print("\nFully quit Claude Desktop (Cmd+Q) and reopen it to apply.")
    return 0


def _run_daemon(ui: str) -> int:
    from mcp_guard.config import Settings

    settings = Settings.from_env()
    if ui == "qt":
        from mcp_guard.ui.popup import run_qt_daemon  # PySide6 is only imported here

        return run_qt_daemon(settings)

    from mcp_guard.daemon.approver import FixedApprover
    from mcp_guard.daemon.audit import AuditLog
    from mcp_guard.daemon.server import GuardDaemon
    from mcp_guard.decision.models import Verdict

    daemon = GuardDaemon(settings, FixedApprover(Verdict(ui)), AuditLog(settings.audit_path))
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

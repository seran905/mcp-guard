"""Wire mcp-guard into Claude Desktop by rewriting claude_desktop_config.json.

Before:
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}
After:
    "filesystem": {"command": "/abs/path/python",
                   "args": ["-m", "mcp_guard", "proxy", "--name", "filesystem", "--",
                            "/opt/homebrew/bin/npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

Design choices:
  - Absolute paths: Claude Desktop is a GUI app and does not get your shell's
    PATH, so bare names like `uv` or `npx` may not resolve. We resolve them now.
  - Reversible: the original command is kept verbatim after `--`, so uninstall
    needs no extra state. A timestamped backup is written before every change.
  - Safe writes: write to a temp file, then os.replace() (atomic on the same
    filesystem), so a crash can never leave Claude with a half-written config.
  - Remote servers (entries with "url") are skipped: Anthropic's cloud calls
    those, not this machine. They go through the HTTP gateway instead.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER_ARGS = ["-m", "mcp_guard", "proxy"]


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Change:
    server: str
    status: str  # guarded | already guarded | restored | skipped (...)


def default_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def is_guarded(entry: dict[str, Any]) -> bool:
    return list(entry.get("args") or [])[:3] == MARKER_ARGS


def wrap_entry(name: str, entry: dict[str, Any], python: str) -> dict[str, Any]:
    command = str(entry["command"])
    resolved = command if os.path.isabs(command) else (shutil.which(command) or command)
    wrapped = dict(entry)  # keeps "env" and any other keys untouched
    wrapped["command"] = python
    wrapped["args"] = [*MARKER_ARGS, "--name", name, "--", resolved, *(entry.get("args") or [])]
    return wrapped


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    args = list(entry["args"])
    sep = args.index("--")
    original = dict(entry)
    original["command"] = args[sep + 1]
    original["args"] = args[sep + 2 :]
    return original


def install(
    config_path: Path,
    python: str = sys.executable,
    demo_server: Path | None = None,
    dry_run: bool = False,
) -> list[Change]:
    data = _load(config_path)
    servers = data.setdefault("mcpServers", {})
    if demo_server is not None:
        servers.setdefault("demo-notes", {"command": python, "args": [str(demo_server.resolve())]})

    changes: list[Change] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict) or "command" not in entry:
            changes.append(Change(name, "skipped (remote/non-stdio server)"))
        elif is_guarded(entry):
            changes.append(Change(name, "already guarded"))
        else:
            servers[name] = wrap_entry(name, entry, python)
            changes.append(Change(name, "guarded"))

    if not dry_run:
        _backup(config_path)
        _atomic_write(config_path, data)
    return changes


def uninstall(config_path: Path, dry_run: bool = False) -> list[Change]:
    data = _load(config_path)
    servers = data.get("mcpServers", {})
    changes: list[Change] = []
    for name, entry in servers.items():
        if isinstance(entry, dict) and is_guarded(entry):
            servers[name] = unwrap_entry(entry)
            changes.append(Change(name, "restored"))
    if changes and not dry_run:
        _backup(config_path)
        _atomic_write(config_path, data)
    return changes


def status(config_path: Path) -> list[Change]:
    servers = _load(config_path).get("mcpServers", {})
    return [
        Change(name, "guarded" if isinstance(e, dict) and is_guarded(e) else "NOT guarded")
        for name, e in servers.items()
    ]


# --------------------------------------------------------------- file helpers


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        # Never overwrite a config we can't parse: the user would lose it.
        raise InstallError(f"{path} is not valid JSON ({exc}); fix it first, nothing was changed") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} must contain a JSON object")
    return data


def _backup(path: Path) -> None:
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-mcp-guard-{stamp}"))


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

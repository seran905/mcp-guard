"""Installer: rewrite Claude Desktop's config safely and reversibly."""

from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from mcp_guard.installer import claude_desktop as cd
from mcp_guard.decision.models import Verdict

from conftest import DEMO_SERVER

ORIGINAL = {
    "globalShortcut": "Ctrl+Space",  # unrelated settings must survive
    "mcpServers": {
        "demo": {"command": sys.executable, "args": [str(DEMO_SERVER)], "env": {"DEMO": "1"}},
        "remote": {"url": "https://example.com/mcp"},
    },
}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps(ORIGINAL))
    return path


def test_install_wraps_stdio_and_skips_remote(config):
    changes = {c.server: c.status for c in cd.install(config, python="/py")}
    assert changes == {"demo": "guarded", "remote": "skipped (remote/non-stdio server)"}

    data = json.loads(config.read_text())
    demo = data["mcpServers"]["demo"]
    assert demo["command"] == "/py"
    assert demo["args"] == ["-m", "mcp_guard", "proxy", "--name", "demo", "--", sys.executable, str(DEMO_SERVER)]
    assert demo["env"] == {"DEMO": "1"}
    assert data["mcpServers"]["remote"] == ORIGINAL["mcpServers"]["remote"]
    assert data["globalShortcut"] == "Ctrl+Space"
    assert list(config.parent.glob("*.bak-mcp-guard-*")), "backup written"


def test_install_is_idempotent(config):
    cd.install(config, python="/py")
    once = config.read_text()
    assert {c.status for c in cd.install(config, python="/py")} >= {"already guarded"}
    assert config.read_text() == once


def test_uninstall_restores_original(config):
    cd.install(config, python="/py")
    assert [c.status for c in cd.uninstall(config)] == ["restored"]
    assert json.loads(config.read_text()) == ORIGINAL


def test_invalid_json_is_never_overwritten(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ broken")
    with pytest.raises(cd.InstallError):
        cd.install(path)
    assert path.read_text() == "{ broken"


def test_dry_run_changes_nothing(config):
    before = config.read_text()
    cd.install(config, dry_run=True)
    assert config.read_text() == before


async def test_guarded_entry_runs_like_claude_desktop_would(config, daemon):
    """Launch the server exactly as Claude Desktop will: from the rewritten config entry."""
    cd.install(config)  # real python, so the entry is runnable
    entry = json.loads(config.read_text())["mcpServers"]["demo"]
    daemon.approver.verdicts["send_email"] = Verdict.BLOCK
    params = StdioServerParameters(
        command=entry["command"],
        args=entry["args"],
        env={**os.environ, **entry["env"], "MCP_GUARD_SOCKET": str(daemon.settings.socket_path)},
    )
    async with Client(params) as client:
        assert not (await client.call_tool("read_note", {"title": "welcome"})).is_error
        assert (await client.call_tool("send_email", {"to": "x", "subject": "s", "body": "b"})).is_error

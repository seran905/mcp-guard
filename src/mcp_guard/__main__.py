"""Allows `python -m mcp_guard ...` (used by tests and by the installer)."""

from mcp_guard.cli import main

raise SystemExit(main())

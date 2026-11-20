"""Demo MCP server used to exercise mcp-guard.

Three risk levels on purpose, so the demo video can show both outcomes:
  - read-only:   list_notes, read_note   -> usually Allow
  - destructive: delete_note             -> usually Block
  - exfiltrating: send_email (fake)      -> usually Block

Run over stdio (Claude Desktop):  uv run python examples/demo_server.py
Run over HTTP   (gateway demo):   uv run python examples/demo_server.py --http 8001
"""

import sys

from mcp.server import MCPServer  # MCP SDK v2 (v1 called this FastMCP)

mcp = MCPServer("demo-notes")

# Module-level dict = in-memory "database". Lives as long as the process.
NOTES: dict[str, str] = {
    "welcome": "Hello from the demo server.",
    "secrets": "AWS_SECRET_ACCESS_KEY=not-a-real-key",
}


@mcp.tool()
def list_notes() -> str:
    """List the titles of all notes, comma-separated."""
    return ", ".join(sorted(NOTES))


@mcp.tool()
def read_note(title: str) -> str:
    """Read a note by its title."""
    return NOTES.get(title, f"No note called {title!r}")


@mcp.tool()
def delete_note(title: str) -> str:
    """Permanently delete a note by its title."""
    if NOTES.pop(title, None) is None:
        return f"No note called {title!r}"
    return f"Deleted {title!r}"


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. (Fake: only logs to stderr, never sends anything.)"""
    # stderr, never stdout: stdout is the JSON-RPC channel.
    print(f"[demo-server] EMAIL to={to!r} subject={subject!r}", file=sys.stderr)
    return f"Email sent to {to}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        mcp.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))  # http://127.0.0.1:PORT/mcp
    else:
        mcp.run()  # no argument = stdio transport

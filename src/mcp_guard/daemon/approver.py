"""The "who decides" interface.

The daemon doesn't know or care whether a human clicks a Qt button, a test
script answers, or (future work) a policy engine decides. Anything with this
async `ask` method works. `Protocol` is structural typing: like a TS
interface, a class matches by shape, no inheritance needed.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from mcp_guard.decision.models import ApprovalRequest, Verdict


class Approver(Protocol):
    async def ask(self, req: ApprovalRequest, deadline: float) -> Verdict:
        """Return the verdict. `deadline` is a time.time() timestamp for UI countdowns.

        May be cancelled at any time (timeout, requester gone); clean up in `finally`.
        """
        ...


class FixedApprover:
    """Headless approver: always returns the same verdict. Used by `--ui allow|block`."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict

    async def ask(self, req: ApprovalRequest, deadline: float) -> Verdict:
        return self.verdict


class ScriptedApprover:
    """Test double: per-tool verdicts and delays. A verdict of None never answers."""

    def __init__(self, default: Verdict | None = Verdict.ALLOW) -> None:
        self.default = default
        self.verdicts: dict[str, Verdict | None] = {}
        self.delays: dict[str, float] = {}
        self.seen: list[ApprovalRequest] = []

    async def ask(self, req: ApprovalRequest, deadline: float) -> Verdict:
        self.seen.append(req)
        await asyncio.sleep(self.delays.get(req.tool, 0))
        verdict = self.verdicts.get(req.tool, self.default)
        if verdict is None:
            await asyncio.Event().wait()  # wait forever (until cancelled)
        assert verdict is not None
        return verdict

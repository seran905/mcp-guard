"""Always-on-top approval popups (PySide6 / Qt 6) and the Qt-hosted daemon.

Threading model -- the key idea in this file:

    main thread                         daemon thread
    -----------                         -------------
    Qt event loop (app.exec)            asyncio event loop (GuardDaemon)
    owns every window                   owns the Unix socket
          ^                                   |
          |   Signal.emit(...)  (thread-safe, |  queued onto the main thread)
          +-----------------------------------+
          |   loop.call_soon_threadsafe(...)  |
          +---------------------------------->+

Qt widgets may only be touched from the main thread, and asyncio objects only
from their loop's thread. The two arrows above are the only crossings.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mcp_guard.config import Settings
from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import GuardDaemon
from mcp_guard.decision.models import ApprovalRequest, Verdict

log = logging.getLogger(__name__)

# The Allow button stays disabled briefly, so a keystroke or click aimed at
# another window can't approve a call by accident when the popup appears.
ALLOW_ARM_DELAY_MS = 700
MAX_ARGS_SHOWN = 20_000

OnDecide = Callable[[Verdict], None]


class ApprovalWindow(QWidget):
    def __init__(self, req: ApprovalRequest, deadline: float, on_decide: OnDecide, stack_index: int) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self._on_decide = on_decide
        self._deadline = deadline
        self._decided = False

        self.setWindowTitle(f"mcp-guard: approve {req.tool}?")
        self.setMinimumWidth(480)

        title = QLabel(f"<b>Claude wants to run</b> <code>{html.escape(req.tool)}</code>")
        title.setTextFormat(Qt.TextFormat.RichText)
        meta = QLabel(f"Server: <b>{html.escape(req.server)}</b> &nbsp;·&nbsp; via {html.escape(req.source)}")
        meta.setTextFormat(Qt.TextFormat.RichText)

        args_text = json.dumps(req.arguments, indent=2, ensure_ascii=False, default=str)
        if len(args_text) > MAX_ARGS_SHOWN:
            args_text = args_text[:MAX_ARGS_SHOWN] + "\n... (truncated)"
        args = QPlainTextEdit(args_text)
        args.setReadOnly(True)
        args.setFont(QFont("Menlo", 11))
        args.setMinimumHeight(160)

        self._countdown = QLabel()
        block = QPushButton("Block")
        allow = QPushButton("Allow")
        block.setDefault(True)  # Enter = Block: the safe choice is the default
        allow.setAutoDefault(False)
        allow.setEnabled(False)
        QTimer.singleShot(ALLOW_ARM_DELAY_MS, lambda: allow.setEnabled(True))
        block.clicked.connect(lambda: self._decide(Verdict.BLOCK))
        allow.clicked.connect(lambda: self._decide(Verdict.ALLOW))

        buttons = QHBoxLayout()
        buttons.addWidget(self._countdown)
        buttons.addStretch()
        buttons.addWidget(block)
        buttons.addWidget(allow)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(meta)
        layout.addWidget(QLabel("Arguments:"))
        layout.addWidget(args)
        layout.addLayout(buttons)

        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(250)
        self._tick()
        self._place(stack_index)

    def present(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def dismiss(self) -> None:
        """Closed by the daemon (timeout / cancelled): no verdict to report."""
        self._decided = True
        self.close()

    # -- internals

    def _decide(self, verdict: Verdict) -> None:
        if self._decided:
            return
        self._decided = True
        self._on_decide(verdict)
        self.close()

    def _tick(self) -> None:
        remaining = max(0, int(self._deadline - time.time()))
        self._countdown.setText(f"Auto-block in {remaining}s")

    def _place(self, stack_index: int) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        offset = 32 * (stack_index % 8)  # cascade concurrent popups
        self.move(area.center().x() - 240 + offset, area.top() + 80 + offset)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if not self._decided:  # closing the window counts as Block
            self._decided = True
            self._on_decide(Verdict.BLOCK)
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._decide(Verdict.BLOCK)
        else:
            super().keyPressEvent(event)


class _Bridge(QObject):
    """Lives in the main thread. Emitting its signals from any thread is safe."""

    show_request = Signal(object, float, object)  # ApprovalRequest, deadline, OnDecide
    close_request = Signal(str)  # approval_id


class QtApprover:
    """Approver implementation backed by popups. Create it on the main thread."""

    def __init__(self) -> None:
        self._windows: dict[str, ApprovalWindow] = {}
        self._bridge = _Bridge()
        self._bridge.show_request.connect(self._on_show)
        self._bridge.close_request.connect(self._on_close)

    async def ask(self, req: ApprovalRequest, deadline: float) -> Verdict:
        # Runs on the daemon thread.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Verdict] = loop.create_future()

        def on_decide(verdict: Verdict) -> None:  # runs on the main thread
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(verdict))

        self._bridge.show_request.emit(req, deadline, on_decide)
        try:
            return await future
        finally:
            self._bridge.close_request.emit(req.approval_id)  # also on timeout/cancel

    @Slot(object, float, object)
    def _on_show(self, req: ApprovalRequest, deadline: float, on_decide: OnDecide) -> None:
        window = ApprovalWindow(req, deadline, on_decide, stack_index=len(self._windows))
        self._windows[req.approval_id] = window
        window.present()

    @Slot(str)
    def _on_close(self, approval_id: str) -> None:
        window = self._windows.pop(approval_id, None)
        if window is not None:
            window.dismiss()
            window.deleteLater()


class _DaemonThread(threading.Thread):
    """Runs GuardDaemon's asyncio loop off the main thread."""

    def __init__(self, daemon: GuardDaemon) -> None:
        super().__init__(name="mcp-guard-daemon", daemon=True)
        self._daemon = daemon
        self.ready = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        async def main() -> None:
            started = asyncio.Event()
            serve = asyncio.create_task(self._daemon.serve(started))
            waiter = asyncio.create_task(started.wait())
            await asyncio.wait({serve, waiter}, return_when=asyncio.FIRST_COMPLETED)
            self.ready.set()
            await serve  # re-raises if serve() failed

        try:
            asyncio.run(main())
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            self.ready.set()


def run_qt_daemon(settings: Settings) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("mcp-guard")
    app.setQuitOnLastWindowClosed(False)  # keep running in the background between popups
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C kill the Qt loop

    approver = QtApprover()
    thread = _DaemonThread(GuardDaemon(settings, approver, AuditLog(settings.audit_path)))
    thread.start()
    thread.ready.wait(10)
    if thread.error is not None:
        log.error("daemon failed to start: %s", thread.error)
        return 1

    # If the asyncio thread dies later, quit the app instead of showing a zombie UI.
    watchdog = QTimer()
    watchdog.timeout.connect(lambda: app.quit() if not thread.is_alive() else None)
    watchdog.start(1000)

    log.info("mcp-guard daemon running. Popups will appear for each MCP tool call. Ctrl+C to stop.")
    return app.exec()

"""Agent-side error capture — a root-logger WARNING+ handler feeding the
control-plane error_events sink (plan 007; pairs with luna-service plan 051).

`logging.Handler.emit` runs synchronously in whatever thread/task logged, so
it only enqueues to a bounded deque (drop-oldest) — a background task owns the
network call. Everything is best-effort: a failed forward never raises and
never logs above DEBUG (a WARNING here would loop back into the handler).
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import client
from .context import scrub

log = logging.getLogger("plugin-feedback.errors")

# Recursion guard: records from these loggers never enter the queue. Our own
# modules (a failed forward that logs would loop), and the HTTP stack we use
# to do the forwarding.
_SKIP_LOGGER_PREFIXES = ("plugin-feedback", "httpx", "httpcore")

_QUEUE_MAX = 200  # bounded; deque(maxlen=…) drops oldest
_BATCH_MAX = 25  # server caps batches at 50; stay well under
_FLUSH_INTERVAL_S = 30.0
_SEND_TIMEOUT_S = 5.0
_MAX_PER_MINUTE = 60  # enqueue throttle — an error storm must not melt a turn

_MAX_MESSAGE_CHARS = 500
_MAX_STACK_CHARS = 16 * 1024


def _severity(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "critical"
    if levelno >= logging.ERROR:
        return "error"
    return "warning"


def _event_from_record(record: logging.LogRecord) -> dict[str, Any]:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 — bad %-args in someone else's log call
        message = str(record.msg)
    context: dict[str, Any] = {
        "logger": record.name,
        "level": record.levelname,
        "module": record.module,
        "line": record.lineno,
    }
    kind = "agent_report"
    if record.exc_info and record.exc_info[0] is not None:
        kind = "plugin_exception"
        try:
            stack = "".join(traceback.format_exception(*record.exc_info))
            context["stack"] = scrub(stack)[:_MAX_STACK_CHARS]
        except Exception:  # noqa: BLE001
            pass
        if not message:
            message = f"{record.exc_info[0].__name__}: {record.exc_info[1]}"
    return {
        "source": "agent",
        "kind": kind,
        "severity": _severity(record.levelno),
        "message": scrub(message)[:_MAX_MESSAGE_CHARS],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "context": context,
    }


class ErrorCaptureHandler(logging.Handler):
    """WARNING+ on the root logger; forwards records to the shared capture."""

    def __init__(self, capture: "ErrorCapture") -> None:
        super().__init__(level=logging.WARNING)
        self._capture = capture

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            name = record.name or ""
            for prefix in _SKIP_LOGGER_PREFIXES:
                if name == prefix or name.startswith(prefix + "."):
                    return
            self._capture.enqueue_record(record)
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            pass


class ErrorCapture:
    """Bounded queue + background sender. One instance per plugin process."""

    def __init__(self) -> None:
        self._ctx: Any = None
        self._queue: deque[dict[str, Any]] = deque(maxlen=_QUEUE_MAX)
        self._handler: ErrorCaptureHandler | None = None
        self._task: asyncio.Task | None = None
        self._minute = 0
        self._minute_count = 0

    # -- lifecycle -------------------------------------------------------
    def attach(self, ctx: Any) -> None:
        """Install the root-logger handler and start the flush loop. Called
        from on_load (inside a running event loop). Idempotent."""
        self._ctx = ctx
        if self._handler is None:
            self._handler = ErrorCaptureHandler(self)
            logging.getLogger().addHandler(self._handler)
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._run())
            except RuntimeError:  # no running loop (tests) — flush manually
                self._task = None

    def detach(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._ctx = None

    # -- intake ----------------------------------------------------------
    def enqueue_record(self, record: logging.LogRecord) -> None:
        if self._throttled():
            return
        self._queue.append(_event_from_record(record))

    def _throttled(self) -> bool:
        minute = int(time.monotonic() // 60)
        if minute != self._minute:
            self._minute = minute
            self._minute_count = 0
        self._minute_count += 1
        return self._minute_count > _MAX_PER_MINUTE

    # -- delivery --------------------------------------------------------
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            await self.flush()

    async def flush(self) -> None:
        """Send one batch. Never raises; on failure the batch is dropped
        (error telemetry is not worth a retry queue)."""
        if not self._queue or self._ctx is None:
            return
        events: list[dict[str, Any]] = []
        while self._queue and len(events) < _BATCH_MAX:
            events.append(self._queue.popleft())
        try:
            await asyncio.wait_for(
                client.report_error(self._ctx, events), _SEND_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — best-effort by contract
            log.debug("error batch dropped (%d events)", len(events))


capture = ErrorCapture()

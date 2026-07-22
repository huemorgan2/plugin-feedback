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
_FAST_FLUSH_DELAY_S = 2.0  # ERROR+ debounce — beat a machine restart, batch a burst
_FAILURE_BACKOFF_S = 20.0  # after a failed delivery, fast-flush stands down

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._kick_pending = False
        self._last_failure = 0.0
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
                self._loop = asyncio.get_running_loop()
                self._task = self._loop.create_task(self._run())
            except RuntimeError:  # no running loop (tests) — flush manually
                self._loop = None
                self._task = None

    def detach(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._loop = None
        self._ctx = None

    # -- intake ----------------------------------------------------------
    def enqueue_record(self, record: logging.LogRecord) -> None:
        if self._throttled():
            return
        self._queue.append(_event_from_record(record))
        if record.levelno >= logging.ERROR:
            self._kick_soon()

    def _kick_soon(self) -> None:
        """ERROR+ records flush after ~2s instead of waiting the 30s tick —
        a crash-adjacent event must reach the sink before a machine restart
        can eat it. Debounced (one pending kick), thread-safe (emit() can run
        on any thread), and stands down while deliveries are failing so a
        dead token doesn't turn every error into a POST storm."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._kick_pending:
            return
        self._kick_pending = True

        def _schedule() -> None:
            async def _fast_flush() -> None:
                try:
                    await asyncio.sleep(_FAST_FLUSH_DELAY_S)
                    if time.monotonic() - self._last_failure >= _FAILURE_BACKOFF_S:
                        await self.flush()
                finally:
                    self._kick_pending = False

            loop.create_task(_fast_flush())

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:  # loop shut down between check and call
            self._kick_pending = False

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
        """Send one batch. Never raises. On delivery failure the batch is
        REQUEUED at the head (bounded by the deque) and retried on a later
        tick — the errors most worth keeping are the ones that explain why
        delivery itself is failing (dead gateway token, network outage), so
        dropping them hid exactly the incidents the sink exists for.
        Unconfigured (OSS, no control plane) still drains to nothing."""
        if not self._queue or self._ctx is None:
            return
        if client.get_config(self._ctx) is None:
            self._queue.clear()  # nowhere to deliver, ever — don't hoard
            return
        events: list[dict[str, Any]] = []
        while self._queue and len(events) < _BATCH_MAX:
            events.append(self._queue.popleft())
        result = None
        try:
            result = await asyncio.wait_for(
                client.report_error(self._ctx, events), _SEND_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — best-effort by contract
            result = None
        if result is None:  # report_error never raises; None = not delivered
            self._last_failure = time.monotonic()
            self._queue.extendleft(reversed(events))
            log.debug("error batch delivery failed — requeued (%d events)", len(events))
        else:
            self._last_failure = 0.0


capture = ErrorCapture()

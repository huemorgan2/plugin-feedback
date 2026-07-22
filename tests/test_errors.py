"""Agent-side error capture (plan 007): handler filtering, queue bounds,
throttle, flush delivery, and the never-raises contract of report_error."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx

from plugin_feedback import client, errors
from plugin_feedback.errors import ErrorCapture, ErrorCaptureHandler


def _record(name="some.app", level=logging.ERROR, msg="boom", exc_info=None):
    return logging.LogRecord(
        name=name, level=level, pathname="x.py", lineno=42,
        msg=msg, args=(), exc_info=exc_info,
    )


def make_capture(configured: bool = False):
    """configured=True gives the ctx gateway credentials so flush() reaches
    the network layer (which tests monkeypatch); default is an OSS install."""
    if configured:
        env = {"LUNA_GATEWAY_URL": "https://cp.example/proxy", "LUNA_GATEWAY_TOKEN": "lsv1-test"}
        ctx = SimpleNamespace(get_env=lambda n: env.get(n))
    else:
        ctx = SimpleNamespace(get_env=lambda n: None)
    cap = ErrorCapture()
    cap._ctx = ctx
    return cap


# -- handler filtering ---------------------------------------------------

def test_handler_skips_own_and_http_stack_loggers():
    cap = make_capture()
    handler = ErrorCaptureHandler(cap)
    for name in ("plugin-feedback", "plugin-feedback.errors", "httpx", "httpcore.http11"):
        handler.emit(_record(name=name))
    assert len(cap._queue) == 0
    handler.emit(_record(name="uvicorn.error"))
    assert len(cap._queue) == 1


def test_handler_level_is_warning_plus():
    handler = ErrorCaptureHandler(make_capture())
    assert handler.level == logging.WARNING


# -- event shaping -------------------------------------------------------

def test_event_from_plain_warning():
    cap = make_capture()
    cap.enqueue_record(_record(level=logging.WARNING, msg="slow thing"))
    event = cap._queue[0]
    assert event["source"] == "agent"
    assert event["kind"] == "agent_report"
    assert event["severity"] == "warning"
    assert event["message"] == "slow thing"
    assert event["context"]["logger"] == "some.app"


def test_event_with_exc_info_is_plugin_exception_and_scrubbed():
    try:
        raise RuntimeError("failed with sk-ABCDEFGHIJKLMNOPQRST token")
    except RuntimeError:
        import sys
        exc_info = sys.exc_info()
    cap = make_capture()
    cap.enqueue_record(_record(level=logging.ERROR, msg="", exc_info=exc_info))
    event = cap._queue[0]
    assert event["kind"] == "plugin_exception"
    assert event["severity"] == "error"
    assert "RuntimeError" in event["message"]
    assert "sk-ABCDEFGHIJKLMNOPQRST" not in event["context"]["stack"]
    assert "[redacted]" in event["context"]["stack"]


# -- bounds --------------------------------------------------------------

def test_queue_drops_oldest_when_full(monkeypatch):
    monkeypatch.setattr(errors, "_MAX_PER_MINUTE", 10_000)
    cap = make_capture()
    for i in range(errors._QUEUE_MAX + 25):
        cap.enqueue_record(_record(msg=f"e{i}"))
    assert len(cap._queue) == errors._QUEUE_MAX
    assert cap._queue[0]["message"] == "e25"  # oldest dropped


def test_per_minute_throttle():
    cap = make_capture()
    for i in range(errors._MAX_PER_MINUTE + 20):
        cap.enqueue_record(_record(msg=f"e{i}"))
    assert len(cap._queue) == errors._MAX_PER_MINUTE


# -- flush ---------------------------------------------------------------

async def test_flush_sends_batch_and_drains(monkeypatch):
    sent = []

    async def fake_report(ctx, events):
        sent.append(events)
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", fake_report)
    cap = make_capture(configured=True)
    for i in range(errors._BATCH_MAX + 5):
        cap.enqueue_record(_record(msg=f"e{i}"))
    await cap.flush()
    assert len(sent) == 1
    assert len(sent[0]) == errors._BATCH_MAX
    assert len(cap._queue) == 5


async def test_flush_unconfigured_drains_to_nothing(monkeypatch):
    async def fake_report(ctx, events):  # pragma: no cover — must not be reached
        raise AssertionError("unconfigured install must never POST")

    monkeypatch.setattr(client, "report_error", fake_report)
    cap = make_capture(configured=False)
    cap.enqueue_record(_record())
    await cap.flush()
    assert len(cap._queue) == 0


async def test_flush_never_raises_and_requeues_on_failure(monkeypatch):
    async def boom(ctx, events):
        raise RuntimeError("network down")

    monkeypatch.setattr(client, "report_error", boom)
    cap = make_capture(configured=True)
    cap.enqueue_record(_record(msg="first"))
    cap.enqueue_record(_record(msg="second"))
    await cap.flush()  # must not raise
    # 0.3.0: failed batch is requeued in order, not dropped — the errors that
    # explain a delivery outage (dead token) must survive until it heals.
    assert [e["message"] for e in cap._queue] == ["first", "second"]
    assert cap._last_failure > 0


async def test_flush_requeued_batch_delivers_after_recovery(monkeypatch):
    calls = {"n": 0}

    async def flaky(ctx, events):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # report_error's own failure signature
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", flaky)
    cap = make_capture(configured=True)
    cap.enqueue_record(_record(msg="keep me"))
    await cap.flush()
    assert len(cap._queue) == 1
    await cap.flush()
    assert len(cap._queue) == 0
    assert cap._last_failure == 0.0


# -- fast flush on ERROR+ ------------------------------------------------

async def test_error_record_triggers_fast_flush(monkeypatch):
    import asyncio

    monkeypatch.setattr(errors, "_FAST_FLUSH_DELAY_S", 0.01)
    sent = []

    async def fake_report(ctx, events):
        sent.append(events)
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", fake_report)
    cap = make_capture(configured=True)
    cap._loop = asyncio.get_running_loop()
    cap.enqueue_record(_record(level=logging.ERROR, msg="crash"))
    await asyncio.sleep(0.1)
    assert len(sent) == 1  # flushed in ~10ms, not the 30s tick
    assert not cap._kick_pending


async def test_warning_record_does_not_fast_flush(monkeypatch):
    import asyncio

    monkeypatch.setattr(errors, "_FAST_FLUSH_DELAY_S", 0.01)
    sent = []

    async def fake_report(ctx, events):
        sent.append(events)
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", fake_report)
    cap = make_capture(configured=True)
    cap._loop = asyncio.get_running_loop()
    cap.enqueue_record(_record(level=logging.WARNING, msg="meh"))
    await asyncio.sleep(0.05)
    assert sent == []  # warnings wait for the interval tick
    assert len(cap._queue) == 1


async def test_fast_flush_stands_down_during_failure_backoff(monkeypatch):
    import asyncio
    import time as _time

    monkeypatch.setattr(errors, "_FAST_FLUSH_DELAY_S", 0.01)
    sent = []

    async def fake_report(ctx, events):
        sent.append(events)
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", fake_report)
    cap = make_capture(configured=True)
    cap._loop = asyncio.get_running_loop()
    cap._last_failure = _time.monotonic()  # a delivery just failed
    cap.enqueue_record(_record(level=logging.ERROR, msg="crash"))
    await asyncio.sleep(0.05)
    assert sent == []  # no POST storm against a dead token
    assert len(cap._queue) == 1  # kept for the next interval tick


async def test_flush_without_ctx_is_noop():
    cap = ErrorCapture()
    cap.enqueue_record(_record())
    await cap.flush()
    assert len(cap._queue) == 1


# -- attach/detach -------------------------------------------------------

async def test_attach_detach_root_handler():
    cap = ErrorCapture()
    ctx = SimpleNamespace(get_env=lambda n: None)
    root = logging.getLogger()
    before = list(root.handlers)
    cap.attach(ctx)
    assert cap._handler in root.handlers
    cap.attach(ctx)  # idempotent — no duplicate handler
    assert root.handlers.count(cap._handler) == 1
    cap.detach()
    assert root.handlers == before


# -- client.report_error -------------------------------------------------

def _clean_env(monkeypatch):
    for name in (client.ENV_SERVICE_URL, client.ENV_TOKEN,
                 client.ENV_GATEWAY_URL, client.ENV_GATEWAY_TOKEN):
        monkeypatch.delenv(name, raising=False)


async def _with_transport(monkeypatch, handler, call):
    _clean_env(monkeypatch)
    monkeypatch.setenv(client.ENV_SERVICE_URL, "http://svc")
    monkeypatch.setenv(client.ENV_TOKEN, "tok")
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return await call()


async def test_report_error_posts_batch(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": 1})

    events = [{"kind": "agent_report", "message": "x"}]
    result = await _with_transport(
        monkeypatch, handler,
        lambda: client.report_error(SimpleNamespace(), events),
    )
    assert result == {"accepted": 1}
    assert seen["url"] == "http://svc/api/agent/errors"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"events": events}


async def test_report_error_unconfigured_returns_none(monkeypatch):
    _clean_env(monkeypatch)
    ctx = SimpleNamespace(get_env=lambda n: None)
    assert await client.report_error(ctx, [{"message": "x"}]) is None


async def test_report_error_http_error_returns_none(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="oops")

    result = await _with_transport(
        monkeypatch, handler,
        lambda: client.report_error(SimpleNamespace(), [{"message": "x"}]),
    )
    assert result is None


async def test_report_error_transport_error_returns_none(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    result = await _with_transport(
        monkeypatch, handler,
        lambda: client.report_error(SimpleNamespace(), [{"message": "x"}]),
    )
    assert result is None


async def test_report_error_empty_batch_is_noop(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv(client.ENV_SERVICE_URL, "http://svc")
    monkeypatch.setenv(client.ENV_TOKEN, "tok")
    assert await client.report_error(SimpleNamespace(), []) is None

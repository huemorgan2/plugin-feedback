"""Plan 002 — ticket idempotency (luna plans/106 phase 6).

Evidence: the 08-31 five-ticket cancel cascade and 09-01 correction tickets —
create_ticket had no idempotency key and no dedupe anywhere.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin_feedback import client as fb_client
from plugin_feedback import tools as fb_tools
from plugin_feedback.tools import _client_ref, register_tools


class FakeToolRegistry:
    def __init__(self):
        self.registered: dict[str, dict] = {}

    def register(self, plugin, tool_def, handler, skill_gated=None):
        self.registered[tool_def.name] = {"handler": handler}


class FakeEvents:
    async def emit(self, name, payload):
        pass


def make_ctx():
    return SimpleNamespace(
        tool_registry=FakeToolRegistry(),
        skill_registry=None,
        events=FakeEvents(),
        current_conversation_id=None,
        get_env=lambda name: "test-host" if name == "LUNA_HOST_NAME" else "",
    )


def test_client_ref_is_deterministic_and_content_addressed():
    ctx = make_ctx()
    a = _client_ref(ctx, "title", "body")
    assert a == _client_ref(ctx, "title", "body")
    assert a != _client_ref(ctx, "title", "other body")
    # different host → different ref (two agents may report the same words)
    other = SimpleNamespace(get_env=lambda n: "host-2")
    assert a != _client_ref(other, "title", "body")


@pytest.mark.asyncio
async def test_payload_carries_client_ref(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.7.0")
    sent = {}

    async def fake_create(ctx_, payload):
        sent.update(payload)
        return {"id": "t-1"}

    monkeypatch.setattr(fb_client, "create_ticket", fake_create)
    handler = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    out = await handler(summary="broken", details="the pane is blank")
    assert out["sent"] is True
    assert sent["client_ref"] == _client_ref(ctx, sent["title"], sent["body"])


@pytest.mark.asyncio
async def test_identical_resend_within_window_is_refused(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.7.0")
    calls = []

    async def fake_create(ctx_, payload):
        calls.append(payload)
        return {"id": "t-1"}

    monkeypatch.setattr(fb_client, "create_ticket", fake_create)
    handler = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]

    first = await handler(summary="broken", details="the pane is blank")
    assert first["sent"] is True
    second = await handler(summary="broken", details="the pane is blank")
    assert second["sent"] is False
    assert second["duplicate_of"] == "t-1"
    assert "feedback_ticket_reply" in second["note"]
    assert len(calls) == 1

    # different content is NOT blocked
    third = await handler(summary="broken", details="now with repro steps")
    assert third["sent"] is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_guard_expires_after_ttl(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.7.0")

    async def fake_create(ctx_, payload):
        return {"id": "t-1"}

    monkeypatch.setattr(fb_client, "create_ticket", fake_create)
    handler = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    await handler(summary="broken", details="x")

    # shrink the window to zero so the entry is pruned as expired
    monkeypatch.setattr(fb_tools, "_RECENT_TTL_S", -1.0)
    again = await handler(summary="broken", details="x")
    assert again["sent"] is True


@pytest.mark.asyncio
async def test_failed_send_does_not_arm_the_guard(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.7.0")
    attempts = []

    async def failing_create(ctx_, payload):
        attempts.append(payload)
        raise fb_client.FeedbackError(503, "down")

    monkeypatch.setattr(fb_client, "create_ticket", failing_create)
    handler = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    out = await handler(summary="broken", details="x")
    assert "error" in out
    retry = await handler(summary="broken", details="x")
    assert "error" in retry  # retried the service, not refused as duplicate
    assert len(attempts) == 2

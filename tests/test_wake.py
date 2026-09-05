"""plan 003 — team-reply wake + pane refetch-loop fix.

Double-trigger contract: one moment per admin reply, and reads never emit
feedback.updated (the pane refetches on that event — emitting from its own
GET looped forever).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from plugin_feedback import FeedbackPlugin, routes
from plugin_feedback import client as fb_client
from plugin_feedback.wake import ReplyWakePoller

ORIGIN = uuid.uuid4()
OPS = uuid.uuid4()


def make_ctx(*, muted=True, ops_ok=True):
    moments: list[dict] = []
    emitted: list[tuple[str, dict]] = []

    async def emit(name, payload):
        emitted.append((name, payload))

    async def send(title, content, **kw):
        moments.append({"title": title, "content": content, **kw})
        return {"queued": True}

    async def ops():
        if not ops_ok:
            raise RuntimeError("no ops chat")
        return OPS

    class Registry:
        def register(self, *a, **k):
            pass

    class Skills(Registry):
        def unregister_plugin(self, plugin):
            pass

    ctx = SimpleNamespace(
        tool_registry=Registry(),
        skill_registry=Skills(),
        events=SimpleNamespace(emit=emit),
        current_conversation_id=None,
        get_env=lambda name: None,
        ops_conversation_id=ops,
        moments=moments,
        emitted=emitted,
    )
    if muted:
        ctx.send_muted_message = send
    return ctx


def _updates(entries):
    async def fn(ctx):
        return {"unread": entries}

    return fn


def _detail(context=None, replies=("We fixed it — try again.",)):
    async def fn(ctx, ticket_id, *, mark_read=True):
        assert mark_read is False  # the wake must not consume unread state
        return {
            "ticket": {
                "id": ticket_id,
                "title": "Agent behaviour: mediocre",
                "context": context or {},
            },
            "messages": [{"author": "user", "body": "help"}]
            + [{"author": "admin", "body": b} for b in replies],
        }

    return fn


# ---------------- poller ----------------


async def test_wake_routes_origin_conversation_exactly_once(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(
        fb_client, "updates",
        _updates([{"id": "t-1", "title": "x", "last_admin_reply_at": "2026-09-05T10:00:00Z"}]),
    )
    monkeypatch.setattr(
        fb_client, "get_ticket", _detail(context={"conversation_id": str(ORIGIN)})
    )
    p = ReplyWakePoller(ctx)
    await p._pass()
    assert len(ctx.moments) == 1
    m = ctx.moments[0]
    assert m["conversation_id"] == ORIGIN
    assert m["channel"] == "moment" and m["source"] == "feedback"
    assert "We fixed it" in m["content"]
    assert ("feedback.updated", {"ticket_id": "t-1"}) in ctx.emitted

    await p._pass()  # same reply → silent
    assert len(ctx.moments) == 1


async def test_newer_admin_reply_notifies_again(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(
        fb_client, "get_ticket", _detail(context={"conversation_id": str(ORIGIN)})
    )
    p = ReplyWakePoller(ctx)
    monkeypatch.setattr(
        fb_client, "updates",
        _updates([{"id": "t-1", "last_admin_reply_at": "2026-09-05T10:00:00Z"}]),
    )
    await p._pass()
    monkeypatch.setattr(
        fb_client, "updates",
        _updates([{"id": "t-1", "last_admin_reply_at": "2026-09-05T11:00:00Z"}]),
    )
    await p._pass()
    assert len(ctx.moments) == 2


async def test_no_origin_falls_back_to_ops(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(
        fb_client, "updates",
        _updates([{"id": "t-1", "last_admin_reply_at": "2026-09-05T10:00:00Z"}]),
    )
    monkeypatch.setattr(fb_client, "get_ticket", _detail(context={}))
    p = ReplyWakePoller(ctx)
    await p._pass()
    assert len(ctx.moments) == 1
    assert ctx.moments[0]["conversation_id"] == OPS


async def test_unroutable_is_marked_and_dropped(monkeypatch):
    ctx = make_ctx(ops_ok=False)
    monkeypatch.setattr(
        fb_client, "updates",
        _updates([{"id": "t-1", "last_admin_reply_at": "2026-09-05T10:00:00Z"}]),
    )
    monkeypatch.setattr(fb_client, "get_ticket", _detail(context={}))
    p = ReplyWakePoller(ctx)
    await p._pass()
    assert ctx.moments == []
    await p._pass()  # never retried into a nag
    assert ctx.moments == []


# ---------------- plugin wiring ----------------


async def test_prompt_note_retired_when_wake_capable(monkeypatch):
    monkeypatch.setattr(
        fb_client, "get_config", lambda ctx: {"base": "http://cp", "token": "t"}
    )
    called = {"n": 0}

    async def updates(ctx):
        called["n"] += 1
        return {"unread": [{"id": "t-1", "title": "x"}]}

    monkeypatch.setattr(fb_client, "updates", updates)
    plugin = FeedbackPlugin()
    ctx = make_ctx()
    await plugin.on_load(ctx)
    assert plugin._poller is not None
    sections = await plugin.prompt_sections()
    assert len(sections) == 1  # wake replaced the note — one delivery path
    assert called["n"] == 0
    plugin._poller.stop()


async def test_old_core_keeps_prompt_note(monkeypatch):
    monkeypatch.setattr(
        fb_client, "get_config", lambda ctx: {"base": "http://cp", "token": "t"}
    )
    monkeypatch.setattr(
        fb_client, "updates", _updates([{"id": "t-1", "title": "Too expensive"}])
    )
    plugin = FeedbackPlugin()
    await plugin.on_load(make_ctx(muted=False))
    assert plugin._poller is None
    sections = await plugin.prompt_sections()
    assert len(sections) == 2
    assert "Too expensive" in sections[1]


# ---------------- route: reads never emit ----------------


async def test_get_ticket_does_not_emit(monkeypatch):
    ctx = make_ctx()
    app = FastAPI()
    routes.register_routes(app, ctx)

    async def fake_get(route_ctx, ticket_id, *, mark_read=True):
        return {"ticket": {"id": ticket_id}, "messages": []}

    monkeypatch.setattr(fb_client, "get_ticket", fake_get)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/api/p/plugin-feedback/tickets/t-1")
    assert resp.status_code == 200
    assert ctx.emitted == []  # reads are not updates — no refetch loop

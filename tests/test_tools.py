from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin_feedback import client as fb_client
from plugin_feedback.tools import register_tools


class FakeToolRegistry:
    def __init__(self, supports_skill_gated: bool = True):
        self.supports = supports_skill_gated
        self.registered: dict[str, dict] = {}

    def register(self, plugin, tool_def, handler, skill_gated=None):
        if skill_gated is not None and not self.supports:
            raise TypeError("no skill_gated kwarg")
        self.registered[tool_def.name] = {
            "handler": handler,
            "gated": bool(skill_gated),
        }


class FakeSkillRegistry:
    def __init__(self):
        self.skills: list = []

    def unregister_plugin(self, plugin):
        pass

    def register(self, plugin, skill):
        self.skills.append(skill)


class FakeEvents:
    def __init__(self):
        self.emitted: list = []

    async def emit(self, name, payload):
        self.emitted.append((name, payload))


def make_ctx(supports_skill_gated=True, with_skills=True):
    return SimpleNamespace(
        tool_registry=FakeToolRegistry(supports_skill_gated),
        skill_registry=FakeSkillRegistry() if with_skills else None,
        events=FakeEvents(),
        current_conversation_id=None,
    )


def test_send_ungated_rest_gated():
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    reg = ctx.tool_registry.registered
    assert set(reg) == {"feedback_ticket_send", "feedback_ticket_list", "feedback_ticket_get", "feedback_ticket_reply"}
    assert reg["feedback_ticket_send"]["gated"] is False
    for name in ("feedback_ticket_list", "feedback_ticket_get", "feedback_ticket_reply"):
        assert reg[name]["gated"] is True
    assert len(ctx.skill_registry.skills) == 1
    skill = ctx.skill_registry.skills[0]
    assert skill.name == "feedback-tickets"
    assert set(skill.tools) == {"feedback_ticket_list", "feedback_ticket_get", "feedback_ticket_reply"}


def test_degrades_without_skill_registry():
    ctx = make_ctx(with_skills=False)
    register_tools(ctx, "0.1.0")
    assert all(not v["gated"] for v in ctx.tool_registry.registered.values())


def test_degrades_without_skill_gated_kwarg():
    ctx = make_ctx(supports_skill_gated=False)
    register_tools(ctx, "0.1.0")
    assert all(not v["gated"] for v in ctx.tool_registry.registered.values())
    assert ctx.skill_registry.skills == []  # no gating → no skill claiming gated tools


async def test_send_builds_payload(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    captured = {}

    async def fake_create(c, payload):
        captured.update(payload)
        return {"id": "t-1", "status": "open"}

    monkeypatch.setattr(fb_client, "create_ticket", fake_create)
    send = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    result = await send(
        summary="Too expensive",
        details="Owner said: this is fucking expensive",
        category="cost",
        written_by="owner",
        technical={"error": "billing 402 sk-abcdefghijklmnop123456"},
    )
    assert result["sent"] is True and result["ticket_id"] == "t-1"
    assert captured["origin"] == "user"
    assert captured["category"] == "cost"
    assert captured["title"] == "Too expensive"
    assert "client_time" in captured["context"]
    assert "[redacted]" in captured["technical"]["error"]
    assert ctx.events.emitted and ctx.events.emitted[0][0] == "feedback.updated"


async def test_send_agent_origin(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    captured = {}

    async def fake_create(c, payload):
        captured.update(payload)
        return {"id": "t-2"}

    monkeypatch.setattr(fb_client, "create_ticket", fake_create)
    send = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    await send(summary="s", details="d", written_by="agent")
    assert captured["origin"] == "agent"


async def test_send_validates_inputs():
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    send = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    assert "error" in await send(summary="s", details="d", category="nope")
    assert "error" in await send(summary="s", details="d", written_by="user")


async def test_not_connected_is_readable(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")

    async def boom(c, payload):
        raise fb_client.NotConnected("feedback service not connected")

    monkeypatch.setattr(fb_client, "create_ticket", boom)
    send = ctx.tool_registry.registered["feedback_ticket_send"]["handler"]
    result = await send(summary="s", details="d")
    assert "not connected" in result["error"]


async def test_reply_maps_author(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    captured = {}

    async def fake_reply(c, ticket_id, *, author, body):
        captured.update(ticket_id=ticket_id, author=author, body=body)
        return {"id": "m-1"}

    monkeypatch.setattr(fb_client, "reply", fake_reply)
    reply = ctx.tool_registry.registered["feedback_ticket_reply"]["handler"]
    await reply(ticket_id="t-1", message="answer", written_by="agent")
    assert captured == {"ticket_id": "t-1", "author": "agent", "body": "answer"}


async def test_get_marks_read(monkeypatch):
    ctx = make_ctx()
    register_tools(ctx, "0.1.0")
    captured = {}

    async def fake_get(c, ticket_id, *, mark_read):
        captured.update(ticket_id=ticket_id, mark_read=mark_read)
        return {"ticket": {"id": ticket_id}, "messages": []}

    monkeypatch.setattr(fb_client, "get_ticket", fake_get)
    get = ctx.tool_registry.registered["feedback_ticket_get"]["handler"]
    await get(ticket_id="t-9")
    assert captured == {"ticket_id": "t-9", "mark_read": True}

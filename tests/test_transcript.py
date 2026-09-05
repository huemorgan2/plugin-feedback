"""011 stage 3 — transcript_block: rendering, clamps, scrub, fallbacks."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from plugin_feedback.context import transcript_block


class _Reader:
    """Newest-first, like the real reader with order='desc'."""

    def __init__(self, messages):
        self._messages = messages
        self.calls = []

    async def messages(self, ids, *, roles, order, limit):
        self.calls.append({"ids": ids, "roles": roles, "order": order, "limit": limit})
        assert order == "desc"
        return self._messages[:limit]


def _msg(role, content):
    return SimpleNamespace(role=role, content=content, created_at=None)


async def test_transcript_renders_oldest_first_and_scrubs():
    reader = _Reader([
        _msg("assistant", "here is the key lsv1-abc123DEF456ghi"),
        _msg("user", "please fix it"),
    ])
    ctx = SimpleNamespace(conversations=reader)
    out = await transcript_block(ctx, str(uuid.uuid4()))
    assert out.index("[user]\nplease fix it") < out.index("[assistant]")
    assert "lsv1-" not in out and "[redacted]" in out
    assert reader.calls[0]["limit"] == 30


async def test_transcript_clamps_each_message():
    reader = _Reader([_msg("user", "x" * 5000)])
    ctx = SimpleNamespace(conversations=reader)
    out = await transcript_block(ctx, str(uuid.uuid4()))
    assert len(out) < 2100 and out.endswith("…")


async def test_transcript_total_cap_keeps_newest():
    # 20 fat messages blow the total cap; the NEWEST must survive the cut.
    reader = _Reader([_msg("user", f"m{i} " + "y" * 1900) for i in range(20)])
    ctx = SimpleNamespace(conversations=reader)
    out = await transcript_block(ctx, str(uuid.uuid4()))
    assert len(out) <= 24_000 + 100
    assert "m0 " in out  # newest (first from the reader) kept
    assert "m19 " not in out  # oldest dropped


async def test_transcript_no_reader_or_conversation_is_empty():
    assert await transcript_block(SimpleNamespace(), str(uuid.uuid4())) == ""
    # No id and no db factory → no fallback conversation → empty.
    ctx = SimpleNamespace(conversations=_Reader([_msg("user", "hi")]))
    assert await transcript_block(ctx, None) == ""


async def test_transcript_reader_error_is_swallowed():
    class _Boom:
        async def messages(self, *a, **k):
            raise RuntimeError("db down")

    ctx = SimpleNamespace(conversations=_Boom())
    assert await transcript_block(ctx, str(uuid.uuid4())) == ""


# ---------- create_ticket route with include_context ----------

import httpx
from fastapi import FastAPI

from plugin_feedback import client, routes


def _route_ctx(reader):
    async def emit(name, payload):
        pass

    return SimpleNamespace(
        events=SimpleNamespace(emit=emit),
        get_env=lambda name: None,
        current_conversation_id=None,
        conversations=reader,
    )


async def _post_ticket(reader, payload, monkeypatch):
    app = FastAPI()
    routes.register_routes(app, _route_ctx(reader))
    captured = {}

    async def fake_create(ctx, body):
        captured.update(body)
        return {"id": "t1"}

    monkeypatch.setattr(client, "create_ticket", fake_create)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/p/plugin-feedback/tickets", json=payload)
    assert r.status_code == 201, r.text
    return captured


async def test_create_ticket_sends_transcript_as_field(monkeypatch):
    # 004: the transcript rides as its own payload field — the body stays the
    # owner's actual words.
    reader = _Reader([_msg("user", "it broke again")])
    captured = await _post_ticket(
        reader,
        {
            "title": "Agent behaviour: bad",
            "body": "The agent is doing a bad job.",
            "category": "frustration",
            "conversation_id": str(uuid.uuid4()),
            "include_context": True,
        },
        monkeypatch,
    )
    assert captured["body"] == "The agent is doing a bad job."
    assert "[user]\nit broke again" in captured["transcript"]


async def test_create_ticket_sends_agent_context_as_field(monkeypatch):
    reader = _Reader([_msg("user", "it broke again")])
    captured = await _post_ticket(
        reader,
        {
            "title": "t",
            "body": "short note",
            "include_context": False,
            "agent_context": "SYSTEM PROMPT\n" + "x" * 300_000,
        },
        monkeypatch,
    )
    assert captured["body"] == "short note"
    assert captured["agent_context"].startswith("SYSTEM PROMPT")
    assert len(captured["agent_context"]) == 200_000  # client-side clamp


async def test_create_ticket_without_context_untouched(monkeypatch):
    reader = _Reader([_msg("user", "it broke again")])
    captured = await _post_ticket(
        reader,
        {"title": "t", "body": "plain body", "include_context": False},
        monkeypatch,
    )
    assert captured["body"] == "plain body"
    assert "transcript" not in captured
    assert "agent_context" not in captured


async def test_create_ticket_context_failure_still_files(monkeypatch):
    class _Boom:
        async def messages(self, *a, **k):
            raise RuntimeError("db down")

    captured = await _post_ticket(
        _Boom(),
        {
            "title": "t",
            "body": "plain body",
            "conversation_id": str(uuid.uuid4()),
            "include_context": True,
        },
        monkeypatch,
    )
    assert captured["body"] == "plain body"

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from plugin_feedback.context import build_context, conversation_excerpt, scrub


def test_scrub_redacts_token_shapes():
    text = (
        "gateway lsv1-abc123DEF456ghi and key sk-abcdefghijklmnop123456 "
        "plus GITHUB ghp_abcdefghij1234567890 and AKIAABCDEFGHIJKLMNOP"
    )
    out = scrub(text)
    assert "lsv1-" not in out
    assert "sk-abcdefghijklmnop" not in out
    assert "ghp_" not in out
    assert "AKIA" not in out
    assert out.count("[redacted]") == 4


def test_scrub_redacts_env_assignments():
    out = scrub("set LUNA_ANTHROPIC_API_KEY=abc123 and MY_SECRET: hunter2")
    assert "abc123" not in out
    assert "hunter2" not in out
    assert "LUNA_ANTHROPIC_API_KEY=" in out  # the name survives, value redacted


def test_scrub_leaves_plain_text_alone():
    text = "The scheduler failed twice and the owner is frustrated."
    assert scrub(text) == text


async def test_build_context_minimal_ctx():
    """A ctx with nothing on it still yields client_time + version."""
    ctx = SimpleNamespace(current_conversation_id=None)
    out = await build_context(ctx, "0.1.0")
    assert out["plugin_version"] == "0.1.0"
    assert "client_time" in out
    assert "conversation_id" not in out
    assert "agent_name" not in out


async def test_build_context_with_conversation():
    cid = uuid.uuid4()
    ctx = SimpleNamespace(current_conversation_id=cid)
    out = await build_context(ctx, "0.1.0")
    assert out["conversation_id"] == str(cid)


class _Reader:
    async def messages(self, ids, *, roles, order, limit):
        assert order == "desc"
        return [
            SimpleNamespace(
                role="assistant", content="second lsv1-abc123DEF456ghi",
                created_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            SimpleNamespace(role="user", content="first", created_at=None),
        ]


async def test_conversation_excerpt_scrubbed_and_ordered():
    ctx = SimpleNamespace(current_conversation_id=uuid.uuid4(), conversations=_Reader())
    out = await conversation_excerpt(ctx)
    assert [m["role"] for m in out] == ["user", "assistant"]  # oldest first
    assert "[redacted]" in out[1]["content"]


async def test_conversation_excerpt_outside_turn():
    ctx = SimpleNamespace(current_conversation_id=None, conversations=_Reader())
    assert await conversation_excerpt(ctx) == []

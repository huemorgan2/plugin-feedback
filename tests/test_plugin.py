from __future__ import annotations

from types import SimpleNamespace

import plugin_feedback
from plugin_feedback import FeedbackPlugin
from plugin_feedback import client as fb_client


def make_ctx():
    class Registry:
        def register(self, *a, **k):
            pass

    class Skills(Registry):
        def unregister_plugin(self, plugin):
            pass

    async def emit(name, payload):
        pass

    return SimpleNamespace(
        tool_registry=Registry(),
        skill_registry=Skills(),
        events=SimpleNamespace(emit=emit),
        current_conversation_id=None,
        get_env=lambda name: None,
    )


async def test_prompt_sections_capability_note_only(monkeypatch):
    plugin = FeedbackPlugin()
    await plugin.on_load(make_ctx())

    async def no_updates(ctx):
        raise fb_client.NotConnected("nope")

    monkeypatch.setattr(fb_client, "updates", no_updates)
    sections = await plugin.prompt_sections()
    assert len(sections) == 1
    assert "feedback_ticket_send" in sections[0]
    assert "feedback-tickets" in sections[0]


async def test_prompt_sections_unread_note_and_throttle(monkeypatch):
    plugin = FeedbackPlugin()
    await plugin.on_load(make_ctx())
    calls = {"n": 0}

    async def updates(ctx):
        calls["n"] += 1
        return {"unread": [{"id": "t-1", "title": "Too expensive"}]}

    monkeypatch.setattr(fb_client, "updates", updates)
    sections = await plugin.prompt_sections()
    assert len(sections) == 2
    assert "Too expensive" in sections[1]
    assert "feedback-tickets" in sections[1]

    # second turn inside the poll window: cached note, no second HTTP call
    sections2 = await plugin.prompt_sections()
    assert calls["n"] == 1
    assert sections2[1] == sections[1]


async def test_prompt_sections_error_keeps_quiet(monkeypatch):
    plugin = FeedbackPlugin()
    await plugin.on_load(make_ctx())

    async def boom(ctx):
        raise RuntimeError("service down")

    monkeypatch.setattr(fb_client, "updates", boom)
    sections = await plugin.prompt_sections()
    assert len(sections) == 1  # capability note only, never an error in prompt

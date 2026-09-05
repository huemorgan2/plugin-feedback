"""plugin-feedback — feedback tickets to the Luna team.

The agent (or the owner, via the Feedback pane) files tickets to the
luna-service control plane; admins answer there; replies surface back both in
the pane and as a one-line prompt note to the agent. All ticket state lives on
the control plane — this plugin is stateless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from luna_sdk import LunaPlugin, PluginContext, PluginManifest, SidebarSection

from . import client
from .errors import capture
from .tools import register_tools
from .wake import ReplyWakePoller

log = logging.getLogger("plugin-feedback")

_CAPABILITY_NOTE = (
    "Feedback to the Luna team: complaints deserve a ticket, not just "
    "sympathy. Direct complaints (\"this is expensive\", \"this doesn't "
    "work\") — offer to send feedback and draft it with feedback_ticket_send. "
    "Indirect frustration (things keep failing, the owner angry or swearing "
    "at you) — send it yourself with written_by='agent', include the "
    "conversation, and tell the owner you did. Put exact errors in "
    "`technical`. The owner also has a Feedback pane in the sidebar; to read "
    "or answer a team reply, call load_skill('feedback-tickets') — "
    "feedback_ticket_list/get/reply unlock on your next turn. Purely "
    "technical breakage the owner needn't hear about (a failing tool, a pane "
    "that won't load) — record it silently with report_issue instead."
)

_POLL_INTERVAL_S = 600.0  # normal cadence for the unread check
_POLL_BACKOFF_S = 3600.0  # after NotConnected / errors
_POLL_TIMEOUT_S = 3.0  # never hold a turn hostage


class FeedbackPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-feedback",
        shown_name="Feedback",
        icon="message-square",
        image="assets/icon.png",
        version="0.9.0",
        description=(
            "Feedback tickets to the Luna team, with threaded replies — plus "
            "silent error capture to the control plane."
        ),
        category="global",
        sidebar_sections=[
            SidebarSection(
                id="feedback", label="Feedback", icon="message-square",
                sort_order=60,
            ),
        ],
        routes_module="routes",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._unread_note: str | None = None
        self._next_poll: float = 0.0
        self._poller: ReplyWakePoller | None = None

    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        register_tools(ctx, self.manifest.version)
        connected = client.get_config(ctx) is not None
        if connected:  # OSS installs get no handler — nothing to send to
            capture.attach(ctx)
        # plan 003: wake the origin conversation when the team replies. Only
        # on cores that can deliver a moment; old cores keep the prompt note.
        if connected and getattr(ctx, "send_muted_message", None) is not None:
            self._poller = ReplyWakePoller(ctx)
        log.info("plugin-feedback loaded (service connected=%s)", connected)

    async def on_server_ready(self) -> None:
        # Post-boot SERVING loop (core >=0.92.030) — boot-time tasks die with
        # the bootstrap loop.
        if self._poller is not None:
            self._poller.ensure()

    async def on_unload(self) -> None:
        if self._poller is not None:
            self._poller.stop()
        capture.detach()

    async def prompt_sections(self) -> list[str]:
        sections = [_CAPABILITY_NOTE]
        if self._poller is not None:
            # Wake replaces the throttled note (017 rule: one delivery path).
            # ensure() doubles as the lazy start on cores without
            # on_server_ready.
            self._poller.ensure()
            return sections
        note = await self._check_unread()
        if note:
            sections.append(note)
        return sections

    async def _check_unread(self) -> str | None:
        """Throttled unread-replies check; cached note between polls. Never
        raises, never blocks a turn for more than _POLL_TIMEOUT_S."""
        ctx = self._ctx
        if ctx is None:
            return None
        now = time.monotonic()
        if now < self._next_poll:
            return self._unread_note
        self._next_poll = now + _POLL_INTERVAL_S
        try:
            result = await asyncio.wait_for(
                client.updates(ctx), _POLL_TIMEOUT_S
            )
        except client.NotConnected:
            self._next_poll = now + _POLL_BACKOFF_S
            self._unread_note = None
            return None
        except Exception:  # noqa: BLE001 — awareness only, never a blocker
            self._next_poll = now + _POLL_BACKOFF_S
            return self._unread_note
        unread: list[dict[str, Any]] = result.get("unread") or []
        if not unread:
            self._unread_note = None
        else:
            titles = ", ".join(f"\"{t.get('title', '?')}\"" for t in unread[:3])
            self._unread_note = (
                f"Feedback: the Luna team replied on {len(unread)} "
                f"ticket(s) — {titles}. Call load_skill('feedback-tickets') "
                "now; next turn, read it with feedback_ticket_get and tell "
                "the owner in plain words."
            )
        return self._unread_note

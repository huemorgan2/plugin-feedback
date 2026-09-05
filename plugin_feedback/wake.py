"""Team-reply wake (plan 003, 017 wake-everywhere pattern).

A background poller watches the control plane's unread feed and wakes the
conversation that filed the ticket the moment the Luna team replies, instead
of leaving the reply to be discovered via the pane or a throttled prompt
note. One moment per admin reply — dedupe key ``(ticket_id,
last_admin_reply_at)``; a read ticket never notifies (``updates()`` filters
on ``agent_read_at`` server-side).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from . import client

log = logging.getLogger("plugin-feedback")

POLL_FIRST_S = 60.0  # early first pass so restarts deliver promptly
POLL_INTERVAL_S = 300.0
POLL_BACKOFF_S = 3600.0  # NotConnected / repeated errors
_SEEN_CAP = 500
_REPLY_CLAMP = 1500

# Containment caps for the woken turn (playbooks-028 idiom).
_MOMENT_CAPS: dict[str, Any] = {
    "tools": "all",
    "max_turns": 12,
    "token_budget": 200_000,
    "timeout_s": 900.0,
}


def _moment_text(ticket: dict[str, Any], reply_body: str) -> tuple[str, str]:
    title = "The Luna team replied to your feedback"
    lines = [
        f"The Luna team answered the feedback ticket "
        f"\"{ticket.get('title', '?')}\" (id {ticket.get('id')}).",
    ]
    if reply_body:
        clamped = reply_body[:_REPLY_CLAMP] + (
            "…" if len(reply_body) > _REPLY_CLAMP else ""
        )
        lines.append(f"Their reply:\n{clamped}")
    lines.append(
        "Call load_skill('feedback-tickets') now; next turn, read the full "
        "thread with feedback_ticket_get (this marks it read) and tell the "
        "owner what the team said in plain words."
    )
    return title, "\n\n".join(lines)


class ReplyWakePoller:
    """Single background task; started from on_server_ready (or lazily from
    prompt_sections on cores without that hook). Never raises out of its
    loop; stops only via stop()/on_unload."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        # ticket_id -> last_admin_reply_at we already notified for
        self._seen: dict[str, str] = {}

    def ensure(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:  # no running loop — caller retries later
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        delay = POLL_FIRST_S
        while True:
            await asyncio.sleep(delay)
            try:
                await self._pass()
                delay = POLL_INTERVAL_S
            except asyncio.CancelledError:
                raise
            except client.NotConnected:
                delay = POLL_BACKOFF_S
            except Exception:  # noqa: BLE001 — poller must survive anything
                log.exception("feedback reply-wake pass failed")
                delay = POLL_BACKOFF_S

    async def _pass(self) -> None:
        result = await client.updates(self._ctx)
        for entry in result.get("unread") or []:
            ticket_id = str(entry.get("id") or "")
            reply_at = str(entry.get("last_admin_reply_at") or "")
            if not ticket_id or self._seen.get(ticket_id) == reply_at:
                continue
            await self._notify(ticket_id)
            self._seen[ticket_id] = reply_at
            while len(self._seen) > _SEEN_CAP:
                self._seen.pop(next(iter(self._seen)))

    async def _notify(self, ticket_id: str) -> None:
        # mark_read=0: the wake must not consume the owner's unread state.
        detail = await client.get_ticket(self._ctx, ticket_id, mark_read=False)
        ticket = detail.get("ticket") or {}
        admin_msgs = [
            m for m in detail.get("messages") or [] if m.get("author") == "admin"
        ]
        reply_body = (admin_msgs[-1].get("body") or "") if admin_msgs else ""

        conv_id = self._route(ticket)
        if conv_id is None:
            conv_id = await self._ops()
        if conv_id is None:
            # Unroutable stays notified (never a nag) — the pane badge and
            # old-core prompt note still surface it.
            log.warning("feedback reply on %s: no conversation to wake", ticket_id)
        else:
            title, content = _moment_text(ticket, reply_body)
            try:
                await self._ctx.send_muted_message(
                    title, content,
                    channel="moment", conversation_id=conv_id,
                    source="feedback", **_MOMENT_CAPS,
                )
            except Exception:  # noqa: BLE001 — delivery is best-effort
                log.exception("feedback reply-wake moment failed for %s", ticket_id)
        try:
            await self._ctx.events.emit(
                "feedback.updated", {"ticket_id": ticket_id}
            )
        except Exception:  # noqa: BLE001 — pane refresh is best-effort
            pass

    @staticmethod
    def _route(ticket: dict[str, Any]) -> uuid.UUID | None:
        context = ticket.get("context") or {}
        raw = context.get("conversation_id")
        if not raw:
            return None
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None

    async def _ops(self) -> uuid.UUID | None:
        fn = getattr(self._ctx, "ops_conversation_id", None)
        if fn is None:
            return None
        try:
            raw = await fn()
            return uuid.UUID(str(raw)) if raw else None
        except Exception:  # noqa: BLE001 — ops chat is a fallback, not a must
            return None

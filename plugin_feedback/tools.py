"""Agent-facing feedback tools.

Prompt-footprint stance: `feedback_ticket_send` is the one always-visible tool — the
moments that need it (owner upset, something broken) are exactly the moments
that must not wait a turn for a skill load. Reading/answering ticket threads
is occasional, so feedback_ticket_list / feedback_ticket_get / feedback_ticket_reply ride behind
the `feedback-tickets` skill.
"""

from __future__ import annotations

from typing import Any

from luna_sdk import PluginContext, ToolDef

try:  # cores with the skill system export it
    from luna_sdk import SkillDef
except ImportError:  # pragma: no cover - older core: tools register ungated
    SkillDef = None

from . import client
from .context import build_context, conversation_excerpt, scrub

PLUGIN_NAME = "plugin-feedback"

CATEGORIES = ("cost", "bug", "frustration", "feature", "praise", "other")
SEVERITIES = ("low", "normal", "high")

# written_by is agent-facing vocabulary; the service contract calls it origin
# with values user|agent (plan 046).
_ORIGIN = {"owner": "user", "agent": "agent"}


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, client.NotConnected):
        return {"error": str(exc)}
    if isinstance(exc, client.FeedbackError):
        return {"error": f"feedback service refused the request: {exc.detail}"}
    return {"error": f"feedback service unreachable: {exc}"}


def register_tools(ctx: PluginContext, plugin_version: str) -> None:
    async def _emit_updated(ticket_id: str | None) -> None:
        try:
            await ctx.events.emit("feedback.updated", {"ticket_id": ticket_id})
        except Exception:  # noqa: BLE001 — pane refresh is best-effort
            pass

    async def _send(
        summary: str,
        details: str,
        category: str = "other",
        severity: str = "normal",
        written_by: str = "owner",
        include_conversation: bool = False,
        technical: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if category not in CATEGORIES:
            return {"error": f"category must be one of {', '.join(CATEGORIES)}"}
        if severity not in SEVERITIES:
            return {"error": f"severity must be one of {', '.join(SEVERITIES)}"}
        origin = _ORIGIN.get(written_by)
        if origin is None:
            return {"error": "written_by must be 'owner' or 'agent'"}
        payload: dict[str, Any] = {
            "origin": origin,
            "category": category,
            "severity": severity,
            "title": scrub(summary.strip())[:200],
            "body": scrub(details.strip()),
            "context": await build_context(ctx, plugin_version),
        }
        if include_conversation:
            excerpt = await conversation_excerpt(ctx)
            if excerpt:
                payload["conversation_excerpt"] = excerpt
        if technical:
            payload["technical"] = {
                str(k): scrub(str(v)) for k, v in technical.items()
            }
        try:
            created = await client.create_ticket(ctx, payload)
        except Exception as exc:  # noqa: BLE001 — degrade to a readable error
            return _error(exc)
        await _emit_updated(created.get("id"))
        return {
            "sent": True,
            "ticket_id": created.get("id"),
            "note": (
                "The Luna team will answer on this ticket. Replies show up in "
                "the Feedback pane, and you will see a prompt note when one "
                "arrives."
            ),
        }

    async def _list() -> dict[str, Any]:
        try:
            return await client.list_tickets(ctx)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    async def _get(ticket_id: str) -> dict[str, Any]:
        try:
            result = await client.get_ticket(ctx, ticket_id, mark_read=True)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        await _emit_updated(ticket_id)
        return result

    async def _reply(
        ticket_id: str, message: str, written_by: str = "owner"
    ) -> dict[str, Any]:
        origin = _ORIGIN.get(written_by)
        if origin is None:
            return {"error": "written_by must be 'owner' or 'agent'"}
        try:
            result = await client.reply(
                ctx, ticket_id, author=origin, body=scrub(message.strip())
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        await _emit_updated(ticket_id)
        return result

    send_def = ToolDef(
        name="feedback_ticket_send",
        description=(
            "Send a feedback ticket to the Luna team (pricing complaints, "
            "bugs, frustration, ideas, praise). written_by='owner' when the "
            "owner asked for it or dictated it; 'agent' when you file it "
            "yourself from indirect signals (repeated failures, the owner "
            "upset or angry at you) — then tell the owner you sent it. Set "
            "include_conversation=true when the feedback is about this "
            "conversation; put exact error messages and technical detail in "
            "`technical`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One-line summary (becomes the ticket title)."},
                "details": {"type": "string", "description": "What happened / what the owner said, in plain words."},
                "category": {"type": "string", "enum": list(CATEGORIES), "default": "other"},
                "severity": {"type": "string", "enum": list(SEVERITIES), "default": "normal"},
                "written_by": {"type": "string", "enum": ["owner", "agent"], "default": "owner"},
                "include_conversation": {"type": "boolean", "default": False, "description": "Attach the recent messages of this conversation as reference."},
                "technical": {"type": "object", "description": "Error messages, plugin/tool names, repro steps — anything the team needs to debug."},
            },
            "required": ["summary", "details"],
        },
        policy="prompt_always",  # the approval card IS the owner's review box
        risk_level="low",
    )

    gated_defs: list[tuple[ToolDef, Any]] = [
        (
            ToolDef(
                name="feedback_ticket_list",
                description="List this Luna's feedback tickets with status and unread team replies.",
                parameters={"type": "object", "properties": {}, "required": []},
                policy="auto_approve", risk_level="low",
            ),
            _list,
        ),
        (
            ToolDef(
                name="feedback_ticket_get",
                description="Read one feedback ticket's full thread (marks the team's replies as read).",
                parameters={
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string"}},
                    "required": ["ticket_id"],
                },
                policy="auto_approve", risk_level="low",
            ),
            _get,
        ),
        (
            ToolDef(
                name="feedback_ticket_reply",
                description=(
                    "Reply on a feedback ticket thread — answer the team's "
                    "follow-up questions. written_by='owner' when relaying the "
                    "owner's words, 'agent' when you answer yourself."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "string"},
                        "message": {"type": "string"},
                        "written_by": {"type": "string", "enum": ["owner", "agent"], "default": "owner"},
                    },
                    "required": ["ticket_id", "message"],
                },
                policy="prompt_always", risk_level="low",
            ),
            _reply,
        ),
    ]

    # feedback_ticket_send stays ungated on every core.
    ctx.tool_registry.register(PLUGIN_NAME, send_def, _send)

    gate = getattr(ctx, "skill_registry", None) is not None and SkillDef is not None
    for tool_def, handler in gated_defs:
        if gate:
            try:
                ctx.tool_registry.register(
                    PLUGIN_NAME, tool_def, handler, skill_gated=True
                )
                continue
            except TypeError:  # core knows skills but not the kwarg
                gate = False
        ctx.tool_registry.register(PLUGIN_NAME, tool_def, handler)

    if gate:
        try:
            ctx.skill_registry.unregister_plugin(PLUGIN_NAME)
        except Exception:  # noqa: BLE001 — stale-sweep is best effort
            pass
        ctx.skill_registry.register(
            PLUGIN_NAME,
            SkillDef(
                name="feedback-tickets",
                description=(
                    "Read the feedback tickets this Luna sent to the Luna "
                    "team, check for team replies, and answer follow-ups on a "
                    "ticket thread. Load when the owner asks about their "
                    "feedback or a team reply is waiting; feedback_ticket_list / "
                    "feedback_ticket_get / feedback_ticket_reply unlock on your next turn."
                ),
                body=(
                    "# Feedback tickets\n\n"
                    "Tools (unlock on your NEXT turn after loading this "
                    "skill): feedback_ticket_list, feedback_ticket_get, feedback_ticket_reply. "
                    "feedback_ticket_send is always available.\n\n"
                    "- feedback_ticket_list shows every ticket with status: open "
                    "(waiting for the team), answered (the team replied — "
                    "read it), closed.\n"
                    "- feedback_ticket_get returns the full thread and marks the "
                    "team's replies as read. Relay the reply to the owner in "
                    "plain words — never paste ticket JSON.\n"
                    "- feedback_ticket_reply answers on the thread; a reply reopens "
                    "a closed or answered ticket. Use written_by='owner' "
                    "when relaying the owner's words.\n"
                    "- The owner sees the same tickets in the Feedback pane "
                    "in the sidebar."
                ),
                tools=[tool_def.name for tool_def, _ in gated_defs],
            ),
        )

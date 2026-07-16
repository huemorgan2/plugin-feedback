"""Ticket context assembly — who is filing, from which install, exactly when.

Everything here is best-effort: a ticket with a thin context block still beats
no ticket, so no helper in this module ever raises.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Credential shapes that must never leave the machine inside a ticket:
# known token prefixes, then any KEY/SECRET/TOKEN/PASSWORD-ish assignment.
_TOKEN_RES = [
    re.compile(r"\blsv1-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[a-z]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
_ASSIGN_RE = re.compile(
    r"([A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*[=:]\s*)(\S+)"
)


def scrub(text: str) -> str:
    """Redact credential-shaped substrings from free text."""
    if not text:
        return text
    for rx in _TOKEN_RES:
        text = rx.sub("[redacted]", text)
    return _ASSIGN_RE.sub(r"\1[redacted]", text)


async def agent_identity(ctx: Any) -> dict[str, Any]:
    """Agent name / owner / mission from the identity table (curiosity's
    read pattern). Empty dict when unavailable."""
    factory = getattr(ctx, "db_session_factory", None)
    if factory is None:
        return {}
    try:
        from sqlalchemy import text as sql_text

        async with factory() as s:
            row = (
                (await s.execute(
                    sql_text("SELECT name, owner_name, mission FROM identity LIMIT 1")
                )).mappings().first()
            )
        if not row:
            return {}
        out = {k: row.get(k) for k in ("name", "owner_name", "mission")}
        mission = out.get("mission")
        if isinstance(mission, str) and len(mission) > 400:
            out["mission"] = mission[:400] + "…"
        return {k: v for k, v in out.items() if v}
    except Exception:  # noqa: BLE001 — identity is enrichment, never a blocker
        return {}


def _env(ctx: Any, name: str) -> str | None:
    import os

    value = os.environ.get(name, "").strip()
    if value:
        return value
    get_env = getattr(ctx, "get_env", None)
    if callable(get_env):
        try:
            return get_env(name)
        except Exception:  # noqa: BLE001
            return None
    return None


async def build_context(ctx: Any, plugin_version: str) -> dict[str, Any]:
    """The `context` block stamped on every ticket (plan 046: client block;
    the server adds its own from the resolved Agent row)."""
    identity = await agent_identity(ctx)
    conversation_id = getattr(ctx, "current_conversation_id", None)
    out: dict[str, Any] = {
        "client_time": datetime.now(timezone.utc).isoformat(),
        "plugin_version": plugin_version,
        "agent_name": identity.get("name"),
        "owner_name": identity.get("owner_name"),
        "mission": identity.get("mission"),
        "host": _env(ctx, "LUNA_HOST_NAME"),
        "conversation_id": str(conversation_id) if conversation_id else None,
    }
    return {k: v for k, v in out.items() if v}


async def conversation_excerpt(ctx: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """The last `limit` user/assistant messages of the current conversation,
    oldest first, scrubbed and truncated. Empty outside a turn."""
    conversation_id = getattr(ctx, "current_conversation_id", None)
    reader = getattr(ctx, "conversations", None)
    if conversation_id is None or reader is None:
        return []
    try:
        messages = await reader.messages(
            [conversation_id], roles=("user", "assistant"),
            order="desc", limit=limit,
        )
    except Exception:  # noqa: BLE001 — excerpt is enrichment, never a blocker
        return []
    out = []
    for m in reversed(messages):
        content = scrub((m.content or "").strip())
        if len(content) > 2000:
            content = content[:2000] + "…"
        out.append({
            "role": m.role,
            "content": content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out

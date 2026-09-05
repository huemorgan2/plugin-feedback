"""HTTP client for the luna-service feedback API (plan 046 contract).

The control plane resolves the agent from the machine's gateway token —
`Authorization: Bearer <LUNA_GATEWAY_TOKEN>` — so there is nothing to
provision and nothing agent-namable in the URL. `LUNA_FEEDBACK_SERVICE_URL` /
`LUNA_FEEDBACK_TOKEN` exist only as overrides for self-hosted or test setups.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

ENV_SERVICE_URL = "LUNA_FEEDBACK_SERVICE_URL"  # override; default: gateway
ENV_TOKEN = "LUNA_FEEDBACK_TOKEN"  # override; default: gateway token
ENV_GATEWAY_URL = "LUNA_GATEWAY_URL"
ENV_GATEWAY_TOKEN = "LUNA_GATEWAY_TOKEN"

BASE_PATH = "/api/agent/feedback"
ERRORS_PATH = "/api/agent/errors"  # plan 051 sink — NOT under the feedback base
TIMEOUT_S = 10.0
ERRORS_TIMEOUT_S = 5.0


class NotConnected(Exception):
    """No service URL/token available — OSS install without a control plane."""


class FeedbackError(Exception):
    """A 4xx/5xx from the service; carries status + the service's detail."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _env(ctx: Any, name: str) -> str:
    """Process env first (matches plugin-scheduler's provisioning), then
    core-resolved config for the declared LUNA_* fields (.env installs)."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    get_env = getattr(ctx, "get_env", None)
    if callable(get_env):
        try:
            return (get_env(name) or "").strip()
        except Exception:  # noqa: BLE001 — config resolution is best-effort
            return ""
    return ""


def get_config(ctx: Any) -> dict[str, str] | None:
    base = _env(ctx, ENV_SERVICE_URL) or _env(ctx, ENV_GATEWAY_URL)
    token = _env(ctx, ENV_TOKEN) or _env(ctx, ENV_GATEWAY_TOKEN)
    if not (base and token):
        return None
    return {"base": base.rstrip("/"), "token": token}


async def _request(
    ctx: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = get_config(ctx)
    if cfg is None:
        raise NotConnected(
            "feedback service not connected — this Luna has no gateway "
            "credentials (hosted Lunas get them automatically)"
        )
    url = f"{cfg['base']}{BASE_PATH}{path}"
    headers = {
        "authorization": f"Bearer {cfg['token']}",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        resp = await c.request(
            method, url,
            content=json.dumps(body) if body is not None else None,
            params=params, headers=headers,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = resp.text
        raise FeedbackError(resp.status_code, str(detail))
    return resp.json()


async def create_ticket(ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return await _request(ctx, "POST", "/tickets", body=payload)


async def list_tickets(ctx: Any, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return await _request(
        ctx, "GET", "/tickets", params={"limit": limit, "offset": offset}
    )


async def get_ticket(
    ctx: Any,
    ticket_id: str,
    *,
    mark_read: bool = True,
    include_attachments: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if mark_read:
        params["mark_read"] = 1
    if include_attachments:
        # 004/079: without this, attachment strings >4k in message meta come
        # back elided as {chars, elided, note} — token safety for agent reads.
        params["include_attachments"] = 1
    return await _request(
        ctx, "GET", f"/tickets/{ticket_id}", params=params or None
    )


async def reply(ctx: Any, ticket_id: str, *, author: str, body: str) -> dict[str, Any]:
    return await _request(
        ctx, "POST", f"/tickets/{ticket_id}/replies",
        body={"author": author, "body": body},
    )


async def updates(ctx: Any) -> dict[str, Any]:
    return await _request(ctx, "GET", "/updates")


async def report_error(ctx: Any, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Post an error-event batch to the control-plane sink (plan 007/051).

    Unlike the ticket calls this NEVER raises — error telemetry is
    best-effort by contract. Returns the service's response dict, or None
    when unconfigured (OSS), on any HTTP error, or on any transport failure.
    """
    if not events:
        return None
    cfg = get_config(ctx)
    if cfg is None:
        return None
    url = f"{cfg['base']}{ERRORS_PATH}"
    headers = {
        "authorization": f"Bearer {cfg['token']}",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=ERRORS_TIMEOUT_S) as c:
            resp = await c.post(
                url, content=json.dumps({"events": events}), headers=headers
            )
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 — never let telemetry touch a turn
        return None

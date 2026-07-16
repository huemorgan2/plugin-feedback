"""plugin-feedback API routes — Feedback pane (left-pane iframe).

Mounted at /api/p/plugin-feedback/* via manifest.routes_module. Data routes
proxy to the luna-service feedback API with the machine's gateway token (the
browser never sees it); the pane UI at /ui/* is served unauthenticated and
fetches data with the token it receives from the Shell via postMessage
(wiki 0.7.1 handshake).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import client
from .context import build_context, scrub

_UI_DIR = Path(__file__).parent / "ui"


# Module-level on purpose: with `from __future__ import annotations`,
# function-local Pydantic body models break FastAPI's forward-ref resolution.
class NewTicketBody(BaseModel):
    title: str
    body: str
    category: str = "other"
    severity: str = "normal"


class ReplyBody(BaseModel):
    body: str


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, client.NotConnected):
        raise HTTPException(503, "Not connected to the Luna service.") from exc
    if isinstance(exc, client.FeedbackError):
        raise HTTPException(exc.status, exc.detail) from exc
    raise HTTPException(502, f"Luna service unreachable: {exc}") from exc


def register_routes(app, ctx):
    from luna_sdk import get_current_user

    router = APIRouter(prefix="/api/p/plugin-feedback", tags=["feedback"])
    version = "0.1.0"
    try:
        import tomllib

        with open(Path(__file__).parent / "luna-plugin.toml", "rb") as f:
            version = tomllib.load(f).get("version", version)
    except Exception:  # noqa: BLE001 — cache-busting only
        pass

    async def _emit_updated(ticket_id: str | None) -> None:
        try:
            await ctx.events.emit("feedback.updated", {"ticket_id": ticket_id})
        except Exception:  # noqa: BLE001 — pane refresh is best-effort
            pass

    @router.get("/status")
    async def status(user=Depends(get_current_user)):
        return {"connected": client.get_config(ctx) is not None}

    @router.get("/tickets")
    async def list_tickets(user=Depends(get_current_user)):
        try:
            return await client.list_tickets(ctx)
        except Exception as exc:  # noqa: BLE001
            _raise_for(exc)

    @router.post("/tickets", status_code=201)
    async def create_ticket(payload: NewTicketBody, user=Depends(get_current_user)):
        body = {
            "origin": "user",  # the pane form is always the owner's own words
            "category": payload.category,
            "severity": payload.severity,
            "title": scrub(payload.title.strip())[:200],
            "body": scrub(payload.body.strip()),
            "context": await build_context(ctx, version),
        }
        try:
            created = await client.create_ticket(ctx, body)
        except Exception as exc:  # noqa: BLE001
            _raise_for(exc)
        await _emit_updated(created.get("id"))
        return created

    @router.get("/tickets/{ticket_id}")
    async def get_ticket(ticket_id: str, user=Depends(get_current_user)):
        try:
            result = await client.get_ticket(ctx, ticket_id, mark_read=True)
        except Exception as exc:  # noqa: BLE001
            _raise_for(exc)
        await _emit_updated(ticket_id)
        return result

    @router.post("/tickets/{ticket_id}/replies", status_code=201)
    async def reply(ticket_id: str, payload: ReplyBody, user=Depends(get_current_user)):
        try:
            result = await client.reply(
                ctx, ticket_id, author="user", body=scrub(payload.body.strip())
            )
        except Exception as exc:  # noqa: BLE001
            _raise_for(exc)
        await _emit_updated(ticket_id)
        return result

    # --- Sidebar pane UI ------------------------------------------------
    # Served unauthenticated: the Shell iframes /ui/ with no auth header;
    # the app inside fetches data with the token it gets via postMessage.

    def _index() -> HTMLResponse:
        index = _UI_DIR / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>plugin-feedback UI missing</h1>")
        html = index.read_text(encoding="utf-8").replace("{{V}}", version)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @router.get("/ui/")
    async def serve_ui_root():
        return _index()

    @router.get("/ui/{path:path}")
    async def serve_ui(path: str):
        if not path or path in ("/", "index.html"):
            return _index()
        target = (_UI_DIR / path).resolve()
        if not str(target).startswith(str(_UI_DIR.resolve())):
            raise HTTPException(403, "Forbidden")
        if not target.exists() or target.is_dir():
            return _index()
        return FileResponse(str(target), headers={"Cache-Control": "no-cache"})

    app.include_router(router)

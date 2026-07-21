"""Browser error intake route + reporter.js serving (plan 007)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from plugin_feedback import client, routes


def make_ctx():
    async def emit(name, payload):
        pass

    return SimpleNamespace(
        events=SimpleNamespace(emit=emit),
        get_env=lambda name: None,
        current_conversation_id=None,
    )


@pytest.fixture
def app():
    app = FastAPI()
    routes.register_routes(app, make_ctx())
    return app


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_ingest_forwards_scrubbed_ui_batch(app, monkeypatch):
    forwarded = {}

    async def fake_report(ctx, events):
        forwarded["events"] = events
        return {"accepted": len(events)}

    monkeypatch.setattr(client, "report_error", fake_report)
    async with await _client(app) as c:
        resp = await c.post(
            "/api/p/plugin-feedback/errors",
            json={"events": [
                {"kind": "js_error", "source": "agent",
                 "message": "boom sk-ABCDEFGHIJKLMNOPQRST"},
                "not-a-dict",
            ]},
        )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 1}
    events = forwarded["events"]
    assert len(events) == 1
    assert events[0]["source"] == "ui"  # client cannot claim another source
    assert "sk-ABCDEFGHIJKLMNOPQRST" not in events[0]["message"]


async def test_ingest_unconfigured_accepts_zero(app, monkeypatch):
    async def fake_report(ctx, events):
        return None  # OSS: not connected

    monkeypatch.setattr(client, "report_error", fake_report)
    async with await _client(app) as c:
        resp = await c.post(
            "/api/p/plugin-feedback/errors",
            json={"events": [{"kind": "js_error", "message": "x"}]},
        )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 0}


async def test_reporter_js_served_unauthenticated(app):
    async with await _client(app) as c:
        resp = await c.get("/api/p/plugin-feedback/reporter.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "__lunaErrorReporter" in resp.text

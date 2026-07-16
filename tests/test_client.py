from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from plugin_feedback import client


def clean_env(monkeypatch):
    for name in (client.ENV_SERVICE_URL, client.ENV_TOKEN,
                 client.ENV_GATEWAY_URL, client.ENV_GATEWAY_TOKEN):
        monkeypatch.delenv(name, raising=False)


def test_config_from_gateway_env(monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv(client.ENV_GATEWAY_URL, "https://luna.com.ai/proxy/")
    monkeypatch.setenv(client.ENV_GATEWAY_TOKEN, "lsv1-abc")
    cfg = client.get_config(SimpleNamespace())
    assert cfg == {"base": "https://luna.com.ai/proxy", "token": "lsv1-abc"}


def test_feedback_env_overrides_gateway(monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv(client.ENV_GATEWAY_URL, "https://luna.com.ai/proxy")
    monkeypatch.setenv(client.ENV_GATEWAY_TOKEN, "lsv1-abc")
    monkeypatch.setenv(client.ENV_SERVICE_URL, "http://127.0.0.1:9911")
    monkeypatch.setenv(client.ENV_TOKEN, "test-token")
    cfg = client.get_config(SimpleNamespace())
    assert cfg == {"base": "http://127.0.0.1:9911", "token": "test-token"}


def test_config_missing_is_none(monkeypatch):
    clean_env(monkeypatch)
    ctx = SimpleNamespace(get_env=lambda name: None)
    assert client.get_config(ctx) is None


def test_config_via_ctx_get_env(monkeypatch):
    clean_env(monkeypatch)
    values = {"LUNA_GATEWAY_URL": "https://x/proxy", "LUNA_GATEWAY_TOKEN": "lsv1-z"}
    ctx = SimpleNamespace(get_env=lambda name: values.get(name))
    assert client.get_config(ctx) == {"base": "https://x/proxy", "token": "lsv1-z"}


async def test_request_not_connected(monkeypatch):
    clean_env(monkeypatch)
    with pytest.raises(client.NotConnected):
        await client.list_tickets(SimpleNamespace(get_env=lambda n: None))


async def _with_transport(monkeypatch, handler, call):
    clean_env(monkeypatch)
    monkeypatch.setenv(client.ENV_SERVICE_URL, "http://svc")
    monkeypatch.setenv(client.ENV_TOKEN, "tok")
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return await call()


async def test_request_success_and_headers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "t-1"})

    result = await _with_transport(
        monkeypatch, handler,
        lambda: client.create_ticket(SimpleNamespace(), {"title": "x"}),
    )
    assert result == {"id": "t-1"}
    assert seen["auth"] == "Bearer tok"
    assert seen["url"] == "http://svc/api/agent/feedback/tickets"
    assert seen["body"] == {"title": "x"}


async def test_request_error_maps_detail(monkeypatch):
    def handler(request):
        return httpx.Response(422, json={"detail": "title required"})

    with pytest.raises(client.FeedbackError) as err:
        await _with_transport(
            monkeypatch, handler,
            lambda: client.create_ticket(SimpleNamespace(), {}),
        )
    assert err.value.status == 422
    assert err.value.detail == "title required"


async def test_updates_path(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/agent/feedback/updates"
        return httpx.Response(200, json={"unread": []})

    result = await _with_transport(
        monkeypatch, handler, lambda: client.updates(SimpleNamespace())
    )
    assert result == {"unread": []}

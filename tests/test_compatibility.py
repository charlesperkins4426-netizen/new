import asyncio
import json
import sqlite3

import pytest

from app import IGNORABLE_PARAMETERS
from gateway.config import ConfigStore
from gateway.errors import GatewayError
from gateway.logs import RequestLogs, utcnow
from tests.test_app import HEADERS, MODEL, Upstream, payload, running


def enable(app):
    app.state.config._transaction(
        lambda values, accounts, disabled: values.__setitem__("IGNORE_UNSUPPORTED_PARAMS", "true")
    )


def compatible_payload(stream=False):
    return {**payload(stream), "temperature": 0.4, "max_tokens": 1,
            "presence_penalty": 1.7, "frequency_penalty": 1.3, "top_logprobs": 3,
            "messages": [{"role": "system", "content": "system"},
                         {"role": "user", "content": "question"},
                         {"role": "assistant", "content": "answer"},
                         {"role": "user", "content": "follow-up"}]}


@pytest.mark.parametrize("stream", [False, True])
async def test_compatibility_preserves_roles_and_reports_ignored_fields(tmp_path, stream):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (app, client):
        enable(app)
        body = compatible_payload(stream)
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert response.status_code == 200, response.text
        assert response.headers["X-DialX-Ignored-Parameters"].split(", ") == list(IGNORABLE_PARAMETERS)
        assert response.headers["cache-control"] == "no-store"
        if stream:
            assert "[DONE]" in response.text and "reasoning_content" in response.text
        else:
            assert response.json()["choices"][0]["message"]["content"] == "正文末尾"
            assert response.json()["usage"] is None
        sent = json.loads(next(r.content for r in upstream.requests if r.url.path == "/api/chat"))
        assert not set(IGNORABLE_PARAMETERS) & sent.keys()
        assert sent["messages"] == body["messages"]
        assert sent["temperature"] == 0.4 and sent["prompt"] == "" and sent["model"] == MODEL
        row = (await app.state.logs.listing())["data"][0]
        detail = await app.state.logs.detail(row["id"])
        assert row["ignored_parameters"] == detail["ignored_parameters"] == list(IGNORABLE_PARAMETERS)
        assert detail["messages"] == body["messages"] and detail["status"] == "success"


@pytest.mark.parametrize("name", IGNORABLE_PARAMETERS)
async def test_default_strict_mode_names_rejected_parameter_without_value(tmp_path, name):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (_, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS,
                                     json={**payload(), name: "private-parameter-value"})
        assert response.status_code == 400
        assert name in response.json()["error"]["message"]
        assert "private-parameter-value" not in response.text
        assert not upstream.requests


@pytest.mark.parametrize("name", ["tools", "stop", "response_format", "stream_options", "arbitrary_field"])
async def test_compatibility_never_ignores_other_features(tmp_path, name):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (app, client):
        enable(app)
        response = await client.post("/v1/chat/completions", headers=HEADERS,
                                     json={**compatible_payload(), name: "private-extra-value"})
        assert response.status_code == 400
        assert name in response.json()["error"]["message"]
        assert "private-extra-value" not in response.text and not upstream.requests


async def test_no_warning_when_no_fields_were_ignored(tmp_path):
    async with running(tmp_path, Upstream()) as (app, client):
        enable(app)
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 200
        assert "X-DialX-Ignored-Parameters" not in response.headers
        assert (await app.state.logs.listing())["data"][0]["ignored_parameters"] == []


@pytest.mark.parametrize("overlap", [False, True])
async def test_warning_metadata_is_isolated_between_requests(tmp_path, overlap):
    async with running(tmp_path, Upstream()) as (app, client):
        app.state.config._transaction(lambda values, accounts, disabled: values.update(
            IGNORE_UNSUPPORTED_PARAMS="true", ACCOUNT_WAIT_SECONDS="1",
        ))
        fields = [{"max_tokens": 1}, {"frequency_penalty": 0, "top_logprobs": 0}, {}]
        async def send(extra):
            return await client.post("/v1/chat/completions", headers=HEADERS, json={**payload(), **extra})
        if overlap:
            responses = await asyncio.gather(*(send(extra) for extra in fields))
        else:
            responses = [await send(extra) for extra in fields]
        for response, extra in zip(responses, fields, strict=True):
            assert response.status_code == 200, response.text
            expected = [name for name in IGNORABLE_PARAMETERS if name in extra]
            header = response.headers.get("X-DialX-Ignored-Parameters")
            assert header == (", ".join(expected) if expected else None)
            detail = await app.state.logs.detail(response.json()["id"])
            assert detail["ignored_parameters"] == expected
        unauthorized = await client.post("/v1/chat/completions", json=compatible_payload())
        assert unauthorized.status_code == 401
        assert "X-DialX-Ignored-Parameters" not in unauthorized.headers


async def test_warning_survives_upstream_error_and_is_logged(tmp_path):
    upstream = Upstream(status=429)
    async def handler(request):
        response = await upstream(request)
        if request.url.path == "/api/chat":
            response.headers["Retry-After"] = "42"
        return response
    async with running(tmp_path, handler) as (app, client):
        enable(app)
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=compatible_payload())
        assert response.status_code == 429
        assert response.headers["X-DialX-Ignored-Parameters"].split(", ") == list(IGNORABLE_PARAMETERS)
        assert response.headers["Retry-After"] == "42"
        assert (await app.state.logs.listing())["data"][0]["ignored_parameters"] == list(IGNORABLE_PARAMETERS)


async def test_invalid_text_message_reports_location_not_content(tmp_path):
    async with running(tmp_path, Upstream()) as (app, client):
        enable(app)
        body = {**compatible_payload(), "messages": [{"role": "user", "content": [{"text": "private-content"}]}]}
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert response.status_code == 400
        assert "messages.0.content" in response.json()["error"]["message"]
        assert "private-content" not in response.text


async def test_legacy_log_migration_preserves_history_and_is_repeatable(tmp_path):
    path = tmp_path / "requests.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE requests (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, account_id TEXT,
            model TEXT, status TEXT NOT NULL, ttft_ms REAL, duration_ms REAL,
            error TEXT, code TEXT, messages TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
            reasoning_content TEXT NOT NULL DEFAULT '')""")
        db.execute("INSERT INTO requests(id,started_at,status,messages,content) VALUES(?,?,'success','[]','kept')",
                   ("existing", utcnow()))
    logs = RequestLogs(tmp_path, lambda: [], 7)
    await logs.initialize()
    await logs.initialize()
    row = await logs.detail("existing")
    assert row["content"] == "kept" and row["status"] == "success"
    assert row["ignored_parameters"] == []
    await logs.start("new", utcnow(), "model", [], ["max_tokens"])
    assert (await logs.detail("new"))["ignored_parameters"] == ["max_tokens"]


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True), ("TRUE", True)])
def test_compatibility_setting_round_trip(tmp_path, value, expected):
    path = tmp_path / ".env"
    path.write_text(f"IGNORE_UNSUPPORTED_PARAMS={value}\n")
    store = ConfigStore(path)
    assert store.settings.ignore_unsupported_params is expected
    assert ConfigStore(path).settings.ignore_unsupported_params is expected


def test_invalid_compatibility_setting_is_not_silently_enabled(tmp_path):
    path = tmp_path / ".env"
    path.write_text("IGNORE_UNSUPPORTED_PARAMS=typo-secret\n")
    with pytest.raises(GatewayError) as caught:
        ConfigStore(path)
    assert "IGNORE_UNSUPPORTED_PARAMS" in caught.value.message
    assert "typo-secret" not in caught.value.message

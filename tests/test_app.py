import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app import create_app

MODEL = {"id": "captured-model", "name": "Test model", "type": "model", "owner": "test",
         "isDefault": True, "features": {"chat_completion": True, "mcp": False}, "limits": {}}
OTHER = {**MODEL, "id": "other-model", "isDefault": False}
APP_MODEL = {**MODEL, "id": "application", "type": "application"}
HEADERS = {"Authorization": "Bearer test-secret"}


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data = data
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for byte in self.data:
            yield bytes([byte])
        if self.fail:
            raise httpx.ReadError("raw-secret-that-must-not-leak")

    async def aclose(self):
        self.closed = True


def wire(values):
    return b"".join(json.dumps(v, ensure_ascii=False).encode() + b"\0" for v in values)


class Upstream:
    def __init__(self, *, values=None, fail=False, status=200):
        self.values = values if values is not None else [
            {"responseId": "fixture"}, {"role": "assistant"},
            {"custom_content": {"stages": [{"index": 0, "name": "Thinking"}]}},
            {"custom_content": {"stages": [{"index": 0, "content": "思考内容"}]}},
            {"content": "正文"}, {"custom_content": {"stages": [{"index": 0, "status": "completed"}]}},
            {"content": "末尾"}, {},
        ]
        self.fail, self.status = fail, status
        self.requests = []
        self.streams = []

    async def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/models":
            return httpx.Response(200, json=[MODEL, OTHER, APP_MODEL])
        if path == "/api/toolsets-listing":
            return httpx.Response(200, json={"data": [{"id": "tool-1", "display_name": "测试工具"}]})
        if path == "/api/auth/session":
            return httpx.Response(200, json={"user": {"name": "test"}, "expires": "2026-10-23T00:00:00Z"})
        if path.endswith("/limits"):
            return httpx.Response(200, json={"dayTokenStats": {"total": 1000, "used": 2}})
        if path == "/api/chat":
            stream = ByteStream(wire(self.values), self.fail)
            self.streams.append(stream)
            return httpx.Response(self.status, headers={"content-type": "application/octet-stream"}, stream=stream)
        raise AssertionError(f"Unexpected upstream path: {path}")


@asynccontextmanager
async def running(tmp_path, upstream, *, accounts=True):
    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("API_TOKEN='test-secret'\nACCOUNT_WAIT_SECONDS='0.05'\n" +
                       ("DIALX_COOKIES='__Secure-next-auth.session-token=test-cookie'\n" if accounts else ""))
    application = create_app(env, transport=httpx.MockTransport(upstream))
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            yield application, client


def payload(stream=False):
    return {"model": MODEL["id"], "messages": [{"role": "user", "content": "原样提示词"}], "stream": stream}


async def test_admin_has_no_login_but_v1_requires_single_key(tmp_path):
    async with running(tmp_path, Upstream()) as (_, client):
        assert (await client.get("/api/admin/overview")).status_code == 200
        assert (await client.get("/api/admin/accounts")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 401
        assert (await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})).status_code == 401
        models = (await client.get("/v1/models", headers=HEADERS)).json()
        assert {model["id"] for model in models["data"]} == {MODEL["id"], OTHER["id"]}
        assert (await client.get("/api/admin/keys")).status_code == 404


async def test_stream_and_nonstream_identical_with_reasoning_tail_and_logs(tmp_path):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (_, client):
        answer = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert answer.status_code == 200, answer.text
        message = answer.json()["choices"][0]["message"]
        assert message == {"role": "assistant", "content": "正文末尾", "reasoning_content": "思考内容"}
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload(True))
        assert response.status_code == 200, response.text
        events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        decoded = [json.loads(e) for e in events[:-1]]
        assert sum(e["choices"][0]["finish_reason"] == "stop" for e in decoded) == 1
        for field in ("content", "reasoning_content"):
            assert "".join(e["choices"][0]["delta"].get(field, "") for e in decoded) == message[field]
        listings = (await client.get("/api/admin/logs")).json()["data"]
        assert len(listings) == 2
        assert all(log["status"] == "success" and log["ttft_ms"] is not None for log in listings)
        detail = (await client.get("/api/admin/logs/" + listings[0]["id"])).json()
        assert detail["content"] == "正文末尾" and detail["reasoning_content"] == "思考内容"
        chat_requests = [r for r in upstream.requests if r.url.path == "/api/chat"]
        assert len(chat_requests) == 2
        for request in chat_requests:
            body = json.loads(request.content)
            assert body["messages"] == payload()["messages"]
            assert body["prompt"] == "" and body["model"] == MODEL
            assert not {"tools", "toolsets", "selectedAddons"} & body.keys()
        assert all(r.method != "PUT" for r in upstream.requests)
        assert all(stream.closed for stream in upstream.streams)


async def test_broken_stream_error_never_done_and_account_released(tmp_path):
    upstream = Upstream(values=[{"responseId": "x"}, {"content": "partial"}], fail=True)
    async with running(tmp_path, upstream) as (app, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload(True))
        assert response.status_code == 200
        assert '"error"' in response.text
        assert "[DONE]" not in response.text
        assert '"finish_reason":"stop"' not in response.text
        assert "raw-secret" not in response.text
        rows = (await client.get("/api/admin/logs")).json()["data"]
        assert rows[0]["status"] == "failed"
        detail = (await client.get("/api/admin/logs/" + rows[0]["id"])).json()
        assert detail["content"] == "partial"
        assert not any(a["busy"] for a in app.state.pool.snapshot())
        assert upstream.streams[0].closed


@pytest.mark.parametrize("stream", [False, True])
async def test_initial_http_failures_return_json_before_sse(tmp_path, stream):
    upstream = Upstream(status=401)
    async with running(tmp_path, upstream) as (_, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload(stream))
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_cookie"
        rows = (await client.get("/api/admin/logs")).json()["data"]
        assert rows[0]["status"] == "failed" and rows[0]["account_id"]


async def test_free_check_never_sends_chat_and_tools_cannot_enable(tmp_path):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (_, client):
        account_id = (await client.get("/api/admin/accounts")).json()["data"][0]["id"]
        result = await client.post(f"/api/admin/accounts/{account_id}/check", json={})
        assert result.status_code == 200, result.text
        assert result.json()["check_status"] == "valid"
        assert result.json()["balance"] is None
        assert result.json()["limits"]["dayTokenStats"]["used"] == 2
        assert all(r.url.path != "/api/chat" for r in upstream.requests)
        tools = (await client.get("/api/admin/tools")).json()
        assert tools["data"][0]["available"] is False
        assert (await client.put("/api/admin/tools", json={"enabled_ids": ["tool-1"]})).status_code == 400
        assert (await client.put("/api/admin/tools", json={"enabled_ids": []})).status_code == 200


async def test_accounts_disabled_and_deleted_persist_and_logs_redact(tmp_path):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (_, client):
        account_id = (await client.get("/api/admin/accounts")).json()["data"][0]["id"]
        assert (await client.patch(f"/api/admin/accounts/{account_id}", json={"enabled": False})).status_code == 200
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 503
    async with running(tmp_path, upstream) as (_, client):
        accounts = (await client.get("/api/admin/accounts")).json()["data"]
        assert accounts[0]["enabled"] is False
        assert (await client.post("/api/admin/accounts/delete", json={"ids": [account_id]})).json()["deleted"] == 1
    async with running(tmp_path, upstream) as (_, client):
        assert (await client.get("/api/admin/accounts")).json()["data"] == []


async def test_unsupported_input_auth_failure_and_no_secret_echo(tmp_path):
    async with running(tmp_path, Upstream()) as (_, client):
        invalid = {**payload(), "max_tokens": 10, "cookie": "test-cookie"}
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=invalid)
        assert response.status_code == 400
        assert "test-cookie" not in response.text
        response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 401
        rows = (await client.get("/api/admin/logs")).json()["data"]
        assert len(rows) == 2 and all(row["status"] == "failed" for row in rows)
        for row in rows:
            detail = (await client.get("/api/admin/logs/" + row["id"])).json()
            assert detail["messages"] == []
        assert (await client.put("/api/admin/settings", json={"api_token": "x"})).status_code == 400


async def test_session_expiry_empty_session_is_not_valid(tmp_path):
    upstream = Upstream()
    async def handler(request):
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={})
        return await upstream(request)
    async with running(tmp_path, handler) as (_, client):
        account_id = (await client.get("/api/admin/accounts")).json()["data"][0]["id"]
        result = await client.post(f"/api/admin/accounts/{account_id}/check", json={})
        assert result.status_code == 502
        row = (await client.get("/api/admin/accounts")).json()["data"][0]
        assert row["check_status"] == "invalid"


async def test_real_fixture_nonstream(tmp_path):
    values = json.loads((Path(__file__).parent / "fixtures" / "gemini-thinking.json").read_text())
    async with running(tmp_path, Upstream(values=values)) as (_, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "54"


async def test_nonstream_missing_terminator_fails_with_partial_log(tmp_path):
    async with running(tmp_path, Upstream(values=[{"role": "assistant"}, {"content": "partial"}])) as (_, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "incomplete_stream"
        row = (await client.get("/api/admin/logs")).json()["data"][0]
        assert row["status"] == "failed"
        assert (await client.get("/api/admin/logs/" + row["id"])).json()["content"] == "partial"


async def test_failed_chat_is_not_replayed_next_request_uses_other_account(tmp_path):
    (tmp_path / ".env").write_text("API_TOKEN='test-secret'\nDIALX_COOKIES='__Secure-next-auth.session-token=a-cookie|||__Secure-next-auth.session-token=b-cookie'\n")
    upstream = Upstream()
    chat_cookies = []
    async def handler(request):
        if request.url.path == "/api/chat":
            chat_cookies.append(request.headers.get("cookie"))
            if len(chat_cookies) == 1:
                return httpx.Response(401)
        return await upstream(request)
    async with running(tmp_path, handler) as (_, client):
        assert (await client.post("/v1/chat/completions", headers=HEADERS, json=payload())).status_code == 502
        assert len(chat_cookies) == 1
        assert (await client.post("/v1/chat/completions", headers=HEADERS, json=payload())).status_code == 200
        assert len(chat_cookies) == 2 and chat_cookies[0] != chat_cookies[1]


async def test_model_rate_limit_does_not_disable_other_models(tmp_path):
    upstream = Upstream()
    async def handler(request):
        if request.url.path == "/api/chat" and json.loads(request.content)["model"]["id"] == MODEL["id"]:
            return httpx.Response(429, headers={"Retry-After": "90"})
        return await upstream(request)
    async with running(tmp_path, handler) as (_, client):
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 429 and response.headers["retry-after"] == "90"
        response = await client.post("/v1/chat/completions", headers=HEADERS, json={**payload(), "model": OTHER["id"]})
        assert response.status_code == 200
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 503


async def test_failure_after_first_frame_closes_response_and_finishes_log(tmp_path, monkeypatch):
    from gateway.errors import GatewayError
    upstream = Upstream()
    async with running(tmp_path, upstream) as (app, client):
        async def fail_bind(*args, **kwargs):
            raise GatewayError(507, "logs_unavailable", "test storage failure")
        original = app.state.logs.bind
        calls = 0
        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return await fail_bind()
            return await original(*args, **kwargs)
        monkeypatch.setattr(app.state.logs, "bind", fail_once)
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 507
        assert all(s.closed for s in upstream.streams)
        assert not any(a["busy"] for a in app.state.pool.snapshot())
        assert (await app.state.logs.listing())["data"][0]["status"] == "failed"

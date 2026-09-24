import asyncio
import json
import sqlite3
import threading

import httpx
import pytest

from tests.test_app import HEADERS, Upstream, payload, running, wire


class PausedStream(httpx.AsyncByteStream):
    def __init__(self, before_first=False, empty_before_wait=False):
        self.before_first = before_first
        self.empty_before_wait = empty_before_wait
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        if not self.before_first:
            yield wire([{"responseId": "test"}, {"content": "partial"}])
        if self.empty_before_wait:
            yield wire([{}])
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed.set()


def asgi_request(app, data, disconnect, messages):
    sent_body = False
    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
             "query_string": b"", "scheme": "http", "http_version": "1.1", "root_path": "",
             "headers": [(b"content-type", b"application/json"), (b"authorization", b"Bearer test-secret")],
             "client": ("127.0.0.1", 1234), "server": ("test", 80)}
    return asyncio.create_task(app(scope, receive, send))


@pytest.mark.parametrize("before_first,stream", [(True, False), (True, True), (False, False), (False, True)])
@pytest.mark.parametrize("empty_before_wait", [False, True])
async def test_disconnect_releases_upstream_without_waiting_for_timeout(tmp_path, before_first, stream, empty_before_wait):
    upstream = Upstream()
    slow = PausedStream(before_first, empty_before_wait)
    async def handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, stream=slow)
        return await upstream(request)
    async with running(tmp_path, handler) as (app, _):
        disconnect = asyncio.Event()
        messages = []
        task = asgi_request(app, payload(stream), disconnect, messages)
        await asyncio.wait_for(slow.waiting.wait(), 2)
        assert not task.done()
        if before_first:
            assert not any(message["type"] == "http.response.start" for message in messages)
        disconnect.set()
        await asyncio.wait_for(task, 2)
        body = b"".join(message.get("body", b"") for message in messages)
        assert b"[DONE]" not in body
        assert b'"finish_reason":"stop"' not in body.replace(b" ", b"")
        assert slow.closed.is_set()
        assert not any(row["busy"] for row in app.state.pool.snapshot())
        row = (await app.state.logs.listing())["data"][0]
        assert row["status"] == "cancelled", row
        assert row["account_id"]
        assert app.state.pool.snapshot()[0]["status"] == "ready"
        detail = await app.state.logs.detail(row["id"])
        assert detail["content"] == ("" if before_first else "partial")


async def test_cancel_during_threaded_insert_finalizes_row(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    async with running(tmp_path, Upstream()) as (app, client):
        logs = app.state.logs
        original = logs._run
        async def delayed(operation):
            if operation.__name__ != "insert":
                return await original(operation)
            def execute():
                entered.set()
                assert release.wait(3)
                with logs.lock, sqlite3.connect(logs.path) as db:
                    db.row_factory = sqlite3.Row
                    return operation(db)
            return await asyncio.to_thread(execute)
        monkeypatch.setattr(logs, "_run", delayed)
        task = asyncio.create_task(client.post("/v1/chat/completions", headers=HEADERS, json=payload()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        rows = (await logs.listing())["data"]
        assert len(rows) == 1 and rows[0]["status"] == "cancelled"
        assert await logs.delete([rows[0]["id"]]) == 1

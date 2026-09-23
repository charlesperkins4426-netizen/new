import os

import pytest

from gateway.errors import GatewayError
from gateway.logs import RequestLogs, utcnow


async def test_logging_redacts_current_and_rotated_secrets_without_rewriting_ids(tmp_path):
    current = ["123456", "cookie-old"]
    logs = RequestLogs(tmp_path / "data", lambda: current, 7)
    await logs.initialize()
    await logs.start("chat-123456", utcnow(), "model", [{"role": "user", "content": "cookie-old"}])
    old = list(current)
    current[:] = ["123456", "cookie-new"]
    await logs.finish("chat-123456", status="success", ttft_ms=2, duration_ms=5,
                      content="cookie-old cookie-new 123456", reasoning_content="reason",
                      extra_secrets=old)
    row = await logs.detail("chat-123456")
    assert row["status"] == "success"
    assert row["content"] == "[已脱敏] [已脱敏] [已脱敏]"
    assert row["messages"][0]["content"] == "[已脱敏]"
    assert os.stat(logs.path).st_mode & 0o777 == 0o600


async def test_interrupted_requests_retention_and_no_running_delete(tmp_path):
    logs = RequestLogs(tmp_path, lambda: [], 7)
    await logs.initialize()
    await logs.start("running", utcnow(), "model", [])
    assert await logs.delete(None) == 0
    await logs.start("old", "2000-01-01T00:00:00+00:00", "model", [])
    await logs.finish("old", status="failed", ttft_ms=None, duration_ms=1, content="", reasoning_content="")
    assert (await logs.listing())["total"] == 1
    await logs.initialize()
    assert (await logs.detail("running"))["status"] == "interrupted"
    assert await logs.delete(["running"]) == 1
    with pytest.raises(GatewayError) as caught:
        await logs.detail("running")
    assert caught.value.status == 404


async def test_database_failure_is_explicit(tmp_path):
    logs = RequestLogs(tmp_path, lambda: [], 7)
    await logs.initialize()
    logs.path.unlink()
    logs.path.mkdir()
    with pytest.raises(GatewayError) as caught:
        await logs.start("x", utcnow(), "model", [])
    assert caught.value.code == "logs_unavailable"

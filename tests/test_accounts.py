import asyncio
import json
import threading
import time

import httpx
import pytest

from gateway.accounts import AccountPool
from gateway.config import ConfigStore, SESSION_COOKIE, _DEFAULTS
from gateway.errors import GatewayError


COOKIE = f"{SESSION_COOKIE}=synthetic-account-one"
OTHER = f"{SESSION_COOKIE}=synthetic-account-two"


@pytest.fixture
def config(tmp_path, monkeypatch):
    for key in _DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("ACCOUNT_WAIT_SECONDS=0.05\n")
    return ConfigStore(path)


@pytest.fixture
async def pool(config):
    pool = AccountPool(config, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    yield pool
    await pool.close()


async def assert_lease_error(pool, code, **kwargs):
    with pytest.raises(GatewayError) as caught:
        async with pool.lease(**kwargs):
            pytest.fail("Unexpected lease")
    assert caught.value.code == code
    return caught.value


async def test_empty_disabled_cooling_busy_and_unavailable_are_distinct(pool):
    assert (await assert_lease_error(pool, "no_accounts")).status == 503
    account_id = (await pool.add_accounts(COOKIE, enabled=False))["ids"][0]
    assert pool.enabled_ids() == set()
    await assert_lease_error(pool, "all_disabled")
    await pool.set_enabled(account_id, True)
    assert pool.enabled_ids() == {account_id}
    await assert_lease_error(pool, "model_unavailable", eligible_ids=set())
    pool.mark_failure(account_id, "upstream_network", "网络故障", retry_after=120)
    error = await assert_lease_error(pool, "all_cooling")
    assert error.retry_after == 120
    assert pool.snapshot()[0]["status"] == "cooling"
    pool.mark_success(account_id)
    async with pool.lease():
        started = time.monotonic()
        error = await assert_lease_error(pool, "accounts_busy")
        assert 0.04 <= time.monotonic() - started < 0.5
        assert error.retry_after == 1
        assert pool.snapshot()[0]["busy"]
    assert pool.snapshot()[0]["status"] == "ready"


async def test_round_robin_and_eligible_account_selection(pool):
    ids = (await pool.add_accounts(COOKIE + "|||" + OTHER))["ids"]
    observed = []
    for _ in range(6):
        async with pool.lease() as lease:
            observed.append(lease.account_id)
    assert observed == ids * 3
    async with pool.lease(eligible_ids={ids[1]}) as lease:
        assert lease.account_id == ids[1]
    await pool.set_enabled(ids[0], False)
    for _ in range(3):
        async with pool.lease() as lease:
            assert lease.account_id == ids[1]


async def test_different_accounts_parallel_same_account_serial(pool):
    ids = (await pool.add_accounts(COOKIE + "\n" + OTHER))["ids"]
    async with pool.lease(ids[0]) as first:
        async with pool.lease() as second:
            assert first.account_id != second.account_id
            assert first.client is not second.client
            assert first.client.cookies is not second.client.cookies
            assert first.client.headers["content-type"] == "application/json"
            assert first.client.headers["referer"] == "https://chat.dialx.ai/"
            assert first.client.headers["x-timezone"] == "UTC"
            assert first.client.headers["x-language"] == "en"
            assert not first.client.follow_redirects
            assert first.client.timeout.read == 120
            assert first.client.timeout.connect == 15
            await assert_lease_error(pool, "accounts_busy", account_id=ids[0], for_check=True)


async def test_waiting_lease_acquires_after_release(pool):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    entered = asyncio.Event()

    async def waiter():
        async with pool.lease(account_id):
            entered.set()

    async with pool.lease(account_id):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        assert not entered.is_set()
    await task
    assert entered.is_set()


async def test_free_check_can_lease_disabled_and_cooling_without_enabling(pool):
    account_id = (await pool.add_accounts(COOKIE, enabled=False))["ids"][0]
    pool.mark_failure(account_id, "upstream_auth", "会话无效")
    async with pool.lease(account_id, for_check=True):
        pool.mark_success(account_id)
    assert pool.enabled_ids() == set()
    assert pool.snapshot()[0]["status"] == "disabled"
    await assert_lease_error(pool, "all_disabled")
    await assert_lease_error(pool, "invalid_request", for_check=True)


async def test_model_cooldown_does_not_disable_other_models(pool):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    pool.mark_failure(account_id, "upstream_quota", "该模型配额不足", model_id="limited")
    await assert_lease_error(pool, "all_cooling", model_id="limited")
    async with pool.lease(model_id="other"):
        pool.mark_success(account_id, model_id="other")
    await assert_lease_error(pool, "all_cooling", model_id="limited")
    pool.mark_success(account_id, model_id="limited")
    async with pool.lease(model_id="limited"):
        pass


async def test_session_jar_rotation_and_chunk_deletion_persist_before_next_lease(config):
    initial = f"{SESSION_COOKIE}.0=old-zero; {SESSION_COOKIE}.1=old-one"
    account_id = config.add_accounts(initial, enabled=False)["ids"][0]
    original = config.accounts()[0]
    received = []

    def handler(request):
        received.append(request.headers["cookie"])
        assert request.url.host == "chat.dialx.ai"
        assert "authorization" not in request.headers
        if len(received) == 1:
            return httpx.Response(200, json={"user": {}, "expires": "2030-01-01T00:00:00Z"}, headers=[
                ("set-cookie", f"{SESSION_COOKIE}.1=new-one; Path=/; Secure; HttpOnly"),
                ("set-cookie", f"{SESSION_COOKIE}.0=new-zero; Path=/; Secure; HttpOnly"),
                ("set-cookie", "ignored-tracking=private-tracking; Path=/"),
            ])
        return httpx.Response(200, json=[], headers=[
            ("set-cookie", f"{SESSION_COOKIE}.1=; Max-Age=0; Path=/; Secure"),
            ("set-cookie", f"{SESSION_COOKIE}.0=final-zero; Path=/; Secure; HttpOnly"),
        ])

    pool = AccountPool(config, transport=httpx.MockTransport(handler))
    try:
        async with pool.lease(account_id, for_check=True) as lease:
            await lease.client.get("/api/auth/session")
            assert config.accounts()[0]["cookie"] == initial
        after = config.accounts()[0]
        assert after == {**original, "cookie": f"{SESSION_COOKIE}.0=new-zero; {SESSION_COOKIE}.1=new-one"}
        async with pool.lease(account_id, for_check=True) as lease:
            await lease.client.get("/api/models")
        assert "new-zero" in received[1] and "old-zero" not in received[1]
        latest = ConfigStore(config.path)
        assert latest.accounts()[0]["cookie"] == f"{SESSION_COOKIE}.0=final-zero"
        assert latest.disabled_ids == {account_id}
        assert "private-tracking" not in config.path.read_text()
    finally:
        await pool.close()


async def test_expired_session_cookie_pauses_account_without_persisting_empty(config):
    account_id = config.add_accounts(COOKIE)["ids"][0]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}, headers={
        "set-cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; Secure",
    }))
    pool = AccountPool(config, transport=transport)
    try:
        with pytest.raises(GatewayError) as caught:
            async with pool.lease() as lease:
                await lease.client.get("/api/auth/session")
        assert caught.value.code == "invalid_cookie"
        assert not pool.snapshot()[0]["busy"]
        assert pool.snapshot()[0]["status"] == "error"
        assert pool.snapshot()[0]["check_status"] == "invalid"
        assert config.accounts()[0]["cookie"] == COOKIE
        await assert_lease_error(pool, "all_cooling")
        async with pool.lease(account_id, for_check=True) as lease:
            lease.client.cookies.set(SESSION_COOKIE, "repaired-value", domain="chat.dialx.ai", path="/")
            pool.mark_success(account_id)
        assert pool.snapshot()[0]["status"] == "ready"
    finally:
        await pool.close()


async def test_disable_during_rotation_preserves_disabled_flag_and_other_config(pool, config):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    async with pool.lease() as lease:
        lease.client.cookies.set(SESSION_COOKIE, "rotated-value", domain="chat.dialx.ai", path="/")
        await pool.set_enabled(account_id, False)
        await asyncio.to_thread(config.set_default_model, "new-default")
        assert pool.snapshot()[0]["busy"]
    restarted = ConfigStore(config.path)
    assert restarted.disabled_ids == {account_id}
    assert restarted.settings.default_model == "new-default"
    assert restarted.accounts()[0]["cookie"] == f"{SESSION_COOKIE}=rotated-value"
    await assert_lease_error(pool, "all_disabled")


async def test_delete_then_readd_cannot_resurrect_old_generation(pool, config):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    async with pool.lease() as lease:
        old_client = lease.client
        old_generation = lease.generation
        assert await pool.delete_accounts([account_id]) == 1
        assert not old_client.is_closed
        assert pool.snapshot() == []
        assert (await pool.add_accounts(COOKIE, enabled=False))["ids"] == [account_id]
        old_client.cookies.set(SESSION_COOKIE, "late-response", domain="chat.dialx.ai", path="/")
    assert old_client.is_closed
    assert config.accounts()[0]["generation"] != old_generation
    assert config.accounts()[0]["cookie"] == COOKIE
    assert config.disabled_ids == {account_id}
    assert not pool.snapshot()[0]["busy"]


async def test_delete_during_response_expiry_ignores_late_empty_jar(pool, config):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    async with pool.lease() as lease:
        await pool.delete_accounts([account_id])
        lease.client.cookies.clear()
    assert config.accounts() == []
    assert lease.client.is_closed


async def test_persistence_failure_pauses_account_and_is_not_swallowed(pool, config, monkeypatch):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    before = config.path.read_bytes()

    def fail(*args):
        raise OSError("sensitive-file-details")

    with monkeypatch.context() as patch:
        patch.setattr(config, "_write", fail)
        with pytest.raises(GatewayError) as caught:
            async with pool.lease() as lease:
                lease.client.cookies.set(SESSION_COOKIE, "rotated-value", domain="chat.dialx.ai", path="/")
        assert caught.value.code == "config_persistence_failed"
        assert caught.value.status == 507
        assert "sensitive-file-details" not in str(caught.value)
    assert config.path.read_bytes() == before
    assert config.accounts()[0]["cookie"] == COOKIE
    assert pool.snapshot()[0]["status"] == "error"
    assert not pool.snapshot()[0]["busy"]
    pool.mark_success(account_id)
    assert pool.snapshot()[0]["status"] == "error"  # health success cannot hide an unsaved jar
    await assert_lease_error(pool, "all_cooling")
    async with pool.lease(account_id, for_check=True):
        pool.mark_success(account_id)
    assert config.accounts()[0]["cookie"] == f"{SESSION_COOKIE}=rotated-value"
    assert pool.snapshot()[0]["status"] == "ready"
    assert pool.snapshot()[0]["last_error"] is None


async def test_cancellation_persists_rotation_and_releases_lease(pool, config):
    await pool.add_accounts(COOKIE)
    entered = asyncio.Event()

    async def work():
        async with pool.lease() as lease:
            lease.client.cookies.set(SESSION_COOKIE, "rotated-value", domain="chat.dialx.ai", path="/")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert config.accounts()[0]["cookie"] == f"{SESSION_COOKIE}=rotated-value"
    assert not pool.snapshot()[0]["busy"]
    async with pool.lease():
        pass


async def test_repeated_cancellation_waits_for_persistence_thread(pool, config, monkeypatch):
    await pool.add_accounts(COOKIE)
    entered, finish = threading.Event(), threading.Event()
    original = config.refresh_cookie

    def blocked(*args):
        entered.set()
        assert finish.wait(timeout=2)
        return original(*args)

    monkeypatch.setattr(config, "refresh_cookie", blocked)

    async def work():
        async with pool.lease() as lease:
            lease.client.cookies.set(SESSION_COOKIE, "rotated-value", domain="chat.dialx.ai", path="/")

    task = asyncio.create_task(work())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert pool.snapshot()[0]["busy"]
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pool.snapshot()[0]["busy"]
    assert config.accounts()[0]["cookie"] == f"{SESSION_COOKIE}=rotated-value"


async def test_cancelled_account_mutation_finishes_reconciliation(pool, config, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    original = config.add_accounts

    def blocked(*args):
        entered.set()
        assert finish.wait(timeout=2)
        return original(*args)

    monkeypatch.setattr(config, "add_accounts", blocked)
    task = asyncio.create_task(pool.add_accounts(COOKIE))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(config.accounts()) == len(pool.snapshot()) == 1
    async with pool.lease():
        pass


async def test_close_defers_busy_client_until_lease_exit(pool):
    ids = (await pool.add_accounts(COOKIE + "|||" + OTHER))["ids"]
    async with pool.lease(ids[1]) as idle:
        pass
    async with pool.lease(ids[0]) as active:
        await pool.close()
        assert idle.client.is_closed
        assert not active.client.is_closed
        await assert_lease_error(pool, "pool_closed")
    assert active.client.is_closed
    await pool.close()


async def test_exception_releases_lease_without_swallowing_original(pool):
    await pool.add_accounts(COOKIE)
    with pytest.raises(ValueError, match="original-error"):
        async with pool.lease():
            raise ValueError("original-error")
    assert not pool.snapshot()[0]["busy"]


async def test_waiter_cancellation_does_not_release_owner(pool):
    await pool.add_accounts(COOKIE)

    async def wait():
        async with pool.lease():
            pytest.fail("The owner still holds this account")

    async with pool.lease():
        task = asyncio.create_task(wait())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.snapshot()[0]["busy"]
    assert not pool.snapshot()[0]["busy"]


async def test_snapshot_and_record_check_are_copies_and_do_not_expose_secrets(pool):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    result = {
        "checked_at": "2026-09-23T00:00:00Z", "session_expires": "2026-10-23T00:00:00Z",
        "check_status": "valid", "limits": {"dayTokenStats": {"total": 100, "used": 4}},
        "error": None,
    }
    pool.record_check(account_id, result)
    result["limits"]["dayTokenStats"]["used"] = 999
    row = pool.snapshot()[0]
    assert row["limits"]["dayTokenStats"]["used"] == 4
    row["limits"]["dayTokenStats"]["used"] = 888
    assert pool.snapshot()[0]["limits"]["dayTokenStats"]["used"] == 4
    pool.mark_failure(account_id, "upstream_auth", f"denied {COOKIE} 123456")
    serialized = json.dumps(pool.snapshot())
    assert "synthetic-account-one" not in serialized
    assert "123456" not in serialized
    assert "generation" not in row and "cookie" not in row and "client" not in row


async def test_cookie_jars_do_not_cross_accounts_and_never_follow_redirects(config):
    ids = config.add_accounts(COOKIE + "|||" + OTHER)["ids"]
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(302, headers={"location": "https://unrelated.invalid/collect"})

    pool = AccountPool(config, transport=httpx.MockTransport(handler))
    try:
        for account_id in ids:
            async with pool.lease(account_id) as lease:
                response = await lease.client.get("/api/models")
                assert response.status_code == 302
        assert seen == [COOKIE, OTHER]
    finally:
        await pool.close()


@pytest.mark.parametrize("code", [
    "invalid_cookie", "upstream_auth", "upstream_network", "upstream_timeout",
    "upstream_connection", "upstream_unavailable",
])
async def test_credential_network_failures_remain_global_with_model_hint(pool, code):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    pool.mark_failure(account_id, code, "请求失败", model_id="specific")
    await assert_lease_error(pool, "all_cooling", model_id="other")


async def test_cooldown_expires_on_monotonic_time(pool, monkeypatch):
    from types import SimpleNamespace
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]
    clock = [1000.0]
    monkeypatch.setattr("gateway.accounts.time", SimpleNamespace(monotonic=lambda: clock[0]))
    pool.mark_failure(account_id, "upstream_network", "请求失败")
    await assert_lease_error(pool, "all_cooling")
    clock[0] += 61
    assert pool.snapshot()[0]["status"] == "ready"
    async with pool.lease():
        pass


async def test_failed_admin_write_does_not_change_pool_state(pool, config, monkeypatch):
    account_id = (await pool.add_accounts(COOKIE))["ids"][0]

    def fail(*args):
        raise OSError("test-failure")

    monkeypatch.setattr(config, "_write", fail)
    with pytest.raises(GatewayError):
        await pool.set_enabled(account_id, False)
    assert pool.enabled_ids() == {account_id}
    assert config.disabled_ids == set()
    with pytest.raises(GatewayError):
        await pool.delete_accounts([account_id])
    assert len(pool.snapshot()) == 1


async def test_duplicate_import_does_not_enable_disabled_account(pool):
    account_id = (await pool.add_accounts(COOKIE, enabled=False))["ids"][0]
    assert (await pool.add_accounts("Cookie: tracking=ignored; " + COOKIE))["duplicates"] == 1
    assert pool.enabled_ids() == set()
    assert pool.snapshot()[0]["id"] == account_id


async def test_chunked_cookie_can_rotate_to_unchunked_session(config):
    config.add_accounts(f"{SESSION_COOKIE}.0=old-zero; {SESSION_COOKIE}.1=old-one")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=[
        ("set-cookie", f"{SESSION_COOKIE}.0=; Max-Age=0; Path=/; Secure"),
        ("set-cookie", f"{SESSION_COOKIE}.1=; Max-Age=0; Path=/; Secure"),
        ("set-cookie", f"{SESSION_COOKIE}=single-new-session; Path=/; Secure"),
    ]))
    pool = AccountPool(config, transport=transport)
    try:
        async with pool.lease() as lease:
            await lease.client.get("/api/auth/session")
        assert config.accounts()[0]["cookie"] == f"{SESSION_COOKIE}=single-new-session"
    finally:
        await pool.close()

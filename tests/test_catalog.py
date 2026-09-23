import asyncio

import httpx

from tests.test_app import HEADERS, MODEL, Upstream, payload, running


async def test_capability_collision_does_not_hide_another_accounts_model(tmp_path):
    (tmp_path / ".env").write_text("API_TOKEN='test-secret'\nDIALX_COOKIES='__Secure-next-auth.session-token=a-cookie|||__Secure-next-auth.session-token=b-cookie'\n")
    upstream = Upstream()
    selected = []
    async def handler(request):
        cookie = request.headers.get("cookie", "")
        if request.url.path == "/api/models":
            return httpx.Response(200, json=[{**MODEL, "features": {"chat_completion": "a-cookie" in cookie}}])
        if request.url.path == "/api/chat":
            selected.append(cookie)
        return await upstream(request)
    async with running(tmp_path, handler) as (_, client):
        result = await client.get("/v1/models", headers=HEADERS)
        assert [m["id"] for m in result.json()["data"]] == [MODEL["id"]]
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 200
        assert len(selected) == 1 and "a-cookie" in selected[0]


async def test_delayed_old_catalog_never_populates_replacement(tmp_path):
    upstream = Upstream()
    entered, release = asyncio.Event(), asyncio.Event()
    first = True
    async def handler(request):
        nonlocal first
        if request.url.path == "/api/models" and first:
            first = False
            entered.set()
            await release.wait()
            return httpx.Response(200, json=[{**MODEL, "id": "old-only"}])
        return await upstream(request)
    async with running(tmp_path, handler) as (app, client):
        account_id = app.state.pool.snapshot()[0]["id"]
        refresh = asyncio.create_task(client.get("/api/admin/models"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await client.post("/api/admin/accounts/delete", json={"ids": [account_id]})
            await client.post("/api/admin/accounts", json={"cookies": "__Secure-next-auth.session-token=test-cookie"})
        finally:
            release.set()
        assert (await refresh).json()["data"] == []
        assert app.state.dialx.tools_view()["data"] == []
        current = (await client.get("/api/admin/models")).json()["data"]
        assert "old-only" not in {m["id"] for m in current}


async def test_model_disappears_while_waiting_for_lease_is_clear_error(tmp_path, monkeypatch):
    upstream = Upstream()
    async with running(tmp_path, upstream) as (app, client):
        await client.get("/v1/models", headers=HEADERS)
        pool = app.state.pool
        original = pool._acquire
        changed = False
        async def acquire(account_id, eligible_ids, model_id, for_check):
            nonlocal changed
            result = await original(account_id, eligible_ids, model_id, for_check)
            if model_id and not changed:
                changed = True
                # A completed refresh replaced the cache while the lease was queued.
                app.state.dialx.cache[result.account_id]["models"] = []
            return result
        monkeypatch.setattr(pool, "_acquire", acquire)
        response = await client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_unavailable"
        assert not any(r.url.path == "/api/chat" for r in upstream.requests)


async def test_concurrent_forced_refreshes_share_one_fetch(tmp_path):
    upstream = Upstream()
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.path == "/api/models":
            entered.set()
            await release.wait()
        return await upstream(request)
    async with running(tmp_path, handler) as (_, client):
        first = asyncio.create_task(client.post("/api/admin/models/refresh", json={}))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(client.post("/api/admin/models/refresh", json={}))
        await asyncio.sleep(0.02)
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(first, second))
        assert sum(r.url.path == "/api/models" for r in upstream.requests) == 1


async def test_cache_ttl_and_refresh_failure_remain_visible_until_success(tmp_path):
    upstream = Upstream()
    fail = False
    async def handler(request):
        if request.url.path == "/api/models" and fail:
            return httpx.Response(503)
        return await upstream(request)
    async with running(tmp_path, handler) as (app, client):
        await client.get("/api/admin/models")
        await client.get("/api/admin/models")
        assert sum(r.url.path == "/api/models" for r in upstream.requests) == 1
        entry = next(iter(app.state.dialx.cache.values()))
        entry["time"] -= app.state.config.settings.model_cache_ttl + 1
        await client.get("/api/admin/models")
        assert sum(r.url.path == "/api/models" for r in upstream.requests) == 2
        fail = True
        failed = (await client.post("/api/admin/models/refresh", json={})).json()
        assert failed["data"] and failed["stale"] and failed["error"]
        cached = (await client.get("/api/admin/models")).json()
        assert cached["stale"] and cached["error"] == failed["error"]
        fail = False
        record = app.state.config.accounts()[0]
        app.state.pool.mark_success(record["id"], generation=record["generation"])
        refreshed = (await client.post("/api/admin/models/refresh", json={})).json()
        assert not refreshed["stale"] and refreshed["error"] is None

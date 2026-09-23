"""Per-account HTTP clients and cancellation-safe, serial round-robin leases."""
from __future__ import annotations

import asyncio
import copy
import math
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from gateway.config import ConfigStore, normalize_cookie
from gateway.errors import GatewayError


@dataclass(repr=False)
class _Account:
    account_id: str
    generation: str
    cookie: str
    enabled: bool
    client: httpx.AsyncClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    removed: bool = False
    paused: bool = False
    cooldown: float = 0
    model_cooldowns: dict[str, float] = field(default_factory=dict)
    last_error: str | None = None
    last_checked: str | None = None
    session_expires: str | None = None
    limits: dict | None = None
    check_status: str = "untested"


@dataclass(frozen=True, repr=False)
class AccountLease:
    account_id: str
    generation: str
    client: httpx.AsyncClient


async def _finish(coro):
    """Complete cleanup even if the caller is cancelled repeatedly."""
    task = asyncio.create_task(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class AccountPool:
    def __init__(self, config: ConfigStore, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._lock = threading.RLock()
        self._mutation_lock = asyncio.Lock()
        self._states: dict[str, _Account] = {}
        self._cursor = 0
        self._closed = False
        self._reconcile()

    def _reconcile(self) -> list[_Account]:
        records, disabled = self.config.accounts(), self.config.disabled_ids
        current = {record["id"]: record for record in records}
        closing = []
        with self._lock:
            for account_id, state in list(self._states.items()):
                if account_id not in current or current[account_id]["generation"] != state.generation:
                    state.removed = True
                    del self._states[account_id]
                    if not state.busy:
                        closing.append(state)
            for record in records:
                state = self._states.get(record["id"])
                if state is None:
                    state = _Account(record["id"], record["generation"], record["cookie"], True)
                    self._states[state.account_id] = state
                state.enabled = state.account_id not in disabled
            self._cursor %= max(1, len(self._states))
        return closing

    @staticmethod
    async def _close_states(states: list[_Account]) -> None:
        for state in states:
            if state.client is not None:
                await state.client.aclose()

    async def _mutate(self, operation, *args):
        async def run():
            async with self._mutation_lock:
                if self._closed:
                    raise GatewayError(503, "pool_closed", "账号池已关闭。")
                result = await asyncio.to_thread(operation, *args)
                closing = self._reconcile()
                await self._close_states(closing)
                return result

        # to_thread cannot be stopped by cancellation: finish publishing its result.
        return await _finish(run())

    async def add_accounts(self, text: str, enabled: bool = True) -> dict:
        return await self._mutate(self.config.add_accounts, text, enabled)

    async def delete_accounts(self, ids: list[str]) -> int:
        return await self._mutate(self.config.delete_accounts, ids)

    async def set_enabled(self, account_id: str, enabled: bool) -> None:
        await self._mutate(self.config.set_enabled, account_id, enabled)

    def _client(self, state: _Account) -> httpx.AsyncClient:
        settings = self.config.settings
        cookies = httpx.Cookies()
        for item in state.cookie.split("; "):
            name, value = item.split("=", 1)
            cookies.set(name, value, domain="chat.dialx.ai", path="/")
        return httpx.AsyncClient(
            base_url="https://chat.dialx.ai", cookies=cookies, transport=self._transport,
            headers={"Content-Type": "application/json", "Referer": "https://chat.dialx.ai/",
                     "x-timezone": "UTC", "x-language": "en"},
            follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(settings.upstream_timeout, connect=settings.connect_timeout),
        )

    def enabled_ids(self) -> set[str]:
        with self._lock:
            return {state.account_id for state in self._states.values() if state.enabled}

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = time.monotonic()
            rows = []
            for state in self._states.values():
                status = "ready"
                if not state.enabled:
                    status = "disabled"
                elif state.paused:
                    status = "error"
                elif state.busy:
                    status = "busy"
                elif state.cooldown > now:
                    status = "cooling"
                until = None
                if state.cooldown > now:
                    until = (datetime.now(timezone.utc) + timedelta(seconds=state.cooldown - now)).isoformat()
                rows.append({
                    "id": state.account_id, "enabled": state.enabled, "busy": state.busy,
                    "status": status, "cooldown_until": until, "last_error": state.last_error,
                    "last_checked": state.last_checked, "session_expires": state.session_expires,
                    "limits": copy.deepcopy(state.limits), "check_status": state.check_status,
                })
            return rows

    async def _acquire(self, account_id, eligible_ids, model_id, for_check) -> _Account:
        deadline = time.monotonic() + self.config.settings.account_wait_seconds
        if for_check and account_id is None:
            raise GatewayError(400, "invalid_request", "免费检查必须指定账号。")
        while True:
            with self._lock:
                if self._closed:
                    raise GatewayError(503, "pool_closed", "账号池已关闭。")
                states = list(self._states.values())
                if not states:
                    raise GatewayError(503, "no_accounts", "尚未配置账号，请先导入 Cookie。")
                candidates = [state for state in states if account_id is None or state.account_id == account_id]
                if not candidates:
                    raise GatewayError(404, "account_not_found", "账号不存在。")
                if eligible_ids is not None:
                    candidates = [state for state in candidates if state.account_id in eligible_ids]
                    if not candidates:
                        raise GatewayError(503, "model_unavailable", "没有账号可使用所选模型。")
                candidates = [state for state in candidates if state.enabled or for_check]
                if not candidates:
                    raise GatewayError(503, "all_disabled", "所有候选账号均已停用。")
                now = time.monotonic()
                healthy = [state for state in candidates if for_check or (
                    not state.paused and state.cooldown <= now
                    and state.model_cooldowns.get(model_id, 0) <= now
                )]
                if not healthy:
                    waits = [max(state.cooldown, state.model_cooldowns.get(model_id, 0)) - now
                             for state in candidates if not state.paused]
                    retry = max(1, math.ceil(min(waits))) if waits else None
                    raise GatewayError(503, "all_cooling", "候选账号正在冷却或等待修复，请稍后重试或免费检查。", retry_after=retry)
                available = {state.account_id for state in healthy if not state.busy}
                for offset in range(len(states)):
                    index = (self._cursor + offset) % len(states)
                    state = states[index]
                    if state.account_id in available:
                        state.busy = True
                        self._cursor = (index + 1) % len(states)
                        return state
                remaining = deadline - now
                if remaining <= 0:
                    raise GatewayError(503, "accounts_busy", "候选账号正在处理其他请求，请稍后重试。", retry_after=1)
            # No network, await, or blocking lock acquisition while the pool lock is held.
            await asyncio.sleep(min(0.025, remaining))

    @asynccontextmanager
    async def lease(self, account_id: str | None = None, *, eligible_ids: set[str] | None = None,
                    model_id: str | None = None, for_check: bool = False):
        state = await self._acquire(account_id, eligible_ids, model_id, for_check)
        locked = False
        try:
            await state.lock.acquire()
            locked = True
            if state.client is None:
                state.client = self._client(state)
            yield AccountLease(state.account_id, state.generation, state.client)
        finally:
            await _finish(self._release(state, locked))

    async def _release(self, state: _Account, locked: bool) -> None:
        try:
            if state.client is not None:
                # HTTPX selects unexpired cookies by domain/path and processes Set-Cookie
                # deletions itself. Never read a Set-Cookie string into a global jar.
                cookie = state.client.build_request("GET", "/api/chat").headers.get("cookie", "")
                if cookie != state.cookie or state.paused:
                    saved = await asyncio.to_thread(
                        self.config.refresh_cookie, state.account_id, state.generation, cookie,
                    )
                    if saved:
                        with self._lock:
                            state.cookie = normalize_cookie(cookie)
                            if state.paused:
                                state.last_error = None
                            state.paused = False
        except GatewayError as error:
            with self._lock:
                state.paused = True
                state.last_error = error.message
                if error.code in {"invalid_cookie", "upstream_auth"}:
                    state.check_status = "invalid"
            raise
        except Exception:
            with self._lock:
                state.paused = True
                state.last_error = "无法保存会话；账号已暂停，请修复配置存储后免费检查。"
            raise GatewayError(507, "config_persistence_failed", state.last_error, account_id=state.account_id) from None
        finally:
            # Keep the serial lease until persistence (including its worker thread) ends.
            try:
                if state.removed or self._closed:
                    await self._close_states([state])
            finally:
                with self._lock:
                    state.busy = False
                    if locked:
                        state.lock.release()

    def _safe_message(self, message: str) -> str:
        secrets = self.config.secret_values()
        with self._lock:
            for state in self._states.values():
                secrets.append(state.cookie)
                secrets.extend(item.split("=", 1)[1] for item in state.cookie.split("; "))
        text = str(message)
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text[:500]

    def mark_failure(self, account_id: str, code: str, message: str, *,
                     retry_after: int | None = None, model_id: str | None = None) -> None:
        message = self._safe_message(message)
        duration = max(self.config.settings.cooldown_seconds, retry_after or 0)
        with self._lock:
            state = self._states.get(account_id)
            if state is None:
                return
            state.last_error = message
            until = time.monotonic() + duration
            global_failure = code in {
                "invalid_cookie", "upstream_auth", "invalid_session", "upstream_network", "upstream_timeout",
                "upstream_connection", "upstream_unavailable",
                "upstream_server_error", "config_persistence_failed",
            }
            if model_id is not None and not global_failure:
                state.model_cooldowns[model_id] = max(state.model_cooldowns.get(model_id, 0), until)
            else:
                state.cooldown = max(state.cooldown, until)
            if code in {"invalid_cookie", "upstream_auth", "invalid_session"}:
                state.check_status = "invalid"

    def mark_success(self, account_id: str, *, model_id: str | None = None) -> None:
        with self._lock:
            state = self._states.get(account_id)
            if state is None:
                return
            if model_id is not None:
                state.model_cooldowns.pop(model_id, None)
            else:
                state.cooldown = 0
            if not state.paused:
                state.last_error = None

    def record_check(self, account_id: str, result: dict) -> None:
        error = self._safe_message(result["error"]) if result.get("error") else None
        with self._lock:
            state = self._states.get(account_id)
            if state is None:
                return
            state.last_checked = result.get("checked_at")
            state.session_expires = result.get("session_expires")
            state.limits = copy.deepcopy(result.get("limits"))
            status = result.get("check_status", "error")
            state.check_status = status if status in {"untested", "valid", "invalid", "error"} else "error"
            state.last_error = error

    async def close(self) -> None:
        async def run():
            async with self._mutation_lock:
                with self._lock:
                    self._closed = True
                    idle = [state for state in self._states.values() if not state.busy]
                await self._close_states(idle)
                # In-flight leases retain their clients and close them on exit.

        await _finish(run())

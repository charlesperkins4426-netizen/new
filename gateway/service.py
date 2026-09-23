from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .errors import GatewayError
from .logs import utcnow
from .protocol import Channels, frames


def retry_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(1, min(int(value), 86400))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            return max(1, min(int((stamp - datetime.now(timezone.utc)).total_seconds()), 86400))
        except (ValueError, TypeError, OverflowError):
            return None


def check_response(response: httpx.Response):
    status = response.status_code
    if 200 <= status < 300:
        return
    retry = retry_seconds(response.headers.get("retry-after"))
    if status == 401 or 300 <= status < 400:
        raise GatewayError(502, "invalid_cookie", "上游登录已失效，请重新获取完整 Cookie。")
    if status == 403:
        raise GatewayError(502, "upstream_forbidden", "上游拒绝访问；账号可能没有此模型权限。")
    if status == 429:
        raise GatewayError(429, "upstream_rate_limited", "上游请求或配额受限，请稍后再试。", retry_after=retry)
    if status in (400, 404, 422):
        raise GatewayError(502, "upstream_rejected", f"上游拒绝请求（HTTP {status}）。")
    raise GatewayError(502, "upstream_unavailable", f"上游服务异常（HTTP {status}）。", retry_after=retry)


async def get_json(client: httpx.AsyncClient, path: str):
    async with client.stream("GET", path) as response:
        check_response(response)
        if "application/json" not in response.headers.get("content-type", "").lower():
            raise GatewayError(502, "unexpected_response", "上游没有返回预期的 JSON，登录可能已失效。")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise GatewayError(502, "response_too_large", "上游目录响应超过大小限制。")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise GatewayError(502, "invalid_json", "上游返回无效的 JSON。") from None


def transport_error(exc: Exception) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(504, "upstream_timeout", "等待上游响应超时。")
    if isinstance(exc, httpx.HTTPError):
        return GatewayError(502, "upstream_connection", "无法连接上游服务。")
    return GatewayError(500, "internal_error", "网关发生内部错误，请检查服务状态。")


class DialX:
    def __init__(self, config, pool):
        self.config = config
        self.pool = pool
        self.cache: dict[str, dict] = {}
        self.refresh_lock = asyncio.Lock()
        self.refresh_failures: dict[str, tuple[str, GatewayError]] = {}
        self.refresh_epoch = 0

    @staticmethod
    def _validate_models(data):
        if not isinstance(data, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("id"), str) or
            not isinstance(m.get("features", {}), dict) for m in data
        ):
            raise GatewayError(502, "invalid_models", "上游模型目录格式错误。")
        return data

    def failure(self, exc: Exception, account_id: str, model_id: str | None = None,
                *, generation: str | None = None) -> GatewayError:
        error = transport_error(exc)
        error.account_id = account_id
        specific = model_id if error.code in {"upstream_forbidden", "upstream_rate_limited", "upstream_rejected"} else None
        if generation is not None and error.status in {429, 502, 504}:
            self.pool.mark_failure(account_id, error.code, error.message,
                                   generation=generation, retry_after=error.retry_after, model_id=specific)
        return error

    async def refresh(self, force=False):
        observed_epoch = self.refresh_epoch
        async with self.refresh_lock:
            force = force and observed_epoch == self.refresh_epoch
            enabled = self.pool.enabled_ids()
            for key in list(self.cache):
                if key not in enabled or not self.pool.is_current(key, self.cache[key]["generation"]):
                    del self.cache[key]
            for key, (generation, _) in list(self.refresh_failures.items()):
                if key not in enabled or not self.pool.is_current(key, generation):
                    del self.refresh_failures[key]
            generations = {record["id"]: record["generation"] for record in self.config.accounts()}
            attempted = False
            for account_id in sorted(enabled):
                cached = self.cache.get(account_id)
                if not force and cached and time.monotonic() - cached["time"] < self.config.settings.model_cache_ttl:
                    continue
                generation = None
                try:
                    attempted = True
                    async with self.pool.lease(account_id=account_id) as lease:
                        generation = lease.generation
                        models = self._validate_models(await get_json(lease.client, "/api/models"))
                        tools, tool_error = [], None
                        try:
                            tool_response = await get_json(lease.client, "/api/toolsets-listing")
                            if not isinstance(tool_response, dict) or not isinstance(tool_response.get("data"), list):
                                raise GatewayError(502, "invalid_tools", "上游工具目录格式错误。")
                            tools = [t for t in tool_response["data"] if isinstance(t, dict) and isinstance(t.get("id"), str)]
                        except (GatewayError, httpx.HTTPError) as exc:
                            tool_error = transport_error(exc).message
                    # Do not re-add cache entries for accounts deleted during the fetch.
                    if account_id in self.pool.enabled_ids() and self.pool.is_current(account_id, generation):
                        self.cache[account_id] = {"models": models, "tools": tools, "tool_error": tool_error,
                                                  "time": time.monotonic(), "updated_at": utcnow(), "generation": generation}
                        self.refresh_failures.pop(account_id, None)
                    self.pool.mark_success(account_id, generation=generation)
                except (GatewayError, httpx.HTTPError) as exc:
                    error = self.failure(exc, account_id, generation=generation)
                    observed = generation or generations.get(account_id)
                    if observed and self.pool.is_current(account_id, observed):
                        if generation is not None or account_id not in self.refresh_failures:
                            self.refresh_failures[account_id] = (observed, GatewayError(
                                error.status, error.code, error.message, account_id=account_id, retry_after=error.retry_after))
            if attempted:
                self.refresh_epoch += 1
        return self.model_view()

    def _active_entries(self):
        enabled = self.pool.enabled_ids()
        return [entry for account, entry in self.cache.items()
                if account in enabled and self.pool.is_current(account, entry["generation"])]

    def _current_failures(self):
        enabled = self.pool.enabled_ids()
        return [error for account, (generation, error) in self.refresh_failures.items()
                if account in enabled and self.pool.is_current(account, generation)]

    @staticmethod
    def _chat_model(model):
        return model.get("type") == "model" and model.get("features", {}).get("chat_completion") is True

    def model_view(self):
        entries = self._active_entries()
        failures = self._current_failures()
        all_models = {m["id"]: m for entry in entries for m in entry["models"]}
        chat_models = {m["id"]: m for entry in entries for m in entry["models"] if self._chat_model(m)}
        models = list(chat_models.values())
        models.sort(key=lambda m: m["id"])
        default = self.config.settings.default_model
        if not default and models:
            default = next((m["id"] for m in models if m.get("isDefault")), models[0]["id"])
        return {"data": models, "default_model": default or None,
                "updated_at": max((e["updated_at"] for e in entries), default=None),
                "stale": bool(entries) and (bool(failures) or any(time.monotonic() - e["time"] >= self.config.settings.model_cache_ttl for e in entries)),
                "error": "；".join(f"{e.account_id}: {e.message}" for e in failures) or None, "total_count": len(all_models),
                "applications_count": sum(m.get("type") == "application" for m in all_models.values())}

    def tools_view(self):
        entries = self._active_entries()
        failures = self._current_failures()
        tools = {t["id"]: t for entry in entries for t in entry["tools"]}
        return {"data": [{"id": t["id"], "name": t.get("display_name", t["id"]),
                          "description": t.get("description", ""), "enabled": False, "available": False,
                          "reason": "原生模型的工具绑定尚未验证，不能开启。"} for t in tools.values()],
                "updated_at": max((e["updated_at"] for e in entries), default=None),
                "error": "；".join(f"{e.account_id}: {e.message}" for e in failures)
                or next((e["tool_error"] for e in entries if e["tool_error"]), None)}

    async def require_models(self):
        view = await self.refresh()
        if view["data"]:
            return view
        failures = self._current_failures()
        if failures:
            raise GatewayError(503, "models_unavailable", "模型目录不可用：" + failures[0].message,
                               account_id=failures[0].account_id)
        # The pool supplies distinct no-account / disabled / cooldown / busy errors.
        async with self.pool.lease():
            raise GatewayError(503, "models_unavailable", "没有可用模型目录，请检查账号或刷新模型。")

    async def check_account(self, account_id: str, requested_model: str | None = None):
        generation = None
        result = {"id": account_id, "checked_at": utcnow(), "check_status": "error",
                  "session_expires": None, "limits": None, "model": requested_model,
                  "balance": None, "subscription": None, "trial_expires": None, "error": None}
        try:
            async with self.pool.lease(account_id=account_id, for_check=True) as lease:
                generation = lease.generation
                session = await get_json(lease.client, "/api/auth/session")
                if not isinstance(session, dict) or not session.get("user"):
                    raise GatewayError(502, "invalid_cookie", "Cookie 已失效，请重新登录 DialX 并复制完整 Cookie。")
                result["check_status"] = "valid"
                result["session_expires"] = session.get("expires")
                models = self._validate_models(await get_json(lease.client, "/api/models"))
                available = [m for m in models if self._chat_model(m)]
                model = requested_model or self.config.settings.default_model
                if not model and available:
                    model = next((m["id"] for m in available if m.get("isDefault")), available[0]["id"])
                result["model"] = model
                if model and any(m["id"] == model for m in available):
                    try:
                        limits = await get_json(lease.client, f"/api/deployments/{quote(model, safe='')}/limits")
                        if not isinstance(limits, dict):
                            raise GatewayError(502, "invalid_limits", "上游配额格式错误。")
                        result["limits"] = limits
                    except (GatewayError, httpx.HTTPError) as exc:
                        result["error"] = transport_error(exc).message
                else:
                    result["error"] = "会话有效，但没有所选模型的配额信息。"
            self.pool.mark_success(account_id, generation=generation)
        except (GatewayError, httpx.HTTPError) as exc:
            error = self.failure(exc, account_id, requested_model, generation=generation)
            result["check_status"] = "invalid" if error.code == "invalid_cookie" else "error"
            result["error"] = error.message
            if generation is not None:
                self.pool.record_check(account_id, result, generation=generation)
            raise error from None
        self.pool.record_check(account_id, result, generation=generation)
        return result

    @asynccontextmanager
    async def completion(self, request: dict, on_selected=None):
        view = await self.require_models()
        model_id = request.get("model") or view["default_model"]
        if not any(m["id"] == model_id for m in view["data"]):
            raise GatewayError(404, "model_not_found", "所选模型不在当前账号的模型目录中。")
        eligible = {account for account, entry in self.cache.items()
                    if self.pool.is_current(account, entry["generation"])
                    and any(m["id"] == model_id and self._chat_model(m) for m in entry["models"])}
        async with self.pool.lease(eligible_ids=eligible, model_id=model_id) as lease:
            entry = self.cache.get(lease.account_id)
            if entry is None or entry["generation"] != lease.generation:
                raise GatewayError(503, "catalog_changed", "账号已重新导入，请刷新模型后重试。", account_id=lease.account_id)
            model = next((m for m in entry["models"] if m["id"] == model_id and self._chat_model(m)), None)
            if model is None:
                raise GatewayError(503, "model_unavailable", "账号的模型目录已变化，请刷新模型后重试。", account_id=lease.account_id)
            if on_selected is not None:
                await on_selected(lease.account_id, model_id)
            body = {"model": model, "messages": request["messages"],
                    "id": f"conversations/local/{model_id}__gateway-{uuid.uuid4().hex}",
                    "reference": uuid.uuid4().hex[:21], "prompt": "",
                    "temperature": request.get("temperature", 1)}
            try:
                async with lease.client.stream("POST", "/api/chat", json=body) as response:
                    check_response(response)
                    if "application/octet-stream" not in response.headers.get("content-type", "").lower():
                        raise GatewayError(502, "unexpected_stream", "上游没有返回已验证的 NUL-JSON 字节流。")
                    stream = frames(response.aiter_bytes(), max_frame_bytes=self.config.settings.max_frame_bytes,
                                    max_response_bytes=self.config.settings.max_response_bytes)
                    first = await anext(stream)
                    channels = Channels()
                    initial = channels.feed(first)
                    yield {"account_id": lease.account_id, "model": model_id, "stream": stream,
                           "first": first, "initial": initial, "channels": channels}
                self.pool.mark_success(lease.account_id, model_id=model_id, generation=lease.generation)
            except (GatewayError, httpx.HTTPError) as exc:
                raise self.failure(exc, lease.account_id, model_id, generation=lease.generation) from None

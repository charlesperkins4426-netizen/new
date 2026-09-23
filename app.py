from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.accounts import AccountPool
from gateway.config import ConfigStore
from gateway.errors import GatewayError
from gateway.logs import RequestLogs, utcnow
from gateway.protocol import sse
from gateway.service import DialX, transport_error

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("dialx_gateway")


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Message(StrictBody):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class ChatRequest(StrictBody):
    model: str | None = Field(default=None, min_length=1, max_length=512)
    messages: list[Message] = Field(min_length=1, max_length=100)
    stream: bool = False
    temperature: float = Field(default=1, ge=0, le=2)


class AddAccounts(StrictBody):
    cookies: str = Field(min_length=1, max_length=1_000_000)
    enabled: bool = True


class IdList(StrictBody):
    ids: list[str] = Field(min_length=1, max_length=1000)


class EnableAccount(StrictBody):
    enabled: bool


class CheckAccount(StrictBody):
    model: str | None = Field(default=None, max_length=512)


class UpdateSettings(StrictBody):
    default_model: str = Field(min_length=1, max_length=512)


class ToolSettings(StrictBody):
    enabled_ids: list[str] = Field(max_length=100)


class DeleteLogs(StrictBody):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    all: bool = False


async def read_chat(request: Request) -> ChatRequest:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise GatewayError(415, "json_required", "请使用 Content-Type: application/json。")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise GatewayError(413, "request_too_large", "请求超过 1 MiB 限制。")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise GatewayError(400, "invalid_json", "请求不是有效的 JSON。") from None
    try:
        return ChatRequest.model_validate(value)
    except ValidationError:
        raise GatewayError(400, "unsupported_request", "仅支持文本 messages、model、stream 和 temperature；请检查参数。") from None


def create_app(env_path: str | Path | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
    config = ConfigStore(Path(env_path or os.environ.get("ENV_FILE", ROOT / ".env")))
    pool = AccountPool(config, transport=transport)
    dialx = DialX(config, pool)
    logs = RequestLogs(config.settings.data_dir, config.secret_values, config.settings.log_retention_days)
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(application):
        await logs.initialize()
        logger.warning("管理后台没有鉴权；只应在本机或受信网络部署。")
        try:
            yield
        finally:
            await pool.close()

    application = FastAPI(title="DialX OpenAI 网关", version="1.0.0", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.state.config = config
    application.state.pool = pool
    application.state.dialx = dialx
    application.state.logs = logs

    @application.middleware("http")
    async def response_policy(request: Request, call_next):
        # JSON-only mutations stop cross-origin HTML forms. This is NOT admin authentication.
        if request.url.path.startswith("/api/admin/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse(GatewayError(415, "json_required", "管理操作必须使用 JSON。").payload(), status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @application.exception_handler(GatewayError)
    async def gateway_exception(request, exc):
        return JSONResponse(logs.redact(exc.payload()), status_code=exc.status, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def validation_exception(request, exc):
        return JSONResponse(GatewayError(400, "invalid_request", "请求参数格式错误或含未支持字段。").payload(), status_code=400)

    def authorize(request: Request):
        supplied = request.headers.get("authorization", "")
        scheme, _, key = supplied.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), config.settings.api_token.encode()):
            raise GatewayError(401, "invalid_api_key", "调用 Key 不正确，请使用 Authorization: Bearer <key>。")

    @application.get("/healthz")
    async def health():
        return {"status": "ok"}

    @application.get("/")
    async def index():
        return FileResponse(ROOT / "web" / "index.html")

    application.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @application.get("/api/admin/accounts")
    async def accounts():
        return {"data": [{**row, "balance": None, "subscription": None, "trial_expires": None,
                          "unsupported_reason": "上游未提供余额、订阅和试用接口。"} for row in pool.snapshot()]}

    @application.post("/api/admin/accounts")
    async def add_accounts(body: AddAccounts):
        return await pool.add_accounts(body.cookies, enabled=body.enabled)

    @application.post("/api/admin/accounts/delete")
    async def delete_accounts(body: IdList):
        deleted = await pool.delete_accounts(body.ids)
        return {"deleted": deleted}

    @application.patch("/api/admin/accounts/{account_id}")
    async def enable_account(account_id: str, body: EnableAccount):
        await pool.set_enabled(account_id, body.enabled)
        return {"ok": True}

    @application.post("/api/admin/accounts/{account_id}/check")
    async def check_account(account_id: str, body: CheckAccount):
        return await dialx.check_account(account_id, body.model)

    def public_models(view):
        return {**view, "data": [{key: model[key] for key in ("id", "name", "owner", "type", "features", "limits", "isDefault")
                                  if key in model} for model in view["data"]]}

    @application.get("/api/admin/models")
    async def models():
        return public_models(await dialx.refresh())

    @application.post("/api/admin/models/refresh")
    async def refresh_models():
        if not pool.enabled_ids():
            async with pool.lease():
                pass
        view = await dialx.refresh(force=True)
        if not view["data"]:
            await dialx.require_models()
        return public_models(view)

    @application.put("/api/admin/settings")
    async def settings(body: UpdateSettings):
        view = await dialx.require_models()
        if body.default_model not in {model["id"] for model in view["data"]}:
            raise GatewayError(404, "model_not_found", "默认模型必须来自真实模型目录。")
        await asyncio.to_thread(config.set_default_model, body.default_model)
        return {"default_model": body.default_model}

    @application.get("/api/admin/tools")
    async def tools():
        await dialx.refresh()
        return dialx.tools_view()

    @application.put("/api/admin/tools")
    async def update_tools(body: ToolSettings):
        if body.enabled_ids:
            raise GatewayError(400, "unverified_tools", "尚未验证原生模型的工具绑定，不能开启工具。")
        return {"enabled_ids": []}

    @application.get("/api/admin/logs")
    async def list_logs(limit: int = 20, offset: int = 0, status: str | None = None,
                        model: str | None = None, account_id: str | None = None):
        if not 1 <= limit <= 100 or offset < 0:
            raise GatewayError(400, "invalid_pagination", "每页数量应为 1–100，偏移量不能为负。")
        return await logs.listing(limit=limit, offset=offset, status=status, model=model, account_id=account_id)

    @application.get("/api/admin/logs/{request_id}")
    async def log_detail(request_id: str):
        return await logs.detail(request_id)

    @application.post("/api/admin/logs/delete")
    async def delete_logs(body: DeleteLogs):
        if body.all and body.ids:
            raise GatewayError(400, "ambiguous_delete", "请选择删除全部或指定记录，不要同时提交。")
        return {"deleted": await logs.delete(None if body.all else body.ids)}

    @application.get("/api/admin/overview")
    async def overview():
        account_rows = pool.snapshot()
        view = dialx.model_view()
        return {"service": {"status": "ok", "uptime_seconds": int(time.monotonic() - started), "version": "1.0.0"},
                "default_model": view["default_model"],
                "accounts": {"total": len(account_rows), "enabled": sum(a["enabled"] for a in account_rows),
                             "available": sum(a["status"] == "ready" for a in account_rows),
                             "cooling": sum(a["status"] == "cooling" for a in account_rows),
                             "busy": sum(a["busy"] for a in account_rows)},
                "models": {"count": len(view["data"]), **{k: view[k] for k in ("total_count", "applications_count", "updated_at", "stale", "error")}},
                "requests": await logs.summary(),
                "settings": {"api_auth": True, "admin_auth": False,
                             "default_key_in_use": config.settings.api_token == "123456",
                             "log_retention_days": config.settings.log_retention_days},
                "recent_requests": (await logs.listing(limit=5))["data"]}

    @application.get("/v1/models")
    async def openai_models(request: Request):
        authorize(request)
        view = await dialx.require_models()
        return {"object": "list", "data": [{"id": model["id"], "object": "model", "created": 0,
                                          "owned_by": model.get("owner", "dialx")} for model in view["data"]]}

    @application.post("/v1/chat/completions")
    async def chat(request: Request):
        request_id = "chatcmpl-" + uuid.uuid4().hex
        began, created = time.monotonic(), int(time.time())
        started_at = utcnow()
        content: list[str] = []
        reasoning: list[str] = []
        ttft: float | None = None
        logged = False
        finished = False
        upstream = None
        upstream_entered = False
        request_secrets = config.secret_values()

        async def finish(status="success", error: GatewayError | None = None):
            nonlocal finished
            if not logged or finished:
                return
            await logs.finish(request_id, status=status, ttft_ms=ttft,
                              duration_ms=round((time.monotonic() - began) * 1000, 2),
                              content="".join(content), reasoning_content="".join(reasoning),
                              error=error.message if error else None, code=error.code if error else None,
                              extra_secrets=request_secrets)
            finished = True

        try:
            authorize(request)
            body = await read_chat(request)
            data = body.model_dump(exclude_none=True)
            await logs.start(request_id, started_at, body.model, data["messages"])
            logged = True
            upstream = dialx.completion(data)
            context = await upstream.__aenter__()
            upstream_entered = True
            ttft = round((time.monotonic() - began) * 1000, 2)
            await logs.bind(request_id, context["account_id"], context["model"])
        except asyncio.CancelledError:
            if upstream_entered:
                await asyncio.shield(upstream.__aexit__(None, None, None))
            await asyncio.shield(finish("cancelled", GatewayError(499, "client_cancelled", "客户端停止接收回答。")))
            raise
        except Exception as exc:
            error = transport_error(exc)
            if upstream_entered:
                try:
                    await upstream.__aexit__(type(error), error, error.__traceback__)
                except GatewayError as closing_error:
                    error = closing_error
            if not logged:
                await logs.start(request_id, started_at, None, [])
                logged = True
            if error.account_id:
                await logs.bind(request_id, error.account_id, data.get("model", "") if "data" in locals() else "")
            await finish("failed", error)
            raise error from None

        def chunk(delta=None, stop=False):
            return {"id": request_id, "object": "chat.completion.chunk", "created": created,
                    "model": context["model"], "choices": [{"index": 0, "delta": delta or {},
                                                               "finish_reason": "stop" if stop else None}]}

        def collect(events):
            for kind, text in events:
                (reasoning if kind == "reasoning_content" else content).append(text)
            return events

        async def events():
            for kind, text in collect(context["initial"]):
                yield kind, text
            if context["first"]:
                async for frame in context["stream"]:
                    for kind, text in collect(context["channels"].feed(frame)):
                        yield kind, text

        async def close_upstream(error=None):
            nonlocal upstream
            if upstream is not None:
                active, upstream = upstream, None
                if error is None:
                    await active.__aexit__(None, None, None)
                else:
                    await active.__aexit__(type(error), error, error.__traceback__)

        async def streaming():
            try:
                yield sse(chunk({"role": "assistant"}))
                async for kind, text in events():
                    yield sse(chunk({kind: text}))
                await close_upstream()
                await finish()
                yield sse(chunk(stop=True))
                yield sse("[DONE]")
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(close_upstream())
                    await asyncio.shield(finish("cancelled", GatewayError(499, "client_cancelled", "客户端停止接收回答。")))
                finally:
                    raise
            except Exception as exc:
                error = transport_error(exc)
                error.account_id = context["account_id"]
                try:
                    await close_upstream(error)
                except GatewayError as closing_error:
                    error = closing_error
                await finish("failed", error)
                yield sse(logs.redact(error.payload()))
            finally:
                if upstream is not None:
                    await asyncio.shield(close_upstream())
                if not finished:
                    await asyncio.shield(finish("interrupted", GatewayError(502, "stream_interrupted", "回答流未正常结束。")))

        if body.stream:
            return StreamingResponse(streaming(), media_type="text/event-stream",
                                     headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})
        try:
            async for _ in events():
                pass
            await close_upstream()
            await finish()
        except asyncio.CancelledError:
            await asyncio.shield(close_upstream())
            await asyncio.shield(finish("cancelled", GatewayError(499, "client_cancelled", "客户端停止接收回答。")))
            raise
        except Exception as exc:
            error = transport_error(exc)
            error.account_id = context["account_id"]
            try:
                await close_upstream(error)
            except GatewayError as closing_error:
                error = closing_error
            await finish("failed", error)
            raise error from None
        return {"id": request_id, "object": "chat.completion", "created": created, "model": context["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(content),
                                                       "reasoning_content": "".join(reasoning)}, "finish_reason": "stop"}],
                "usage": None}

    return application


if __name__ == "__main__":
    import uvicorn

    server = create_app()
    settings = server.state.config.settings
    uvicorn.run(server, host=settings.host, port=settings.port, workers=1, access_log=False)

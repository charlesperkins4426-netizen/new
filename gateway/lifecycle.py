"""Cancellation helpers for work done before response streaming starts."""

from __future__ import annotations

import asyncio

from .errors import GatewayError


async def settle(task: asyncio.Task):
    """Resolve an uncancellable side effect before propagating caller cancellation.

    Cancelling a to_thread await does not stop the database thread. Its owner must
    know whether the write completed before deciding how to finalize the record.
    """
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


async def while_connected(request, operation):
    """Run after the request body is consumed; cancel work on http.disconnect.

    StreamingResponse owns disconnect monitoring after response headers. This
    helper covers preparation (including first-frame wait) and non-streaming.
    It never runs alongside StreamingResponse's receive loop.
    """
    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise GatewayError(499, "client_cancelled", "客户端已断开，网关停止接收上游回答。")
        return await work
    finally:
        if not work.done():
            work.cancel()
        watcher.cancel()
        cleanup = asyncio.ensure_future(asyncio.gather(work, watcher, return_exceptions=True))
        await settle(cleanup)

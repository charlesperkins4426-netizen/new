"""The observed DialX protocol: UTF-8 JSON objects separated by NUL bytes.

This is deliberately not an SSE parser.  See docs/protocol.md for the capture.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator

from .errors import GatewayError


async def frames(
    chunks: AsyncIterable[bytes], *, max_frame_bytes: int, max_response_bytes: int
) -> AsyncIterator[dict]:
    buffer = bytearray()
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_response_bytes:
            raise GatewayError(502, "response_too_large", "上游回答超过配置的大小限制。")
        buffer.extend(chunk)
        while (end := buffer.find(b"\x00")) >= 0:
            if end > max_frame_bytes:
                raise GatewayError(502, "frame_too_large", "上游事件超过配置的大小限制。")
            raw = bytes(buffer[:end])
            del buffer[: end + 1]
            if not raw:
                raise GatewayError(502, "invalid_frame", "上游返回了空字节帧。")
            try:
                frame = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise GatewayError(502, "invalid_frame", "上游流包含无效的 UTF-8 或 JSON。") from None
            if not isinstance(frame, dict):
                raise GatewayError(502, "invalid_frame", "上游事件必须是 JSON 对象。")
            if not frame and buffer.strip():
                raise GatewayError(502, "invalid_terminator", "上游结束帧之后仍有数据。")
            yield frame
            if not frame:
                return
        if len(buffer) > max_frame_bytes:
            raise GatewayError(502, "frame_too_large", "上游事件超过配置的大小限制。")
    raise GatewayError(502, "incomplete_stream", "上游连接提前结束，没有完整结束帧。")


class Channels:
    """Keep stage names by index; every content value is an append-only delta."""

    def __init__(self) -> None:
        self.stage_names: dict[int, str] = {}

    def feed(self, frame: dict) -> list[tuple[str, str]]:
        if "error" in frame:
            # Raw upstream errors can contain session identifiers or request data.
            raise GatewayError(502, "upstream_stream_error", "上游在生成过程中返回错误。")
        output: list[tuple[str, str]] = []
        custom = frame.get("custom_content")
        if custom is not None:
            if not isinstance(custom, dict):
                raise GatewayError(502, "invalid_frame", "上游 custom_content 格式错误。")
            stages = custom.get("stages", [])
            if not isinstance(stages, list):
                raise GatewayError(502, "invalid_frame", "上游阶段列表格式错误。")
            for stage in stages:
                if not isinstance(stage, dict) or type(stage.get("index")) is not int:
                    raise GatewayError(502, "invalid_frame", "上游阶段编号格式错误。")
                index = stage["index"]
                if "name" in stage:
                    if not isinstance(stage["name"], str):
                        raise GatewayError(502, "invalid_frame", "上游阶段名称格式错误。")
                    self.stage_names[index] = stage["name"].casefold()
                text = stage.get("content")
                if text is not None:
                    if not isinstance(text, str):
                        raise GatewayError(502, "invalid_frame", "上游阶段内容不是文本。")
                    if index not in self.stage_names:
                        raise GatewayError(502, "unknown_stage", "上游阶段缺少名称，无法安全区分推理。")
                    if self.stage_names[index] in {"thinking", "reasoning"} and text:
                        output.append(("reasoning_content", text))
            # custom_content.state is a final snapshot, never a text delta.
        if "content" in frame:
            if not isinstance(frame["content"], str):
                raise GatewayError(502, "invalid_frame", "上游正文不是文本。")
            if frame["content"]:
                output.append(("content", frame["content"]))
        return output


def sse(data: dict | str) -> bytes:
    value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"data: {value}\n\n".encode("utf-8")

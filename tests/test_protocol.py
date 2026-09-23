import json
from pathlib import Path

import pytest

from gateway.errors import GatewayError
from gateway.protocol import Channels, frames, sse

FIXTURES = Path(__file__).parent / "fixtures"


def encode(values):
    return b"\0".join(json.dumps(value, ensure_ascii=False).encode() for value in values) + b"\0"


async def chunks(values):
    for value in values:
        yield value


async def parse(parts, **limits):
    channels = Channels()
    result = {"content": "", "reasoning_content": ""}
    async for frame in frames(chunks(parts), max_frame_bytes=limits.get("frame", 100_000),
                              max_response_bytes=limits.get("response", 1_000_000)):
        for kind, text in channels.feed(frame):
            result[kind] += text
    return result


@pytest.mark.parametrize("name", ["claude", "gemini-thinking"])
async def test_real_capture_all_split_points_and_single_bytes(name):
    values = json.loads((FIXTURES / f"{name}.json").read_text())
    body = encode(values)
    expected = await parse([body])
    assert expected["content"] == ("17×19 = 323。" if name == "claude" else "54")
    assert bool(expected["reasoning_content"]) == (name == "gemini-thinking")
    for index in range(len(body) + 1):
        assert await parse([body[:index], body[index:]]) == expected
    assert await parse([bytes([byte]) for byte in body]) == expected


async def test_stage_completion_does_not_end_body_or_deduplicate_reasoning():
    values = [
        {"custom_content": {"stages": [{"index": 0, "name": "Thinking"}]}},
        {"custom_content": {"stages": [{"index": 0, "content": "思考"}]}},
        {"custom_content": {"stages": [{"index": 0, "content": "思考继续"}]}},
        {"content": "正文"},
        {"custom_content": {"stages": [{"index": 0, "status": "completed"}]}},
        {"content": ""}, {"content": "末尾🙂"},
        {"custom_content": {"state": {"claude_message_content": [{"text": "不得重复"}]}}}, {},
    ]
    assert await parse([encode(values)]) == {"reasoning_content": "思考思考继续", "content": "正文末尾🙂"}


@pytest.mark.parametrize("body,code", [
    (b'', 'incomplete_stream'), (b'{"content":"x"}\0', 'incomplete_stream'),
    (b'{}', 'incomplete_stream'), (b'{bad}\0', 'invalid_frame'),
    (b'{"content":"\xff"}\0', 'invalid_frame'), (b'[]\0', 'invalid_frame'),
    (b'\0', 'invalid_frame'), (b'{}\0{}\0', 'invalid_terminator'),
    (encode([{"content": ["wrong"]}, {}]), 'invalid_frame'),
    (encode([{"error": "secret-cookie"}]), 'upstream_stream_error'),
    (encode([{"custom_content": {"stages": [{"index": 0, "content": "unknown"}]}}, {}]), 'unknown_stage'),
])
async def test_bad_streams_never_succeed(body, code):
    with pytest.raises(GatewayError) as caught:
        await parse([body])
    assert caught.value.code == code
    assert "secret-cookie" not in str(caught.value)


async def test_size_limits():
    with pytest.raises(GatewayError, match="事件"):
        await parse([b'12345'], frame=4)
    with pytest.raises(GatewayError, match="回答"):
        await parse([encode([{"content": "long"}, {}])], response=4)


def test_sse_encodes_one_event_not_literal_newlines():
    assert sse({"content": "中文\n正文"}).startswith('data: {"content":"中文\\n正文"}'.encode())
    assert sse("[DONE]") == b"data: [DONE]\n\n"

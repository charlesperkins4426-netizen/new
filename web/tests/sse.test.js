import test from 'node:test';
import assert from 'node:assert/strict';
import { SSEParser, readCompletionStream } from '../sse.js';

const encode = new TextEncoder();
const event = (delta, finish = null) => `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
const ending = event({}, 'stop') + 'data: [DONE]\n\n';
function stream(bytes, widths = [1]) {
  let offset = 0;
  let index = 0;
  return new ReadableStream({ pull(controller) {
    if (offset === bytes.length) { controller.close(); return; }
    const end = Math.min(bytes.length, offset + widths[index++ % widths.length]);
    controller.enqueue(bytes.slice(offset, end)); offset = end;
  } });
}
async function parse(text, widths) {
  const output = { content: '', reasoning_content: '' };
  await readCompletionStream(stream(encode.encode(text), widths), (field, delta) => { output[field] += delta; });
  return output;
}

test('Unicode survives every byte boundary and reasoning does not terminate content', async () => {
  const source = event({ role: 'assistant' }) + event({ reasoning_content: '先思考中文𠮷' }) + event({ status: 'completed' }) +
    event({ reasoning_content: '重复重复' }) + event({ content: '正文依然接收' }) + event({ content: '。' }) + ending;
  assert.deepEqual(await parse(source), { reasoning_content: '先思考中文𠮷重复重复', content: '正文依然接收。' });
  assert.deepEqual(await parse(source, [3, 19, 1, 99]), await parse(source, [65536]));
});

test('SSE multiline data, CRLF split, comments and ignored fields', () => {
  const result = [];
  const source = ': heartbeat\r\nid: 1\r\nevent: message\r\ndata: {"first":1,\r\ndata: "second":2}\r\n\r\n';
  const parser = new SSEParser((part) => result.push(part));
  for (const char of source) parser.feed(char);
  parser.finish();
  assert.deepEqual(result, [{ event: 'message', data: '{"first":1,\n"second":2}' }]);
});

test('multiline OpenAI JSON and CR-only line endings parse', async () => {
  const data = 'data: {"choices":[\rdata: {"index":0,"delta":{"content":"正常"},"finish_reason":null}]}\r\r';
  assert.equal((await parse(data + ending.replaceAll('\n', '\r'))).content, '正常');
});

test('every split position preserves a complete answer', async () => {
  const bytes = encode.encode(event({ content: '中文' }) + ending);
  for (let split = 1; split < bytes.length; split += 1) {
    let content = '';
    const body = new ReadableStream({ start(controller) { controller.enqueue(bytes.slice(0, split)); controller.enqueue(bytes.slice(split)); controller.close(); } });
    await readCompletionStream(body, (_, delta) => { content += delta; });
    assert.equal(content, '中文');
  }
});

test('bare EOF and missing DONE never become success', async () => {
  await assert.rejects(parse(event({ content: '部分' })), /未收到 \[DONE\]/);
  await assert.rejects(parse(event({}, 'stop')), /未收到 \[DONE\]/);
  await assert.rejects(parse('data: [DONE]\n\n'), /缺少完整结束状态/);
  await assert.rejects(parse('data: {"choices":'), /不完整事件/);
});

test('server error preserves partial output, cancels and releases reader', async () => {
  let cancelled = false;
  const body = new ReadableStream({ start(controller) {
    controller.enqueue(encode.encode(event({ content: '部分正文' }) + 'data: {"error":{"message":"上游超时"}}\n\n'));
  }, cancel() { cancelled = true; } });
  let content = '';
  await assert.rejects(readCompletionStream(body, (_, value) => { content += value; }), /上游超时/);
  assert.equal(content, '部分正文');
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

test('malformed JSON, invalid UTF-8, oversized events and abnormal finish reject', async () => {
  await assert.rejects(parse('data: {oops}\n\n'), /有效 JSON/);
  await assert.rejects(readCompletionStream(stream(new Uint8Array([0xff])), () => {}), /无效 UTF-8/);
  const parser = new SSEParser(() => {}, 10);
  assert.throws(() => parser.feed('data: 12345678901\n'), /过大/);
  await assert.rejects(parse(event({}, 'length') + 'data: [DONE]\n\n'), /未正常结束/);
});

test('no content is required for a verified terminal event', async () => {
  assert.deepEqual(await parse(event({ role: 'assistant', content: '' }) + ending), { content: '', reasoning_content: '' });
});

test('transport cancellation remains an error rather than completed', async () => {
  const body = new ReadableStream({ start(controller) { controller.error(new DOMException('已停止', 'AbortError')); } });
  await assert.rejects(readCompletionStream(body, () => {}), { name: 'AbortError' });
  assert.equal(body.locked, false);
});

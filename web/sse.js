// A transport chunk is not an SSE event. Preserve lines across reads, including CRLF splits.
export class SSEParser {
  constructor(onEvent, maxEventChars = 2097152) {
    this.onEvent = onEvent;
    this.maxEventChars = maxEventChars;
    this.buffer = '';
    this.data = [];
    this.event = '';
    this.size = 0;
  }

  feed(text, final = false) {
    this.buffer += text;
    let start = 0;
    for (let index = 0; index < this.buffer.length; index += 1) {
      const char = this.buffer[index];
      if (char !== '\n' && char !== '\r') continue;
      if (char === '\r' && index === this.buffer.length - 1 && !final) break;
      this.line(this.buffer.slice(start, index));
      if (char === '\r' && this.buffer[index + 1] === '\n') index += 1;
      start = index + 1;
    }
    this.buffer = this.buffer.slice(start);
    if (this.buffer.length + this.size > this.maxEventChars) throw new Error('响应事件过大，已停止接收。');
  }

  line(line) {
    if (!line) {
      if (this.data.length) this.onEvent({ data: this.data.join('\n'), event: this.event || 'message' });
      this.data = [];
      this.event = '';
      this.size = 0;
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') { this.data.push(value); this.size += value.length; }
    else if (field === 'event') this.event = value;
    if (this.size > this.maxEventChars) throw new Error('响应事件过大，已停止接收。');
  }

  finish() {
    this.feed('', true);
    if (this.buffer || this.data.length) throw new Error('响应流意外结束，收到不完整事件；已保留部分输出。');
  }
}

export async function readCompletionStream(stream, onDelta) {
  if (!stream) throw new Error('浏览器未收到响应流。');
  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const decode = (bytes, options) => {
    try { return decoder.decode(bytes, options); }
    catch { throw new Error('响应包含无效 UTF-8 文本，已保留部分输出。'); }
  };
  let done = false;
  let finished = false;
  const parser = new SSEParser(({ data }) => {
    if (done || !data.trim()) return;
    if (data.trim() === '[DONE]') {
      if (!finished) throw new Error('响应缺少完整结束状态，不能确认回答完成。');
      done = true;
      return;
    }
    let payload;
    try { payload = JSON.parse(data); }
    catch { throw new Error('响应事件不是有效 JSON，已保留部分输出。'); }
    if (!payload || typeof payload !== 'object') throw new Error('响应事件格式不正确。');
    if (payload.error) throw new Error(payload.error.message || '响应中途发生错误，已保留部分输出。');
    if (!Array.isArray(payload.choices)) throw new Error('响应事件格式不正确。');
    const choice = payload.choices.find((item) => item && item.index === 0);
    if (!choice) return;
    const delta = choice.delta || {};
    if (typeof delta.reasoning_content === 'string') onDelta('reasoning_content', delta.reasoning_content);
    if (typeof delta.content === 'string') onDelta('content', delta.content);
    if (choice.finish_reason !== null && choice.finish_reason !== undefined) {
      if (choice.finish_reason !== 'stop') throw new Error('回答未正常结束，已保留部分输出。');
      finished = true;
    }
  });
  try {
    while (!done) {
      const part = await reader.read();
      if (part.done) {
        parser.feed(decode());
        parser.finish();
        if (!done) throw new Error('响应流意外断开，未收到 [DONE]；已保留部分输出。');
        break;
      }
      parser.feed(decode(part.value, { stream: true }));
    }
  } finally {
    try { await reader.cancel(); } catch { /* An aborted fetch may already be closed. */ }
    reader.releaseLock();
  }
}

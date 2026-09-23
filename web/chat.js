import { api, responseError } from './api.js';
import { readCompletionStream } from './sse.js';
import { confirmAction } from './dialogs.js';
import { el, button, badge, muted, notice, pageHead, card, empty, field, pairs, privacyNotice, notify } from './dom.js';

// Deliberately page-memory only. Reloading starts again at the overview.
const conversation = { messages: [], key: '123456', model: '', draft: '' };

export async function renderChat(root, { signal, navigate }) {
  let active = null;
  const messages = conversation.messages;
  const model = el('select', { 'aria-label': '聊天模型', disabled: true }, el('option', { value: '' }, '正在获取模型…'));
  const thread = el('div', { class: 'chat-thread', 'aria-label': '当前对话', 'aria-busy': 'false' });
  const key = el('input', { type: 'password', value: conversation.key, autocomplete: 'off', spellcheck: false, autocapitalize: 'off', 'data-component-id': 'chat-request-key' });
  const settingsError = el('div', { class: 'message-error', role: 'alert' });
  key.addEventListener('input', () => { conversation.key = key.value; settingsError.textContent = ''; });
  const requestSettings = el('details', { class: 'request-settings' }, el('summary', {}, '请求设置 · 调用 Key'),
    field('调用 Key', key, '仅用于 /v1/* 请求，默认 123456。若修改了 .env 中的 API_TOKEN，在此输入匹配值；仅存在本页内存，不影响后台访问。'), settingsError);
  const draft = el('textarea', { rows: 3, value: conversation.draft, placeholder: '输入消息，开始对话…', 'aria-label': '消息内容', autocomplete: 'off', spellcheck: false });
  draft.addEventListener('input', () => { conversation.draft = draft.value; syncControls(); });
  draft.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!send.disabled) form.requestSubmit(); }
  });
  const send = el('button', { type: 'submit', class: 'btn primary', disabled: true }, '发送消息');
  const stop = button('停止生成', () => active?.abort(), 'danger', { hidden: true });
  const clear = button('清空对话', () => confirmAction({
    title: '清空当前对话？', description: '只清空浏览器中的对话和草稿，不删除服务器已记录的日志。', confirmText: '确认清空',
    action: async () => { messages.length = 0; conversation.draft = ''; draft.value = ''; },
    onSuccess: () => { drawHistory(); syncControls(); },
  }));
  const modelName = el('span', {}, '尚未选择');
  model.addEventListener('change', () => { conversation.model = model.value; modelName.textContent = model.selectedOptions[0]?.textContent || '尚未选择'; syncControls(); });
  const form = el('form', { class: 'composer', autocomplete: 'off', onsubmit: (event) => { event.preventDefault(); sendMessage(); } }, draft,
    el('div', { class: 'between' }, el('span', {}, '回车发送 · Shift + Enter 换行'), el('div', { class: 'row' }, stop, send)));
  const catalogStatus = el('div');
  const chatCard = el('section', { class: 'card chat-card' },
    el('div', { class: 'chat-toolbar' }, el('label', { for: 'chat-model' }, '模型'), model, badge('流式响应', 'info')), catalogStatus,
    requestSettings, thread, form, el('p', { class: 'chat-note' }, '停止只取消本次接收，不保证上游免于计费。已有输出保留，不伪装成完整回答。'));
  model.id = 'chat-model';
  const aside = el('aside', { class: 'stack chat-aside', 'aria-label': '对话说明' },
    card('本次对话', null, el('div', { class: 'card-pad stack' }, pairs([['模型', modelName], ['返回方式', '流式'], ['工具', '关闭'], ['账号选择', '由账号池轮询'], ['对话保存', '仅页面内存']]))),
    card('关于推理内容', null, el('div', { class: 'card-pad' }, muted('只有上游实际返回推理时才显示折叠区。推理阶段结束，不代表正文已经结束。'))),
    card('日志与耗时', null, el('div', { class: 'card-pad stack' }, muted('准确的首事件延迟（TTFT）和总耗时请查看服务端日志。TTFT 计至上游首个完整事件，元数据也计入，不是首个文字。'),
      button('查看请求日志 →', () => navigate('logs'), 'link'))));
  root.append(pageHead('在线聊天', '用真实接口验证模型；本页对话只存在于当前页面内存。', clear), privacyNotice(), el('div', { class: 'chat-layout' }, chatCard, aside));

  function syncControls() {
    send.disabled = Boolean(active) || !draft.value.trim() || !model.value;
    send.hidden = Boolean(active);
    stop.hidden = !active;
    clear.disabled = Boolean(active) || (!messages.length && !draft.value);
    model.disabled = Boolean(active) || !model.value;
    key.disabled = Boolean(active);
    draft.disabled = Boolean(active);
    thread.setAttribute('aria-busy', String(Boolean(active)));
  }

  function renderMessage(message) {
    const isAssistant = message.role === 'assistant';
    const status = badge('等待响应', 'info');
    status.setAttribute('role', 'status');
    const content = el('div', { class: isAssistant ? 'answer' : 'user-bubble' }, message.content);
    const reasoningText = el('div', { class: 'reason-text' }, message.reasoning_content || '');
    const reasoning = el('details', { class: 'reasoning', hidden: !message.reasoning_content }, el('summary', {}, '推理过程 · reasoning_content'), reasoningText);
    const error = el('p', { class: 'message-error' });
    const copy = button('复制正文', async () => {
      try { await navigator.clipboard.writeText(message.content); notify('正文已复制。'); }
      catch { notify('浏览器不允许复制，请选择正文后手动复制。', true); }
    }, 'link');
    const node = el('article', { class: 'message' }, el('div', { class: `message-avatar ${isAssistant ? 'assistant' : ''}`, 'aria-hidden': 'true' }, isAssistant ? '模' : '我'),
      el('div', { class: 'message-main' }, el('div', { class: 'message-meta' }, el('b', {}, isAssistant ? message.model : '你'), isAssistant ? status : null),
        isAssistant ? reasoning : null, content, isAssistant ? error : null, isAssistant ? el('div', { class: 'message-foot' }, copy) : null));
    const update = () => {
      const atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 100;
      content.textContent = message.content;
      reasoningText.textContent = message.reasoning_content || '';
      reasoning.hidden = !message.reasoning_content;
      const states = { waiting: ['等待响应', 'info'], receiving: ['正在生成', 'info'], success: ['已完成', 'success'], stopped: ['已中止', 'warning'], error: ['未完成', 'danger'] };
      const [label, tone] = states[message.state] || states.waiting;
      status.textContent = label;
      status.className = `badge ${tone}`;
      error.textContent = message.error || '';
      copy.disabled = !message.content;
      if (atBottom) thread.scrollTop = thread.scrollHeight;
    };
    update();
    return { node, update };
  }

  function drawHistory() {
    thread.replaceChildren(...(messages.length ? messages.map((message) => renderMessage(message).node)
      : [empty('开始一段新对话', '选择模型并输入消息。此处不预填演示内容，所有回答来自实际接口。')]));
    thread.scrollTop = thread.scrollHeight;
  }

  async function sendMessage() {
    const text = draft.value.trim();
    if (active || !text || !model.value) return;
    if (!conversation.key.trim()) {
      requestSettings.open = true;
      settingsError.textContent = '请输入与服务端 API_TOKEN 匹配的调用 Key。'; key.focus(); return;
    }
    const controller = new AbortController();
    active = controller;
    messages.push({ role: 'user', content: text });
    // Never include reasoning or incomplete assistant output in subsequent prompts.
    const history = messages.filter((item) => item.role !== 'assistant' || item.state === 'success')
      .map(({ role, content }) => ({ role, content }));
    const answer = { role: 'assistant', model: model.selectedOptions[0].textContent, content: '', reasoning_content: '', state: 'waiting', error: '' };
    messages.push(answer);
    conversation.draft = ''; draft.value = '';
    drawHistory();
    const live = renderMessage(answer);
    thread.lastChild.replaceWith(live.node);
    thread.scrollTop = thread.scrollHeight;
    syncControls();
    try {
      const response = await fetch('/v1/chat/completions', {
        method: 'POST', signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', Authorization: `Bearer ${conversation.key}` },
        body: JSON.stringify({ model: model.value, messages: history, stream: true }),
      });
      if (!response.ok) {
        const error = await responseError(response);
        if (response.status === 401) { requestSettings.open = true; settingsError.textContent = '调用 Key 不匹配，请修改请求设置后重试。后台访问不受影响。'; }
        throw error;
      }
      if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('服务没有返回预期的流式响应。');
      await readCompletionStream(response.body, (field, delta) => {
        answer[field] += delta;
        answer.state = 'receiving';
        live.update();
      });
      answer.state = 'success';
    } catch (error) {
      answer.state = controller.signal.aborted ? 'stopped' : 'error';
      answer.error = controller.signal.aborted ? '已停止接收；以上是部分输出，不代表完整回答。'
        : error instanceof TypeError ? '连接中断或响应无法读取，已保留部分输出，请确认服务状态。'
          : error.message || '请求失败，已保留部分输出。';
    } finally {
      active = null;
      live.update();
      syncControls();
      if (!signal.aborted) draft.focus();
    }
  }

  signal.addEventListener('abort', () => active?.abort(), { once: true });
  drawHistory();
  syncControls();
  async function loadModels() {
    try {
      const data = await api('models', { signal });
      if (signal.aborted) return;
      model.replaceChildren(...data.data.map((item) => el('option', { value: item.id }, item.name || item.id)));
      if (!data.data.length) {
        model.append(el('option', { value: '' }, '暂无可用模型'));
        catalogStatus.replaceChildren(notice('先配置账号并获取模型', '请前往账号池添加 Cookie，再到模型目录刷新缓存。'));
      } else {
        if (data.data.some((item) => item.id === conversation.model)) model.value = conversation.model;
        else if (data.default_model && data.data.some((item) => item.id === data.default_model)) model.value = data.default_model;
        conversation.model = model.value;
        modelName.textContent = model.selectedOptions[0]?.textContent;
        catalogStatus.replaceChildren(...(data.stale || data.error ? [notice('当前使用缓存目录', data.error || '模型缓存已过期，可到模型目录刷新。', 'warning')] : []));
      }
    } catch (error) {
      if (signal.aborted) return;
      model.replaceChildren(el('option', { value: '' }, '模型获取失败'));
      catalogStatus.replaceChildren(notice('模型目录获取失败', error.message, 'danger'), button('重试获取模型', loadModels));
    }
    syncControls();
  }
  await loadModels();
}

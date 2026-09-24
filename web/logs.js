import { api } from './api.js';
import { dialog, confirmAction } from './dialogs.js';
import { el, button, muted, date, duration, notice, pageHead, card, empty, loading, errorBox, pairs, table, field, statusBadge, privacyNotice, notify } from './dom.js';

function logDetail(id) {
  const controller = new AbortController();
  const view = dialog('请求详情', '输入、推理和正文独立展示', { drawer: true, onClose: () => controller.abort() });
  view.body.append(loading());
  view.footer.append(button('关闭', view.close));
  view.open();
  async function load() {
    try {
      const log = await api(`logs/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const roles = { system: '系统', user: '用户', assistant: '助手' };
      const input = el('section', { class: 'detail-section' }, el('h3', {}, '输入消息'),
        (log.messages || []).length ? log.messages.map((message) => el('div', { class: 'mt-sm' }, muted(roles[message.role] || '消息'), el('pre', { class: 'raw-data' }, message.content))) : muted('未记录输入内容。'));
      view.body.replaceChildren(el('div', { class: 'between mb' }, el('h3', {}, '一次模型调用'), statusBadge(log.status)),
        el('p', { class: 'mono muted mb' }, `请求编号：${log.id}`), pairs([['调用时间', date(log.started_at)], ['账号', el('code', {}, log.account_id || '—')],
          ['模型', log.model || '—'], ['首事件延迟（TTFT）', duration(log.ttft_ms)], ['总耗时', duration(log.duration_ms)]]), input,
        el('section', { class: 'detail-section' }, el('details', { class: 'reasoning' }, el('summary', {}, '推理内容 · reasoning_content'),
          el('div', { class: 'reason-text' }, log.reasoning_content || '未提供推理内容。'))),
        el('section', { class: 'detail-section' }, el('h3', {}, '最终正文 · content'), el('pre', { class: 'raw-data' }, log.content || '未收到正文。')),
        el('section', { class: 'detail-section' }, pairs([['错误摘要', log.error || '无'], ['错误代码', log.code || '—']])),
        el('div', { class: 'mt' }, notice('内容来自服务端请求日志', '关闭或刷新聊天页不会删除这些记录。TTFT 统计至上游首个完整事件，包含元数据事件。', 'warning')));
    } catch (error) { if (!controller.signal.aborted) view.body.replaceChildren(errorBox(error, load)); }
  }
  load();
}

export async function renderLogs(root, { signal }) {
  const limit = 20;
  let offset = 0;
  let total = 0;
  let rows = [];
  let requestId = 0;
  const selected = new Set();
  const contents = el('div', {}, loading());
  const status = el('select', {}, [['', '全部状态'], ['running', '进行中'], ['success', '成功'], ['failed', '失败'], ['cancelled', '已中止'], ['interrupted', '已中断']]
    .map(([value, label]) => el('option', { value }, label)));
  const model = el('input', { type: 'search', placeholder: '完整模型标识', autocomplete: 'off' });
  const account = el('input', { type: 'search', placeholder: '稳定账号标识', autocomplete: 'off' });
  let filters = { status: '', model: '', account_id: '' };
  const filterForm = el('form', { class: 'toolbar', onsubmit: (event) => {
    event.preventDefault(); offset = 0; selected.clear();
    filters = { status: status.value, model: model.value.trim(), account_id: account.value.trim() }; load();
  } }, field('账号', account), field('模型', model), field('状态', status),
  el('button', { type: 'submit', class: 'btn' }, '筛选'), button('重置', () => {
    status.value = ''; model.value = ''; account.value = ''; filters = { status: '', model: '', account_id: '' }; offset = 0; selected.clear(); load();
  }, 'link'));
  const refresh = button('刷新日志', () => load());
  const selectedCount = el('span', { class: 'selection-note' });
  const deleteSelected = button('删除所选', () => remove(false), 'danger', { disabled: true });
  const deleteAll = button('删除全部日志', () => remove(true), 'danger');
  const summary = el('span', { class: 'muted' });
  const panel = el('section', { class: 'card' }, filterForm,
    el('div', { class: 'toolbar' }, summary, el('span', { class: 'grow' }), selectedCount, deleteSelected, deleteAll), contents);
  root.append(pageHead('请求日志', '查看调用耗时、失败原因，以及服务端记录的请求与响应。', refresh), privacyNotice(), panel,
    el('div', { class: 'grid-two mt' },
      card('耗时如何理解', null, el('div', { class: 'card-pad' }, muted('首事件延迟（TTFT）从请求开始计至上游首个完整事件，元数据也计入，不是首个文字。未收到事件时显示「—」。总耗时包含等待与生成过程。'))),
      card('日志保护范围', null, el('div', { class: 'card-pad' }, muted('服务端过滤凭据字段，但消息本身仍可能敏感。后台无需鉴权，能访问后台的人也能查看日志；请勿直接暴露到不可信网络。')))));

  function remove(all) {
    if (!all && !selected.size) return;
    const ids = [...selected];
    confirmAction({
      title: all ? '删除全部服务端日志？' : `删除所选 ${ids.length} 条日志？`,
      description: all ? '将删除所有服务端请求日志，包括不符合当前筛选条件的记录。此操作不可撤销，不影响账号或上游会话。' : '将删除所选服务端请求、回复正文和推理记录。此操作不可撤销，不影响账号或上游会话。',
      confirmText: all ? '确认删除全部日志' : `确认删除 ${ids.length} 条`,
      content: all ? notice('范围是全部日志', '不是仅删除当前页或当前筛选结果。', 'warning') : el('ul', { class: 'selected-list' }, ids.map((id) => el('li', {}, el('code', {}, id)))),
      action: () => api('logs/delete', { method: 'POST', body: all ? { all: true } : { ids }, signal }),
      onSuccess: async (result) => { selected.clear(); offset = 0; notify(`已删除 ${result.deleted} 条日志。`); await load(); },
    });
  }

  function draw() {
    const all = el('input', { type: 'checkbox', 'aria-label': '全选当前页日志', disabled: !rows.length });
    const sync = () => {
      selectedCount.textContent = `已选择 ${selected.size} 条`;
      deleteSelected.disabled = !selected.size;
      all.checked = rows.length > 0 && rows.every((log) => selected.has(log.id));
      all.indeterminate = rows.some((log) => selected.has(log.id)) && !all.checked;
    };
    all.addEventListener('change', () => { rows.forEach((log) => all.checked ? selected.add(log.id) : selected.delete(log.id)); draw(); });
    const tableRows = rows.map((log) => {
      const check = el('input', { type: 'checkbox', checked: selected.has(log.id), 'aria-label': `选择日志 ${log.id}`, onchange: () => {
        check.checked ? selected.add(log.id) : selected.delete(log.id); sync();
      } });
      return el('tr', {}, el('td', {}, check), el('td', {}, date(log.started_at)), el('td', {}, el('code', {}, log.account_id || '—')),
        el('td', {}, log.model || '—'), el('td', {}, duration(log.ttft_ms)), el('td', {}, duration(log.duration_ms)),
        el('td', {}, statusBadge(log.status), log.code ? muted(log.code) : null), el('td', {}, button('详情', () => logDetail(log.id), 'link')));
    });
    summary.textContent = `符合筛选：${total} 条 · 时间按浏览器本地时区显示`;
    const footer = el('div', { class: 'table-footer' }, `显示 ${total ? offset + 1 : 0}–${offset + rows.length}，共 ${total} 条`,
      el('div', { class: 'row' }, button('上一页', () => { offset = Math.max(0, offset - limit); selected.clear(); load(); }, '', { disabled: offset === 0 }),
        `第 ${Math.floor(offset / limit) + 1} 页`, button('下一页', () => { offset += limit; selected.clear(); load(); }, '', { disabled: offset + limit >= total })));
    contents.replaceChildren(tableRows.length ? table([all, '日期 / 时间', '账号', '模型', '首事件延迟（TTFT）', '总耗时', '状态', '操作'], tableRows, '请求日志列表')
      : empty('暂无请求日志', '当前筛选下没有记录。开始调用后，这里会显示服务端实际留存的内容。'), footer);
    sync();
  }

  async function load() {
    const current = ++requestId;
    refresh.disabled = true;
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
    try {
      const data = await api(`logs?${params}`, { signal });
      if (signal.aborted || current !== requestId) return;
      total = data.total; rows = data.data;
      for (const id of selected) if (!rows.some((row) => row.id === id)) selected.delete(id);
      if (offset && !rows.length) { offset = Math.max(0, Math.ceil(total / limit) * limit - limit); return await load(); }
      draw();
    } catch (error) { if (!signal.aborted && current === requestId) contents.replaceChildren(errorBox(error, load)); }
    finally { if (current === requestId) refresh.disabled = false; }
  }
  await load();
}

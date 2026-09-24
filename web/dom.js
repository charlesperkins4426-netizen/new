// Dynamic values always become text nodes, never parsed markup.
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && !key.startsWith('aria')) node[key] = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

export const button = (text, onclick, variant = '', attrs = {}) =>
  el('button', { type: 'button', class: `btn ${variant}`, onclick, ...attrs }, text);
export const muted = (text) => el('p', { class: 'muted small' }, text);
export const badge = (text, tone = 'neutral') => el('span', { class: `badge ${tone}` }, text);
export const date = (value) => {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? '—' : parsed.toLocaleString('zh-CN', { hour12: false });
};
export const duration = (ms) => ms === null || ms === undefined ? '—' : `${(ms / 1000).toFixed(2)} 秒`;
export const count = (value) => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN');
export function notice(title, description, tone = 'info') {
  return el('div', { class: `notice ${tone}` }, el('span', { class: 'notice-icon', 'aria-hidden': 'true' }, '!'),
    el('div', {}, el('strong', {}, title), description ? el('p', {}, description) : null));
}
export function pageHead(title, subtitle, ...actions) {
  return el('div', { class: 'page-head' }, el('div', {}, el('h1', {}, title), muted(subtitle)), el('div', { class: 'head-actions' }, actions));
}
export function card(title, description, body, action) {
  return el('section', { class: 'card' }, el('div', { class: 'card-head' }, el('div', {}, el('h2', {}, title), description ? muted(description) : null), action), body);
}
export function empty(title, description, ...actions) {
  return el('div', { class: 'empty' }, el('span', { class: 'empty-mark', 'aria-hidden': 'true' }, '—'),
    el('h2', {}, title), el('p', { class: 'muted' }, description), el('div', { class: 'row wrap center' }, actions));
}
export const loading = () => el('p', { class: 'loading', role: 'status' }, '正在加载…');
export function errorBox(error, retry) {
  return el('div', { class: 'error-state', role: 'alert' }, notice('操作未完成', error.message || '暂时无法连接服务，请重试。', 'danger'),
    retry ? button('重试', retry) : null);
}
export function notify(message, isError = false) {
  const area = document.getElementById('notifications');
  const item = el('div', { class: `toast ${isError ? 'danger' : ''}`, role: isError ? 'alert' : 'status' }, message);
  area.append(item);
  setTimeout(() => item.remove(), isError ? 9000 : 5000);
}
export function pairs(items) {
  return el('dl', { class: 'pairs' }, items.map(([name, value]) => [el('dt', {}, name), el('dd', {}, value ?? '未提供')]));
}
export function table(headers, rows, label) {
  return el('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': label },
    el('table', {}, el('thead', {}, el('tr', {}, headers.map((h) => el('th', { scope: 'col' }, h)))), el('tbody', {}, rows)));
}
export function field(label, input, help) {
  return el('label', { class: 'field' }, el('span', {}, label), input, help ? muted(help) : null);
}
export async function busy(control, action, pending = '处理中…') {
  const label = control.textContent;
  control.disabled = true;
  control.setAttribute('aria-busy', 'true');
  control.textContent = pending;
  try { return await action(); }
  finally { control.disabled = false; control.removeAttribute('aria-busy'); control.textContent = label; }
}
export const privacyNotice = () => notice('浏览器仅保留页面内存，服务器仍记录内容',
  '刷新会清空对话与临时输入，但不会删除服务端日志。服务器按配置记录输入、回复正文和推理；能访问后台的人也能读取日志，请勿输入凭据。', 'warning');

export const statuses = {
  ready: ['可用', 'success'], disabled: ['已停用', 'neutral'], cooling: ['冷却中', 'warning'],
  busy: ['使用中', 'info'], error: ['异常', 'danger'], running: ['进行中', 'info'],
  success: ['成功', 'success'], failed: ['失败', 'danger'], cancelled: ['已中止', 'warning'],
  interrupted: ['已中断', 'warning'], valid: ['会话有效', 'success'], invalid: ['会话无效', 'danger'], untested: ['未检查', 'neutral'],
};
export const statusBadge = (status) => badge(...(statuses[status] || ['未知状态', 'neutral']));

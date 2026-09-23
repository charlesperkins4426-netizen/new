import { renderOverview } from './overview.js';
import { renderAccounts } from './accounts.js';
import { renderModels, renderTools } from './catalog.js';
import { renderChat } from './chat.js';
import { renderLogs } from './logs.js';
import { renderGuide } from './guide.js';
import { el, errorBox } from './dom.js';

const pages = {
  overview: ['概览', renderOverview], accounts: ['账号池', renderAccounts], models: ['模型目录', renderModels],
  tools: ['工具目录', renderTools], chat: ['在线聊天', renderChat], logs: ['请求日志', renderLogs], guide: ['获取 Cookie', renderGuide],
};
let current = null;
async function navigate(page, options = {}) {
  if (!pages[page]) return;
  current?.abort();
  const controller = new AbortController();
  current = controller;
  const [title, render] = pages[page];
  const main = document.getElementById('main');
  const view = el('div');
  main.replaceChildren(view);
  document.title = `${title} · DialX 网关`;
  document.getElementById('current-page').textContent = title;
  document.querySelectorAll('[data-page]').forEach((item) => {
    const active = item.dataset.page === page;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  if (!options.initial) { main.focus(); window.scrollTo({ top: 0 }); }
  try { await render(view, { signal: controller.signal, navigate, options }); }
  catch (error) { if (!controller.signal.aborted) view.replaceChildren(errorBox(error, () => navigate(page))); }
}

document.querySelectorAll('[data-page]').forEach((item) => {
  item.setAttribute('data-component-id', `nav-${item.dataset.page}`);
  item.addEventListener('click', () => navigate(item.dataset.page));
});
window.addEventListener('pagehide', () => current?.abort());
// Never restore a route, transcript or key from persistent browser storage or the URL.
navigate('overview', { initial: true });

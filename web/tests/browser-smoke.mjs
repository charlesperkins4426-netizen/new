// Optional offline UI smoke check: uses the preinstalled agent-browser, starts no server.
// All fixtures remain inside this test harness; the production UI has no fallback data.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = mkdtempSync(join(tmpdir(), 'dialx-ui-smoke-'));
const modules = new Map();
function moduleURL(name) {
  if (modules.has(name)) return modules.get(name);
  const source = readFileSync(join(web, name), 'utf8').replace(/from '(\.\/[^']+)'/g,
    (_, dependency) => `from '${moduleURL(dependency.slice(2))}'`);
  const url = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
  modules.set(name, url);
  return url;
}

function fixture() {
  window.__errors = [];
  window.__requests = [];
  addEventListener('error', (event) => window.__errors.push(event.message));
  addEventListener('unhandledrejection', (event) => window.__errors.push(String(event.reason)));
  let accounts = [];
  let defaultModel = 'test-model';
  let logs = [{ id: 'test-log', started_at: '2026-09-23T14:00:00Z', account_id: 'acc_test', model: 'test-model', status: 'success', ttft_ms: 12, duration_ms: 45, code: null, error: null }];
  const models = [{ id: 'test-model', name: '测试模型', owner: '测试提供方', type: 'model', features: { chat_completion: true, mcp: false } },
    { id: 'test-other', name: '<img src=x onerror=alert(1)>', type: 'model', features: { chat_completion: true, mcp: false } }];
  const catalog = () => ({ data: models, default_model: defaultModel, updated_at: '2026-09-23T14:00:00Z', stale: false, error: null, total_count: 2, applications_count: 0 });
  const json = (value) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  window.fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    window.__requests.push({ url, method: options.method || 'GET', headers: options.headers, body });
    if (url === '/api/admin/overview') return json({ service: { status: 'ok', uptime_seconds: 3600 }, default_model: null,
      accounts: { total: accounts.length, enabled: accounts.length, available: accounts.length, cooling: 0, busy: 0 },
      models: { count: 0, total_count: 0, applications_count: 0, updated_at: null, stale: false, error: null },
      requests: { total: 0, success: 0, failed: 0, average_ttft_ms: null, average_duration_ms: null, hourly: [] },
      settings: { default_key_in_use: true, log_retention_days: 7, admin_auth: false }, recent_requests: [] });
    if (url === '/api/admin/accounts' && options.method === 'POST') {
      if (accounts.length) return json({ added: 0, duplicates: 2, ids: [] });
      accounts = ['acc_test', 'acc_second'].map((id) => ({ id, enabled: true, busy: false, status: 'ready', cooldown_until: null, last_error: null, last_checked: null, session_expires: null, limits: null, check_status: 'untested' }));
      return json({ added: 2, duplicates: 1, ids: accounts.map((item) => item.id) });
    }
    if (url === '/api/admin/accounts') return json({ data: accounts });
    if (url === '/api/admin/accounts/delete') { const before = accounts.length; accounts = accounts.filter((item) => !body.ids.includes(item.id)); return json({ deleted: before - accounts.length }); }
    if (url.match(/^\/api\/admin\/accounts\/[^/]+\/check$/)) return json({ id: 'acc_test', check_status: 'valid', checked_at: '2026-09-23T14:00:00Z', session_expires: '2026-10-23T14:00:00Z', limits: { daily: { cost: { total: 100, used: 7 } } }, model: body.model || defaultModel, error: null });
    if (url.startsWith('/api/admin/accounts/') && options.method === 'PATCH') {
      const account = accounts.find((item) => item.id === url.split('/').at(-1)); account.enabled = body.enabled; account.status = body.enabled ? 'ready' : 'disabled'; return json({ ok: true });
    }
    if (url.startsWith('/api/admin/models')) return json(catalog());
    if (url === '/api/admin/settings') { defaultModel = body.default_model; return json({ default_model: defaultModel }); }
    if (url === '/api/admin/tools') return json({ data: [{ id: 'test-tool', name: '测试工具', description: '离线测试目录', enabled: false, available: false, reason: '绑定尚未验证' }], updated_at: null, error: null });
    if (url.startsWith('/api/admin/logs?')) return json({ data: logs, total: logs.length });
    if (url === '/api/admin/logs/delete') { const number = logs.length; logs = []; return json({ deleted: number }); }
    if (url === '/api/admin/logs/test-log') return json({ ...logs[0], messages: [{ role: 'user', content: '<script>不会执行</script>' }], content: '日志正文', reasoning_content: '日志推理' });
    if (url === '/v1/chat/completions') {
      const event = (delta, finish_reason = null) => `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] })}\n\n`;
      const bytes = new TextEncoder().encode(event({ role: 'assistant' }) + event({ reasoning_content: '独立推理' }) + event({ status: 'completed' }) + event({ content: '后续正文' }) + event({}, 'stop') + 'data: [DONE]\n\n');
      let index = 0;
      return new Response(new ReadableStream({ async pull(controller) {
        if (window.__slow) await new Promise((resolve) => setTimeout(resolve, 20));
        if (options.signal.aborted) { controller.error(new DOMException('已停止', 'AbortError')); return; }
        if (index === bytes.length) { controller.close(); return; }
        controller.enqueue(bytes.slice(index, ++index));
      } }), { headers: { 'Content-Type': 'text/event-stream' } });
    }
    throw new Error(`测试不允许未声明请求：${url}`);
  };
}

const html = readFileSync(join(web, 'index.html'), 'utf8')
  .replace('<link rel="stylesheet" href="/static/styles.css">', `<style>${readFileSync(join(web, 'styles.css'), 'utf8')}</style>`)
  .replace('<script type="module" src="/static/app.js"></script>', `<script>(${fixture.toString().replaceAll('</script', '<\\/script')})()</script><script type="module" src="${moduleURL('app.js')}"></script>`);
const path = join(temporary, 'index.html');
writeFileSync(path, html);
const session = `dialx-ui-smoke-${process.pid}`;
function browser(...args) {
  const output = execFileSync('agent-browser', ['--session', session, '--json', ...args], { encoding: 'utf8', maxBuffer: 2000000, timeout: 40000 });
  const parsed = JSON.parse(output);
  assert.equal(parsed.success, true, output);
  return parsed.data;
}
function evaluate(source) { return browser('eval', source).result; }
function check(source, description) { assert.equal(evaluate(source), true, description); }
function clickText(label) { browser('find', 'role', 'button', 'click', '--name', label, '--exact'); }
function navigate(page) { browser('scrollintoview', `[data-page="${page}"]`); browser('click', `[data-page="${page}"]`); }

try {
  browser('open', `file://${path}`);
  browser('set', 'viewport', '1440', '1000');
  check('document.querySelector("h1").textContent === "概览" && document.querySelectorAll("input[type=password]").length === 0', 'root opens overview without credentials');
  browser('screenshot', join(temporary, 'overview.png'));
  navigate('accounts');
  clickText('批量添加账号');
  browser('fill', 'dialog textarea', 'offline-fixture-one|||offline-fixture-two\noffline-fixture-one');
  check('document.querySelector("dialog").textContent.includes("识别 3 条")', 'newline and delimiter feedback');
  clickText('保存账号');
  check('!document.querySelector("dialog") && document.body.textContent.includes("跳过 1 个重复会话")', 'import clears dialog and reports server dedup');
  browser('click', 'tbody tr:first-child [role=switch]');
  check('document.querySelector("tbody tr:first-child [role=switch]").getAttribute("aria-checked") === "false"', 'account toggle reflects backend');
  browser('click', 'tbody tr:first-child button.btn');
  check('document.querySelector("dialog").textContent.includes("会话有效") && document.querySelector("dialog pre").textContent.includes("cost")', 'free check renders raw quota and valid session');
  browser('press', 'Escape');
  check('!document.querySelector("dialog") && document.activeElement.textContent === "免费检查"', 'escape closes dialog and restores focus');
  browser('check', 'thead input[type=checkbox]');
  clickText('批量删除');
  check('document.activeElement.textContent === "取消"', 'danger confirmation initially focuses cancel');
  browser('press', 'Shift+Tab');
  browser('press', 'Tab');
  check('Boolean(document.activeElement.closest("dialog"))', 'dialog traps focus');
  browser('screenshot', join(temporary, 'delete.png'));
  clickText('取消');
  navigate('models');
  check('document.querySelectorAll("tbody tr").length === 2 && !document.querySelector("main img")', 'models render dynamic text without injection');
  browser('fill', 'input[type=search]', 'test-other');
  check('document.querySelectorAll("tbody tr").length === 1', 'model search filters dynamic rows');
  clickText('设为默认');
  check('document.querySelector("tbody").textContent.includes("当前默认")', 'default model saved through settings');
  navigate('tools');
  check('document.querySelector("main [role=switch]").disabled', 'unverified tools cannot be activated');
  navigate('chat');
  check('!document.querySelector(".request-settings").open && document.querySelector("input[type=password]").value === "123456"', 'request key is optional and collapsed');
  browser('fill', '.composer textarea', '测试提问');
  clickText('发送消息');
  check('document.querySelector(".answer").textContent === "后续正文" && document.querySelector(".reason-text").textContent === "独立推理" && document.querySelector(".message-meta [role=status]").textContent === "已完成"', 'reasoning never truncates final content');
  check('window.__requests.filter(r => r.url.startsWith("/api/admin")).every(r => !r.headers?.Authorization) && window.__requests.find(r => r.url.startsWith("/v1/")).headers.Authorization === "Bearer 123456"', 'only completion requests send Bearer');
  browser('screenshot', join(temporary, 'chat.png'));
  evaluate('window.__slow = true');
  browser('fill', '.composer textarea', '第二个测试问题');
  browser('scrollintoview', '.composer button[type=submit]');
  clickText('发送消息');
  browser('scrollintoview', '.composer .danger');
  clickText('停止生成');
  browser('wait', '.composer button[type=submit]');
  check('Array.from(document.querySelectorAll(".message-meta [role=status]")).at(-1).textContent === "已中止"', 'stop never marks incomplete response successful');
  check('window.__requests.filter(r => r.url.startsWith("/v1/")).at(-1).body.messages.every(m => !Object.hasOwn(m, "reasoning_content"))', 'multi-turn sends only roles and body');
  evaluate('window.__slow = false');
  browser('set', 'viewport', '390', '844');
  for (const page of ['overview', 'accounts', 'models', 'tools', 'chat', 'logs', 'guide']) {
    navigate(page);
    check('document.documentElement.scrollWidth <= innerWidth', `${page} has no narrow-screen page overflow`);
  }
  browser('screenshot', join(temporary, 'mobile-guide.png'));
  navigate('logs');
  browser('scrollintoview', 'tbody button');
  browser('click', 'tbody button');
  browser('wait', 'dialog');
  check('document.querySelector("dialog").textContent.includes("日志正文") && !document.querySelector("dialog script")', 'log details are safe text');
  browser('press', 'Escape');
  clickText('删除全部日志');
  clickText('确认删除全部日志');
  check('document.querySelector("main").textContent.includes("暂无请求日志")', 'confirmed deletion refreshes logs');
  navigate('chat');
  clickText('清空对话');
  clickText('确认清空');
  check('document.querySelectorAll(".message").length === 0', 'clear only resets page conversation');
  check('window.__requests.filter(r => r.method !== "GET" && r.url.startsWith("/api/admin/")).every(r => r.headers["Content-Type"] === "application/json")', 'admin mutations use JSON');
  browser('scrollintoview', '.request-settings summary');
  browser('click', '.request-settings summary');
  browser('fill', 'input[type=password]', 'offline-temporary-request-value');
  browser('reload');
  check('document.querySelector("h1").textContent === "概览"', 'refresh resets route to overview');
  navigate('chat');
  check('document.querySelector("input[type=password]").value === "123456" && !document.querySelector(".message")', 'refresh resets call key and conversation');
  assert.deepEqual(evaluate('window.__errors'), [], 'no runtime errors');
  console.log(`Offline UI smoke passed; screenshots: ${temporary}`);
} catch (error) {
  browser('screenshot', join(temporary, 'failure.png'));
  console.error(JSON.stringify(evaluate('({ errors: window.__errors, requests: window.__requests.slice(-3), text: document.querySelector("main").textContent, dialog: document.querySelector("dialog")?.textContent, notifications: document.querySelector("#notifications").textContent })')));
  throw error;
} finally {
  browser('close');
}

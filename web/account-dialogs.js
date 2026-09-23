import { api } from './api.js';
import { dialog } from './dialogs.js';
import { el, button, muted, badge, date, notice, errorBox, pairs, field, statusBadge, notify } from './dom.js';

export function importAccounts(reload) {
  const input = el('textarea', { rows: 8, autocomplete: 'off', spellcheck: false, autocapitalize: 'off', placeholder: '每行粘贴一个账号的完整 Cookie，或用 ||| 分隔多个账号。', 'data-component-id': 'account-import-cookies' });
  const view = dialog('批量添加账号', '支持多行与 ||| 分隔；保存时按完整会话去重。', { onClose: () => { input.value = ''; } });
  const enabled = el('input', { type: 'checkbox', checked: true });
  const estimate = badge('尚未输入');
  const errors = el('div');
  const save = button('保存账号', async () => {
    if (!input.value.trim()) return;
    save.disabled = true;
    input.disabled = true;
    enabled.disabled = true;
    save.textContent = '正在保存…';
    errors.replaceChildren();
    try {
      const result = await api('accounts', { method: 'POST', body: { cookies: input.value, enabled: enabled.checked } });
      input.value = '';
      view.close();
      notify(`新增 ${result.added} 个账号，跳过 ${result.duplicates} 个重复会话。`);
      await reload();
    } catch (error) { if (view.modal.open) errors.replaceChildren(errorBox(error)); }
    finally { input.disabled = false; enabled.disabled = false; save.disabled = !input.value.trim(); save.textContent = '保存账号'; }
  }, 'primary', { disabled: true });
  input.addEventListener('input', () => {
    const number = input.value.split(/\r?\n|\|\|\|/).filter((value) => value.trim()).length;
    estimate.textContent = `识别 ${number} 条输入 · 实际去重以保存结果为准`;
    save.disabled = number === 0;
  });
  view.body.append(notice('请粘贴完整 Cookie，不要提供 Google 密码', '分号连接同一账号的多个 Cookie；换行或 ||| 分隔不同账号。所有 NextAuth 会话分块必须一起保留。', 'warning'),
    field('批量 Cookie', input, '仅保留已验证的会话 Cookie 家族。格式错误时整批不保存，错误只标注输入序号。'),
    el('div', { class: 'mb' }, estimate), el('label', { class: 'check-label mb' }, enabled, '添加后启用账号'),
    muted('原文用于上游请求，保存后列表只显示稳定账号标识，不回显原文或片段。关闭此窗口会清空临时输入。'), errors);
  view.footer.append(button('取消', view.close), save);
  view.open(input);
}

export function checkAccount(account, reload, parentSignal) {
  const controller = new AbortController();
  const view = dialog('账号免费检查', `稳定标识：${account.id}`, { wide: true, onClose: () => controller.abort() });
  const model = el('select', { 'aria-label': '检查模型配额' }, el('option', { value: '' }, '使用默认模型'));
  const resultArea = el('div');
  const errors = el('div');
  const run = button('免费检查', check, 'primary');
  const result = {
    ...account, checked_at: account.last_checked, model: null, error: account.last_error,
  };

  function draw(data) {
    const quota = data.limits === null || data.limits === undefined
      ? muted('尚未获取模型配额，或上游未提供。')
      : el('pre', { class: 'raw-data', tabindex: '0', 'aria-label': '模型配额原始数据' }, JSON.stringify(data.limits, null, 2));
    resultArea.replaceChildren(el('div', { class: 'between mb' }, statusBadge(data.check_status || 'untested'), muted(`最近检查：${date(data.checked_at)}`)),
      pairs([['账号标识', el('code', {}, account.id)], ['会话过期时间', date(data.session_expires)], ['最近错误', data.error || '无'], ['检查方式', '读取会话与模型配额，不发送对话']]),
      el('section', { class: 'detail-section' }, el('h3', {}, '模型配额 · 原始返回'), data.model ? muted(`模型：${data.model}`) : null, quota,
        muted('仅展示上游实际返回的 total / used 与统计周期。cost 单位未证实，不换算美元，也不视为账户余额。')),
      el('section', { class: 'detail-section' }, pairs([['账户余额', '未提供'], ['订阅信息', '未提供'], ['试用到期', '未提供']])));
  }
  async function check() {
    run.disabled = true;
    model.disabled = true;
    run.textContent = '正在免费检查…';
    errors.replaceChildren();
    try {
      const data = await api(`accounts/${encodeURIComponent(account.id)}/check`, {
        method: 'POST', body: model.value ? { model: model.value } : {}, signal: controller.signal,
      });
      if (!controller.signal.aborted) draw(data);
    } catch (error) {
      if (!controller.signal.aborted) errors.replaceChildren(errorBox(error));
    } finally {
      if (!parentSignal.aborted) await reload();
      run.disabled = false;
      model.disabled = false;
      run.textContent = '重新免费检查';
    }
  }
  draw(result);
  view.body.append(notice('会话有效不等于订阅有效或模型额度充足', '此检查不会发送聊天；会话过期时间不是订阅或试用到期时间。'),
    field('检查模型配额', model), errors, resultArea);
  view.footer.append(button('关闭', view.close), run);
  view.open(run);
  api('models', { signal: controller.signal }).then((data) => {
    if (controller.signal.aborted) return;
    data.data.forEach((item) => model.append(el('option', { value: item.id }, item.name || item.id)));
  }).catch(() => { /* The check endpoint can resolve the default model itself. */ });
  check();
}

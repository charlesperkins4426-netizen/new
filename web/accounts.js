import { api } from './api.js';
import { importAccounts, checkAccount } from './account-dialogs.js';
import { confirmAction } from './dialogs.js';
import { el, button, badge, muted, date, notice, pageHead, card, empty, loading, errorBox, pairs, table, statusBadge, notify } from './dom.js';

export async function renderAccounts(root, { signal, navigate, options }) {
  let accounts = [];
  let filter = '';
  let loadId = 0;
  const selected = new Set();
  const contents = el('div', {}, loading());
  const add = button('批量添加账号', () => importAccounts(load), 'primary');
  root.append(pageHead('账号池', '多账号轮询与故障隔离；只使用你有权管理的会话。', button('获取 Cookie', () => navigate('guide')), add), contents);

  function remove(ids) {
    if (!ids.length) return;
    const toDelete = accounts.filter((account) => ids.includes(account.id));
    const remaining = accounts.filter((account) => !ids.includes(account.id) && account.status === 'ready').length;
    const details = el('div', {}, el('ul', { class: 'selected-list' }, toDelete.map((account) => el('li', { class: 'between' }, el('code', {}, account.id), statusBadge(account.status)))),
      remaining ? muted(`删除后，当前列表中还剩 ${remaining} 个可用账号。`) : notice('删除后将没有当前可用的账号', '剩余账号可能处于使用中、冷却或停用状态，对话请求可能返回 503。', 'warning'));
    confirmAction({
      title: `删除这 ${ids.length} 个账号？`, description: '将从网关配置中移除所选账号及其 Cookie。不注销上游账号，不删除已有请求日志。恢复时需要重新添加完整 Cookie。',
      content: details, confirmText: `确认删除 ${ids.length} 个`,
      action: () => api('accounts/delete', { method: 'POST', body: { ids }, signal }),
      onSuccess: async (result) => { selected.clear(); notify(`已删除 ${result.deleted} 个账号。`); await load(); },
    });
  }

  function draw() {
    const visible = accounts.filter((account) => !filter || account.status === filter);
    const all = el('input', { type: 'checkbox', 'aria-label': '全选当前筛选的账号', checked: visible.length > 0 && visible.every((account) => selected.has(account.id)), disabled: !visible.length });
    const deleteSelected = button('批量删除', () => remove([...selected]), 'danger');
    const selection = el('span', { class: 'selection-note' });
    function syncSelection() {
      deleteSelected.disabled = !selected.size;
      selection.textContent = `已选择 ${selected.size} 个账号`;
      all.checked = visible.length > 0 && visible.every((account) => selected.has(account.id));
      all.indeterminate = visible.some((account) => selected.has(account.id)) && !all.checked;
    }
    all.addEventListener('change', () => {
      visible.forEach((account) => all.checked ? selected.add(account.id) : selected.delete(account.id));
      draw();
    });
    const stateFilter = el('select', { 'aria-label': '按账号状态筛选' },
      [['', '全部状态'], ['ready', '可用'], ['busy', '使用中'], ['cooling', '冷却中'], ['disabled', '已停用'], ['error', '异常']]
        .map(([value, name]) => el('option', { value }, name)));
    stateFilter.value = filter;
    stateFilter.addEventListener('change', () => { filter = stateFilter.value; draw(); });
    const rows = visible.map((account) => {
      const check = el('input', { type: 'checkbox', checked: selected.has(account.id), 'aria-label': `选择账号 ${account.id}`, onchange: () => {
        check.checked ? selected.add(account.id) : selected.delete(account.id); syncSelection();
      } });
      const toggle = el('button', { type: 'button', role: 'switch', class: 'switch', 'aria-checked': String(account.enabled), 'aria-label': `启用账号 ${account.id}`, onclick: async () => {
        toggle.disabled = true;
        try {
          await api(`accounts/${encodeURIComponent(account.id)}`, { method: 'PATCH', body: { enabled: !account.enabled }, signal });
          if (!signal.aborted) { notify(account.enabled ? '账号已停用；正在进行的请求可以结束。' : '账号已启用。'); await load(); }
        } catch (error) { if (!signal.aborted) notify(error.message, true); }
        finally { toggle.disabled = false; }
      } });
      return el('tr', {}, el('td', {}, check),
        el('td', {}, el('p', { class: 'strong' }, '账号'), el('code', {}, account.id)),
        el('td', {}, statusBadge(account.status), muted(account.cooldown_until && account.status === 'cooling' ? `冷却至 ${date(account.cooldown_until)}` : account.enabled ? '参与轮询' : '不参与轮询')),
        el('td', {}, date(account.session_expires), muted(`最近检查：${date(account.last_checked)}`), statusBadge(account.check_status)),
        el('td', {}, el('p', {}, account.last_error || '无'), muted('详情已脱敏')),
        el('td', {}, toggle), el('td', {}, button('免费检查', () => checkAccount(account, load, signal), 'link', { 'data-focus-key': `check-${account.id}` })));
    });
    const panel = el('section', { class: 'card' },
      el('div', { class: 'summary-bar' }, badge(`共 ${accounts.length} 个账号`), badge(`${accounts.filter((a) => a.status === 'ready').length} 个可用`, 'success'),
        badge(`${accounts.filter((a) => a.status === 'cooling').length} 个冷却中`, 'warning'), badge(`${accounts.filter((a) => !a.enabled).length} 个已停用`), muted('只显示稳定账号标识，不显示 Cookie 片段')),
      el('div', { class: 'toolbar' }, stateFilter, selection, button('取消选择', () => { selected.clear(); draw(); }, 'link', { disabled: !selected.size }),
        el('span', { class: 'grow' }), button('刷新', load), deleteSelected, button('删除全部', () => remove(accounts.map((a) => a.id)), 'danger', { disabled: !accounts.length })),
      rows.length ? table([all, '账号 / 稳定标识', '状态', '会话过期 / 最近检查', '最近错误', '启用', '操作'], rows, '账号池列表')
        : empty(accounts.length ? '没有符合筛选的账号' : '还没有添加账号', accounts.length ? '请选择其他状态。' : '粘贴你有权管理的完整会话 Cookie，开始配置账号池。', !accounts.length ? button('批量添加账号', () => importAccounts(load), 'primary') : null),
      el('div', { class: 'table-footer' }, `显示 ${visible.length} / ${accounts.length} 个账号`, '启停状态会持久保存'));
    contents.replaceChildren(panel, el('div', { class: 'grid-two mt' },
      card('按模型查看配额', '配额与账号、模型绑定，不是余额', el('div', { class: 'card-pad stack' },
        muted('点击账号的「免费检查」查看会话过期时间与上游模型配额原始数据，支持选择模型。'), pairs([['账户余额', '未提供'], ['订阅信息', '未提供'], ['试用到期', '未提供']]))),
      card('会话检查与轮询', '检查会话有效性，不发送对话', el('div', { class: 'card-pad stack' }, pairs([['免费检查', '只校验会话与读取模型配额'], ['失败处理', '暂时冷却，避免连续失败'], ['全部停用后', '对话接口返回 503']]),
        muted('会话有效不保证所有模型均可调用。cost 单位未知，不换算美元。')))));
    syncSelection();
  }
  async function load() {
    const current = ++loadId;
    try {
      const result = await api('accounts', { signal });
      if (signal.aborted || current !== loadId) return;
      accounts = result.data;
      for (const id of selected) if (!accounts.some((account) => account.id === id)) selected.delete(id);
      draw();
    } catch (error) { if (!signal.aborted && current === loadId) contents.replaceChildren(errorBox(error, load)); }
  }
  await load();
  if (options?.import && !signal.aborted) importAccounts(load);
}

import { api } from './api.js';
import { el, button, badge, muted, date, duration, count, notice, pageHead, card, empty, loading, errorBox, pairs, table, statusBadge, busy, notify } from './dom.js';

function stat(title, value, caption, label, tone = 'neutral', model = false) {
  return el('section', { class: 'card stat' }, el('div', { class: 'stat-top' }, title, badge(label, tone)),
    el('div', { class: `stat-value ${model ? 'model' : ''}` }, value), el('div', { class: 'stat-bottom' }, caption));
}

function requestChart(requests) {
  const hourly = requests.hourly || [];
  const max = Math.max(1, ...hourly.map((entry) => entry.count));
  const metrics = el('div', { class: 'grid-three' }, [
    ['请求数', count(requests.total)], ['成功率', requests.total ? `${(requests.success / requests.total * 100).toFixed(1)}%` : '—'],
    ['平均首事件延迟（TTFT）', duration(requests.average_ttft_ms)],
  ].map(([title, value]) => el('div', {}, el('p', { class: 'mini-stat-title' }, title), el('p', { class: 'mini-stat' }, value))));
  const bars = hourly.map(({ hour, count: value }) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 20 100');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('class', 'chart-bar');
    svg.setAttribute('aria-hidden', 'true');
    const rect = document.createElementNS(svg.namespaceURI, 'rect');
    const height = Math.max(1, Math.min(100, value / max * 100));
    for (const [key, val] of Object.entries({ x: 0, y: 100 - height, width: 20, height, rx: 1 })) rect.setAttribute(key, String(val));
    svg.append(rect);
    return el('div', { class: 'chart-column', title: `${hour}：${value} 次请求` }, svg);
  });
  const chart = hourly.length ? el('div', {}, el('div', { class: 'chart', role: 'img', 'aria-label': '按小时统计的请求数量' }, bars),
    el('div', { class: 'chart-labels' }, el('span', {}, hourly[0].hour), el('span', {}, hourly.at(-1).hour))) : muted('暂无按小时统计的请求数据。');
  return el('div', { class: 'card-pad stack' }, metrics, chart);
}

export async function renderOverview(root, { signal, navigate }) {
  const contents = el('div', {}, loading());
  root.append(pageHead('概览', '网关运行状况、模型缓存与调用摘要，一目了然。',
    button('查看请求日志', () => navigate('logs')), button('在线聊天', () => navigate('chat'), 'primary')),
  notice('后台直接访问，无需鉴权', '可直接修改账号和日志。默认仅监听本机，请勿直接暴露到不可信网络。'), contents);

  async function load() {
    try {
      const data = await api('overview', { signal });
      if (signal.aborted) return;
      const { accounts, models, requests, settings, service } = data;
      const keyInfo = card('调用 API · 单一配置', '/v1/* 使用调用 Key；管理后台与管理接口无需调用 Key。',
        el('div', { class: 'card-pad' }, el('code', {}, settings.default_key_in_use ? 'Authorization: Bearer 123456' : 'Authorization: Bearer <你的 API_TOKEN>'),
          muted(settings.default_key_in_use ? '当前使用默认 API_TOKEN=123456。可自行修改 .env 并重启；此提示不阻挡使用。' : '已配置非默认调用 Key。后台不读取或修改该值；聊天请求设置可填写匹配值。')));
      const stats = el('div', { class: 'grid-four mt' },
        stat('服务状态', service.status === 'ok' ? '运行正常' : '状态异常', `已运行 ${Math.floor(service.uptime_seconds / 3600)} 小时 ${Math.floor(service.uptime_seconds / 60) % 60} 分钟`, service.status === 'ok' ? '健康' : '异常', service.status === 'ok' ? 'success' : 'danger', true),
        stat('默认模型', data.default_model || '待配置', '省略 model 字段时使用', data.default_model ? '已配置' : '待配置', 'neutral', true),
        stat('可用账号', `${count(accounts.available)} / ${count(accounts.total)}`, `${accounts.cooling} 个冷却中 · ${accounts.total - accounts.enabled} 个已停用`, '实时', 'info'),
        stat('模型缓存', models.count ? count(models.count) : '尚未获取', `另有 ${count(models.applications_count)} 个应用 · 默认不对外开放`, models.stale ? '缓存过期' : (models.updated_at ? '已缓存' : '待配置'), models.stale ? 'warning' : 'neutral', !models.count));
      const refresh = button('刷新缓存', () => busy(refresh, async () => {
        try { await api('models/refresh', { method: 'POST', body: {}, signal }); await load(); notify('模型缓存已刷新。'); }
        catch (error) { if (!signal.aborted) notify(error.message, true); }
      }), '', { 'data-component-id': 'overview-refresh-models' });
      const cache = card('模型缓存', '从上游动态发现，不使用写死的模型列表', el('div', { class: 'card-pad stack' },
        pairs([['最近成功刷新', date(models.updated_at)], ['目录总数', `${count(models.total_count)} 项`], ['对外开放规则', '原生模型 + 支持对话'], ['应用类型', `${count(models.applications_count)} 项 · 默认不开放`]]),
        models.error ? notice('最近刷新失败', models.error, 'warning') : muted('刷新失败时保留上次可用缓存，并显示错误与时间。')), refresh);
      const boundary = card('能力边界', '只展示真实发现与已验证的能力', el('div', { class: 'card-pad stack' },
        pairs([['工具默认状态', '关闭'], ['原生模型工具绑定', '未验证'], ['余额 / 订阅 / 试用', '上游未提供'], ['日志保留', `${settings.log_retention_days} 天`]]),
        muted('模型配额不是账户余额；会话过期时间也不是订阅到期时间。')));
      const recent = (data.recent_requests || []).map((log) => el('tr', {}, [date(log.started_at), log.model || '—',
        el('span', { class: 'mono' }, log.account_id || '—'), duration(log.duration_ms), statusBadge(log.status)].map((value) => el('td', {}, value))));
      const recentCard = card('最近请求', '消息按服务器配置留存，凭据字段经过过滤', recent.length
        ? table(['时间', '模型', '账号', '总耗时', '状态'], recent, '最近请求列表')
        : empty('暂无请求记录', '开始调用后，将在这里显示真实请求摘要。'), button('查看全部 →', () => navigate('logs'), 'link'));
      contents.replaceChildren(keyInfo, stats);
      if (!accounts.total) {
        contents.append(el('section', { class: 'card mt' }, empty('先添加一个 DialX 账号', '网关已经启动。添加完整会话 Cookie，免费检查账号并刷新模型，即可开始调用。',
          button('添加账号', () => navigate('accounts', { import: true }), 'primary'), button('查看 Cookie 获取教程', () => navigate('guide'))),
        el('div', { class: 'card-pad' }, muted('暂无可用账号时，对话接口返回 503，不会虚构响应。'))),
        el('div', { class: 'grid-three mt' }, [
          ['1 · 添加会话 Cookie', '支持多行或 ||| 分隔，保留完整分块。'],
          ['2 · 检查账号，刷新模型', '免费会话检查不发送对话请求。'],
          ['3 · 使用调用 API', '调用 API 使用单一 Key；后台直接访问。'],
        ].map(([title, description]) => card(title, null, el('div', { class: 'card-pad' }, muted(description))))));
      } else {
        contents.append(el('div', { class: 'split mt' }, card('调用摘要', '统计以服务端返回的日志记录为准', requestChart(requests)), cache),
          el('div', { class: 'split mt' }, recentCard, boundary));
      }
    } catch (error) { if (!signal.aborted) contents.replaceChildren(errorBox(error, load)); }
  }
  await load();
}

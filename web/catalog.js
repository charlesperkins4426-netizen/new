import { api } from './api.js';
import { el, button, badge, muted, date, notice, pageHead, card, empty, loading, errorBox, pairs, table, field, busy, notify } from './dom.js';

export async function renderModels(root, { signal }) {
  let catalog = null;
  let search = '';
  const contents = el('div', {}, loading());
  const refresh = button('刷新模型缓存', () => load(true), 'primary');
  root.append(pageHead('模型目录', '从上游动态发现；只开放已确认支持对话的原生模型。', refresh), contents);

  function draw() {
    const input = el('input', { type: 'search', placeholder: '搜索模型名称、标识或提供方', value: search, 'aria-label': '搜索模型' });
    const rowsArea = el('div');
    const summary = el('span', { class: 'muted' });
    function drawRows() {
      const query = search.trim().toLocaleLowerCase();
      const models = catalog.data.filter((model) => [model.id, model.name, model.owner].some((value) => String(value || '').toLocaleLowerCase().includes(query)));
      summary.textContent = `显示 ${models.length} / ${catalog.data.length} 个模型`;
      if (!models.length) { rowsArea.replaceChildren(empty(catalog.data.length ? '未找到匹配模型' : '暂无模型目录', catalog.data.length ? '请尝试其他关键词。' : '添加并启用账号后，刷新模型缓存。')); return; }
      const rows = models.map((model) => {
        const setDefault = button('设为默认', () => busy(setDefault, async () => {
          try {
            await api('settings', { method: 'PUT', body: { default_model: model.id }, signal });
            catalog.default_model = model.id;
            if (!signal.aborted) { draw(); notify('默认模型已保存。'); }
          } catch (error) { if (!signal.aborted) notify(error.message, true); }
        }), 'link');
        return el('tr', {}, el('td', {}, el('p', { class: 'strong' }, model.name || model.id), el('p', { class: 'mono muted' }, model.id)),
          el('td', {}, badge('原生模型'), muted(model.owner || '提供方未提供')),
          el('td', {}, badge(model.features?.chat_completion ? '支持对话' : '未确认对话', model.features?.chat_completion ? 'success' : 'neutral')),
          el('td', {}, el('code', {}, `mcp=${model.features?.mcp ?? '未提供'}`), muted('工具绑定未验证')),
          el('td', {}, model.id === catalog.default_model ? badge('当前默认', 'info') : setDefault));
      });
      rowsArea.replaceChildren(table(['模型', '类型 / 提供方', '对话能力', '工具能力', '默认模型'], rows, '动态模型目录'));
    }
    input.addEventListener('input', () => { search = input.value; drawRows(); });
    const panel = el('section', { class: 'card' }, el('div', { class: 'toolbar' }, input, el('span', { class: 'grow' }), badge(`原生模型 ${catalog.data.length}`), badge(`应用 ${catalog.applications_count ?? 0}`)),
      rowsArea, el('div', { class: 'table-footer' }, summary, `最近成功刷新：${date(catalog.updated_at)}`));
    const defaultCard = card('默认调用行为', '省略 model 字段时使用默认模型', el('div', { class: 'card-pad stack' },
      pairs([['当前默认', catalog.default_model || '待配置'], ['默认模型配置', '写入 .env 并持久保存'], ['配置校验', '仅接受支持对话的原生模型']]),
      muted('默认模型不可用时明确报错，不静默改用另一模型。')));
    contents.replaceChildren(notice('应用不作为普通模型开放', '应用可能包含不可控的预置工具。当前仅对外暴露原生模型中支持 features.chat_completion 的条目。'));
    if (catalog.stale || catalog.error) contents.append(notice('模型缓存需要刷新', catalog.error || '当前显示上次成功缓存，目录已过期。', 'warning'));
    contents.append(panel, el('div', { class: 'grid-two mt' }, defaultCard,
      card('接口可见性', '目录按真实类型与对话能力筛选', el('div', { class: 'card-pad stack' },
        pairs([['模型发现接口', el('code', {}, 'GET /v1/models')], ['对话接口', el('code', {}, 'POST /v1/chat/completions')], ['缓存刷新失败', '保留上次缓存，并提示原因']]),
        muted('目录不改变上游权限或配额，不代表模型一定有剩余额度。')))));
    drawRows();
  }
  async function load(force = false) {
    await busy(refresh, async () => {
      try {
        catalog = await api(force ? 'models/refresh' : 'models', { method: force ? 'POST' : 'GET', body: force ? {} : undefined, signal });
        if (!signal.aborted) draw();
      } catch (error) {
        if (signal.aborted) return;
        if (catalog) { catalog.stale = true; catalog.error = error.message; draw(); }
        else contents.replaceChildren(errorBox(error, () => load()));
      }
    }, '正在刷新…');
  }
  await load();
}

export async function renderTools(root, { signal }) {
  const contents = el('div', {}, loading());
  const refresh = button('刷新工具目录', load);
  root.append(pageHead('工具目录', '看见目录，不代表已经获得工具调用能力。', refresh),
    notice('当前工具无法启用', '工具绑定尚未验证。仅查看目录，开关保持禁用，不发送猜测的工具参数，也不把工具状态显示为已激活。', 'warning'), contents);
  async function load() {
    await busy(refresh, async () => {
      try {
        const result = await api('tools', { signal });
        if (signal.aborted) return;
        const cards = result.data.map((tool) => el('section', { class: 'card tool-card' },
          el('div', { class: 'between' }, el('div', { class: 'row' }, el('span', { class: 'tool-symbol', 'aria-hidden': 'true' }, (tool.name || tool.id).slice(0, 1)),
            el('div', {}, el('h2', {}, tool.name || tool.id), el('code', { class: 'muted' }, tool.id))), badge('不可用')),
          el('p', { class: 'muted small mt' }, tool.description || '上游未提供描述。'),
          el('div', { class: 'tool-reason' }, el('strong', {}, '不可用原因'), tool.reason || '原生模型工具绑定尚未验证。'),
          el('div', { class: 'between' }, muted('启用此工具'), el('button', { type: 'button', role: 'switch', class: 'switch', disabled: true, 'aria-checked': 'false', 'aria-label': `启用 ${tool.name || tool.id}（绑定未验证）` }))));
        contents.replaceChildren(el('div', { class: 'grid-three mb' },
          card('目录发现', null, el('div', { class: 'card-pad' }, `${result.data.length} 个工具集`)),
          card('模型能力', null, el('div', { class: 'card-pad' }, '需按上游实际能力判断')),
          card('调用绑定', null, el('div', { class: 'card-pad' }, badge('未验证', 'warning')))));
        if (result.error) contents.append(notice('目录读取提示', result.error, 'warning'));
        contents.append(cards.length ? el('div', { class: 'grid-two' }, cards) : el('section', { class: 'card' }, empty('暂无工具目录', '添加可用账号后刷新。这里只展示上游实际返回的工具集。')),
          el('p', { class: 'muted small mt' }, `最近获取：${date(result.updated_at)}。发现条目不代表工具可执行；不生成假的工具执行结果。`));
      } catch (error) { if (!signal.aborted) contents.replaceChildren(errorBox(error, load)); }
    }, '正在刷新…');
  }
  await load();
}

import { el, button, muted, notice, pageHead, card } from './dom.js';

function steps(items) {
  return el('ol', { class: 'steps' }, items.map(([title, description, extra]) => el('li', {},
    el('div', {}, el('h3', {}, title), el('p', {}, description), extra))));
}

export function renderGuide(root, { navigate }) {
  const network = steps([
    ['在浏览器中登录 DialX', '使用自己的账号在上游站点完成登录。网关不接收、也不保存你的 Google 密码。',
      el('a', { href: 'https://chat.dialx.ai/', target: '_blank', rel: 'noopener noreferrer', class: 'btn link' }, '打开 DialX →')],
    ['打开开发者工具的「网络」面板', '按浏览器开发者工具快捷键（通常为 F12），选择「网络」（Network），然后刷新上游页面。'],
    ['找到模型列表请求', '筛选 /api/models，打开「标头」（Headers）中的「请求标头」（Request Headers）。'],
    ['复制 Cookie 字段的完整值', '复制 Cookie 后的完整内容，不含字段名。不要只复制某一个分块；无需复制其他请求头。',
      el('div', { class: 'mt-sm' }, muted('格式说明 · 以下占位符不是有效凭据'), el('pre', { class: 'raw-data' }, '__Secure-next-auth.session-token.0=<分块一>; __Secure-next-auth.session-token.1=<分块二>'))],
    ['添加账号并做一次免费检查', '在「账号池」粘贴内容，保存后点击「免费检查」。会话检查不发送对话，也不代表订阅有效或存在余额。'],
  ]);
  const application = steps([
    ['进入应用面板的 Cookie 列表', '打开「应用」（Application）→「存储」（Storage）→ Cookies，选择当前 DialX 站点。'],
    ['复制全部会话 Cookie 名称和值', '保留 __Secure-next-auth.session-token 及其实际存在的所有分块，如 .0、.1。每段名称和值都需完整，不可只复制一段，也不要同时混入不同会话。'],
    ['用分号连接，同一账号放在一行', '按「名称=值; 名称=值」组合。多个账号用换行或 ||| 分隔，不要混合不同账号的分块。'],
  ]);
  root.append(pageHead('获取 Cookie', '从你已登录的 DialX 会话中复制完整 Cookie，不需要 Google 密码。', button('去添加账号', () => navigate('accounts', { import: true }), 'primary')),
    notice('Cookie 等同于登录凭据，请勿分享', '只粘贴到你信任的本机网关。不要发到聊天、工单、代码仓库或截图中；后台无需鉴权，不要在不可信公共预览中导入真实 Cookie。', 'warning'),
    el('div', { class: 'tutorial-layout' }, el('div', { class: 'stack' },
      card('方式一：从网络请求复制', '推荐 · 一次获取完整 Cookie 请求头', el('div', { class: 'card-pad' }, network)),
      card('方式二：从应用面板组合', '适合需要核对分块完整性的情况', el('div', { class: 'card-pad' }, application))),
    el('aside', { class: 'stack', 'aria-label': 'Cookie 注意事项' },
      card('复制前检查', null, el('div', { class: 'card-pad' }, steps([
        ['登录状态有效', '先确认上游页面能正常打开。'], ['完整保留分块', '只复制 .0 或 .1 都可能失败。'], ['不混淆账号分隔符', '分号属于一个账号；换行与 ||| 分隔多个账号。'],
      ]))),
      card('检查失败怎么办？', null, el('div', { class: 'card-pad stack' },
        el('div', {}, el('h3', {}, '会话已过期'), muted('在上游重新登录，再复制完整 Cookie。')),
        el('div', {}, el('h3', {}, '分块缺失或值被截断'), muted('优先从网络请求复制完整字段，避免列表显示截断。')),
        el('div', {}, el('h3', {}, '仍然无法使用'), muted('查看账号最近错误。不要通过无限重试或轮换账号绕过上游限制。')))))));
}

# DialX 协议证据（已脱敏）

## 采集方式

2026-09-23，使用用户右侧浏览器的登录会话，通过 Chrome DevTools Protocol 的 Network 事件记录真实请求。
在页面中分别选择 Claude Opus 4.6 与 Gemini 2.5 Pro，并实际发送短文本问题。
使用 `Network.getResponseBody` 与字节流记录读取响应。原始 Cookie 和原始捕获不进入仓库或 ZIP。

## 已观察的接口

| 方法与路径（站点 https://chat.dialx.ai） | 用途 / 格式 |
|---|---|
| `GET /api/auth/session` | JSON；`user` 表示会话，`expires` 是会话期限，不是试用期限。 |
| `GET /api/models` | JSON 数组；本次 87 项：64 个模型与 23 个应用。运行时重新获取。 |
| `GET /api/toolsets-listing` | JSON `{data:[...]}`；本次 7 个工具集，不代表原生模型能绑定工具。 |
| `GET /api/deployments/{encoded_model_id}/limits` | JSON；按周期返回 Token、请求、cost 的 used/total。cost 单位未验证。 |
| `POST /api/chat` | JSON 请求，NUL 分隔 JSON 响应；不是 SSE。 |
| `GET /api/bucket` | 浏览器读取历史存储位置。网关不需要它。 |
| `PUT /api/conversations/{bucket}/{encoded_name}` | 浏览器在回答完成后保存历史。网关不执行这个操作。 |

没有观察到先创建会话的必需 POST。客户端先产生 local ID，再发送聊天，最后才保存历史。
没有观察到 `/organizations/{id}`、`balanceCents`、美元余额、订阅或试用接口。

## 鉴权

本次会话使用 Cookie：

```text
__Secure-next-auth.session-token.0=<分块0>; __Secure-next-auth.session-token.1=<分块1>
```

只带这两段，Python HTTP 客户端可以成功读取 session 与 models。
不需要 Google 密码、Google Cookie、AWS 负载均衡 Cookie 或上游 Bearer API Key。
不同会话可以是单段，也可以是多段；导入时必须保留全部 NextAuth session 分块。
session 与 models 响应会通过 `Set-Cookie` 更新分块，网关必须保存新值并处理旧分块删除。

## 实际聊天请求

```http
POST /api/chat
Content-Type: application/json
Referer: https://chat.dialx.ai/
x-timezone: UTC
x-language: en
Cookie: <完整 NextAuth session Cookie>
```

```json
{
  "model": {"id": "claude-opus-4-6@default", "说明": "真实请求使用 /api/models 中完整模型对象，此处省略元数据"},
  "messages": [{"role": "user", "content": "请计算 17×19，用一句话回答。"}],
  "id": "conversations/local/claude-opus-4-6@default__<客户端名称>",
  "reference": "<客户端随机引用>",
  "prompt": "",
  "temperature": 1
}
```

上面的“说明”仅为文档注释，程序不会发送这个字段。
真实请求没有 `tools`、`toolsets` 或 `selectedAddons`。当前网关同样不发送这些字段。

## 响应不是 SSE

响应头为 `Content-Type: application/octet-stream`。
每个 UTF-8 JSON 对象后跟一个 NUL 字节 `0x00`。网络读取块可以截断任何位置，包括中文字符中间。
以下 `\0` 仅表示实际 NUL 字节，不是两个文本字符。

Claude 实际帧：

```text
{"responseId":"<脱敏响应ID>"}\0
{"role":"assistant"}\0
{"content":"17×19 = 323"}\0
{"content":"。"}\0
{"custom_content":{"state":{"claude_message_content":[{"text":"17×19 = 323。","type":"text"}]}}}\0
{}\0
```

Gemini 实际帧类型和顺序：

```text
{"responseId":"<脱敏响应ID>"}\0
{"role":"assistant"}\0
{"custom_content":{"stages":[{"index":0,"name":"Thinking","status":null}]}}\0
{"custom_content":{"stages":[{"index":0,"content":"<推理增量1>","status":null}]}}\0
{"custom_content":{"stages":[{"index":0,"content":"<推理增量2>","status":null}]}}\0
{"custom_content":{"stages":[{"index":0,"content":"<推理增量3>","status":null}]}}\0
{"content":"54"}\0
{"custom_content":{"stages":[{"index":0,"status":"completed"}]}}\0
{"content":""}\0
{}\0
```

完整脱敏回放帧在 `tests/fixtures/` 中。

## 映射规则

1. 按 stage.index 记住阶段名称，Thinking 的 content 增量进入 `reasoning_content`。
2. stage.content 是增量，浏览器最后保存的会话记录证实逐段拼接。上游重复字句也保留。
3. 普通 content 进入正文。阶段完成和空 content 不结束正文。
4. state 中的全文快照不再次追加，避免正文重复。
5. `{}` 也可能是生成过程中的空增量，不能单独表示终止。必须读到 HTTP EOF，并确认最后完整帧是 `{}` 且没有残留字节，才发出成功终止。中途、开头及连续空对象不传给聚合器；残帧、缺少最终空帧或网络错误仍失败。
6. 对外转换为 OpenAI SSE 的 `delta.reasoning_content` / `delta.content`，最后输出 stop 和 `[DONE]`。
7. 非流式通过同一解析器聚合，`message` 同时保留 content 与 reasoning_content。

### 长回复修正

2026-09-23 的独立 VPS 长回复捕获读到 HTTP EOF：第 283 帧为空对象，但后续仍有 216 个正文增量、完整状态快照和另一个空对象。首个空对象在约 17.17 秒到达，HTTP EOF 在约 28.95 秒到达；总正文 14,399 字符，旧解析器只保留 8,113 字符。最终快照与完整正文增量一致，不应追加快照来修补正文。

同一字节流使用不同网络分块时，旧实现会截断或报错。现有解析器将空帧保留为待确认状态，继续实时传递非空增量，只在真正的 HTTP EOF 确认最后空帧。模型目录的输出限制字段不能用于解释这次网关丢字问题。

## 能力边界

本次选择的原生模型 `features.mcp=false`。虽然可以列出工具集，但没有已验证的模型绑定请求。
工具卡片保持关闭且不可启用。应用可能有自己的预置工具，因此不作为原生模型开放。
上游返回的模型权限和配额可随账号、时间变化，抓包数量不是代码中的固定列表。

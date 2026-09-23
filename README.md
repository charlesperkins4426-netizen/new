# DialX OpenAI 兼容网关

把本人 DialX 登录会话转换为文本聊天 API。使用 Python、FastAPI 和 HTTPX，自带无登录中文管理后台。

**后台没有鉴权。能打开后台的人可以添加或删除账号、读取或删除请求日志。默认只监听本机。**
调用接口使用一个 `API_TOKEN`，默认 `123456`。按需求不提供管理员密码、后台登录或多 Key 管理。
不要把加载真实 Cookie 的服务直接暴露到不可信网络。公开前请自行配置访问控制。

## 能力与边界

- `POST /v1/chat/completions`：流式和非流式，分别保留 `reasoning_content` 与 `content`。
- `GET /v1/models`：使用账号的真实模型目录，默认缓存 3600 秒；后台支持刷新与设置默认模型。
- 多 Cookie：逐行或 `|||` 批量导入、去重、轮询、启停、批量删除、失败冷却。
- 免费检查：只读取会话与模型配额，不发送聊天，不消耗对话额度。
- 中文后台：概览、账号池、模型、工具目录、在线聊天、日志与 Cookie 教程。
- Cookie 续期、默认模型与账号启停原子写回 `.env`，重启保留。
- 工具默认关闭。原生模型工具绑定未验证，因此卡片保持不可用，不伪造开关成功。
- 没有已验证的美元余额、订阅或试用接口，这些字段显示“未提供”。模型 cost 配额不当成美元余额。
- 原生映射仅支持 `model`、文本 `messages`、`stream` 和 `temperature`。默认严格校验；可显式开启下述四个生成参数的兼容忽略模式。其他未知参数、图片、工具调用、JSON Schema 和 `stream_options` 等仍返回明确 400。
- 上游没有提供已验证的 token usage，返回 `usage: null`，不编造用量。
- 应用可能包含预置工具，因此只开放支持聊天的原生模型，不把应用当成模型。

### 兼容客户端附带的生成参数

某些客户端会附带 `max_tokens`、`presence_penalty`、`frequency_penalty` 和 `top_logprobs`。DialX 网页接口没有已验证的映射来保证这些参数生效：真实测试中，`max_tokens=1` 仍返回了完整的六词回答，也没有得到 logprobs 数据。模型目录的能力标记不等于网页接口会转发这些参数。

默认 `IGNORE_UNSUPPORTED_PARAMS=false`，仍严格拒绝这些字段。如接受忽略这四个字段，可设置 `IGNORE_UNSUPPORTED_PARAMS=true` 并重启。流式和非流式响应都会通过 `X-DialX-Ignored-Parameters` 响应头列出已忽略字段；请求日志列表和详情 API 的 `ignored_parameters` 数组保留同样记录。

**兼容模式不会实施 `max_tokens` 输出限制，也不会生成 logprobs 或 token 用量。** 被忽略字段的值不会发送给 DialX；正文、角色和已支持的温度保持原样。其他未知字段、工具、图片和输出格式仍报错，错误信息会指出具体字段，不回显字段值。

详细真实抓包、请求头、请求体、响应帧和鉴权字段见 [docs/protocol.md](docs/protocol.md)。
上游不是 SSE，而是 NUL 分隔 JSON。测试回放数据来自右侧浏览器的真实调用，不是猜测协议。

## 快速启动

需要 Python 3.11 或更高版本。

```bash
cd dialx-openai-gateway
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
python app.py
```

Ubuntu 若缺少 venv：

```bash
sudo apt-get update
sudo apt-get install python3-venv
```

打开 `http://127.0.0.1:8000/`。后台直接显示概览，没有登录步骤。
进入“账号池”添加本人的 DialX session Cookie，再执行“免费检查”和“刷新模型”。
后台不会导入本项目开发时使用的任何真实账号。

程序也可通过工厂方式启动：

```bash
venv/bin/uvicorn app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

CLI 参数控制这个命令的端口。`python app.py` 则读取 `.env` 中的 HOST/PORT。
只运行一个 worker，不使用开发热重载。进程内支持不同账号并发，同一账号串行，避免续期冲突。

## 获取和导入 Cookie

1. 在本人浏览器登录 `https://chat.dialx.ai/`。
2. 打开开发者工具的 Network 面板。
3. 刷新页面，选择 `/api/models` 请求。
4. 在 Request Headers 中复制完整 Cookie。
5. 在后台“账号池”粘贴并保存。

也可以从 Application → Cookies → `https://chat.dialx.ai` 复制全部 NextAuth session 项。
不要只复制分块 `.0` 而遗漏 `.1`。不要提供 Google 密码。

```text
__Secure-next-auth.session-token.0=<账号A分块0>; __Secure-next-auth.session-token.1=<账号A分块1>
__Secure-next-auth.session-token=<账号B完整session>
```

每行代表一个账号；也可以用 `|||` 分隔账号。分号只分隔同一账号内的 Cookie。
只有 NextAuth session Cookie 被保留；其他网站 Cookie 不参与鉴权。
同一规范化 Cookie 去重。不同登录会话不保证识别为同一个自然人。

也可在 `.env` 初始化导入：

```dotenv
DIALX_COOKIES='__Secure-next-auth.session-token=<账号A>|||__Secure-next-auth.session-token=<账号B>'
```

首次加载会转换为稳定账号记录 `DIALX_ACCOUNTS`，并清空导入字段，防止删除后重启重新导入。
`COGITO_DISABLED` 保存停用账号 ID，以兼容原需求中的名称。它不是另一个上游网站。
Cookie 的新分块会安全写回 `.env`。不要在服务运行时同时手改受管理配置；手动更改后重启。
`.env` 已有配置优先于启动环境。`ENV_FILE=/path/to/.env` 可以切换配置文件。

## 调用 API

先从模型列表选择真实模型 ID：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H 'Authorization: Bearer 123456'
```

非流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer 123456' \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":false}'
```

`model` 可以省略以使用默认模型；也可以设置为 `/v1/models` 返回的 ID。
单条消息不加角色前缀，多轮历史保留原来的 role/content 数组。

流式调用把 `stream` 改成 `true`，并给 curl 添加 `-N`。
事件的 `choices[0].delta.reasoning_content` 是上游返回的推理，`delta.content` 是正文。
有些模型不返回推理，此时字段为空，不由网关生成。
只有整体成功结束才返回 `finish_reason: "stop"` 与 `[DONE]`。
流中出现 `{ "error": ... }` 或没有终止事件就断开时，必须按失败处理，不把部分回答当成完整结果。

OpenAI Python SDK 示例（SDK 需另行安装）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="123456")
model = client.models.list().data[0].id
reply = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "你好"}],
)
print(reply.choices[0].message.content)
print(getattr(reply.choices[0].message, "reasoning_content", ""))
```

不需要管理 Key。以后修改调用 Key 时，只修改 `.env` 的 `API_TOKEN` 并重启。
后台在线聊天的“请求设置”可输入匹配的新 Key，刷新后恢复默认 `123456`。这不是后台登录。

## 日志、隐私与失败处理

在线聊天、调用 Key 与待导入文本只放在页面内存，不写 localStorage、sessionStorage 或 IndexedDB。
刷新会清空聊天和临时输入，后台仍直接打开。浏览器自动填充和系统剪贴板由浏览器／操作系统控制。

**服务端仍按需求保存完整输入、正文和上游推理。** SQLite 位于 DATA_DIR，默认 `.env` 同目录的 `data/`。
日志还记录稳定账号 ID、模型、首事件延迟（TTFT）、总耗时和错误原因。TTFT 的首事件包括元数据，不是正文首字。
默认保留 7 天，可通过 `LOG_RETENTION_DAYS` 配置。后台支持删除完成的日志；进行中的记录不会被删除。
删除账号不会同时删除历史日志。要彻底清理本机内容，还需清理对应日志与备份。

典型错误：

| 状态 | 含义 |
|---|---|
| 400 | 参数格式错误，或请求了未验证能力。 |
| 401 | 网关调用 Key 缺失或不正确。 |
| 404 | 模型或记录不存在。 |
| 429 | 上游限流或配额限制；尊重 Retry-After。 |
| 502 | 上游登录失效、连接失败或协议异常；检查错误中的账号 ID。 |
| 503 | 无账号、全停用、全冷却、全部忙或没有可用模型目录。 |
| 504 | 上游超时。 |
| 507 | 配置或日志无法持久化；检查磁盘和权限。 |

发送失败后隔离相应账号或账号/模型组合，下一个请求再轮询。已经发送的聊天不自动换号重发。
不要用账号池绕过上游封禁或配额。停止接收回答也不保证上游停止计费。
网关不调用 DialX 的历史保存 PUT，但不承诺上游供应商没有自己的日志或计费记录。

## systemd 部署与开机自启

以下命令假设把程序部署到 `/opt/dialx-gateway`，并使用专用用户 `dialx`。

```bash
sudo useradd --system --home /opt/dialx-gateway --shell /usr/sbin/nologin dialx
sudo mkdir -p /opt/dialx-gateway
sudo cp -a app.py requirements.txt .env.example gateway web docs deploy /opt/dialx-gateway/
sudo python3 -m venv /opt/dialx-gateway/venv
sudo /opt/dialx-gateway/venv/bin/pip install -r /opt/dialx-gateway/requirements.txt
sudo cp /opt/dialx-gateway/.env.example /opt/dialx-gateway/.env
sudo chown -R dialx:dialx /opt/dialx-gateway
sudo chmod 600 /opt/dialx-gateway/.env
sudo cp deploy/dialx-gateway.service /etc/systemd/system/dialx-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now dialx-gateway
sudo systemctl status dialx-gateway
```

若用户已经存在，则跳过 useradd。升级部署时不要覆盖现有 `.env` 和 `data/`。
服务文件中的关键设置：

```ini
WorkingDirectory=/opt/dialx-gateway
ExecStart=/opt/dialx-gateway/venv/bin/python /opt/dialx-gateway/app.py
User=dialx
Group=dialx
UMask=0077
```

查看服务日志与重启：

```bash
sudo journalctl -u dialx-gateway -n 100 --no-pager
sudo systemctl restart dialx-gateway
```

远程服务器保持 HOST=127.0.0.1，通过 SSH 转发访问后台：

```bash
ssh -L 8000:127.0.0.1:8000 your-user@your-server
```

然后在本机打开 `http://127.0.0.1:8000/`。不要为了方便直接把无鉴权后台暴露到公网。
反向代理若用于可信网络，需要关闭 SSE 缓冲，并使用足够长的读取超时；仍须自行保护后台路径。

默认 `UPSTREAM_TIMEOUT=1800` 秒，`CONNECT_TIMEOUT=15` 秒。前者控制上游连续无数据的读写等待，不是整次回答的总时长限制。升级不会覆盖已有 `.env`；已有部署需要自行修改该配置并重启。反向代理的读取和发送超时也须同步设置为 `1800s`，避免代理先关闭连接。

### 子路径部署与「API not found」

后台使用相对于页面的资源和 API 地址，支持根路径 `/` 或 `/dialx/` 等子路径。子路径入口必须以 `/` 结尾。代理须去掉此前缀，并把页面、`static/`、`api/admin/` 和 `v1/` 转给同一个 FastAPI 端口。

例如公开入口为 `https://example.com/dialx/` 时，管理接口是 `/dialx/api/admin/overview`，OpenAI 客户端的 `base_url` 是 `https://example.com/dialx/v1`。不要把该站点根路径 `/api/` 或 `/v1/` 全部改到本程序，以免影响同域名的其他服务。

```nginx
location = /dialx { return 308 /dialx/; }
location ^~ /dialx/ {
    # 在此保留已有的访问控制；此示例本身不提供后台鉴权。
    proxy_pass http://127.0.0.1:8000/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

不要使用 `sub_filter` 注入内联脚本来修改 fetch 地址。程序的 CSP 会阻止内联脚本，且本版不需要这些脚本。保留站点已有登录保护；如需独立开放 `/dialx/v1/`，仅为该路径保留调用 Key 校验，不应开放管理接口。

若出现「API not found」，先在部署机器请求实际端口的 `/healthz` 和 `/api/admin/overview`，再检查浏览器的失败请求是否保留子路径。这条错误也可能来自同域名的其他 API 服务；不要通过替换 Cookie 处理路由错误。

## 测试与打包

源仓库包含测试与真实脱敏回放帧；发行 ZIP 只包含运行所需程序和说明。

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -q
venv/bin/ruff check .
venv/bin/python scripts/package.py /tmp/dialx-openai-gateway.zip
```

打包脚本使用文件白名单。不包含 `.env`、venv、缓存、数据库、日志、Git 记录或真实捕获。
ZIP 不包含任何可用的 DialX Cookie；运行后请自行添加本人的账号。

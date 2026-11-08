# AI智能客服

## rtc_basic2.0版

- 暗月红主题，文字与语音共用聊天记录；用户消息靠右，机器回复靠左。
- 增加账号登录、会话鉴权与独立的 RTC 身份配置。
- 后端继续使用 Python FastAPI，前端版本号为 `2.0.0`。

- AI智能客服对话场景下，火山引擎 AIGC-RTC Server 云端服务，通过整合 RTC 音视频流处理，ASR 语音识别，大模型接口调用集成，以及 TTS 语音生成等能力，提供基于流式语音的端到端AIGC能力链路。
- 用户只需调用基于标准的 OpenAPI 接口即可配置所需的 ASR、LLM、TTS 类型和参数。火山引擎云端计算服务负责边缘用户接入、云端资源调度、音视频流压缩、文本与语音转换处理以及数据订阅传输等环节。简化开发流程，让开发者更专注在对大模型核心能力的训练及调试，从而快速推进AIGC产品应用创新。     
- 同时火山引擎 RTC拥有成熟的音频 3A 处理、视频处理等技术以及大规模音视频聊天能力，可支持 AIGC 产品更便捷的支持多模态交互、多人互动等场景能力，保持交互的自然性和高效性。 

> 项目后端统一使用 `rag_llm_server/` 中的 Python FastAPI 服务。Node.js 和根目录的 `node_modules/` 用于前端开发、测试和构建。

## 目录说明

```text
rtc_basic/
├─ src/                    # React + TypeScript 前端
├─ public/                 # 前端静态资源
├─ rag_llm_server/         # FastAPI 后端
│  ├─ main.py              # HTTP API 与 RTC / LLM 回调
│  ├─ config.py            # 环境变量配置
│  ├─ services/            # Token、LLM、RAG 等服务
│  ├─ pyproject.toml       # Python / uv 依赖定义（推荐使用）
│  └─ requirements.txt     # 基础 pip 依赖清单
├─ package.json            # 前端依赖与脚本
└─ package-lock.json       # 前端依赖锁定文件
```

## 环境要求

- Node.js 16 或更高版本（用于前端开发、构建）
- Python 3.13 或更高版本（与 `rag_llm_server/pyproject.toml` 一致）
- PostgreSQL（本机开发默认 `localhost:5432`，独立数据库 `rtc_basic`）
- 建议安装 [uv](https://docs.astral.sh/uv/) 管理 Python 依赖；也可使用 pip
- 已开通火山引擎 RTC、ASR、TTS 和方舟大模型等所需服务

## 配置后端

进入 `rag_llm_server`，参考 `.env.example` 创建或补充 `.env` 文件。该文件包含密钥，已被 Git 忽略，请勿提交；已有云端配置请保留。

```env
# 火山引擎账号 AK/SK：用于调用 RTC OpenAPI
VOLC_ACCESS_KEY=your_access_key
VOLC_SECRET_KEY=your_secret_key

# RTC 应用配置
RTC_APP_ID=your_rtc_app_id
RTC_APP_KEY=your_rtc_app_key
# 单人联调可改用控制台临时 Token；与上面的真实 AppKey 区分。
RTC_STATIC_TOKEN=
RTC_ROOM_ID=
RTC_USER_ID=
RTC_TASK_ID=ChatTask01

# 方舟大模型配置
ARK_ENDPOINT_ID=your_ark_endpoint_id
ARK_API_KEY=your_ark_api_key
ARK_BASE_URL=your_ark_base_url

# 意图识别与 query 改写小模型；独立配置，不会覆盖主回答模型
# 留空时复用 ARK_API_KEY；不同的方舟账号可填写各自的 Key
INTENTION_API_KEY=
INTENTION_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
INTENTION_MODEL=doubao-seed-2-0-mini-260428
INTENTION_TIMEOUT=5
INTENTION_ENABLED=true

# RTC 服务可访问的回调根地址；线上环境必须使用公网 HTTPS 地址
SERVER_URL=https://your-public-domain.example.com

# 登录数据库：填写实际数据库密码，URL 中的特殊字符需进行 URL 编码。
DATABASE_URL=postgresql+psycopg://postgres:YOUR_DATABASE_PASSWORD@localhost:5432/rtc_basic
JWT_SECRET_KEY=your_random_secret_at_least_32_bytes
RTC_CALLBACK_SECRET=your_separate_random_callback_secret_at_least_32_characters
AUTH_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
AUTH_COOKIE_SECURE=false
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your_initial_password_at_least_12_characters

# 以下知识库配置可选；未配置 API Key 时会跳过 RAG 检索
KNOWLEDGE_BASE_API_KEY=
KNOWLEDGE_BASE_DOMAIN=api-knowledgebase.mlp.cn-beijing.volces.com
KNOWLEDGE_BASE_PROJECT=default
KNOWLEDGE_BASE_COLLECTION=rtc_ai
KNOWLEDGE_BASE_LIMIT=1
KNOWLEDGE_BASE_TIMEOUT=10
```

`SERVER_URL` 会作为 CustomLLM 回调地址的一部分。RTC 云端必须能访问它，因此不能在部署环境中填写仅本机可访问的 `localhost` 地址。

RTC 凭证支持两种方式：

- 使用真实 `RTC_APP_KEY` 时，后端为不同登录会话签发独立房间与用户的 Token。
- 使用 `RTC_STATIC_TOKEN` 时，后端直接下发控制台 Token，并读取它绑定的 RoomId 和 UserId；`RTC_ROOM_ID`、`RTC_USER_ID` 留空即可，填写时必须与 Token 一致。固定身份只适合单人联调，多人部署应使用真实 AppKey 动态签发。
- 兼容旧配置：如果原 `RTC_APP_KEY` 中放的是以 `001` 开头的完整临时 Token，会按临时 Token 处理，避免把它再次当作 AppKey 签名。临时 Token 到期后需重新从控制台生成。
- 每次点击“开始对话”都会重新获取 RTC 配置；进房或启动智能体失败时，会释放连接并显示错误，随后可以重试。

第三方模型继续通过 `CustomLLM` 回调进入本地意图识别、RAG 和模型服务。开发时可保留 ngrok，
将 `SERVER_URL` 设为实际 HTTPS 隧道地址，并启动指向后端 `3001` 端口的隧道。未配置可达的回调地址时，
本地调试接口仍可使用，云端语音任务会返回明确的配置错误，不会把本机地址下发给 RTC。

## 启动后端（Python）

推荐使用 uv：

```shell
cd rag_llm_server
uv sync
uv run python manage.py init-db
uv run python main.py
```

也可以在项目根目录运行 `npm run server:start`，该命令会进入 `rag_llm_server/` 并通过 uv 启动同一个 Python 服务，需要已安装 uv。

或使用 pip：

```shell
cd rag_llm_server
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install "volcengine-python-sdk[ark]>=5.0.3"
python manage.py init-db
python main.py
```

服务默认监听 `0.0.0.0:3001`，并提供以下接口：

| 接口 | 用途 |
| --- | --- |
| `POST /api/auth/login` | 账号密码登录，设置 JWT / Refresh / CSRF Cookie |
| `GET /api/auth/me` | 获取当前账号，未登录返回 401 |
| `POST /api/auth/refresh` | 轮换刷新令牌并续期 Access JWT |
| `POST /api/auth/logout` | 撤销当前登录会话并清理 Cookie |
| `POST /getScenes` | 返回前端场景与 RTC 进房信息 |
| `POST /proxy?Action=StartVoiceChat` | 启动 RTC 语音对话任务 |
| `POST /proxy?Action=StopVoiceChat` | 停止 RTC 语音对话任务 |
| `POST /api/chat_callback` | 供 RTC 调用的 OpenAI 兼容 SSE 大模型回调 |
| `POST /debug/chat` | 本地调试意图识别、RAG 与大模型，附带 intention 结果 |
| `POST /debug/intention` | 仅调试 query 改写和回复形式识别 |
| `GET /debug/rag?query=...` | 本地查看 RAG 检索结果 |

## 账号登录与访问拦截

登录页位于 `/login`，沿用暗月红主题并适配手机。未登录访问页面会跳转登录，登录成功后返回原地址。
第一版不开放自助注册；账号由服务端管理员创建。网页账号和 PostgreSQL 数据库账号是两套独立账号。

首次执行 `manage.py init-db` 会创建数据库、执行 Alembic 迁移，并在空用户表中使用 `.env` 的
`ADMIN_USERNAME` / `ADMIN_PASSWORD` 创建首次账号。密码仅以 Argon2 哈希入库；修改 `.env`
不会重置已有账号。数据库用户需要建库权限才能执行 `init-db`；生产环境可由管理员预建数据库，
后端启动时会自动执行待应用的迁移。每次部署可先运行 `uv run alembic upgrade head`。

两个服务端密钥应分别随机生成，例如运行两次：

```shell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

在 `rag_llm_server` 下管理账号（密码通过交互输入，不写在命令行）：

```shell
uv run python manage.py create-user yourname
uv run python manage.py set-password admin
```

账号为 3–32 位字母、数字、下划线、短横线或点，忽略大小写；新密码需为 12–128 位。
重置密码会立即撤销该账号的所有登录会话。

- Access JWT 默认 15 分钟；前端遇到 401 会自动刷新并重试一次，同一浏览器的多个页面通过 Web Locks 协调刷新。
- 勾选“保持登录 7 天”时使用持久 Cookie，刷新会话有固定的 7 天绝对有效期；未勾选则使用浏览器会话 Cookie，最长 12 小时。
- Access / Refresh 凭证使用 HttpOnly Cookie，不写入 localStorage。刷新令牌只保存哈希，每次使用后轮换；重复使用旧令牌会撤销该登录会话。
- 后端除校验 JWT 签名、有效期、issuer / audience 外，还检查会话撤销与账号状态，因此退出和封禁立即对接口生效。
- Cookie 认证的写入请求需携带 `X-CSRF-Token`，值取自可读取的 `rtc_csrf` Cookie；前端请求层自动处理。
- 登录请求以 PostgreSQL 计数，每 15 分钟每账号最多 10 次、每来源 IP 最多 40 次，超限返回 429 和 Retry-After。仅信任真实连接 IP；反向代理需正确配置可信代理，不能信任任意客户端提供的转发头。
- `/getScenes`、`/proxy` 和 `/debug/*` 均要求登录；RTC 房间和语音任务按登录会话隔离，前端只能发起开始/停止语音操作。
- RTC 云端调用 `/api/chat_callback` 使用独立的回调凭证，后端下发回调 URL 时自动附加；不使用网页 Cookie。应用关闭默认访问日志；反向代理也应隐藏该路径的 query string，避免记录回调凭证。

本地前后端请统一使用 `localhost` 或统一使用 `127.0.0.1`，避免混用主机名导致 Cookie 无法共享。
局域网使用时，把前端的完整 Origin 加入 `AUTH_ALLOWED_ORIGINS`。前端可在根目录 `.env.local`
配置 `REACT_APP_API_BASE_URL`；开发默认请求当前页面主机的 `3001` 端口，生产默认同源。
生产建议通过 HTTPS 同源部署，将 `/api/`、`/getScenes`、`/proxy`、`/debug/` 转发到 FastAPI，
静态路由使用 `try_files $uri /index.html`，并设置 `AUTH_COOKIE_SECURE=true` 和实际前端 Origin。

运行验证：

```shell
# 项目根目录
npm test -- --watchAll=false --runInBand
npm run build
# rag_llm_server 目录
uv run python -X utf8 -m unittest discover -s tests
```

账号集成测试使用真实 PostgreSQL：默认创建 `DATABASE_URL` 数据库名加 `_test` 的独立测试数据库，
每次使用并清理独立测试 schema，不访问开发账号表。可用 `AUTH_TEST_DATABASE_URL` 覆盖，数据库名必须以 `_test` 结尾。
RTC / LLM 外部服务在测试中被替换，不产生云端调用。

## Query 改写与意图识别

业务逻辑位于 `rag_llm_server/services/intention.py`，复用项目已有的方舟 SDK，
通过 Responses API 调用 `doubao-seed-2-0-mini-260428`，固定设置 `thinking.type=disabled`。
模型地址和密钥由 `INTENTION_*` 配置控制，与主回答模型 `ARK_ENDPOINT_ID` 分开。
`INTENTION_API_KEY` 未填写时复用 `ARK_API_KEY`，`INTENTION_BASE_URL` 默认使用北京方舟地址。

RTC 回调与 `/debug/chat` 都按“意图识别/改写 → RAG 检索 → 主模型文字回复”执行。
小模型结合近期历史补全指代，改写结果仅用于检索，主模型仍接收原始对话。
小模型只读取文字，识别的是用户希望获得的回复形式，无需上传图片或视频。
例如，“视频课多少钱”“写视频脚本”属于 `text`，“发课程表图片”属于 `image`，
“播放课程录像”属于 `video`。

可向 `POST /debug/intention` 发送：

```json
{
  "history": [
    {"role": "user", "content": "我想了解 Python 课程"},
    {"role": "assistant", "content": "你想了解课程的哪些方面？"}
  ],
  "question": "它多少钱？"
}
```

预期返回格式（改写文本由模型决定）：

```json
{
  "rewritten_query": "Python 课程多少钱？",
  "output_type": "text",
  "response_type": "text",
  "used_fallback": false
}
```

`output_type` 保留用户期望的 `text/image/video`；当前尚未接入图片/视频业务，
实际 `response_type` 固定为 `text`，主模型会被要求说明媒体能力暂不支持。
后续增加媒体处理时，可按 `output_type` 分流，更新实际回复形式。
超时（默认最多 5 秒）、请求错误或返回格式不合法时，以原问题继续文字链路，
`output_type=text`、`used_fallback=true`；此时不代表模型确认了文字意图。
设置 `INTENTION_ENABLED=false` 可跳过小模型调用。

运行后端相关测试：在 `rag_llm_server` 下执行 `uv run python -X utf8 -m unittest discover -s tests`。

## 启动前端

在项目根目录执行：

```shell
npm ci
npm run dev
```

开发服务会启动 React 页面，先打开 `/login`。前端开发时默认请求 `http://<当前页面主机名>:3001`，因此请先启动 Python 后端；局域网访问时，还需把前端地址加入后端 `AUTH_ALLOWED_ORIGINS`。

构建生产静态资源：

```shell
npm run build
```

构建产物输出到 `build/`，可由 Nginx、CDN 或任意静态文件服务器托管。

## 语音与文字交流

页面使用暗月红主题。点击“开始对话”加入现有 RTC 会话后，可以继续语音交流，也可以在底部输入框中发送文字；关闭麦克风后仍可输入文字。

- 点击“发送”或按 Enter 发送，Shift + Enter 换行；中文输入法选词时的回车不会发送。
- 每次最多输入 199 字，前端按 RTC 协议为没有句末标点的消息补上句号。
- 文字复用当前会话的 `ExternalTextToLLM` 指令，AI 回复仍通过现有语音和字幕通道返回；发送文字时会自动打开字幕。
- 发送失败会保留输入，方便重试。此功能不需要修改 Python 后端、模型或知识库配置。

协议说明：[火山引擎：传入文本直接提问](https://docs.volcengine.com/docs/real_time_communication/Directquestionsinincomingtext?lang=zh)。

运行文字交互测试：`npm test -- --watchAll=false --runInBand`。

## 关于 `node_modules`

`node_modules/` 只包含前端的本地依赖，不属于 Python 后端，也不应提交到 Git。它是以下操作所必需的：

- `npm run dev`：启动前端开发环境
- `npm run build`：构建前端静态文件
- `npm test`、代码检查和格式化

因此它可以在需要释放磁盘空间时删除，但删除后上述命令无法运行，直到重新执行 `npm ci` 或 `npm install`。已经构建并部署的静态文件在运行时不依赖本机的 `node_modules/`。

## 常见排查

- 浏览器无法使用麦克风或摄像头：请通过 `https` 或 `localhost` 访问，且确认已授予浏览器设备权限。
- 页面停在“AI 准备中”：检查 RTC、ASR、TTS、方舟模型权限及 `.env` 中的 App ID、密钥和模型配置。
- RTC 无法获得大模型回复：确认 `SERVER_URL` 是 RTC 可访问的公网地址，并检查 `/api/chat_callback` 的服务日志。
- `token_error`：临时 Token 必须使用它绑定的房间号、用户 ID，且尚未过期；不要用临时 Token 作为 AppKey 重新签名。真实 AppKey 动态签发时，核对 App ID 与 AppKey 是否属于同一应用。
- ngrok 返回 `ERR_NGROK_3200`：对应隧道未在线，启动隧道并确认映射到后端 `3001` 端口。`ERR_NGROK_9009` 表示代理环境变量触发了 ngrok 的付费代理限制，可仅为 ngrok 进程移除 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 后重试。

## 相关文档

- [火山引擎 RTC 文档](https://www.volcengine.com/docs/6348/66812)
- [RTC AIGC 场景文档](https://www.volcengine.com/docs/6348/1310537)

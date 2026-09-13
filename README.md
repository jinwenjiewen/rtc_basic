# 交互式 AIGC RTC Demo

这是一个基于火山引擎 RTC 的实时语音对话示例。项目由 React 前端和 Python（FastAPI）后端组成：浏览器采集音频并通过 RTC 加入房间，Python 服务负责签发 RTC Token、调用 RTC OpenAPI，以及将大模型流式回复转发给 RTC。

> 旧版 Node.js/Koa 后端目录 `Server/` 已移除，当前后端为 `rag_llm_server/`。前端仍须使用 Node.js 依赖进行开发和构建。

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
- 建议安装 [uv](https://docs.astral.sh/uv/) 管理 Python 依赖；也可使用 pip
- 已开通火山引擎 RTC、ASR、TTS 和方舟大模型等所需服务

## 配置后端

进入 `rag_llm_server`，创建 `.env` 文件。该文件包含密钥，已被 Git 忽略，请勿提交。

```env
# 火山引擎账号 AK/SK：用于调用 RTC OpenAPI
VOLC_ACCESS_KEY=your_access_key
VOLC_SECRET_KEY=your_secret_key

# RTC 应用配置
RTC_APP_ID=your_rtc_app_id
RTC_APP_KEY=your_rtc_app_key

# 方舟大模型配置
ARK_ENDPOINT_ID=your_ark_endpoint_id
ARK_API_KEY=your_ark_api_key
ARK_BASE_URL=your_ark_base_url

# RTC 服务可访问的回调根地址；线上环境必须使用公网 HTTPS 地址
SERVER_URL=https://your-public-domain.example.com

# 以下知识库配置可选；未配置 API Key 时会跳过 RAG 检索
KNOWLEDGE_BASE_API_KEY=
KNOWLEDGE_BASE_DOMAIN=api-knowledgebase.mlp.cn-beijing.volces.com
KNOWLEDGE_BASE_PROJECT=default
KNOWLEDGE_BASE_COLLECTION=rtc_ai
KNOWLEDGE_BASE_LIMIT=1
KNOWLEDGE_BASE_TIMEOUT=10
```

`SERVER_URL` 会作为 CustomLLM 回调地址的一部分。RTC 云端必须能访问它，因此不能在部署环境中填写仅本机可访问的 `localhost` 地址。

## 启动后端（Python）

推荐使用 uv：

```shell
cd rag_llm_server
uv sync
uv run python main.py
```

或使用 pip：

```shell
cd rag_llm_server
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install "volcengine-python-sdk[ark]>=5.0.3"
python main.py
```

服务默认监听 `0.0.0.0:3001`，并提供以下接口：

| 接口 | 用途 |
| --- | --- |
| `POST /getScenes` | 返回前端场景与 RTC 进房信息 |
| `POST /proxy?Action=StartVoiceChat` | 启动 RTC 语音对话任务 |
| `POST /proxy?Action=StopVoiceChat` | 停止 RTC 语音对话任务 |
| `POST /api/chat_callback` | 供 RTC 调用的 OpenAI 兼容 SSE 大模型回调 |
| `POST /debug/chat` | 本地调试大模型与 RAG |
| `GET /debug/rag?query=...` | 本地查看 RAG 检索结果 |

## 启动前端

在项目根目录执行：

```shell
npm ci
npm run dev
```

开发服务会启动 React 页面。前端默认请求 `http://<当前页面主机名>:3001`，因此本地开发时请先启动 Python 后端；局域网访问时，也应让前端页面和 Python 服务使用同一台可访问的主机。

构建生产静态资源：

```shell
npm run build
```

构建产物输出到 `build/`，可由 Nginx、CDN 或任意静态文件服务器托管。

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
- `token_error`：核对 RTC App ID、App Key、房间号、用户 ID 与 Token 是否来自同一配置。

## 相关文档

- [火山引擎 RTC 文档](https://www.volcengine.com/docs/6348/66812)
- [RTC AIGC 场景文档](https://www.volcengine.com/docs/6348/1310537)

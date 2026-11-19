# rtc_basic3.0版：Harness + Agent Loop 后端

3.0 发布包只包含本目录中的新版后端；已退役的 `rag_llm_server` 不再随代码发布。当前产品启动使用独立配置，历史 M0～M3 迁移探针如需读取旧源码，须由操作人员另行显式提供。下文的历史验收与退役记录描述当时的状态。

本目录包含 M0 验证工具、M1 两层运行时、M2 上下文、M3 只读知识检索及 M4 正式 React/登录接入。`client/serve/text` 保留独立开发入口；`product` 使用现有 PostgreSQL 账号、Cookie/CSRF 授权并同源托管 React，文字和 RTC 都进入同一个 Agent Loop 实现。2026-11-17 已执行本机旧业务服务退役：独立配置、默认启动和前端均切换至本目录，旧服务默认启动被封锁。当前仍为单 worker、本机运行，对外生产发布另行配置。设计与实际验证边界见 [主设计文档第 19～24 节](../docs/rtc-agent-backend-design.md)。

## 旧服务已退役

用户明确授权退役后，已移除常规 `server:legacy` 与正式前端 `/proxy` 回退，旧 `main.py` 默认脚本/ASGI 加载在 SDK 或数据库导入前退出；Windows 未发现该旧服务的运行进程或自动启动项。原代码和配置保留，新产品不导入或读取旧目录。退役后 468 项 Python、34 项开发页 Node、27 项 React 测试及构建通过，真实账号与 Pro/Flash 三轮 HTTP 文字复验通过。详见 [退役记录](../docs/legacy-service-retirement.md) 和 [脱敏报告](validation_results/2026-11-17-m4-retirement.json)。

## 当前状态：M4 正式 React、登录、文字和 RTC 基础验真通过

2026-11-17：462 项 Python、34 项开发页 Node、25 项正式 React 测试通过，TypeScript 和 React 生产构建通过。现有 PostgreSQL 账号登录/刷新/注销及真实 Pro、Flash 共 3 轮 HTTP 文字调用通过。正式浏览器另完成文字回复、新对话和 RTC 验真：3 个独立云任务、8 轮输入，7 轮完成、1 轮 `user_interrupt` 取消，模型失败 0 轮；全部完成轮次在原 9000ms 首输出预算内，最大 8767.199ms。用户确认开麦识别、声音及 Pro 正常。取消单列，全部历史失败保留，不称为生产 SLO。见 [M4 脱敏报告](validation_results/2026-11-17-m4.json)、[浏览器/真人 RTC 记录](validation_results/2026-11-17-m4-browser-01.json) 与设计第 23 节。

正式 RTC 接口与前端生命周期已接入，用户随后补齐 AppKey，已写入本地忽略文件并清空静态 Token；真实 SDK 进房与语音已基础通过。缺配置时页面仍明确显示语音不可用，不将 M0 固定房间临时 Token 交给不同账号。测试通话已回收，仅撤销本次账本绑定的测试登录；本机预览及受限回调隧道保留运行。后续已按用户指令执行本机旧服务退役；生产部署、完整发布回归和性能仍与基础验收分开。

从项目根目录运行，先确认端口上的身份和活动会话：

```powershell
uv sync --locked --project backend_harness_loop
npm run build
npm run server:start
```

打开 `http://127.0.0.1:3101/login`，使用原账号；文字页为 `/chat`。开发 React 可另用 `npm start`（默认连接 3101）。`server:start` 引用本机已有的独立账号、RTC、ASR/TTS、模型、知识库与业务规则配置文件；新机器先按模板准备，勿覆盖已有文件。产品默认账本为 `.local/product-runtime.sqlite3`，已接续原 M4 账本等价副本，原文件保留。

`--auth-env-file` 默认 `.local/product-auth.env`，读取原有 `DATABASE_URL/JWT_SECRET_KEY` 与 Cookie 设置，模板见 [product_auth.env.example](product_auth.env.example)。`--env-file` 默认 `.local/product-rtc.env`，见 [RTC 模板](product_rtc.env.example)；`--voice-config` 默认 `.local/voice-profile.json`，见 [ASR/TTS 模板](voice_profile.example.json)。产品只检查现有账号表结构，不迁移、建表或覆盖管理员/密码；新账号库需由操作人员另行准备。正式命令要求 PostgreSQL；隔离 SQLite 账号库仅用于自动测试。

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop product --knowledge-config backend_harness_loop/.local/m3-knowledge.json --knowledge-env-file backend_harness_loop/.local/m3-knowledge.env --business-policy backend_harness_loop/.local/m3-business-policy.json --port 3101
```

`product` 只绑定 loopback；相应 3101 本地 Origin 与配置中明确允许的 Origin 可访问，生产 HTTPS、Secure Cookie 和发布流程留到部署阶段。正式前端遇到旧场景协议直接拒绝，不自动回退旧服务；旧代码的明确人工恢复边界见退役记录。产品不安装 `/m0`、`/m1/text`、开发 nonce 管理或通用 `/proxy`。

语音使用显式 `--public-url` 或 `product-rtc.env` 的 `SERVER_URL`。本机当前隧道仍映射至 3101，地址同时保存在忽略文件 `.local/m4-public-url.txt`。临时地址变化后需更新配置并在空闲时重启；Start 会校验公网 health 的实例身份，不能复用已失效的地址。两项地址均为空时只提供登录和文字。公网隧道 Host 未作为浏览器 Origin 授权时，只开放 health 与 RTC 认证回调，登录和前端仍仅在允许的 Host 上提供。

会话持久绑定账号与本次登录；注销立即撤销本地执行，外部账号变动以 2 秒间隔巡检发现并清理。文字/RTC 分别最多 4 个活动会话，进程内各最多 128 个，租期最多 600 秒、心跳失联 60 秒回收；达到容量后明确拒绝。RTC 需干净的公网 HTTPS `--public-url`、真实 AppKey 和云端/ASR/TTS 配置；缺配置保持文字可用。静态 Token 继续只用于开发验证入口。

最小真实 HTTP 自测使用新报告名，读取本地原管理员登录信息且不输出；只清理自建会话：

```powershell
python -m backend_harness_loop.probes.product_smoke --database backend_harness_loop/.local/product-runtime.sqlite3 --output backend_harness_loop/.local/m4-product-new.json
```

报告会分开保存真实模型、隔离脚本测试、首文字样本和保留项。不会保存账号/密码、Cookie、Token、Key 或原始资料；HTML 返回 200 不单独作为浏览器交互通过的证据。

## M3 历史检查点：基础跑通完成

2026-11-16 用户决定不再补齐 M2 剩余性能验收，直接进入 M3。历史 9 秒超长导入失败、RTC 量化缺口和失败追踪均保留，不改成性能通过。见 [M2 验收决定](validation_results/2026-11-16-m2-acceptance.json)。

M3 已接入模型自主调用的 `knowledge_search`，复用完整原生工具链、持久账本、取消和容量校验。配置缺失时不提供该能力；没有每轮强制搜索或报名/下单等写工具。主模型在会话内固定，知识库侧查询改写、重排按用户最终决定先关闭。默认仍为 9 秒首输出、单工具 1.5 秒、4 次模型调用、8 次工具调用、30 秒轮次期限。最终阶段使用独立规则，当前 Pro / Flash 显式发送空工具清单与 `none`，保留控制文本保护，不增加重试。

**当前检查点：451 项 Python / 34 项 Node 通过。** 用户明确改为不报价、确认报名意向，并要求简单跑通后准备 M4。当前规则不输出总价/价格区间/优惠金额或折扣数字；尚无真实报名入口，只确认意向，不生成链接或声称已下单。自然文字 6/6 通过，真实 RTC 两型号业务主路径及打断指令跑通。额外回调缺失、9 秒超时和此前协议/字段失败均保留，不称为全量稳定性或生产时延通过。详见 [M3 收尾报告](validation_results/2026-11-16-m3-closure.json)、设计文档第 22 节及 [M4 接入准备](../docs/m4-entry-plan.md)。

## M3：独立知识库配置与文字入口

接口、资源和密钥引用的模板为 [knowledge_config.example.json](knowledge_config.example.json)，密钥模板为 [knowledge_keys.env.example](knowledge_keys.env.example)。本机已准备忽略文件 `.local/m3-knowledge.json` 和 `.local/m3-knowledge.env`，真实 Key 只保存在后者。不要覆盖已存在的本地配置。

JSON 中分别配置 `adapter`、HTTPS `base_url`、相对 `search_path`、`resource.collection/project`、`api_key_env`、`limits.max_hits/timeout_ms`。密钥文件只需提供所引用的变量；进程环境变量优先，不修改 `os.environ`。使用 JSON 时，资源以 JSON 为准，密钥文件中的旧资源字段不生效。模型配置与模型密钥继续使用各自原有文件。业务答复规则另见 [business_policy.example.json](business_policy.example.json)，本地文件为 `.local/m3-business-policy.json`；换知识库不会撤销不报价规则。

从项目根目录运行以下文字服务，先核对端口上的服务身份和活动会话：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop text --models-file backend_harness_loop/model_profiles.json --model-env-file backend_harness_loop/.env --knowledge-config backend_harness_loop/.local/m3-knowledge.json --knowledge-env-file backend_harness_loop/.local/m3-knowledge.env --business-policy backend_harness_loop/.local/m3-business-policy.json --database backend_harness_loop/.local/m3-text-runtime.sqlite3 --port 3101
```

打开 `http://127.0.0.1:3101/m1/text`，选择 Pro / Flash 并建立会话。`serve` 接受同样的知识库参数；RTC、模型与知识库凭据仍分别读取。`m1-doctor` 也接受两项知识库参数，仅输出无密钥配置清单，不证明云端调用成功。

下次更换同协议的知识库，修改 JSON 的接口和资源、更新本地 Key 后重启即可。当前内置 `viking_knowledge`。不同协议实现 `knowledge.py` 中的 `KnowledgeBackend`（`config`、异步 `search` / `aclose`，返回 `KnowledgeHit`），用 `register_knowledge_adapter` 注册后在 JSON 选择；无需修改 `knowledge_search`、主模型 Loop、来源/容量/取消处理。新 Backend 应提供稳定来源身份、可取消 I/O 和有界结果，避免把 SDK 或业务编排导入运行时。

细项文字探针使用独占端口、新 SQLite 与新报告路径；已存在报告不覆盖，所有自建会话和服务在结束时关闭。`fixture` 是**真实主模型＋合成知识 HTTP 响应**；`real` 是**真实主模型＋真实知识服务**，分别记账。当前默认 `enrollment` 套件验证不报价引导；历史 `legacy_quote` 仅在显式启用 `evidence_only` 业务配置时可运行，其历史失败不改写。

```powershell
python -m backend_harness_loop.probes.knowledge --mode fixture --database backend_harness_loop/.local/m3-fixture-new.sqlite3 --output backend_harness_loop/.local/m3-fixture-new.json --port 3103
python -m backend_harness_loop.probes.knowledge --mode real --knowledge-config backend_harness_loop/.local/m3-knowledge.json --knowledge-env-file backend_harness_loop/.local/m3-knowledge.env --business-policy backend_harness_loop/.local/m3-business-policy.json --database backend_harness_loop/.local/m3-real-new.sqlite3 --output backend_harness_loop/.local/m3-real-new.json --port 3103
```

快速检查已启动服务可使用 `python -m backend_harness_loop.probes.business_smoke --transport text --database <该服务数据库> --output <新报告路径>`；RTC 方式使用 `--transport rtc --rtc-ledger <该服务.sessions.json>`。后者创建并清理自己的真实云任务，经 `ExternalTextToLLM` 触发业务回调和打断指令，不冒充麦克风或人耳采样。默认两步覆盖课程检索与不报价引导，`--steps 3` 另查退款；记录实际 9000ms 期限及失败，不自动重试。

检索失败、无结果和超时分别保存终态。`kb:` 引用只证明实际成功片段的来源身份，不能自动证明报价适用或资料仍有效。当前工具没有访问任意 URL、图片/附件下载、报名或订单能力；资料不足时应明确未确认。原始业务资料留在忽略的本地账本，脱敏报告仅包含哈希、计数、布尔检查、稳定错误码和时间。

## 安装与运行（历史 M0/M1 开发探针）

以下保留原开发验证命令和源码只读回放方式，不是正式服务启动入口；当前产品使用本文顶部的独立配置与 `npm run server:start`。显式 `--legacy-source` 仅通过 AST 读取历史语音字面量，不加载旧服务；正式产品拒绝该参数。运行探针前应使用独占端口和新报告路径，避免影响当前 3101 产品实例。

要求 Python 3.13+。在项目根目录执行 `uv sync --locked --project backend_harness_loop` 安装锁定依赖；随后使用 `backend_harness_loop/.venv/Scripts/python.exe` 运行模块。也可以暂用已有且满足依赖的 Python 环境。

以下命令均从项目根目录运行，`python` 替换为实际解释器：

```powershell
python -m backend_harness_loop doctor --env-file rag_llm_server/.env
python -m unittest discover -s backend_harness_loop/tests -v
python -m backend_harness_loop offline --output backend_harness_loop/.local/offline.json
python -m backend_harness_loop model --env-file rag_llm_server/.env --samples 3 --output backend_harness_loop/.local/model.json
python -m backend_harness_loop rtc --env-file rag_llm_server/.env --rtc-api-version 2024-12-01 --legacy-source rag_llm_server/main.py --output backend_harness_loop/.local/rtc-stop.json
```

`--env-file` 是显式读取，不会复制或修改旧 `.env`；环境变量优先。缺凭据返回 `blocked`，不会把缺配置当成通过。默认忽略环境代理，需要代理时显式加 `--trust-env`。

当前按已选路线默认使用 `2024-12-01`。旧版 ASR/TTS 参数通过 `--legacy-source` 从指定源码的字面量提取，使用 AST，不导入或执行旧后端；只读取 `ASRConfig/TTSConfig`，不复用其业务编排。动态表达式或多个候选配置会被拒绝。新版 `2025-06-01` 仍可显式选择，但使用独立公共资源配置，不能混用旧资源。

模型探针默认使用主模型和 `thinking.type=disabled`。`M0_MODEL_THINKING=omit/enabled/disabled` 可以控制此参数。实际发现默认思考模式可能与指定工具调用冲突，修改模式后的结果另存，保留首次失败证据。

模型实测每次最多 8 个请求：原生工具调用及结果回填 2 次，加 1 ～ 3 组两阶段回复。所有业务资料为合成夹具，不运行真实业务工具。样本的 P50/P95 仅描述本批样本；后端首有效文本不等于用户听到首音。

## M1：模型配置与启动

模型接口、模型名和密钥分开配置：

- `model_profiles.json`：`providers` 保存 HTTPS `base_url`、`adapter` 与 `api_key_env` 名称；`profiles` 引用 provider 并指定 `model` 和调用选项，不含真实密钥。
- `.env`：只保存被引用的密钥变量。模板为 `model_keys.env.example`，已存在的本地密钥文件不要覆盖。
- Pro / Flash 分别引用独立 provider，可用不同接口与不同密钥，也可引用同一个变量。配置在启动时读取，修改后重启。默认 Flash；会话加入后固定所选 profile，所有决策和最终回复使用该型号，切换需结束后重新加入。
- profile 可配置 `decision_tool_choice=auto|required`，缺省 auto 兼容其它供应商。当前 Pro / Flash 模板和本地配置均显式 required、thinking disabled：主模型选择上下文工具或 `prepare_response`；最终回复仍关闭工具，不指定业务工具或改变预算。DeepSeek 的支持范围见 [官方参数说明](https://api-docs.deepseek.com/api/create-chat-completion/)。旧进程不会热更新，本轮未重启已有 3101 / 3102。

首次准备配置（从项目根目录执行）：

```powershell
if (-not (Test-Path backend_harness_loop/model_profiles.json)) { Copy-Item backend_harness_loop/model_profiles.example.json backend_harness_loop/model_profiles.json }
if (-not (Test-Path backend_harness_loop/.env)) { Copy-Item backend_harness_loop/model_keys.env.example backend_harness_loop/.env }
python -m backend_harness_loop m1-doctor --models-file backend_harness_loop/model_profiles.json --model-env-file backend_harness_loop/.env
```

以后接第三方 OpenAI Chat Completions 兼容服务，只需修改 provider 的 `base_url` / 密钥变量引用和 profile 的 `model`。其他协议需要新增适配器，Agent Loop 的接口保持不变。URL 使用接口根地址（可含 `/v1`），不要在 URL / JSON 里放密钥。

模板请求 `deepseek-v4-pro` 与 `deepseek-v4-flash`。截至本次验证，官方推荐 Flash 标识为 `deepseek-flash`；旧 `deepseek-v4-flash` 仍被接受，但服务已由 V4.1 Flash 承接。模板保留用户指定的兼容标识，第三方型号以其实际提供的名称配置。[官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)

建议打开两个普通 PowerShell 终端，从项目根目录分别以前台命令运行隧道和服务，保留各自输出便于检查启动结果。无需组合 `Start-Process`、重定向和内联脚本。

终端 1：建立指向本机 3101 的 HTTPS RTC 回调入口，关闭请求检查，并保持终端运行：

```powershell
ngrok http 3101 --inspect=false
```

终端 2：将下方 `--public-url` 替换为终端 1 给出的 HTTPS 根地址，再启动 M1。可先设置本轮独立的 `M0_CAPTURE_INSTANCE_ID`，后续公网健康检查使用同一值：

```powershell
$env:M0_CAPTURE_INSTANCE_ID='choose-a-new-local-instance-id'
python -m backend_harness_loop serve --env-file rag_llm_server/.env --overlay-env-file backend_harness_loop/.local/client-credentials.env --legacy-source rag_llm_server/main.py --model-env-file backend_harness_loop/.env --models-file backend_harness_loop/model_profiles.json --database backend_harness_loop/.local/runtime.sqlite3 --public-url https://your-validation-host.example --output backend_harness_loop/.local/m1-capture.jsonl
```

本机 `/healthz` 应返回 `mode: agent_loop`，公网健康检查还需匹配同一 `instance_id`；随后打开 `http://127.0.0.1:3101/m0`。管理页面与管理 API 仅在本机开放，公网 403 是预期行为。验证结束后先在页面结束会话并确认云端任务停止，再分别用 `Ctrl+C` 停止服务和隧道。

RTC 凭据来自显式 RTC 配置；模型密钥来自 `--model-env-file` 和进程环境变量，不继承旧 `ARK_ENDPOINT_ID` 作为模型路由。模型配置不修改进程环境。`--profile deepseek_v4_pro` 可覆盖新会话默认型号。`/m0` 开发页会显示 M1 模式、型号选择及运行状态，沿用 RTC SDK 入房 / 字幕 / 打断能力，默认不开麦。

M1 使用两层循环：控制循环处理新输入、精准取消、断连与退出；模型循环运行“决策 → 工具 → 观察 → 再决策 → 最终文本”。决策草稿与工具 JSON 不进 TTS；每次最终回复关闭工具。默认最多 4 次模型调用（预留最终调用）、8 次原生工具调用（包含 `prepare_response` 和非法参数尝试）、30 秒轮次总期限。`serve` 首有效模型文本预算为 9000ms，M0 模型探针仍为 6000ms。预算通过显式运行环境中的 `M1_*` 配置调整，不能把空白分片当有效首输出。

已接入 `context_read_history`、`context_search_history`、`context_update_notes`。模型选择是否使用，系统校验范围、版本、epoch 与预算。SQLite 原始记录位于忽略的 `.local/`；M1 保存会话原文、完整工具链和交付状态，M0 JSONL 仍只记录元数据。隐藏推理不入库，不当作可播报文本。历史呈现真实发送片段，不把未消费的完整生成当成用户已经听到。

M1 最初采用 20 个历史轮次和 48000 字符的显式保护。M2 已接入下文的模型驱动压缩与后台维护：先计量全部未覆盖历史，再按已提交快照装配；不会先截成 20 轮。产品账号登录及现有 React 页面的登录协议适配仍未完成。当前只运行一个 worker，SQLite 仍是开发阶段存储，PostgreSQL 为后续部署目标。

2026-11-08 初轮 **213 项 Python / 16 项 Node 测试通过**，真实 DeepSeek Pro / Flash 与本地 ASGI 回调验证见 [历史 M1 报告](validation_results/2026-11-08-m1.json)。后续分开以前台命令成功启动隧道与 M1 服务，未修改安全设置；真实 Flash 语音输入、模型回复和打断获得用户功能确认，Pro 补充文字回传。SDK 停音单样本约 295ms，未完成多轮首音或物理延迟基准；RTC 笔记测试仍未完成。RTC 与恢复修正后的检查点为 **231 项 Python / 16 项 Node 通过**，见 [真实 RTC 报告](validation_results/2026-11-08-m1-rtc-live.json) 与设计文档 19.6 ～ 19.7。

## 文字输入：无需 RTC 的同一 Agent Loop

只需模型配置与模型密钥，即可从项目根目录启动本机文字服务：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop text --model-env-file backend_harness_loop/.env --models-file backend_harness_loop/model_profiles.json --database backend_harness_loop/.local/text-runtime.sqlite3 --port 3101
```

打开 `http://127.0.0.1:3101/m1/text`，选择主模型、建立文字会话后自由输入。页面显示流式回复，可打断当前轮次、继续追问和结束会话。此命令无需 RTC 凭据、ASR/TTS、旧后端源码或公网隧道，也不会打开麦克风。`serve` 模式同时提供此页面，RTC 页顶部可进入文字对话。

文字 Adapter 将 user 输入交给同一个 `AgentRuntime`，复用上下文工具、模型决策、预算、epoch、持久化与取消机制；客户端只显示最终文本。文字会话与 RTC 会话各自建身份，当前不自动合并两者的历史。型号在会话创建时固定，接口和密钥仍由同一模型配置读取。

本机 API 为 `/api/m1/text/sessions` 和 `/{session_id}/turns`、`interrupt`、`heartbeat`、`stop`；请求使用页面提供的进程级 `X-M0-Client-Key`，继续限制 loopback、Host 和 Origin。turn body 只有 `round_id` 与 `text`，SSE 区分 `turn`、`delta` 和 `terminal`；中断必须指定轮次，旧轮次不能取消新回复。开发会话有容量与租约限制，退出页面尽力停止，后台回收超时会话。此凭据用于本机开发，不是产品账号登录。

原始文字与工作笔记仍只在忽略的 SQLite 中持久保存，页面和模型调用不把密钥写入日志。自动化可以通过真实 HTTP 入口验证“写工作笔记 → 下一轮回忆 → 历史工具读取”、重复轮次、中断与关闭，不需要人工开口或听音；语音设备及实际停音仍由 RTC 专项验证。

保持上述文字服务运行，在另一终端执行可重复的真实模型自测，`--database` 必须指向该服务使用的数据库：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.text --database backend_harness_loop/.local/text-runtime.sqlite3 --output backend_harness_loop/.local/text-http-probe.json
```

默认测试 Flash、Pro，共 7 个合成轮次；可用 `--profile 配置中的型号ID` 指定一到两个 profile。脚本从本机页读取 nonce，只停止自己创建的文字会话，逐项保留失败结果，不自动重试或切换模型。报告只包含匹配布尔值、工具与状态计数、身份哈希及 HTTP 文本耗时，不输出原文或密钥。真实结果见 [文字联调报告](validation_results/2026-11-08-m1-text-live.json)。

2026-11-08 本轮真实自测全部通过：Flash / Pro 都实际写入笔记并正确回忆，Flash 调用历史工具核对；运行中取消、旧轮次隔离和重复 409 均通过，脚本自建会话全部关闭。另有浏览器 Flash 三轮自由输入验证。当前本地检查为 **247 项 Python / 34 项 Node 通过**；这些结果不等于产品账号登录、完整压缩或物理音频延迟验收。

完整本地检查：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m unittest discover -s backend_harness_loop/tests -q
node --test backend_harness_loop/web/measurements.test.cjs backend_harness_loop/web/m0.test.cjs backend_harness_loop/web/m1.test.cjs backend_harness_loop/web/text.test.cjs
```

## M2：模型驱动的上下文压缩

`text` 和 `serve` 已共用 M2 上下文引擎。普通轮次仍由所选主模型直接进入工具/回复循环；达到容量阈值时，同一 profile 生成有 `seq:N` 来源的结构化摘要。快照和覆盖水位原子提交，原文继续保存在 SQLite，可通过历史工具按引用、关键词或游标读取。

默认保留最近 4 个完整轮次和当前原生工具链；这是压缩保护范围，不是日常读取上限。容量计算先纳入全部未概括历史。估算包含实际消息、工具定义、输出预留和模型协议字段，采用保守 UTF-8 序列化估计，不声称是供应商 tokenizer。

主要配置见 `.env.example`：`M2_CONTEXT_INPUT_TOKENS=24576`、`M2_CONTEXT_WINDOW_TOKENS=32768`、提示/自动阈值 `0.70/0.85`、目标 `0.50`。最初 16384 在固定协议、四轮原文和摘要之后余量不足，按实测调整到 24576，压力样本也由 1200 增为 1800 字节；窗口值是本服务配置，并非模型能力声明。接口/型号/密钥配置方式保持不变。后台默认启用，限同主模型 1 次请求、5 秒、1500 输出 Token，新输入和打断会抢占；前台压缩继续计入原有步骤及首输出预算。

压缩的 `source_refs` 仅保存出处，`keep_refs` 会额外加载原文，通常为空。历史工具按当前剩余窗口分配结果预算：整轮装不下时返回原文片段、`reference`、`truncated` 和 `next_offset` 等续读元数据；不能裁剪当前原生协议，也不能把未加载的原文当成不存在。

本轮用独立端口 3102 验证，不影响之前 3101 的用户会话。为复现前台功能路径，在忽略的 `backend_harness_loop/.local/m2-functional-validation.env` 中写入：

```dotenv
M2_CONTEXT_BACKGROUND=false
M1_FIRST_OUTPUT_DEADLINE_MS=20000
```

这是文字功能验收配置；服务默认仍为 9000ms，长轮在默认预算下的超时记录继续保留，不作为时延通过。再运行：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop text --env-file backend_harness_loop/.local/m2-functional-validation.env --model-env-file backend_harness_loop/.env --models-file backend_harness_loop/model_profiles.json --database backend_harness_loop/.local/m2-text.sqlite3 --port 3102
```

另一终端运行真实 HTTP 自测，数据库路径须与服务相同：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.context --base-url http://127.0.0.1:3102 --database backend_harness_loop/.local/m2-text.sqlite3 --output backend_harness_loop/.local/m2-context.json
```

默认 Flash / Pro 各最多 16 轮、每段约 1800 字节合成压力材料，必须至少两次实际压缩后仍正确回答最新预算、周末否定和订单未提交，并由该轮工具真正返回最初用户记录和预算。笔记版本要求为 0，避免笔记替代摘要验证。探针只停止自己创建的会话，不改政策、不自动重试或更换型号。

2026-11-09 核心压缩验真两型号各 **13 轮、2 次压缩通过**，全部严格语义和原文回查检查通过，笔记版本均为 0；该检查点为 321 项 Python / 34 项 Node。最终文字功能配置用 20 秒预算。原有 8 批失败/成功与配置差异保留在 [M2 报告](validation_results/2026-11-09-m2.json)，本轮分段恢复结果见下文及 [设计文档第 20 节](../docs/rtc-agent-backend-design.md)。

### 超长历史：分段压缩与暂存恢复

维护模型每次处理“上一累计摘要 + 紧接着的闭合历史段”。若新摘要加完整剩余历史仍装不下，写入独立 `context_candidates`，保留父段与连续边界；活动 `context_version`、`compacted_through_seq` 和普通输入均不变。下一次维护从经校验的边界续接，整段视图装得下后才原子提交快照。不会用暂存进度或最大已处理序号冒充活动覆盖。

每段共享当前轮次的模型/工具/时间预算；若本轮预算用完且完整输入仍超限，终态明确返回 `context_maintenance_pending`，已完成段可在下一次文字输入时复用。新输入、打断和关闭会取消正在生成的段，迟到结果不能落库；已完成段只有通过基础版本、笔记、配置和前缀材料指纹校验，才交给新 epoch 的维护模型。SQLite 重开后也按这些条件恢复。后台仍限每次 1 请求 / 5 秒 / 1500 输出 Token。单个闭合轮次、累计摘要或保护区本身装不下时明确失败，保留原文和旧快照。

新增探针自建 **3103 专用文字 HTTP 服务与新数据库**，结束时关闭自己创建的会话和服务，不连接已有的 3101 / 3102。与只读 `probes.context` 不同，本探针只向自己的新数据库导入 48 轮合成历史及脚本确认回复；导入后的压缩、决策、最终回复和工具选择均由真实选定模型完成。数据库及报告路径必须尚不存在：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.segments --database backend_harness_loop/.local/segments-new.sqlite3 --output backend_harness_loop/.local/segments-new.json
```

默认显式使用 20000ms 文字功能预算，输入上限仍为 24576，模型 / 工具 / 总期限仍为 4 / 8 / 30 秒，最多 6 次恢复输入加 2 次核对。可加 `--first-output-ms 9000` 单独测默认期限，结果另存。探针在第一次暂存后重开 SQLite 并清空上下文缓存，以验证落库候选恢复；这不代表产品登录和浏览器会话跨进程重连已实现。

真实功能结果：两型号各导入 48 轮，初始完整输入估算约 9.8 万；经 3 次恢复输入后，Flash / Pro 分别保留 7 / 6 个暂存段并激活 1 次快照，再完成语义与原文核对，共各 5 次文字请求。预算修正、周末否定、订单未提交、原始 5000 与引用检查全部通过，笔记版本为 0。首批因探针统计字段错误仍记失败，修正后按相同预算重跑通过。默认 9 秒专项两型号均超时，已完成的 2 / 1 段保留且未激活，不能称为时延通过。

分段检查点为 **340 项 Python / 34 项 Node 通过**；19 项新回归覆盖连续链、存储恢复、跨轮抢占、迟到拒绝、逐段收益、回滚与探针汇总。详见 [分段恢复脱敏报告](validation_results/2026-11-09-m2-segments.json)。本轮继续补齐下文的独立编辑工具；真实后台性能、生产长会话性能和压缩后 RTC 专项继续保留，未进入 M3。

### 独立修改摘要与选择证据

`context_update` 使用 `expected_context_version` 和 `upserts` 按 ID 更新 `goals/items/open_questions/pending_actions/topic_index`，不要求改写整份摘要。每条包含 `section/id/kind/source_type/source_refs/status` 和 `text/value/topic` 至少一个；新修正可用 `supersedes` 指向已有 ID，旧条目保留并标为 superseded。已有压缩条目未带 ID 时，模型可按资料中的 `summary_entry_ids`（如 `legacy:items:0`）更新。

`context_select` 使用同一版本号和 `keep_refs/release_refs/purpose`。keep 是增量保留，release 只移除显式保留；不会删除账本、压缩历史或去掉近期保护原文。选中的旧工具记录以资料加载，当前原生工具调用和结果继续配对保留。单条证据当前最多加载 3000 字符，截断时携带续读信息。

两个工具都只接受本次模型可见的 `seq:N` 来源，并检查来源角色、会话、epoch、版本和冻结材料。新增视图、当前原生参数和成功结果预留全部参与容量预检；拒绝超容量或失效写入。成功修改和原生工具结果、事件在同一事务保存至 `context_edits`，只递增 `context_version`，不推进覆盖水位或压缩次数。旧笔记工具保留独立的 `notes_version`。同一模型决策批的写调用共享冻结版本，后一个同版写入会冲突，模型须在后续决策中使用最新版本。

新文字探针仍自建 3103 专用服务和全新数据库。每 profile 最多 8 次真实 HTTP 输入；为让前两轮具备闭合压缩条件，另外导入 4 轮合成临时讨论及脚本确认回复。摘要、条目修改、压缩、历史读取、选择、释放与最终回复均由真实选定模型完成，不预置脚本摘要。路径必须未存在：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.edits --database backend_harness_loop/.local/edits-new.sqlite3 --output backend_harness_loop/.local/edits-new.json
```

默认显式使用 20000ms 功能首输出预算，其余仍为 24576 输入、4 次模型、8 次原生工具、30 秒总期限和四轮原文保护。可用 `--profile deepseek_v4_flash` 或 `--profile deepseek_v4_pro` 单测；不会自动重试或切换型号。探针要求真实写入与版本事件一致，验证预算 5000→3000→3200、周末否定、订单待确认，压缩后读原文、保留后实际加载且只加载一次、释放后停止额外加载但原文仍在。仅回答正确或回复“收到”不能替代实际工具成功。

当前输入前加入可信边界，避免把历史或选中原文中的旧编辑请求当作当前任务；最终阶段遵守本轮指定格式。参数被拒绝时，模型收到有界的当前来源/字段/已有 ID 反馈，修正继续计入原预算。真实功能结果与所有失败批次见 [独立编辑脱敏报告](validation_results/2026-11-09-m2-edits.json)；功能验收不等于默认 9 秒或 RTC 性能通过。

当前两型号采用 required 决策协议，防止无原生动作的“收到”草稿直接转入成功回复。模型仍自主选择工具；若供应商违反 required 只给文本，则明确报 `model_action_required`。第三方可按能力配置 auto，旧笔记、压缩和历史接口继续可用。

v0.13 最终第 8 批 Flash / Pro 各 8 轮全部通过：3 次摘要更新、2 次选择/释放、1 次压缩，最终 context_version=6、notes_version=0；新预算 3200 实际持久化，保留与释放通过实际模型请求观察验证。该检查点 **372 项 Python / 34 项 Node 通过**，新增 32 项。全部 8 批及原有历史报告保留，包含早期模型漏调、参数/来源/格式拒绝和一次网络失败；不能把最终样例通过称为任意语义或默认时延通过。后续性能推进见下节，未进入 M3。

## M2：默认预算文字性能

`probes.performance` 自建专用文字 TCP 服务与全新 SQLite，默认端口 3103，已有监听时直接失败，不停止其它服务。每次指定新的数据库和输出文件，旧报告不覆盖：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.performance --database backend_harness_loop/.local/performance-new.sqlite3 --output backend_harness_loop/.local/performance-new.json
```

默认 Flash / Pro 各 28 次真实输入，包括 24 段约 1800 字节压力材料、预算修正、压缩后语义和原文工具回查。长会话每轮等待当次空闲维护结束；另测空闲压缩、新输入抢占、精确打断、关闭。后台专项明确导入 14 个脚本闭合轮次，临时材料每段 850 字节，使压力落在 warn/auto 之间；所有摘要、工具选择和回复仍由真实模型生成。`--scenario long` 或 `--scenario background` 可分别运行，`--turns` 限 20～36，不自动重试或换型号。

探针强制默认 **9000ms 首输出、24576 输入估算上限、4 次前台模型 / 8 次工具 / 30 秒总时限、后台 1 次 / 5 秒 / 1500 输出 Token**。服务端 `model.first_output` 仅记录真实非空白最终文本，计入模型构造、上下文装配、压缩、工具与决策时间；HTTP 首分片另列。失败提示不算模型首输出，后台耗时加上规划时间才与 5 秒比较。

本地存储基准另行运行，不调用云模型，100 / 500 / 1000 个闭合轮次及归档快照均为脚本夹具：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.perf_store --directory backend_harness_loop/.local/store-perf-new --output backend_harness_loop/.local/store-perf-new.json
```

每项保留 3 次样本及首次构造，不丢弃冷样本。完整材料、四轮未覆盖原文、原始账本留存与 SQL 数一并记录。运行时批量读取材料、在一次同步预检内复用同一冻结材料，跨 I/O 重读；HTTP 连接按 profile 复用，每次模型实例和 usage 仍独立。没有闭合旧前缀或工具定义装不下时不提供 `context_compact`；压缩仍由主模型决定内容。

2026-11-10 的 Flash / Pro 各 28 轮长会话通过，模型首文本最大分别 2.602 / 5.904 秒；预算修正、否定、待确认与原文回查正确。最终后台专项 8 个用例通过；长会话另有 1 次未分类后台失败，不能因专项通过而抹去。最终独立编辑另有两型号各 8 轮默认 9 秒复测通过，最慢模型首文本为 8.089 秒。该检查点 **396 项 Python / 34 项 Node 通过**。历史失败、不同夹具/源码/统计口径及本地基准见 [性能脱敏报告](validation_results/2026-11-10-m2-performance.json) 和 [设计文档 20.8](../docs/rtc-agent-backend-design.md)。后续 RTC 功能确认见下节，48 轮超长导入的默认 9 秒失败仍保留。

## M2：功能收尾与压缩后 RTC

2026-11-16 完成 M2 功能收尾：两型号真实 RTC 会话各有 2 次模型生成快照，用户实际开麦、听音并确认回复和打断；状态页显示实际压缩次数。25 个 SDK 会话回调全部关联到状态与字幕，另有 2 个无 SDK 云端控制回调验证历史工具。合计 22 个生成完成、5 个主动取消、0 个运行失败，已记录模型首文本最大 7.761 秒，保持默认 9 秒。

最终文字复验另有 **70 次真实输入全部通过**，两型号各 28 轮长会话及 8 个后台/取消用例；41 次后台成功，含规划最大 4.637 秒，原预算未放宽。代码检查为 **403 项 Python / 34 项 Node 通过**。

最终输出适配器会拒绝内部 DSML 调用标记，包括跨分片标记，防止其进入 TTS；不会把文本标记解释为工具动作。后台候选增加字节/哈希、阶段、异常类别及字段位置诊断，不记录正文或密钥，不增加模型重试。

专用 RTC 压缩夹具服务需显式启动，数据库与输出必须为新路径；RTC 资源和凭据沿用既有本地配置。先准备关闭请求检查的 HTTPS 隧道，再执行：

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m backend_harness_loop.probes.rtc_context --public-url https://your-validation-host.example --database backend_harness_loop/.local/rtc-context-new.sqlite3 --output backend_harness_loop/.local/rtc-context-new.jsonl
```

打开 `http://127.0.0.1:3101/m0`。此专用实例在创建 RTC 会话时导入 14 个合成闭合轮次，不导入摘要；本机探针可通过 `/api/m2/rtc-validation/{session_id}` 的有限步骤追加一次 4 轮压力、发语义/原文核对或长回复，沿用页面进程 nonce 和完整 Host/Origin/loopback 校验。维护或回复未结束时不能追加压力。正常 `serve/text` 不提供这些夹具控制，也不预置合成历史。

**功能确认不等于完整性能验收。** 本轮没有有效的 SDK 首音/停音计时样本；人工确认不代替量化数据。RTC 有一次后台超时（含规划/取消退出 5.007 秒）及两条 `1005004` 事件，历史未分类后台失败和 48 轮导入 9 秒失败也全部保留。`m2_functional_complete=true`，`full_m2_milestone_complete=false`，未进入 M3。全部批次、清理与限制见 [RTC/M2 脱敏报告](validation_results/2026-11-16-m2-rtc.json) 和 [设计文档 20.9](../docs/rtc-agent-backend-design.md)。

## M0：采集真实 RTC 回调

```powershell
$env:M0_CAPTURE_INSTANCE_ID='choose-a-new-local-instance-id'
python -m backend_harness_loop capture --env-file rag_llm_server/.env --port 3101 --output backend_harness_loop/.local/cloud-capture.jsonl
```

采集服务默认仅绑定 `127.0.0.1:3101`。将可访问的 HTTPS 临时隧道映射到该端口，并禁用隧道的完整 HTTP 请求检查 / 记录。`/healthz` 必须返回本实例的 service 与 instance_id。

在另一个终端设置相同的 `M0_CAPTURE_INSTANCE_ID` 后，使用验证实例的 HTTPS 根地址：

```powershell
python -m backend_harness_loop public-health --env-file rag_llm_server/.env --public-url https://your-validation-host.example
python -m backend_harness_loop rtc --env-file rag_llm_server/.env --legacy-source rag_llm_server/main.py --public-url https://your-validation-host.example --start --output backend_harness_loop/.local/rtc-start.json
# Optional: trigger two synthetic text turns and an interrupt on this isolated task.
python -m backend_harness_loop rtc --env-file rag_llm_server/.env --legacy-source rag_llm_server/main.py --public-url https://your-validation-host.example --start --text-probe --output backend_harness_loop/.local/rtc-text.json
```

只有 `--start` 且公网实例校验成功，才会创建独立的 `m0_*` 房间与任务；结束时无论启动是否超时，均对同一任务执行停止清理。普通 `rtc` 命令只观察一个从未启动的独立任务的停止行为。两者都不会操作旧任务。

采集器只返回固定的验证文字和合法 SSE，不接业务模型；JSONL 仅记录字段形状、标识哈希、阶段及时间，不保存对话原文、认证头、签名或带凭据 URL。实际云端 ID 的存在只构成候选关联证据，不能自动证明稳定轮次和重试语义。

## 结果与门槛

机器结果保存到指定的 JSON 文件，原始本机运行日志放在忽略的 `.local/`。公开的验证摘要放入 `validation_results/` 并同步至主设计文档。

报告严格区分离线夹具、模型实测、RTC OpenAPI 接受、云端回调、客户端及音频实测。单个探针成功不代表 M0 整体通过；未验证项继续保持阻塞。单阶段早期文本 / 工具分支承诺默认关闭。

临时隧道、采集进程和云端任务使用结束后停止。若停止无法确认，报告保留本次生成的任务标识供查证，不能把未知结果写成成功。

## 首次新版验证记录（当前已选旧版）

2026-11-08 实测 `2025-06-01` 的 Start 返回 `NoPermissionForApp`。官方说明此错误对应 API 版本与应用类型不匹配：新版使用 AI 音视频互动方案应用，旧版使用实时音视频应用。[错误说明](https://docs.volcengine.com/docs/real_time_communication/CallingtheStartVoiceChatinterfacereportsanerrorNoPermissionForApp?lang=zh)、[迁移指南](https://docs.volcengine.com/docs/real-time-communication/migrate-conversation-ai-to-new-api?lang=zh)。

继续新版验证时，在本目录自己的 `.env` 配置匹配的 `RTC_APP_ID`，或在读取旧 `.env` 时用环境变量 `M0_RTC_APP_ID` 覆盖。后续客户端入房还需来自同一新版应用的 AppKey；不能仅换请求版本后继续使用旧 Token。不要把凭据写入文档或提交 Git。

当前同时提供旧版自有资源与新版公共资源两种配置，按用户选择显式运行，不在云端失败后自动切换 API。未验证的客户端音频和其他验收项不会因 Start 成功被标记通过。

## 有界故障注入

采集器支持显式环境配置，默认均关闭：`M0_CAPTURE_FAIL_FIRST=true` 令首个认证回调返回一次合成 503；`M0_CAPTURE_DELAY_MS` 和 `M0_CAPTURE_CHUNKS` 生成有界慢流（最多 8 个分片、每片间隔最多 2 秒）。这些选项仅用于隔离的 M0 回调服务。

`rtc --start --text-probe` 发两次相同文本，用于观察新轮次；`rtc --start --interrupt-probe` 只发一次文本、等待后打断，再观察断连。两者互斥。配合慢流观察的取消属于服务端 SSE 取消，不等于用户端音频已经停止。

回调元数据中的 `round_id`、状态 `RoundID` 和字幕 `data[].roundId` 通过同一运行下的哈希比较；`evidence.analyze_capture` 汇总实际匹配和重复尝试，不把相同文本当作去重键。

## 旧版实测结果

2026-11-08 的 5 个隔离旧版任务启停与文本指令均成功。最终正常样本收到 2 个不同轮次，均与状态及字幕匹配；没有错误事件。一次合成 503 后观察窗口内没有该轮自动重试，后续新轮正常；最终慢流实验在 3/8 分片时收到取消。这些是服务端观测，不是客户端停音证明。

早期旧模板的 `Method/ApiType` 字段导致未定义参数错误，移除后复测消失。旧版已显式启用 `EnableRoundId`；正式运行以运行 ID 和原生轮次 ID 关联，不能以文本内容判重。

第 18.4 节对应批次的 67 项离线测试通过，当时真实客户端音频尚未验证；后续进展见下节。该批完整证据见 [旧版验证结果](validation_results/2026-11-08-m0-legacy.json)。M0 的 `--legacy-source` 是迁移验证辅助；正式服务会使用新目录独立的语音资源配置，不依赖运行旧项目源码。

## 本地真实客户端验证页

当前新增独立 `/m0` 页面，使用根目录 `node_modules/@volcengine/rtc/index.min.js`（本机安装版本 4.66.35）；须先安装项目 Node 依赖。无需启动产品 React 页面或旧业务服务。

```powershell
python -m backend_harness_loop client --env-file rag_llm_server/.env --overlay-env-file backend_harness_loop/.local/client-credentials.env --legacy-source rag_llm_server/main.py --public-url https://your-validation-host.example --output backend_harness_loop/.local/client-live-capture.jsonl
```

先建立指向 3101 的 HTTPS 隧道，关闭请求检查，随后启动服务并访问 `http://127.0.0.1:3101/m0`。基础配置 < 显式 overlay < 环境变量；各文件都只读取，不导入旧服务或修改进程环境。overlay 应位于忽略的 `.local/`，不要提交凭据。

- AppKey 模式：提供真正的 `RTC_APP_KEY` 或 `M0_RTC_APP_KEY`，为新房间和用户签发短期 Token。
- 显式临时 Token 模式：提供 `M0_RTC_STATIC_TOKEN`、`M0_RTC_STATIC_ROOM_ID`、`M0_RTC_STATIC_USER_ID`；只使用该 Token 绑定的房间和用户，不能声称它是新建隔离房间。任务与 Agent 仍使用唯一 M0 ID。不会自动猜测旧 `RTC_APP_KEY` 字段中的临时 Token。

`TargetUserId` 始终与客户端 `user_id` 一致。临时 Token 本地只检查格式、绑定和有效期，签名由 RTC 入房验证。AppKey 模式按官方公开发布与订阅权限签发；页面只启用音频，不能把 Token 描述为音频专用权限。

页面按顺序提供固定文字播放、点击开麦、打断和三项人工确认。默认不采集麦克风或摄像头。回调是最多 8 片、间隔 1 秒的固定合成回复，便于听音和打断，不接业务模型。字幕只暂存在页面，不上传原文。

管理接口仅供本机访问，公网请求验证页和管理 API 返回 403；认证 RTC 回调仍可经 HTTPS 到达。单活动会话、最多 600 秒硬期限、60 秒无心跳清理、启动失败补偿、未知结果保留与重启账本恢复均有测试。浏览器退出的请求仅为尽力而为，进程强杀后须重启恢复清理；勿删除尚有未知任务的 `.sessions.json`。

v0.6 批次的真实客户端文字音频、轮次关联与 SDK 打断观测通过，用户聊天确认三项试音正常。SDK 近似停音单样本为 474ms；不是实际声卡停音 P95。该批会话未采集到 `mic_started`，故当时语音确认保留为用户报告。该批 **101 项 Python 测试通过**；见 [客户端结果](validation_results/2026-11-08-m0-client.json) 与设计文档第 18.5 节。后续真实开麦结果见下文和设计文档 18.7。

## SDK 多样本基准入口

新增“语音首音采样（3 轮）”，主动开麦后按页面提示逐轮说话。在 AI 有声时点击“打断回复”可记录该轮停音估计。开始记录先落盘，失败、超时、重复和未完成样本保留计数；本轮麦克风能量和真实 CustomLLM 轮次共同关联语音样本。

每种指标需要三个不同轮次，P50/P95 均用 nearest-rank；它们是 SDK 工程估计，不是人耳首音、声卡输出或生产 SLO。旧版 474ms 不混入新统计。`ready` 不自动放行 M0。协议和采集口径见设计文档第 18.6 节。

```powershell
python -m unittest discover -s backend_harness_loop/tests -q
node --test backend_harness_loop/web/measurements.test.cjs backend_harness_loop/web/m0.test.cjs
```

测量元数据写入 `.local/*.measurements.jsonl`，不含音频或字幕原文；状态 API 只汇总当前进程的本会话样本。重启会清理旧任务并保留审计文件，应新建会话重新采集，不合并为同一批次。

v0.7 批次结果：**116 项 Python / 15 项 Node 测试通过**。两个真实会话记录到开麦、音量、用户字幕及页面三项人工确认，用户明确确认测试通过；功能验收完成。SDK 打断有 243 / 278 / 311ms 三个跨会话观察，语音首音仍无有效完成样本，量化门槛未完成；一次 `1003006` ASR 连接异常保留待查。两个任务、临时服务与隧道均已清理。见 [本轮结果](validation_results/2026-11-08-m0-benchmark.json) 和设计文档第 18.7 节。

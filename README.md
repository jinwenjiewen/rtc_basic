# rtc_basic3.0版：RTC Agent + Harness

当前版本为 **rtc_basic3.0版（3.0.0）**，正式后端是 `backend_harness_loop product`，前端为 React / TypeScript。3.0 仅发布新版 Harness 后端，不再包含已退役的 `rag_llm_server`；本机旧目录与配置、现有账号库和运行账本继续保留，不上传密钥或用户数据。原服务的默认启动与 ASGI 导入已封锁，常规启动脚本和前端 `/proxy` 回退已移除。

正式 React 的登录、独立文字、RTC 语音/文字、字幕、打断和退出已完成基础验收，用户确认 Flash / Pro 语音正常。模型自主选择工具、检索和上下文取舍；Harness 负责容量、执行、持久化与取消。当前业务不报价、无真实报名入口，不生成下单链接或声称已报名。

## 启动

在项目根目录运行，使用 Python 3.13+ 和已安装的 Node/npm：

```powershell
uv sync --locked --project backend_harness_loop
npm ci
npm run build
npm run server:start
```

先核对 3101 上的服务身份和活动会话，勿重复启动或中断其他用户。打开 `http://127.0.0.1:3101/login` 使用原账号，文字入口为 `/chat`。新后端复用原 PostgreSQL 账号，不迁移、重置或覆盖密码。

`npm start` 单独启动 React 开发服务器，默认请求 3101；构建后的页面由产品后端同源提供。Node 只负责前端开发/构建，实际后端为 Python。

## 独立配置

本机已将以下配置迁出旧目录并校验等价。`.local/` 和 `.env` 均被忽略，勿将真实值写入文档或提交：

| 文件 | 用途 |
| --- | --- |
| `backend_harness_loop/.local/product-auth.env` | 原 PostgreSQL、JWT、Cookie 与明确允许的前端 Origin；管理员变量仅作本地账号参考，不自动重置数据库账号 |
| `backend_harness_loop/.local/product-rtc.env` | RTC AppKey、云端 AK/SK、回调凭据和 `SERVER_URL`；不使用共享静态 Token |
| `backend_harness_loop/.local/voice-profile.json` | 独立 ASR/TTS JSON 数据，不读取旧 Python 源码 |
| `backend_harness_loop/model_profiles.json`、`.env` | 可选 Pro / Flash 与供应商协议、型号、独立模型密钥 |
| `backend_harness_loop/.local/m3-knowledge.json`、`m3-knowledge.env` | 知识库适配器、接口/资源与独立密钥 |
| `backend_harness_loop/.local/m3-business-policy.json` | 运营答复规则，与知识库和型号配置分离 |

新环境参考 [账号模板](backend_harness_loop/product_auth.env.example)、[RTC 模板](backend_harness_loop/product_rtc.env.example)、[ASR/TTS 模板](backend_harness_loop/voice_profile.example.json) 及 [模型模板](backend_harness_loop/model_profiles.example.json)。已有本机文件应保留。

产品命令默认使用上述三个账号/RTC/语音本地文件，可用 `--auth-env-file`、`--env-file`、`--voice-config` 显式指定。`--legacy-source` 只保留历史开发探针用途，正式产品命令拒绝该参数。

## RTC 回调与数据

语音需要指向本实例 3101 的可达 HTTPS 回调地址。将当前地址配置为 `product-rtc.env` 的 `SERVER_URL`，或显式传入 `--public-url`；临时隧道地址变化后需更新并重启空闲实例。Start 会校验公网 health 的实例身份。

临时回调 Host 只提供 health 与经过凭据认证的 RTC 回调，未授权为浏览器 Origin 的公网 Host 无法访问登录/前端。产品当前只绑定 loopback；生产 HTTPS、进程托管和对外发布另行配置。

正式默认账本为 `.local/product-runtime.sqlite3`，云任务恢复记录为 `.local/product-capture.sessions.json`。本轮已从原 M4 测试账本复制等价快照到默认路径，原文件继续保留，未删除对话与失败记录。部署前先核对路径与活动会话，勿同时用两个进程写同一账本。

## 校验与记录

```powershell
& .\backend_harness_loop\.venv\Scripts\python.exe -m unittest discover -s backend_harness_loop/tests -q
node --test backend_harness_loop/web/*.test.cjs
npm test -- --watchAll=false --runInBand
npm run build
```

默认仍为 9000ms 首有效文字、4 次模型调用、8 次原生工具与 30 秒轮次期限。历史失败全部保留，基础验收不等于长会话或生产时延 SLO 通过。

- [详细运行说明](backend_harness_loop/README.md)
- [设计文档](docs/rtc-agent-backend-design.md)
- [退役记录与恢复边界](docs/legacy-service-retirement.md)
- [M4 基础验收报告](backend_harness_loop/validation_results/2026-11-17-m4.json)
- [旧服务退役报告](backend_harness_loop/validation_results/2026-11-17-m4-retirement.json)
- [退役前 README 原文存档](docs/archive/README-before-retirement-2026-11-17.md)

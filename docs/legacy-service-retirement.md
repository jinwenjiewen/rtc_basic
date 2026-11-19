# 本机旧业务服务退役记录（2026-11-17）

用户在确认正式 React、登录、文字、Flash/Pro RTC 基础通过后，明确要求“旧服务退役执行”。本次退役范围是当前 Windows 项目中的旧 `rag_llm_server` 业务服务，不包含未知的远程部署或外部账号资源撤销。

## 核对与切换

- 执行前 3001 无监听，未发现运行中的旧业务服务；3101 确认是新 `backend_harness_loop.product`，活动运行时会话为 0，已知 RTC 云任务均停止。
- Windows 服务、计划任务、Run 注册表与用户/公共 Startup 中没有匹配该旧服务的自动启动项，因此未停止无关进程或修改无关自启动配置。
- 当前新实例仍引用旧 `.env` 与 Python ASR/TTS 字面量。已复制到独立忽略文件并核对账号/JWT、RTC 设置、ASR/TTS 数据等价；正式产品不再读取旧目录。
- 在空闲状态结束该新实例，以正式 `npm run server:start` 重新启动 3101。公网回调 health 与新实例身份匹配，临时回调 Host 的登录入口仍拒绝访问。

## 退役措施

旧 `main.py` 在 SDK、数据库及业务模块导入前拒绝默认脚本启动和 ASGI 导入，返回 `legacy_service_retired`。移除 `server:legacy` npm 脚本和正式 React 的 `/proxy` 调用及无 owner 会话回退；遇到旧场景协议直接拒绝进房。

新产品启动默认使用：

- `.local/product-auth.env`：同一 PostgreSQL、原 JWT 和 Cookie 配置。
- `.local/product-rtc.env`：有效 AppKey、云端凭据、回调凭据与 `SERVER_URL`。
- `.local/voice-profile.json`：独立 ASR/TTS JSON，仅允许语音配置。

型号、模型密钥、知识库和业务规则保持各自配置。`--legacy-source` 继续用于历史开发探针，正式 `product` 命令拒绝它。默认 9 秒首输出预算未修改。

## 数据保留与验证

PostgreSQL 用户主表在操作前后比较规范化指纹，确认账号、密码哈希、启用状态和创建时间未变；登录测试可以新增/撤销自己的测试登录与节流记录，不能据此声称整个账号库字节不变。

旧 M4 运行时 SQLite 通过一致性 backup 复制到正式默认 `product-runtime.sqlite3`，包含已提交 WAL 数据，逻辑 SQL 指纹一致；RTC 恢复账本等字节复制到 `product-capture.sessions.json`。原数据库、恢复记录、回调记录与所有 M0～M4 验证报告继续保留，不移动或删除用户数据；WAL/SHM 的正常检查点生命周期由 SQLite 管理。旧根 README 原字节另存 [存档](archive/README-before-retirement-2026-11-17.md)。

退役后自动回归与真实 HTTP 登录、刷新、文字、注销另存独立报告，失败不覆盖。语音数据配置与已验收版本等价，当前 RTC 能力和回调身份复核通过；本次未追加真人麦克风采样或把原样本改写为生产 SLO。

当前证据见 [退役脱敏报告](../backend_harness_loop/validation_results/2026-11-17-m4-retirement.json)。生产 HTTPS、外部发布、正式进程托管和完整回滚演练仍与本机退役分开。

## 人工恢复边界

原代码和忽略配置保留于旧目录；仅在操作人员显式设置进程环境变量 `RTC_LEGACY_RECOVERY=1` 时，旧模块才允许加载。该标志不写入日常配置或自动启动项，也不进行故障自动回退。恢复需重新核对旧凭据是否有效、端口/隧道映射、活动会话和数据兼容性；不能在新服务使用中的账本上直接启动旧进程。

旧账号维护与迁移工具仅作历史保留，调用前需显式核对目标账号库配置，不自动运行初始化或迁移。

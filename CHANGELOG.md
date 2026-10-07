# 更新日志 (Changelog)

本项目遵循 [Semantic Versioning 2.0.0](https://semver.org/lang/zh-CN/) 规范。

---

## [1.1.3] - 2026-10-07

### 修复
- **修复默认 LLM 提供商解析失败**：修复当 `chat_provider_id` 配置留空时，因缺少 `umo` 导致无法获取模型并抛出 `Provider None not found` 的问题。
- **简化模型解析与容灾机制**：
  - **默认模型解析**：配置留空时直接调用 `context.get_using_provider_async(None)` 获取 AstrBot 全局当前默认聊天模型。
  - **配置容灾回退**：若配置了不存在或未激活的模型提供商，自动告警并平滑回退至系统默认模型。
  - **空模型安全兜底**：系统未配置任何可用模型时输出预置摘要，杜绝向 `llm_generate` 传递 `None` 导致报错。
  - **双通道调用容灾**：优先通过 `context.llm_generate` 调用，若框架层异常则自动切换至 Provider 实例的 `text_chat` 直接调用进行兜底。

---

## [1.1.2] - 2026-09-23

### 优化
- **快照与预览版标题匹配**：新增 `_match_version_title` 归一化子序列匹配算法，完美解决 Mojang Version Manifest 中的版本 ID（如 `26.4-snapshot-1`、`26.3-rc-2`、`26.3-pre-1`）与 Zendesk 官方文章标题命名差异（如 `26.4 Snapshot 1`、`26.3 Release Candidate 2`）导致快照日志抓取失败的问题。
- **Token 消耗与截断优化**：在更新日志清洗中加入换行对齐的智能截断机制，自动保留核心特性与重磅改动，过滤数十条琐碎的 Bug 编号列表，将单次输入 LLM 的 Token 稳定控制在约 500~600 Token，大幅节省模型调用成本。

### 文档
- 新增 `CHANGELOG.md` 记录详细版本演进与改动历史。

---

## [1.1.1] - 2026-09-22

### 优化
- 添加插件 Logo 图标与元数据展示。
- 完善平台兼容性声明（支持 `qq_official` 与 `aiocqhttp`）及 AstrBot 依赖版本约束（`>=4.5.7`）。

---

## [1.1.0] - 2026-09-20

### 新增
- **Cron 表达式定时调度**：支持标准 5 字段 Cron 表达式（`cron_expression`），采用内置纯 Python 调度解析器，无需引入额外依赖库即可实现整点或定点检测。

### 优化
- Cron 解析异常时自动告警并平滑回退至基础轮询间隔（`check_interval`）模式。

### 文档
- 在 `README.md` 中补充 Cron 表达式配置项说明与常用时间表范例。

---

## [1.0.0] - 2026-09-19

### 初始发布
- **版本更新检测**：对接 Mojang 官方 Version Manifest V2 API，定时自动检测 Java Edition 正式版（Release）与快照版（Snapshot）发布。
- **更新日志抓取**：支持从 Mojang 官方 Zendesk 支持中心与 Minecraft Wiki 自动获取更新说明并进行 HTML 结构化清洗。
- **LLM 智能速报**：调用 AstrBot 接入的大模型，自动提炼 400 字以内的精炼中文更新速报。
- **QQ 官方机器人深度适配**：基于 `unified_msg_origin` (UMO) 会话机制，无缝兼容 QQ 开放平台 `group_openid` 主动消息推送与纯文本安全排版。
- **交互指令集**：提供 `/mc help`、`/mc sub`、`/mc unsub`、`/mc check`、`/mc latest` 指令，支持群聊订阅状态自管理。
- **持久化存储**：采用 AstrBot KV 存储并兼容本地配置，保障群订阅列表与版本记录不丢失。

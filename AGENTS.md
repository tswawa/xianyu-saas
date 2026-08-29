# DeepWhale 项目协作入口

开始任务前按顺序读取：

1. 全局 `~/.codex/memory/MEMORY.md`（由全局 `AGENTS.md` 指定）。
2. `handoff/AGENTS.md`：项目安全、架构和交付边界。
3. `handoff/MEMORY.md`：项目长期事实、决策和当前状态。
4. `README.md` 及与当前任务相关的 `docs/`。

项目 memory 的详细整理规则在 `handoff/MEMORY_OPERATIONS.md`，通用规则在
`~/.codex/memory/MEMORY_OPERATIONS.md`。主 memory 只保存稳定、可复用且不含敏感信息的结论；
复杂操作、当天过程和未验证内容放到外置文档或全局 daily log。

先查看 `git status --short` 并保留用户已有改动。完成任务后按 memory 规范记录结果、验证证据、
未完成事项和需要复用的逻辑；真实账号、凭据、订单、库存、数据库和日志永远不写入 memory。

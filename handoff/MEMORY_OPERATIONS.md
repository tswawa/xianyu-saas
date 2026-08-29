# 项目 Memory 操作补充

通用流程见 `~/.codex/memory/MEMORY_OPERATIONS.md`。本文只规定 DeepWhale 闲鱼客服项目的文件边界、事实来源和复核方式。

## 文件分工

- `handoff/MEMORY.md`：项目稳定事实、产品决策、当前发布状态和验证入口；这是项目 memory 的唯一主索引。
- `handoff/AGENTS.md`：工作前检查、安全边界、架构边界和交付规则；这是项目指令，不是事实日志。
- `README.md`、`docs/`：详细搭建、部署和产品说明；memory 只引用结论，不复制整篇流程。
- `~/.codex/memory/daily/YYYY-MM-DD.md`：本机当天工作记录，不提交生产凭据或用户数据。
- `~/.codex/memory/reports/YYYY-MM-DD.md`：当天整理结果，供会话交接快速查看。

## 项目优先级

### P0：安全和真实性

1. 任何真实 Cookie、Token、Key、验证码、库存、订单/买家正文、数据库、日志和生产备份都不进 Git 或 memory。
2. 未完成真实扫码、验证码、安全认证、商品同步或履约验收时，必须标记 `unverified`。
3. 生产部署由维护者按精确 commit 串行完成；开发机不直接写生产目录。

### P1：架构与边界

把跨模块且会影响后续实现的架构事实写入 `handoff/MEMORY.md`，例如前端、backend、worker 的职责和内部接口边界。改动前后以源码、测试和用户明确决策复核。

### P2：产品决策和发布状态

会员权限、登录方式、履约核验和当前发布编号都可能变化。每次发布、产品决策或用户撤销决定时更新日期和状态；旧版本移到 `P4` 或归档，不保留两条互相矛盾的“当前”结论。

### P3：验证入口

只记录仓库已有且可复现的命令：`npm test`、`npm run test:api`、`npm run test:worker`、`npm run test:ui`、`npm run test:repository`。命令通过只证明对应合同通过，不代表真实账号或生产验收完成。

## 任务结束检查

1. `git status --short`，确认没有覆盖用户改动。
2. 根据风险运行必要测试、`git diff --check`，如无法运行说明原因。
3. 更新 `handoff/MEMORY.md` 的稳定结论和状态日期；一次性过程只写 daily log。
4. 在全局 report 中写新增、修正、验证和待办，并在回复中简要告知用户。

## 错误改进记录

用户指出错误后，项目 memory 只保留能跨任务复用的防错规则，例如“真实履约必须核验订单证明”；具体失败命令、临时环境问题和一次性修复放 daily log。每条规则必须有对应验证入口，避免只写口号。

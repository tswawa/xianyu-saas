# DeepWhale 闲鱼客服项目协作规则

## 开始工作前

1. 完整读取 `handoff/MEMORY.md`、根目录 `README.md` 和与任务相关的 `docs/`。
2. 先查看 `git status --short`，保留用户已有改动。
3. 不读取、输出或提交 `.env`、Cookie、Token、库存、买家消息、订单正文、数据库或日志。

## Memory 生命周期

- 详细整理、纠错、晋级、过期和简报流程见 `handoff/MEMORY_OPERATIONS.md` 及全局 `~/.codex/memory/MEMORY_OPERATIONS.md`。
- 用户指出错误时，记录错误表现、根因、修正、预防规则和验证方式；任务完成或会话结束时，即使用户没有提出问题，也回写结果和当天日志。
- 用户要求每次错误都回写 memory：跨任务规则进入 `handoff/MEMORY.md`，一次性过程进入全局 daily log，不记录凭据或用户数据。
- `handoff/MEMORY.md` 只保留稳定、可复用且不含敏感信息的项目结论；一次性过程和未验证内容放在全局 daily log。

## 架构边界

- `frontend/` 只负责简单操作和展示，不实现套餐授权或平台登录逻辑。
- `backend/` 是账号、权限、店铺登录、商品同步、支付入口和 worker 生命周期的权威控制面。
- `worker/` 处理闲鱼消息与履约；免费 `rules` 和会员 `rules_ai` 必须由服务端显式隔离。
- 普通页面不展示浏览器连接组件或手动 Cookie；主流程是服务端官方二维码，页面永远不接收闲鱼 Cookie 或 Token。仓库中的兼容资产只能走独立测试/运维边界。

## 产品约束

- 面向非技术店主，文案使用日常语言，避免暴露 Cookie、MTOP、Token、JSON 等实现概念。
- 免费用户不显示会员功能入口；服务端仍必须逐接口校验权限。
- 商品按真实同步结果一商品一卡片，展示名称、简介、价格和状态。
- 对话按买家会话隔离；不能把多个 `chat_id` 混排。
- 自动发货必须依赖平台订单证明，买家文字和商品标题不能授权履约。

## 修改与验证

- 小范围修改遵循现有代码风格，不进行无关重构。
- 新增环境变量时同步更新 `config/saas.env.example` 或 `worker/.env.example`，只写占位值。
- 默认测试离线运行；真实账号、验证码、安全认证和订单测试必须由用户明确安排。
- 交付前运行 `npm test`、`git diff --check` 和 `git status --short`。
- 部署由生产服务器维护者串行完成；开发机只 push 精确 commit，不直接修改生产。

## Git

- 仓库必须保持私有。
- 只暂存明确文件，不盲目 `git add .`。
- 不在 remote URL、提交信息、文档或聊天中放 PAT。
- `worker/` 的 GPL-3.0 许可证与 `NOTICE.md` 不得删除。

# 贡献指南

感谢你为闲鱼客服工作台提交改进。项目优先保证账号隔离、真实业务状态、故障可恢复和安全边界，再考虑新增功能。

## 开始前

1. 阅读 [`README.md`](README.md)、[`SECURITY.md`](SECURITY.md)、[`LICENSING.md`](LICENSING.md) 和 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
2. 创建独立分支，不直接在 `main` 上堆叠无关改动。
3. 使用 Python 3.10+、Node.js 20+ 和 npm 10+。
4. 运行 `./scripts/bootstrap-dev.sh`，再按需安装 Chromium：`npx playwright install --with-deps chromium`。

## 安全红线

绝不要提交或粘贴以下内容：

- GitHub、模型平台、支付平台或内部 API 的 Token、Key、密码；
- 闲鱼 Cookie、验证码、二维码登录态或浏览器导出的凭据；
- 真实订单号、买家昵称、聊天正文、库存、兑换码、网盘资料、数据库和日志；
- 生产主机路径、备份位置或未公开域名；
- 容器数据卷 `data/`、构建产物和本机 `config/saas.env`。

测试请使用本地临时目录、固定假数据和模拟上游。真实账号、真实订单、真实发货和第三方模型请求不属于默认 CI 范围。

## 提交改动

- 一个提交只解决一个主题，提交标题使用 Conventional Commits 风格，例如 `fix(worker): ...`、`test(api): ...`、`docs(repo): ...`。
- 只暂存明确审阅过的路径，不使用未经检查的 `git add .`。
- 新增环境变量时同步更新对应的 `*.example` 模板，并只放占位值；影响容器运行的变量还需同步 `config/saas.env.docker.example`。
- 改动 `Dockerfile`、`docker-compose.yml` 或 `docker/entrypoint.sh` 时，需说明是否影响控制面派生 Worker 所依赖的 `<SAAS_BOT_ROOT>/.venv/bin/python` 布局。
- 影响 `worker/` 的改动必须保留 GPL-3.0 和上游 NOTICE 边界；镜像分发同样需要保留 `worker/NOTICE.md` 与 `LICENSE`。
- 不要为了格式化而重写无关的大文件；先说明必要性和回归风险。

## 架构边界

改动前先确认职责归属，跨层实现会破坏账号隔离与凭据边界：

- `frontend/` 只负责展示和简单操作，不实现平台登录逻辑。
- `backend/` 是账号、权限、店铺登录、商品同步和 worker 生命周期的权威控制面。
- `worker/` 处理闲鱼消息与履约，每个店铺账号使用独立的运行态、配置和会话边界。
- 页面永远不接收闲鱼 Cookie 或 Token；店铺登录主流程是服务端官方二维码。

## 产品约束

- 面向非技术店主，文案使用日常语言，避免暴露 Cookie、MTOP、Token、JSON 等实现概念。
- 商品按真实同步结果一商品一卡片，不展示推测或占位数据。
- 对话按买家会话隔离，不能把多个 `chat_id` 混排。
- 自动发货必须依赖平台订单证明；买家文字和商品标题不能授权履约。

## 本地门禁

提交前至少运行：

```bash
python3 tests/repository-contract.py
npm test
git diff --check
git status --short
```

如果只改动了某个子系统，也应运行对应专项命令，例如 `npm run test:ai`、`npm run test:isolation`、`npm run test:worker` 或 `npm run test:ui`。

## Pull Request 要求

Pull Request 描述应说明：

- 用户可见变化和不变的业务边界；
- 账号/店铺作用域是否受影响；
- 并发、重试、恢复和失败路径；
- 已运行的测试及尚未验证的真实环境边界；
- 新增依赖、环境变量、许可证或第三方资产。

不要在 PR 中上传截图、日志或数据库来“证明问题”；请先脱敏，并只保留能够复现问题的最小样例。

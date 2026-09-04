# 贡献指南

感谢你为 xianyu-saas 提交改进。项目优先保证账号隔离、真实业务状态、可恢复性和安全边界，再考虑增加功能。

## 开始前

1. 阅读 [`README.md`](README.md)、[`SECURITY.md`](SECURITY.md)、[`LICENSING.md`](LICENSING.md) 和 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
2. 从独立分支开始工作，不在 `main` 上堆叠无关改动。
3. 使用 Python 3.10+、Node.js 20+ 和 npm 10+。
4. 运行 `./scripts/bootstrap-dev.sh`；需要 UI 合同时安装 Chromium：`npx playwright install --with-deps chromium`。

## 安全红线

不要提交或粘贴：

- GitHub、模型平台、支付平台或其他服务的 Token、Key、密码和 Cookie；
- 闲鱼二维码登录材料、账号登录态或浏览器导出的凭据；
- 真实订单号、买家信息、聊天正文、库存、兑换码、网盘资料、数据库和日志；
- 生产主机路径、备份位置、未公开域名或未公开的运行配置；
- `data/`、构建产物、测试截图以及本机 `config/saas.env` 等运行文件。

测试使用本地临时目录、脱敏样例和模拟上游。真实账号、真实订单、真实发货和第三方模型请求不属于默认 CI 范围。

## 提交改动

- 一个提交只解决一个主题，提交标题使用 Conventional Commits 风格，例如 `fix(worker): ...`、`test(api): ...`、`docs(repo): ...`。
- 只暂存已经审阅的路径，不使用未经检查的 `git add .`。
- 新增环境变量时同步更新相应的 `*.example` 模板；影响容器运行的变量还要更新 `config/saas.env.docker.example`。
- 改动 `Dockerfile`、`docker-compose.yml` 或 `docker/entrypoint.sh` 时，说明是否影响控制面派生 Worker 使用的虚拟环境布局。
- 改动 `worker/` 时保留 GPL-3.0 许可证和 [`worker/NOTICE.md`](worker/NOTICE.md) 的来源说明；镜像分发也必须保留许可证文件。
- 不为格式化而重写无关的大文件；如确有必要，请在 PR 中说明原因和回归风险。

## 架构边界

- `frontend/` 负责展示和简单操作，不实现平台登录或权限判断。
- `backend/` 是账号、权限、店铺连接、商品同步和 Worker 生命周期的控制面。
- `worker/` 处理平台消息、规则/AI 回复、人工 outbox 和订单证明后的履约；每个店铺账号使用独立作用域。
- 页面不接收平台 Cookie 或 Token；店铺连接使用服务端官方二维码流程。
- 自动履约必须依赖可验证的订单证明，不能由买家文字、商品标题或模型输出授权。

## 本地门禁

提交前至少运行：

```bash
python3 tests/repository-contract.py
npm test
git diff --check
git status --short
```

只改动某个子系统时，也应运行相应专项命令，例如 `npm run test:ai`、`npm run test:isolation`、`npm run test:manual-reply` 或 `npm run test:ui`。

## Pull Request 要求

PR 描述应说明：

- 用户可见变化和保持不变的业务边界；
- 账号/店铺作用域是否受影响；
- 并发、重试、恢复和失败路径；
- 已运行的测试，以及尚未验证的真实环境边界；
- 新增依赖、环境变量、许可证或第三方资产。

不要上传截图、日志或数据库来证明问题。请先脱敏，并只保留能够复现问题的最小样例。

# 贡献指南

感谢你关注并愿意为 xianyu-saas 提交贡献！为了保持代码库的高质量与可维护性，请在提交代码前阅读以下指引。

## 开发准备

1. **技术栈**：Python 3.10+、Node.js 20+、FastAPI、Playwright；
2. **快速初始化**：
   ```bash
   git clone https://github.com/tswawa/xianyu-saas.git
   cd xianyu-saas
   ./scripts/bootstrap-dev.sh
   npx playwright install --with-deps chromium
   ```

## 安全红线

为了保护开源仓库安全，严禁提交以下内容：
- 任何平台凭据：GitHub Token、大模型 API Key、闲鱼登录态 Cookie 等；
- 真实业务数据：真实买家信息、订单号、聊天记录、卡密库存或私有网盘链接；
- 本机环境路径、私人域名或运行生成的 `./data/` 目录。

## 架构与核心规范

- **`frontend/`**：纯原生现代静态单页应用，无繁杂打包，注重响应速度；
- **`backend/`**：FastAPI 控制面，负责身份鉴权、店铺调度、AI 客服引擎与发货任务消费；
- **`worker/`**：单店铺独立常驻进程，负责 WebSocket 接入、关键词规则匹配与发货状态机；
- **发货安全底线**：自动发货必须依赖官方双接口验单与事务锁，严禁仅凭买家文字或 AI 输出直接发货。

## 提交前本地测试

在发起 Pull Request 之前，请在本地运行并通过以下门禁检查：

```bash
python3 tests/repository-contract.py
npm test
git diff --check
git status --short
```

## Pull Request 规范

- 推荐使用常规提交信息格式（Conventional Commits），例如 `fix(worker): ...`、`feat(ai): ...`、`docs: ...`；
- 描述清楚本次 PR 解决的问题、实现的特性以及本地测试验证情况。

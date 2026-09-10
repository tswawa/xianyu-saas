# 贡献指南

感谢关注并参与 xianyu-saas 的开发与维护。为了保持代码库的可靠性与工程规范，请在提交代码前阅读以下指引。

## 开发准备

### 系统环境要求
- Python 3.10 及以上版本（包含 `python3-venv`）
- Node.js 20 及以上版本，npm 10 及以上版本
- Git 2.40 及以上版本

### 本地初始化步骤
```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

# 安装 Python 虚拟环境与本地测试工具依赖
./scripts/bootstrap-dev.sh

# 运行全套本地测试（含浏览器端 UI 回归）需要安装 Chromium：
npx playwright install --with-deps chromium
# 注意：测试工具依赖仅用于本地开发与门禁校验，生产部署环境仅需 Python 3.10+ 与 Nginx，切勿混淆两类依赖。
```

## 安全红线

提交代码前请仔细检查，严禁提交以下内容：
1. **平台凭据与密钥**：大模型 API Key、GitHub Token、闲鱼登录态 Cookie、服务端加解密主密钥等。
2. **真实业务数据**：真实买家联系方式、订单编号、聊天对话记录、可用卡密库存或私有网盘链接。
3. **环境特定路径与私有配置**：本地绝对路径、私有域名、包含测试密码的文件，或运行生成的 `./data/` 目录。

## 模块架构与边界规范

- **`frontend/`**：前端为原生静态单页应用，由 HTML、CSS 和原生 JavaScript 构建，无需构建工具打包编译。修改样式与交互时应注意保持响应式适配与组件独立性。
- **`backend/`**：FastAPI 控制面服务，负责用户认证鉴权、店铺生命周期管理、任务消费以及统一模型调用适配。
- **`worker/`**：单店铺独立常驻进程，负责平台会话连接、关键词规则匹配与发货状态机。
- **发货逻辑底线**：自动化发货流程必须建立在平台订单状态核验与数据库事务锁的基础之上，严禁绕过订单核验直接触发发货。

## 提交前本地测试

发起 Pull Request 之前，必须在本地终端运行并通过以下门禁检查：

```bash
# 1. 检查代码仓库便携性、路径合规与敏感信息拦截
python3 tests/repository-contract.py

# 2. 运行完整自动化测试套件（含浏览器 UI 回归；纯后端修改可单独执行特定后端测试，如 npm run test:api）
npm test

# 3. 检查代码空白字符与格式残留
git diff --check

# 4. 确认工作区文件状态干净
git status --short
```

## 文档截图生成与离线验证

文档中的界面截图（`docs/assets/readme/*.png`）支持通过自动化测试脚本全量离线重现生成：

```bash
# 生成文档全套演示截图（覆盖桌面端与移动端共 12 张视图）
SAAS_UI_SCOPE=docs-capture node tests/ui-check.mjs
```

- **执行要求**：需要安装 Node.js 与 Playwright Chromium 依赖（`npx playwright install --with-deps chromium`）；
- **写入目标**：所有断言校验通过后自动更新 `docs/assets/readme/` 目录下的 12 张演示截图文件；
- **离线安全**：仅在显式指定 `SAAS_UI_SCOPE=docs-capture` 时触发截图生成与写入（日常 `npm test` 不会触发截图写入）；测试全程使用本地离线 Mock 数据与演示资产，不发起外部网络请求，不连接任何真实平台或外部模型服务。

## 版本发布与打包规范

项目遵循语义化版本规范，新版本通过 GitHub Actions 自动化流水线签名并发布：

- **版本号统一**：发布新版本前，须同步更新 `package.json`、`package-lock.json` 与 `backend/version.py` 中的版本号（例如 `0.2.0`），确保版本标识全局一致；
- **本地打包验证**：可通过仓库内置脚本针对选定的 Git 提交进行离线打包演练（打包过程严格基于 Git 跟踪对象，不收录未跟踪的本地文件与私有配置）：
  ```bash
  python3 scripts/build-release.py --ref HEAD --output .local/releases/test-build
  ```
- **自动化发布工作流**：向仓库推送 `v*` 格式版本标签（例如 `v0.2.0`）会触发 `.github/workflows/release.yml`。工作流首先执行全套 CI 测试门禁，校验通过后使用 GitHub Actions secret `RELEASE_SIGNING_KEY` 托管的私钥对发布清单进行数字签名，生成包含 8 项正式资产的发布草稿并自动发布；
- **公钥与私钥分离**：签名公钥保存于 `deploy/update-signing.pub`，签名私钥仅由服务端 Actions Secret 托管，严禁将私钥以任何形式提交至代码仓库或打入分发包。

## 提交信息与 PR 规范

- 推荐使用常规提交信息格式（Conventional Commits），例如：
  - `feat(worker): 支持根据商品规格选择不同回复话术`
  - `fix(backend): 修复并发创建管理员时的死锁问题`
  - `docs(readme): 补充 Windows 环境原生运行说明`
- 提交 PR 时请完整填写 Pull Request 模板，详细说明变更原因、影响模块以及本地测试验证情况。

# xianyu-saas

按店铺账号隔离的闲鱼客服工作台：集中管理店铺、商品、会话、AI 回复和订单履约。

[![CI](https://github.com/tswawa/xianyu-saas/actions/workflows/ci.yml/badge.svg)](https://github.com/tswawa/xianyu-saas/actions/workflows/ci.yml)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue.svg)](LICENSE)

## 项目预览
<p align="center">
  <img src="docs/assets/readme/overview.png" alt="运营概览" width="49%" />
  <img src="docs/assets/readme/customer-service.png" alt="客服会话工作台" width="49%" />
</p>

截图来自脱敏本地测试夹具，只用于展示界面布局。

## 运行要求

- Linux（Ubuntu 22.04/24.04 或兼容发行版）
- Python 3.10+
- Node.js 20+、npm 10+
- 完整 `npm test` 包含 UI 合同，首次运行前需安装 Playwright Chromium；真实闲鱼、模型和订单接入需要单独的受控环境

## 快速启动

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas
./scripts/bootstrap-dev.sh
```

初始化脚本会安装 API、Worker 和 Node 开发依赖，并创建未跟踪的 `config/saas.env`、`worker/.env` 与 `.local/`。复制/编辑 `config/saas.env` 时，本地开发请将 `SAAS_PUBLIC_ORIGIN` 设为 `http://127.0.0.1:4173`（或实际工作台地址），否则浏览器写请求来源校验可能失败。

首次运行完整 `npm test` 前执行：

```bash
npx playwright install --with-deps chromium
```

编辑配置后运行：

```bash
npm run dev
```

默认地址：工作台 `http://127.0.0.1:4173/xianyu-saas/`；API 健康检查 `http://127.0.0.1:8096/health`。
`npm run dev` 会同时启动 API、consumer 和 Web；按 `Ctrl+C` 一起停止。

## 首位管理员

空数据库不会自动开放公开注册。公开注册开关只控制普通账号注册，不能单独创建首位管理员。

1. 在受控初始化窗口显式启用 `SAAS_BOOTSTRAP_ENABLED=1`，配置 `SAAS_BOOTSTRAP_TOKEN_FILE` 指向权限为 `0600` 的一次性令牌文件/凭据，并限制 `SAAS_BOOTSTRAP_TRUSTED_SOURCES`。
2. 从受信任入口提交首位管理员表单；服务端原子创建 `admin` 并消费令牌。
3. 登录确认后关闭 bootstrap、移除令牌文件/凭据并重启 API。

## 主要功能

- **店铺管理**：服务端官方二维码授权、多店铺切换、店铺级登录态/数据/Worker 隔离。
- **商品管理**：同步标题、简介、价格、上下架状态；补充资料可用 AI 整理预览，确认后保存。
- **智能客服**：规则优先、AI 补充、人工接管；支持连续对话、快捷回复、文字和图片消息。
- **对话沙盘**：与实际客服共用控制面引擎，只回显测试结果，不向闲鱼发送消息。
- **订单与履约**：核验订单号、商品、买家、卖家、状态和数量；事务预留库存，异常转人工复核。
- **运营与管理**：查看连接、Worker、消息和待办状态，维护发货模板、卡密池、账号权限和版本状态。

AI 仅作用于客服交互层，不直接改订单或触发发货。平台 ACK 只表示接口受理，不表示买家已读或最终送达。

> **当前边界**：离线合同和本地测试已覆盖主要 API、认证、隔离、AI 适配器、Worker 与 UI；真实闲鱼扫码/验证码/风控、真实模型调用质量、真实订单履约、Nginx/systemd 生产部署仍未验收。

## 配置

开发配置由初始化脚本创建：`config/saas.env`（控制面与本地运行参数）和 `worker/.env`（Worker 参数）。

常用变量：

| 变量 | 作用 |
| --- | --- |
| `SAAS_DB`、`SAAS_TENANTS_DIR` | SQLite 数据库与账号私有目录 |
| `SAAS_PUBLIC_ORIGIN` | 浏览器写请求来源，保持与工作台的协议、主机和端口一致 |
| `SAAS_TRUSTED_HOSTS` | 允许的 Host |
| `SAAS_ALLOW_REGISTRATION` | 普通注册开关，生产保持 `0` |
| `SAAS_AI_MASTER_KEY` | 加密保存账号级 AI 连接 |
| `SAAS_BOOTSTRAP_*` | 首位管理员一次性初始化（含 `SAAS_BOOTSTRAP_TOKEN_FILE` 指向的令牌文件/凭据） |

平台凭证、Cookie、Token、API Key 和主密钥只放在未跟踪配置或外部秘密系统中。

## 开发验证

```bash
npm test
npm run test:ui
npm run test:repository
git diff --check
```

## 项目结构

- `frontend/`：静态工作台
- `backend/`：FastAPI 控制面
- `backend/job_consumer.py`：异步同步任务消费者
- `worker/`：消息处理、规则/AI 回复与履约 Worker
- `deploy/nginx/`、`deploy/systemd/`、`deploy/updater/`：生产模板与更新器；`tests/`、`worker/tests/`：API、隔离、Worker 和浏览器合同

## 安全与许可证

- 不要把 Cookie、Token、API Key、密码、验证码、订单/买家消息、库存、数据库或日志提交到 Git。
- 发现安全问题请通过 [`SECURITY.md`](SECURITY.md) 私下报告。
- 原创代码采用 [`GPL-3.0-only`](LICENSE)。
- `worker/` 基于 [`shaxiu/XianyuAutoAgent`](https://github.com/shaxiu/XianyuAutoAgent)，来源和许可见 [`worker/NOTICE.md`](worker/NOTICE.md)。
- 本项目与闲鱼、淘宝、阿里巴巴或模型供应商无官方关联；使用者需遵守法律法规和平台规则。

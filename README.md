# xianyu-saas

闲鱼多店铺客服与自动履约工作台。项目按店铺账号隔离数据和运行进程，提供店铺连接、商品同步、规则回复、AI 客服、人工接管、订单核验与数字资料履约能力。

当前产品按自用工作台形态维护：登录账号可以使用已实现的经营功能，历史订阅字段仅为兼容用途，不代表当前前端的功能分级。

[![CI](https://github.com/tswawa/xianyu-saas/actions/workflows/ci.yml/badge.svg)](https://github.com/tswawa/xianyu-saas/actions/workflows/ci.yml)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

## 功能概览

### 店铺管理

| 能力 | 说明 |
| --- | --- |
| 官方扫码连接 | 通过服务端流程生成二维码，完成店铺绑定 |
| 多店铺切换 | 工作台按店铺标签切换当前操作范围 |
| 账号隔离 | 商品、会话、订单、配置和 Worker 运行态按店铺独立保存 |
| 状态与同步 | 查看连接状态、商品快照和同步结果 |
| 故障隔离 | 单个店铺失效或暂停不会覆盖其他店铺的数据 |

### 智能客服

| 能力 | 说明 |
| --- | --- |
| 规则优先 | 商品级和店铺级关键词规则优先于 AI |
| 多模型接入 | 支持 OpenAI 兼容接口、OpenAI Responses、Anthropic Messages、Google Gemini 和 Ollama |
| 自然语言配置 | 用店铺与商品说明描述客服口径，不要求编辑 JSON |
| 沙盘预览 | 保存前连续模拟对话，查看回复、引用资料和安全状态 |
| 人工接管 | 工作台可切换人工接管，支持冷却和超时交回 |
| 人工回复 | 支持选择、粘贴或拖入最多 8 张图片；图片按顺序逐张发送，文字最后单独发送。每段等待协议 ACK，失败只重试未确认部分 |
| 常用设置 | 支持人格预设、语气、称呼、回复长度、营业时间、欢迎语和快捷短语 |

### 自动履约

| 能力 | 说明 |
| --- | --- |
| 订单核验 | 交叉核验订单号、商品、买家、卖家、状态和数量 |
| 发货资料 | 支持兑换码、网盘链接和固定资料等数字内容 |
| 模板与库存 | 按商品绑定发货模板和兑换码库存池 |
| 事务预留 | 库存预留与扣减在事务内完成，避免重复发放 |
| 异常处理 | 库存不足、发送失败或证明不完整时转人工复核 |

### 运营与管理

- 运营概览：查看消息、自动回复、人工接管和履约摘要。
- 待办提醒：集中处理连接失效、同步失败、Worker 停止和待复核履约。
- 账号管理：`admin` 与 `owner` 两级平台角色；管理员可管理账号、审计和更新设置。
- 版本更新：systemd 部署支持签名 Release、健康检查和失败回滚；容器部署需重新构建镜像。

## 界面预览

<div align="center">
  <img src="docs/assets/readme/overview.png" width="700" alt="运营概览">
  <br>
  <em>运营概览</em>
</div>

<div align="center">
  <img src="docs/assets/readme/shops.png" width="700" alt="店铺管理">
  <br>
  <em>店铺管理与连接状态</em>
</div>

<div align="center">
  <img src="docs/assets/readme/customer-service.png" width="700" alt="客服会话">
  <br>
  <em>客服会话工作台</em>
</div>

<div align="center">
  <img src="docs/assets/readme/ai-config.png" width="700" alt="AI 客服设置">
  <br>
  <em>AI 客服设置与连续对话沙盘</em>
</div>

<div align="center">
  <img src="docs/assets/readme/cards.png" width="700" alt="卡密库存池">
  <br>
  <em>卡密库存池</em>
</div>

<div align="center">
  <img src="docs/assets/readme/orders.png" width="700" alt="订单列表">
  <br>
  <em>订单与履约状态</em>
</div>

## 运行方式

| 平台 | 推荐方式 |
| --- | --- |
| Linux | Docker 或源码安装 |
| Windows | Docker Desktop（WSL2 后端） |
| macOS | Docker Desktop |

控制面使用 Linux 的进程与文件系统能力管理店铺 Worker。Windows 和 macOS 建议通过容器运行。

### Docker

需要 Docker 20.10+ 和 Docker Compose v2。

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```

- 工作台：`http://127.0.0.1:4173/xianyu-saas/`
- 健康检查：`http://127.0.0.1:8096/health`
- 数据库和店铺数据位于宿主机 `./data`，重建容器不会清空数据。

对外提供服务时，请保留宿主回环端口映射，在前面配置 TLS 反向代理，并把 `SAAS_PUBLIC_ORIGIN`、`SAAS_TRUSTED_HOSTS` 设置为实际访问地址。生产环境应显式设置 `SAAS_AI_MASTER_KEY`，不要依赖开发回退密钥。更多说明见[部署指南](docs/DEPLOYMENT.md)。

停止或查看日志：

```bash
docker compose logs -f
docker compose down
```

### Linux 源码安装

要求：Linux、Python 3.10+、Node.js 20+ 和 npm 10+。

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas
./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
npm run dev
```

工作台和健康检查地址与 Docker 方式相同。源码开发默认使用本机回环地址；需要局域网访问时，按[开发环境搭建](docs/NEW_UBUNTU_HANDOFF.md)配置 `SAAS_DEV_WEB_HOST`、`SAAS_PUBLIC_ORIGIN` 和 `SAAS_TRUSTED_HOSTS`。

空数据库不会自动开放注册。首次使用请按[部署指南](docs/DEPLOYMENT.md)中的 bootstrap 流程创建管理员，再连接店铺。

## 日常配置

1. 在“店铺管理”添加店铺并使用闲鱼 App 扫码连接。
2. 在“智能客服中心”填写店铺与商品说明，配置模型连接并保存。
3. 在“自动化履约中心”导入兑换码、建立发货模板，再绑定商品。
4. 在“运营概览”和“智能客服中心”查看状态、会话和待办。

模型连接支持五种格式：

- OpenAI Chat Completions 兼容接口
- OpenAI Responses
- Anthropic Messages
- Google Gemini
- Ollama Chat

AI 只能生成客服文本或人工接管/不回复决策，不能授权发货、修改订单或读取其他店铺资料。

## 主要配置项

| 变量 | 用途 |
| --- | --- |
| `SAAS_PUBLIC_ORIGIN` | 工作台实际访问地址 |
| `SAAS_TRUSTED_HOSTS` | 允许的 Host 列表 |
| `SAAS_COOKIE_SECURE` | HTTPS 部署设为 `1`；本机 HTTP 调试可设为 `0` |
| `SAAS_AI_MASTER_KEY` | 加密模型凭据的主密钥；生产环境必填 |
| `SAAS_MAX_BOTS` | 最大并发店铺 Worker 数 |
| `SAAS_BOT_MEM_MB` | 单个 Worker 的内存上限 |
| `SAAS_ALLOW_REGISTRATION` | 是否允许公开注册，生产环境建议保持 `0` |
| `SAAS_AUDIT_HMAC_KEY` | 安全审计摘要密钥 |
| `SAAS_UPDATE_PUBLIC_KEY_FILE` | systemd 签名更新使用的 Ed25519 公钥文件 |

完整模板见 [`config/saas.env.example`](config/saas.env.example) 和 [`config/saas.env.docker.example`](config/saas.env.docker.example)。

## 测试

首次运行 UI 合同前安装 Chromium：

```bash
npx playwright install --with-deps chromium
npm test
```

常用专项命令：

```bash
npm run test:worker       # Worker 消息、履约与恢复
npm run test:isolation    # 账号和店铺隔离
npm run test:ai           # AI provider 与安全边界
npm run test:auth         # 注册、角色与权限
npm run test:manual-reply # 人工回复 outbox
npm run test:ui           # 浏览器工作台
npm run test:repository   # 仓库文件合规
```

默认测试使用脱敏样例、临时目录和模拟上游。真实扫码、验证码、安全认证、第三方模型、真实订单、真实发货和生产部署需要在目标环境中单独验证。

## 目录结构

```text
frontend/             静态工作台
backend/              FastAPI 控制面与任务消费者
worker/               闲鱼消息、回复引擎与履约状态机
config/               环境变量模板
deploy/               Nginx、systemd 与更新器模板
docker/               容器入口脚本
scripts/              初始化与启动脚本
tests/                API、隔离、部署与 UI 合同
docs/                 架构、部署、权限与开发文档
Dockerfile            整站运行镜像
docker-compose.yml    一键启动编排
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 组件边界、数据流与一致性规则 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Docker 与 systemd 部署 |
| [`docs/NEW_UBUNTU_HANDOFF.md`](docs/NEW_UBUNTU_HANDOFF.md) | 开发环境搭建 |
| [`docs/ACCESS_MODEL.md`](docs/ACCESS_MODEL.md) | 角色、账号与店铺作用域 |
| [`docs/AI-CUSTOMER-SERVICE-REQUIREMENTS.md`](docs/AI-CUSTOMER-SERVICE-REQUIREMENTS.md) | AI 客服内容与安全边界 |
| [`docs/PLAN.md`](docs/PLAN.md) | 产品边界与路线图 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 贡献流程与本地门禁 |
| [`SECURITY.md`](SECURITY.md) | 漏洞报告与敏感信息边界 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更记录 |

## 免责声明

本项目与闲鱼、淘宝、阿里巴巴集团及模型服务商没有官方关联。使用者应遵守平台规则、服务条款和适用法律，并自行评估账号安全、隐私和数据合规风险。协议 ACK 仅表示平台协议层接收，不代表买家已读或最终送达。

## 许可证

本项目基于 [GPL-3.0-only](LICENSE) 发布。`worker/` 的上游来源与修改说明见 [`worker/NOTICE.md`](worker/NOTICE.md)。

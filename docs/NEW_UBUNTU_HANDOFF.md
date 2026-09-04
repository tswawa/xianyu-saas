# 开发环境搭建

本文用于搭建本地开发环境，不授权生产部署，也不应把生产数据复制到开发机。

如果只想快速运行，或宿主不是 Linux，可以直接使用容器：

```bash
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```

工作台默认在 `http://127.0.0.1:4173/xianyu-saas/`，数据位于未跟踪的 `./data`。

## 1. 准备环境

源码开发需要：

- Linux（建议 Ubuntu 22.04+ 或 Debian 12）；
- Git 2.40+；
- Python 3.10+，包含 `venv`；
- Node.js 20+；
- npm 10+。

Ubuntu 可安装基础系统包：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential
```

Node.js 请使用官方发行方式或受信任的版本管理器安装，并核对 `node --version` 和 `npm --version`。

## 2. 获取源码

将 `<repository-url>` 替换为你有权限访问的项目地址：

```bash
git clone <repository-url> xianyu-saas
cd xianyu-saas
```

不要把访问令牌写入命令、remote URL、提交信息或文档。

## 3. 安装依赖

```bash
./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
```

脚本会创建 `backend/.venv` 和 `worker/.venv`，并根据示例生成未跟踪的本机配置。不要把生产凭据写入示例文件或仓库。

## 4. 配置本机

控制面配置文件为 `config/saas.env`：

- 本机 HTTP 调试可使用 `SAAS_COOKIE_SECURE=0`；生产必须启用安全 Cookie；
- 设置 `SAAS_PUBLIC_ORIGIN` 和 `SAAS_TRUSTED_HOSTS`，使其与浏览器实际访问地址一致；
- 本机可以使用开发脚本生成的忽略目录密钥，生产必须显式提供固定的 `SAAS_AI_MASTER_KEY`；
- `SAAS_BOT_ROOT=./worker` 让控制面使用同一仓库中的 Worker。

Worker 配置文件为 `worker/.env`：

- 默认 `AUTOMATION_MODE=rules`；
- 没有受控测试账号时保持平台登录配置为空，也不要启动 Worker；
- 规则、商品映射和交付资料只放在未跟踪运行目录。

## 5. 启动开发服务

一条命令启动 API、任务消费者和静态工作台：

```bash
npm run dev
```

工作台：

```text
http://127.0.0.1:4173/xianyu-saas/
```

健康检查：

```text
http://127.0.0.1:8096/health
```

需要局域网访问时，可设置 `SAAS_DEV_WEB_HOST=0.0.0.0`，并把实际访问地址同步加入 `SAAS_PUBLIC_ORIGIN` 与 `SAAS_TRUSTED_HOSTS`。开发静态服务器不能替代生产反向代理。

也可以分终端启动：

```bash
./scripts/dev-api.sh
./scripts/dev-consumer.sh
npm run dev:web
```

如需连接受控测试账号，再单独启动：

```bash
./scripts/dev-worker.sh
```

## 6. 验证

```bash
npm test
python3 tests/repository-contract.py
git diff --check
```

默认测试只使用脱敏样例、临时目录和模拟上游。真实扫码、验证码、安全认证、第三方模型、商品同步、真实订单和履约必须另行安排。

## 7. Git 工作流

```bash
git switch -c feature/<short-name>
git status --short
git add <明确文件路径>
git commit -m "feat: describe the change"
git push -u origin feature/<short-name>
```

提交前确认没有 `.env`、`.local/`、`data/`、运行目录、数据库、日志、测试截图或其他敏感产物。生产部署应由目标环境维护者使用经过审阅的精确版本完成。
# Ubuntu / Debian 本地开发环境搭建

本文档指导在全新的 Ubuntu 或 Debian 系统上搭建 xianyu-saas 的本地源码开发与调试环境。

## 1. 系统要求与环境准备

- **推荐系统**：Ubuntu 22.04 LTS / 24.04 LTS 或 Debian 12
- **基础依赖**：Git 2.40+、Python 3.10+（包含 `python3-venv`）、Node.js 20+、npm 10+

在终端中安装基础系统软件包：
```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential curl
```

安装 Node.js 20.x：
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

检查版本：
```bash
python3 --version
node --version
npm --version
```

## 2. 获取项目源码

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas
```

## 3. 初始化项目与依赖安装

运行初始化脚本，自动创建 Python 虚拟环境并安装全部前后端依赖：
```bash
./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
```

## 4. 本地调试与运行

### 启动全栈开发服务
```bash
npm run dev
```

该命令会同时启动 FastAPI 控制面服务和前端本地静态服务：
- **管理后台**：`http://127.0.0.1:4173/xianyu-saas/`
- **控制面 API**：`http://127.0.0.1:8096`

本地开发模板按默认 Node 服务设置 `SAAS_PUBLIC_ORIGIN=http://127.0.0.1:4173`，`SAAS_TRUSTED_HOSTS=127.0.0.1:4173,127.0.0.1:8096`。若已有 `config/saas.env` 仍使用示例域名，请同步修改；浏览器写请求的来源必须匹配，不要通过放宽 CSRF 检查解决地址不一致。

### 首次使用

默认 `SAAS_BOOTSTRAP_ENABLED=0` 时，全新数据库可直接在登录页创建首位管理员，无需命令或令牌；`SAAS_ALLOW_REGISTRATION=0` 不限制这次注册。服务原子创建管理员、默认店铺的 5 个 JSON 文件及 `ai_knowledge` 目录，成功后自动登录。后续注册只能创建 `owner`，且必须同时打开 `SAAS_ALLOW_REGISTRATION` 与后台 `registration_open`。

> **安全提示**：空站任何能访问者可抢先注册管理员，部署者应先注册再公开分享。

显式启用 `SAAS_BOOTSTRAP_ENABLED=1` 的运维部署保留令牌 bootstrap，不开放无令牌首次注册；历史 CLI 账号仅在默认店铺未使用且已有文件为空默认配置时受限补缺，不覆盖业务或损坏文件。详见 [`ACCESS_MODEL.md`](ACCESS_MODEL.md)。

### 运行自动化测试
```bash
npm test                      # 运行全套本地测试
python3 tests/repository-contract.py  # 检查代码仓库合规性
```

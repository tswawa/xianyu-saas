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

### 运行自动化测试
```bash
npm test                      # 运行全套本地测试
python3 tests/repository-contract.py  # 检查代码仓库合规性
```

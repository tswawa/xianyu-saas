# Ubuntu / Debian 源码开发环境搭建指南

本文档面向需要修改代码或调试系统的开发者，介绍如何在全新的 Ubuntu 或 Debian 系统上搭建 xianyu-saas 的本地源码开发与测试验证环境。

> **普通用户安装入口提示**：若仅需部署和使用 xianyu-saas，请勿通过克隆源码搭建开发环境。普通用户请直接前往 [GitHub Releases](https://github.com/tswawa/xianyu-saas/releases) 下载对应架构的 Ubuntu 安装器（`xianyu-saas-<version>-linux-x86_64` 或 `xianyu-saas-<version>-linux-aarch64`）执行原生安装，或使用推荐的 Docker 部署。详见 [README.md](../README.md) 与 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 1. 系统要求与工具准备

- **推荐操作系统**：Ubuntu 22.04 LTS / 24.04 LTS 或 Debian 12；
- **核心基础软件**：Git 2.40+、Python 3.10+（包含 `python3-venv`）、Node.js 20+、npm 10+。

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

检查并确认版本号：
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

## 3. 开发环境初始化与依赖安装

执行仓库内置的开发初始化脚本，脚本会自动创建 Python 虚拟环境并安装开发调试所需的全部依赖：
```bash
./scripts/bootstrap-dev.sh
```

可选操作（仅在需要执行端到端浏览器自动化与 UI 回归测试时安装）：
```bash
npx playwright install --with-deps chromium
```

生产服务器环境请参考 [`DEPLOYMENT.md`](DEPLOYMENT.md)，仅安装后端和 Worker 的生产运行依赖，不要在生产机器执行 `bootstrap-dev.sh`。

## 4. 本地启动与服务调试

启动本地全栈开发服务：
```bash
npm run dev
```

该命令会并行拉起控制面 API、后台任务消费者（consumer）与前端 Web 静态服务三项核心进程：
- **Web 控制台界面**：`http://127.0.0.1:4173/xianyu-saas/`
- **控制面 API 接口**：`http://127.0.0.1:8096`
- **后台任务消费者**：监控任务队列并驱动异步处理
- **健康检查接口**：`http://127.0.0.1:8096/health`

本地开发环境变量默认配置：
- `SAAS_PUBLIC_ORIGIN=http://127.0.0.1:4173`
- `SAAS_TRUSTED_HOSTS=127.0.0.1:4173,127.0.0.1:8096`

若已有 `config/saas.env` 使用了其他域名，请同步调整。浏览器发送写请求时的 Origin 请求头必须与 `SAAS_PUBLIC_ORIGIN` 保持一致，切勿通过关闭服务端的 CSRF 安全校验来绕过地址不匹配问题。

## 5. 首次使用与管理员初始化

在本地全新启动且数据库为空时（默认配置 `SAAS_BOOTSTRAP_ENABLED=0`）：
- 打开浏览器访问 `http://127.0.0.1:4173/xianyu-saas/`；
- 在登录界面点击「创建首个管理员账号」即可完成初始化注册，无需命令行操作或特定令牌；
- 服务端会原子创建首个管理员账号、默认店铺，并初始化 `redeem_codes.json`、`pan_links.json`、`reply_rules.json`、`automation_settings.json`、`products_config.json` 与 `ai_knowledge` 目录；
- 初始化与后续注册支持安全补齐空目录与纯默认配置残留，严格拒绝业务数据、未知/损坏/非默认配置及符号链接，两入口均接入并发回滚保护（仅清理本次新建且未被认领的目录；SQLite 写事务串行化，已有事务或数据库异常时保守保留存储目录）；
- 初始化成功后系统自动完成登录。

**安全提示**：空数据库对外暴露时任何访问者均可注册成为首个管理员，请务必在完成部署后立即完成首次注册。

后续网页注册仅创建普通店主角色（`owner`），且要求环境变量 `SAAS_ALLOW_REGISTRATION=1` 与系统设置中「开放注册」同时处于开启状态；首次管理员与后续注册开关权限边界保持独立不变。完整的权限作用域说明请参考 [`ACCESS_MODEL.md`](ACCESS_MODEL.md)。

## 6. 本地自动化测试

提交代码前请执行全量本地测试套件：
```bash
# 运行仓库文件与路径合规检查
python3 tests/repository-contract.py

# 运行前后端核心单元测试与流程校验
npm test

# 检查代码格式与多余空白
git diff --check
```

# 新开发环境交接

本文只负责搭建开发环境，不授权部署生产或迁移生产数据。

如果只想快速跑起来，或宿主不是 Linux，用容器方式：

```bash
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```

工作台在 `http://127.0.0.1:4173/xianyu-saas/`，数据在 `./data`。容器方式不需要下面的虚拟环境步骤，但也无法直接运行仓库的测试门禁——需要改代码并跑测试时，仍按下面的源码方式安装。

## 1. 准备环境

需要：

- Ubuntu 22.04 或 24.04
- Git 2.40+
- Python 3.10+，含 `venv`
- Node.js 20+
- npm 10+

系统包可先安装：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential
```

Node.js 请使用受信任的官方发行方式或已有的版本管理器安装，并用 `node --version`、`npm --version` 核对版本。仓库不携带系统安装脚本或第三方安装密钥。

## 2. 克隆私有仓库

推荐使用 GitHub SSH Key，不把 PAT 写进命令、remote URL 或聊天：

```bash
git clone <PRIVATE_REPOSITORY_SSH_URL> deepwhale-xianyu-service
cd deepwhale-xianyu-service
```

`<PRIVATE_REPOSITORY_SSH_URL>` 是明确占位符，需替换为负责人提供的真实私有仓库地址。当前服务器本地仓库尚未配置该远端，文档不猜测仓库名称。

## 3. 安装项目依赖

```bash
./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
```

脚本会创建两个独立虚拟环境：`backend/.venv` 和 `worker/.venv`，并安装本仓库锁定的 Node 依赖。它只从示例生成本机未跟踪的 `config/saas.env`、`worker/.env`，不会复制生产凭据。

## 4. 配置本机

控制台配置：`config/saas.env`。

- 保持 `SAAS_COOKIE_SECURE=0` 仅用于本机 HTTP；生产必须为安全 Cookie。
- 不测试旧平台托管 AI 时让 `SAAS_PLATFORM_AI_KEY` 为空。
- 店铺自填 AI 连接需要服务端加密主密钥；本机未显式配置 `SAAS_AI_MASTER_KEY` 时，`dev-api.sh` 会在 Git 忽略的 `.local/ai-master-key` 中生成并复用权限为 `0600` 的开发主密钥。生产环境必须显式、安全地提供固定主密钥，不能依赖本机开发回退。
- `SAAS_BOT_ROOT=./worker` 让控制面使用单仓库内 worker。

worker 配置：`worker/.env`。

- 默认 `AUTOMATION_MODE=rules`。
- 不连接受控测试账号时保持 `COOKIES_STR` 为空，也不要启动 worker。
- 店铺 AI 内容通过工作台填写自然语言，JSON 只作内部存储/协议；不要让普通店主编辑提示词、结构化知识库或“草稿/发布”状态。
- `reply_rules.json` 与 `automation_settings.json` 只在明确注册或新建店铺时播种。既有账号缺失或损坏这些文件时必须暂停自动化并修复，不能通过重启或访问接口静默恢复默认值。
- 真实商品映射、规则和资料放在 `worker/runtime-data/`，权限设为 `0600`，不要修改仓库里的空模板来保存生产值。

## 5. 启动开发服务

一条命令拉起 API、任务消费者和静态页面，按 `Ctrl+C` 一起停止，日志留在 `.local/dev-logs/`：

```bash
npm run dev
```

也可以分终端单独启动，便于观察单个服务：

```bash
./scripts/dev-api.sh        # 终端一
./scripts/dev-consumer.sh   # 终端二
npm run dev:web             # 终端三
```

浏览器打开：

```text
http://127.0.0.1:4173/xianyu-saas/
```

静态页面服务器默认只绑 `127.0.0.1`。需要从同网段其他设备访问时设 `SAAS_DEV_WEB_HOST=0.0.0.0`，并把该访问地址同时加入 `SAAS_PUBLIC_ORIGIN` 与 `SAAS_TRUSTED_HOSTS`，否则登录等写请求会被来源校验拒绝。该服务器只用于开发，不能替代仓库中的生产 nginx 配置。

注意 `npm run dev` 会监控子进程，任一服务退出即整体停止。单独 kill 其中一个进程会导致整栈退出，重启请直接再跑 `npm run dev`。

worker 仅在已经准备受控测试账号和本地运行态文件时启动：

```bash
./scripts/dev-worker.sh
```

## 6. 验证

```bash
npm test
```

完整门禁包含 Worker 单元测试与浏览器合同，首次运行前需装一次 Chromium。也可按子系统单独运行，命令表见 `README.md` 的测试章节。测试不得读取真实 `.env` 或生产数据库；真实扫码、验证码、安全认证、第三方模型、商品同步和订单履约仍需单独安排，不应伪造成自动化已验证。

## 7. Git 工作流

```bash
git switch -c feature/<short-name>
git status --short
git add <明确文件路径>
git commit -m "feat: describe the change"
git push -u origin feature/<short-name>
```

提交前运行 `npm test` 和 `git diff --check`。不要提交生成的 `.env`、`.local/`、`runtime-data/`、数据库、日志或测试截图。

开发完成后把分支名和 commit SHA 交给生产服务器维护者。生产端应拉取精确提交、重新测试、建立 Git 回滚点后再部署；新 Ubuntu 开发机不直接写生产目录。

## 8. 交给协作者或 AI 助手

先完整读取：

1. `handoff/AGENTS.md`
2. `handoff/MEMORY.md`
3. 本文件
4. `README.md`

如果所用客户端支持项目级指令或长期记忆，可由用户手动把上述内容加入对应位置；不要自动覆盖已有根目录规则，也不要把本机 `.env` 内容加入记忆。

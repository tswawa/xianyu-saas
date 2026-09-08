# 生产部署指南

本文档介绍如何在服务器上部署 xianyu-saas，支持 **Docker Compose（推荐，最省心）** 与 **Linux systemd 原生服务** 两种部署方式。

## 方式一：Docker Compose 部署（推荐）

适用于各类 Linux 服务器、本地开发或轻量云主机，无需手动配置 Python/Node 环境。

### 1. 准备环境
- 安装 Docker 20.10+ 与 Docker Compose v2。

### 2. 克隆仓库并配置
```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

# 复制环境变量模板
cp config/saas.env.docker.example config/saas.env
```

根据实际情况修改 `config/saas.env`：
- `SAAS_PUBLIC_ORIGIN`：本机访问保持模板的 `http://127.0.0.1:4173`；通过反向代理发布时改为浏览器实际来源（例如 `https://xianyu.example.com`，包含协议和端口、不含路径），并同步 `SAAS_TRUSTED_HOSTS` 中的实际 Host；
- `SAAS_COOKIE_SECURE`：如果启用了 HTTPS，设为 `1`；纯 HTTP 调试设为 `0`；
- `SAAS_AI_MASTER_KEY`：设置一个高强度的随机密钥（用于加密各店铺配置的 API Key）。

### 3. 构建并启动
```bash
docker compose up -d --build
```

- **管理后台访问**：`http://127.0.0.1:4173/xianyu-saas/`
- **健康检查地址**：`http://127.0.0.1:8096/health`
- **数据持久化**：SQLite 数据库、店铺配置及卡密库存保存在本地 `./data` 目录，容器重启或重建镜像数据不丢失。

以上地址用于宿主机本地访问（Compose 默认绑定回环地址）；远程访问应通过受控隧道或已配置的反向代理，并保持浏览器来源与配置一致，不能只把地址替换为服务器 IP。来源不匹配时应修正配置，不要放宽 CSRF 检查。

查看日志或停止：
```bash
docker compose logs -f
docker compose down
```

---

## 方式二：Linux systemd 原生服务部署

适用于需要与宿主机 systemd 深度集成、使用独立守护进程管理的生产环境。

### 1. 系统依赖
- 操作系统：Ubuntu 22.04+ 或 Debian 12
- Python 3.10+（包含 `python3-venv`）、Git 2.40+、Nginx
- 前端由 Nginx 直接提供静态文件；生产部署无需 Node.js/npm、Playwright 或 Chromium。

### 2. 初始化环境
```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
python3 -m venv worker/.venv
worker/.venv/bin/python -m pip install -r worker/requirements.txt
```

生产环境只安装上述运行依赖，不执行 `scripts/bootstrap-dev.sh`、`npm test` 等开发测试命令。已有部署沿用当前服务的虚拟环境和数据目录。

### 3. 配置服务
参考 `deploy/systemd/` 和 `deploy/nginx/` 中的模板配置控制面、任务消费者与静态页面服务；模板内路径须与实际安装位置一致。

---

## 版本信息与更新方式

- 网页显示的是已安装代码的版本、构建信息和最近一次发布检查结果。尚无 Release、未发现更高版本、制品缺失与网络失败会分别显示；CHANGELOG 中其他版本的条目不代表当前已安装版本。
- **Docker 源码构建**：网页只检查发布信息，不控制宿主机 Docker。先备份实际数据挂载、取得并核对目标源码，再使用原 Compose 文件及本地覆盖配置执行 `up -d --build --wait`。例如有 `.local/docker-compose.local.yml` 时，必须继续使用 `docker compose -f docker-compose.yml -f .local/docker-compose.local.yml up -d --build --wait`，不要丢弃原端口或数据卷配置，也不要执行 `down -v`。当前源码构建方式不依赖公共预构建镜像，不能用 `docker compose pull` 代替源码更新。
- **构建信息**：Docker 构建时写入时间并校验 `package.json` 与后端版本一致。可通过构建环境变量 `SAAS_BUILD_COMMIT` 和 `SAAS_BUILD_DIRTY=true/false` 提供真实提交号与本地修改状态；未提供时明确显示未知，不在运行时读取 Git。
- **systemd 签名部署**：API/消费者模板标记 `SAAS_DEPLOYMENT_MODE=systemd`，但这本身不代表已启用在线更新。必须配置可信的版本目录与 `current` 链接、签名公钥、可写的 staging/intent 目录，以及已加载的 `xianyu-saas-updater.service` 和活动的 `xianyu-saas-updater.path`；监听路径须与 `SAAS_UPDATE_INTENT_FILE` 一致。页面仅在这些条件可核验时开放安装，并仍要求签名校验和管理员二次确认。普通源码部署不应伪装成签名发布安装。

## 统一模型连接与本地网络

- 模型连接统一在「设置」管理，按登录用户隔离。服务端加密存储与主密钥、SQLite 数据库应一起备份。旧店铺连接不会自动迁移，需用户明确选择并测试确认；共享连接删除后不会回退旧密钥。
- 若连接测试提示 `dns_fake_ip`，说明本地代理返回了 `198.18.0.0/15` Fake-IP。应在代理 DNS 配置中排除对应模型域名，或仅在本地 Compose 覆盖文件中使用经核实的域名解析；不要关闭私网地址保护、TLS 验证或改成任意目标代理。固定解析随服务 IP 变化需要重新核实。
- 智能运维仅对所选店铺的客服资料和规则生成修改方案，确认后逐项执行。页面会区分待确认、成功、部分失败和需复核；不会执行系统命令、真实发货或库存扣减。

## 首次使用与账号初始化

默认 `SAAS_BOOTSTRAP_ENABLED=0` 时，全新数据库允许在登录页直接创建首位管理员，无需手工管理员命令或令牌，且不受 `SAAS_ALLOW_REGISTRATION=0` 对后续注册的限制。服务原子创建首位管理员与默认店铺，初始化 5 个 JSON 配置文件和 `ai_knowledge` 目录，成功后自动登录。

> **安全提示**：空站任何能访问者可抢先注册管理员，部署者应先注册再公开分享。

后续网页注册仅创建 `owner`，必须同时打开 `SAAS_ALLOW_REGISTRATION=1` 和后台 `registration_open`。已显式启用 `SAAS_BOOTSTRAP_ENABLED=1` 的运维部署仍使用原令牌 bootstrap，不开放无令牌首次注册；初始化后应关闭开关并移除令牌。

已有 CLI 账号若创建时未传 `initializer`，仅在登录时确认默认店铺从未使用、现有文件为空默认配置后受限补缺，不覆盖业务或损坏文件，也不重置已使用店铺。实际用户 ID 与存储路径由服务确定，完整初始化边界见 [`ACCESS_MODEL.md`](ACCESS_MODEL.md)。

## 生产安全建议

1. **反向代理与 TLS**：强烈建议在前端挂载 Nginx 并配置 SSL 证书（HTTPS），仅将 443 端口对外暴露；
2. **关闭后续公开注册**：私有部署保持 `SAAS_ALLOW_REGISTRATION=0`；这不阻止默认空站的首次注册，须先创建管理员再公开分享；
3. **定期备份**：定期对 `./data` 目录进行冷备份，确保数据库和各店铺运行配置安全；
4. **主密钥保护**：妥善保管 `SAAS_AI_MASTER_KEY`，切勿泄露或遗失。

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
- Python 3.10+、Node.js 20+、npm 10+、Git 2.40+

### 2. 初始化环境
```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
```

### 3. 配置服务
参考 `deploy/systemd/` 中的服务模板配置控制面与任务消费者守护进程。

---

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

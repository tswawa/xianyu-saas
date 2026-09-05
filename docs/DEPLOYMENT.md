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
- `SAAS_PUBLIC_ORIGIN`：填写你的访问域名或公网 IP（例如 `https://xianyu.example.com`）；
- `SAAS_COOKIE_SECURE`：如果启用了 HTTPS，设为 `1`；纯 HTTP 调试设为 `0`；
- `SAAS_AI_MASTER_KEY`：设置一个高强度的随机密钥（用于加密各店铺配置的 API Key）。

### 3. 构建并启动
```bash
docker compose up -d --build
```

- **管理后台访问**：`http://你的服务器IP:4173/xianyu-saas/`
- **健康检查地址**：`http://你的服务器IP:8096/health`
- **数据持久化**：SQLite 数据库、店铺配置及卡密库存保存在本地 `./data` 目录，容器重启或重建镜像数据不丢失。

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

## 生产安全建议

1. **反向代理与 TLS**：强烈建议在前端挂载 Nginx 并配置 SSL 证书（HTTPS），仅将 443 端口对外暴露；
2. **关闭公开注册**：部署完成后，务必确认 `SAAS_ALLOW_REGISTRATION=0`，防止外部人员注册；
3. **定期备份**：定期对 `./data` 目录进行冷备份，确保数据库和各店铺运行配置安全；
4. **主密钥保护**：妥善保管 `SAAS_AI_MASTER_KEY`，切勿泄露或遗失。

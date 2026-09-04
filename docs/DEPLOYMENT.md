# 部署指南

本文面向自托管维护者。仓库提供配置模板和部署步骤，不包含生产凭据、真实业务数据或固定主机路径。部署时请在目标环境明确设置域名、用户、目录、密钥和权限。

## 选择部署方式

| 方式 | 适用场景 | 签名自动更新 |
| --- | --- | --- |
| Docker Compose | 开发、自托管、非 Linux 宿主 | 不支持，需重建镜像 |
| 版本化 Release + systemd | Linux 生产环境 | 支持 |

两种方式应视为互斥方案：容器方式不使用 `current` 符号链接，`deploy/updater/` 的原子切换和回滚只适用于 systemd。

## Docker Compose

需要 Docker 20.10+ 和 Docker Compose v2。backend 与 worker 必须在同一镜像中，因为控制面会在容器内派生店铺 Worker。

```bash
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```

部署要点：

- 数据库、店铺目录和模型主密钥位于数据卷 `./data`（容器内 `/data`）；备份前应停止写入并复制该目录；
- `SAAS_PUBLIC_ORIGIN` 和 `SAAS_TRUSTED_HOSTS` 必须与浏览器实际访问地址一致；
- compose 默认将端口绑定到宿主回环地址。对外提供服务时保留回环映射，在前面配置 TLS 反向代理，不要直接公开容器端口；
- 容器服务使用非特权用户运行，代码目录不可写；
- 生产环境应显式提供 `SAAS_AI_MASTER_KEY`，不要依赖首次启动生成的回退密钥；
- 升级通过拉取经过审阅的代码并重新执行 `docker compose up -d --build`，管理后台的签名更新入口不适用于容器方式。

首次启动后至少检查 `/health`、`/api/ready`、工作台资源和容器内 Worker 派生。镜像构建与真实平台连接应在目标环境单独验证。

## systemd 版本化部署

以下变量只是示例名称，请替换为目标环境值：

| 变量 | 用途 |
| --- | --- |
| `$INSTALL_ROOT` | 版本化代码和共享运行时的根目录 |
| `$RELEASES_ROOT` | 每个版本一个只读目录 |
| `$CURRENT_LINK` | 指向当前版本的符号链接 |
| `$RUNTIME_ROOT` | Python 虚拟环境和共享运行时 |
| `$STATE_ROOT` | 数据库、账号目录、任务、更新暂存和备份 |
| `$UPDATE_STAGING` | 候选版本暂存目录 |
| `$UPDATE_INTENTS` | 一次性更新意图目录 |
| `$BACKUP_ROOT` | SQLite 备份目录 |
| `$ENV_FILE` | 权限为 `0600` 的环境文件 |
| `$SIGNING_KEY` | Ed25519 发布公钥文件 |

真实路径、数据库、Cookie、Token、订单、库存和日志不能提交到 Git。

### 目录和依赖

- 每个 Release 解包到 `$RELEASES_ROOT/<version>`，运行数据始终位于 `$STATE_ROOT` 或独立密钥存储；
- `$CURRENT_LINK` 只能指向 `$RELEASES_ROOT` 的直接子目录；
- Python 运行时位于 `$RUNTIME_ROOT`，自动更新不执行依赖安装；
- API、任务消费者和 Worker 使用最小权限用户；release 目录切换后视为只读；
- 生产配置至少包含 `SAAS_ENV=production`、安全 Cookie、关闭公开注册、关闭浏览器日志和有效的 `SAAS_AI_MASTER_KEY`；
- 更新公钥、数据库、账号目录和环境文件应使用部署用户可读的最小权限。

### 首位管理员

空数据库不会因第一个公网请求自动开放注册。推荐流程：

1. 在受控维护入口确认数据库为空，并保持公开注册关闭；
2. 生成高熵一次性 bootstrap 令牌，写入权限为 `0600` 的 credential 文件；
3. 临时启用 bootstrap 配置，只允许受信任的维护来源访问；
4. 在登录页创建首位管理员，服务端在同一事务中创建账号并消费令牌；
5. 确认管理员角色和 bootstrap 已关闭；
6. 删除 credential、恢复 `SAAS_BOOTSTRAP_ENABLED=0`，重新加载并重启 API。

令牌不得放入 URL、命令行参数、日志、数据库、提交信息或普通环境变量。

### 签名 Release

更新器只接受服务端固定的 Release 来源和按版本命名的三项制品：

- `xianyu-saas-<version>.tar.gz`；
- `xianyu-saas-<version>.manifest.json`；
- `xianyu-saas-<version>.manifest.sig`。

manifest 覆盖版本、路径、大小和 SHA-256，并使用部署时固定的 Ed25519 公钥验签。下载和解包会拒绝重定向、版本降级、路径穿越、绝对路径、链接文件、设备文件、运行数据路径和超限归档。

当前更新源由服务端实现固定，浏览器不能提交任意 URL、仓库、owner 或路径。使用 fork 或自建 Release 源时，发布前必须同时检查更新器源码、签名公钥、制品命名和相关合同；未配置公钥时更新应安全失败。

### updater 流程

1. API 将已验证的候选版本写入暂存目录和一次性更新意图；API 不执行 `systemctl`，也不直接切换当前版本；
2. updater 取得文件锁，重新验证意图、manifest、全部文件哈希和签名；
3. 停止 API/消费者，建立 SQLite 一致性备份，将候选版本安装到独立目录；
4. 使用临时符号链接和原子替换切换 `$CURRENT_LINK`；
5. 依次检查 `/health`、`/api/ready`、匿名鉴权响应、公开版本信息和静态资源；
6. 检查失败时切回旧版本并重新验证，成功后清理已消费意图和过旧版本。

代码回滚不回滚数据库、账号目录、库存或订单数据。破坏性迁移必须走独立维护流程并先完成恢复演练。

## 发布前检查

```bash
python3 tests/repository-contract.py
npm test
git diff --check
```

还应检查：

- Nginx 只代理预期路径，内部接口不会公开；
- 来源校验、请求体上限、限流和安全响应头已启用；
- API 只监听受信任的本机或内网地址；
- 生产密钥、Cookie、账号、买家内容、库存、数据库和日志不在 Git、命令输出或发布包中；
- 真实平台、第三方模型和真实履约有独立的受控验收记录。

## 回滚

健康检查失败时，systemd updater 只回滚代码链接，不回滚运行数据。人工回滚仍需管理员确认、一次性意图、文件锁、备份和完整健康检查；不能从浏览器提交任意路径。若旧版本与当前数据库不兼容，应停止写入，在隔离环境验证备份后再执行恢复方案。

# 部署指南

本文面向负责部署的维护者。仓库只提供模板和验证步骤，不包含任何生产凭据、真实运行态或固定主机信息。生产部署应在目标环境中明确设置路径、域名、用户、密钥和权限。

## 推荐目录变量

以下变量仅用于说明，部署时请替换为目标环境的实际值：

| 变量 | 用途 |
| --- | --- |
| `$APP_ROOT` | 后端和 Worker 的只读应用目录 |
| `$UI_ROOT` | nginx 提供的前端静态目录 |
| `$STATE_ROOT` | 账号数据、任务数据库和运行态文件 |
| `$ENV_FILE` | 权限为 `0600` 的生产环境文件 |

不要把这些变量展开后的真实主机路径、数据库、Cookie、Token、订单或日志提交到 Git。

## 组件

| 仓库路径 | 部署角色 |
| --- | --- |
| `backend/` | FastAPI 控制面 |
| `frontend/` | 静态工作台 |
| `worker/` | 店铺消息与履约 Worker |
| `backend/job_consumer.py` | 异步同步任务消费者 |
| `deploy/nginx/` | 反向代理、限流和安全响应头 |
| `deploy/systemd/` | API 与消费者服务模板 |
| `worker/systemd/` | Worker 服务模板 |

## 发布前检查

1. 从私有仓库拉取负责人确认的精确提交。
2. 检查工作树、变更范围和仓库脱敏合同。
3. 在候选代码树创建独立虚拟环境，安装 runtime 与开发依赖并运行 `npm test`。
4. 为当前服务和配置建立可恢复的 Git bundle 或 snapshot；不要使用未经审阅的目录覆盖作为回滚方案。
5. 确认目标环境的运行用户、目录权限、TLS、公开 Origin、代理信任和密钥文件已准备好。
6. 真实账号、真实订单和真实发货必须使用受控验收窗口，不得用生产订单做自动化盲测。

## 安装与配置

生产控制面只安装 `backend/requirements.txt` 中的运行依赖；开发/CI 测试额外使用 `backend/requirements-dev.txt`。Worker 使用 `worker/requirements.txt`。

建议通过环境文件或密钥管理系统提供：

- `SAAS_DB`、`SAAS_TENANTS_DIR`；
- `SAAS_PUBLIC_ORIGIN`、`SAAS_TRUSTED_HOSTS`；
- `SAAS_AI_MASTER_KEY` 及其他必要的上游连接参数；
- 生产 Cookie 安全开关、任务租约、限流和资源上限。

生产必须保持：

- `SAAS_ENV=production`；
- `SAAS_ALLOW_REGISTRATION=0`；
- `SAAS_BROWSER_LOGS_MODE=off`；
- `SAAS_COOKIE_SECURE=1`；
- `SAAS_TESTING` 未设置或为 `0`；
- Worker 和 API 使用独立的最小权限用户，运行态目录不可被应用代码之外的用户读取。

## 发布顺序

1. 先安装并验证 Worker 代码和依赖。
2. 安装控制面后端、任务消费者和前端静态文件；排除 `.env`、`.venv`、数据库、运行态、日志和测试产物。
3. 对 nginx 候选配置执行 `nginx -t`，确认内部路径不会被公开代理。
4. 重新加载 nginx，再按顺序重启 API、任务消费者和 Worker。
5. 检查 systemd 的 `ActiveState`、`SubState`、重启次数、资源限制和脱敏日志。
6. 检查 `/health`、`/api/ready`、首页、版本化静态资源、匿名鉴权响应和桌面/移动页面。

## 上线闸门

- `npm test`、`git diff --check` 和仓库脱敏合同全绿；
- 源码、静态资源和候选部署树的哈希一致；
- nginx 限流、请求体上限、代理信任、内部路径 404 和安全响应头存在；
- API 只监听受信任的本机代理或明确的内网地址；
- Cookie、Token、账号、买家正文、库存、数据库和日志没有进入 Git、命令输出或发布包；
- 真实平台和真实履约验收有独立记录，并明确区分已验证与未验证。

## 回滚

从发布前的 Git bundle/snapshot 恢复到临时目录，审阅后再安装旧版本。不要在开发工作树执行破坏性 reset，也不要把新旧运行态库存、数据库或账号目录混合。回滚后重复健康、权限、静态资源和只读数据完整性检查。

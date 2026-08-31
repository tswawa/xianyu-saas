# 部署指南

本文面向负责部署的维护者。仓库只提供模板和验证步骤，不包含任何生产凭据、真实运行态或固定主机信息。生产部署应在目标环境中明确设置路径、域名、用户、密钥和权限。

## 推荐目录变量

以下变量仅用于说明，部署时请替换为目标环境的实际值：

| 变量 | 用途 |
| --- | --- |
| `$INSTALL_ROOT` | 版本化代码与共享运行时的安装根目录 |
| `$RELEASES_ROOT` | `$INSTALL_ROOT/releases`，每个语义版本一个只读目录 |
| `$CURRENT_LINK` | `$INSTALL_ROOT/current`，原子指向当前版本的符号链接 |
| `$RUNTIME_ROOT` | 独立于 release 的 Python 虚拟环境与共享运行时 |
| `$STATE_ROOT` | 数据库、租户目录、任务状态、更新 staging、意图与备份 |
| `$UPDATE_STAGING` | `$STATE_ROOT/update-staging`，API 写入已验签候选版本 |
| `$UPDATE_INTENTS` | `$STATE_ROOT/update-intents`，只保存 0600 一次性更新意图 |
| `$BACKUP_ROOT` | `$STATE_ROOT/backups`，SQLite 一致性备份与非敏感元数据 |
| `$ENV_FILE` | 权限为 `0600` 的生产环境文件 |
| `$SIGNING_KEY` | root 管理、不可写的 Ed25519 发布公钥文件 |

不要把这些变量展开后的真实主机路径、数据库、Cookie、Token、订单或日志提交到 Git。

## 组件

| 仓库路径 | 部署角色 |
| --- | --- |
| `backend/` | FastAPI 控制面 |
| `frontend/` | 静态工作台 |
| `worker/` | 店铺消息与履约 Worker |
| `backend/job_consumer.py` | 异步同步任务消费者 |
| `deploy/nginx/` | 反向代理、限流和安全响应头 |
| `deploy/systemd/` | API、消费者和独立 updater 服务模板 |
| `deploy/updater/` | 只消费已验签意图的原子切换与回滚程序 |
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
- `SAAS_ALLOW_REGISTRATION=0`，确需短时开放公开注册时再由维护者同时调整部署上限和后台数据库开关；
- `SAAS_BOOTSTRAP_ENABLED=0`，仅在首次管理员初始化窗口短时开启；
- `SAAS_ADMIN_TOKEN` 为空，或仅以独立秘密提供给 loopback 应急流程；
- `SAAS_BROWSER_LOGS_MODE=off`；
- `SAAS_COOKIE_SECURE=1`；
- `SAAS_TESTING` 未设置或为 `0`；
- `SAAS_UPDATE_PUBLIC_KEY_FILE=$SIGNING_KEY`，公钥为普通文件、不可被组或其他用户写入；
- `SAAS_CURRENT_ROOT=$CURRENT_LINK`、`SAAS_RELEASES_DIR=$RELEASES_ROOT`、`SAAS_UPDATE_STAGING_DIR=$UPDATE_STAGING`、`SAAS_UPDATE_INTENT_FILE=$UPDATE_INTENTS/intent.json`、`SAAS_UPDATE_BACKUP_DIR=$BACKUP_ROOT`；
- Worker 和 API 使用独立的最小权限用户，运行态目录不可被应用代码之外的用户读取。

## 首次管理员 bootstrap

空数据库不会自动开放注册，也不能依赖公网第一个请求抢占管理员。推荐的受控流程如下：

1. 在 API 尚未对公网开放写入时，确认 `users` 为空、数据库 bootstrap 状态为 `pending`，并保持 `SAAS_ALLOW_REGISTRATION=0`。
2. 由运维在目标机生成高熵一次性令牌，写入 root 管理且权限为 `0600` 的 credential 文件；不要把令牌放入环境文件、URL、命令行参数、工单、日志或数据库。
3. 临时安装 `deploy/systemd/xianyu-saas-bootstrap.conf.example` 对应的 systemd drop-in。模板通过 `LoadCredential` 挂载令牌，并把 `SAAS_BOOTSTRAP_TRUSTED_SOURCES` 限制为本机维护来源。
4. 执行 daemon reload 并重启 API；只从受信任的本机/维护入口打开登录页。`GET /api/auth/capabilities` 应只显示 `bootstrap_available=true`，不返回令牌、摘要或文件路径。
5. 在“创建首个管理员账号”表单中输入一次性令牌。浏览器只通过 `X-Bootstrap-Token` 请求头发送它；服务端在同一 SQLite 事务内创建首位 `admin` 并消费令牌。
6. 登录后确认 `/api/me` 的角色为 `admin`，再次读取 capabilities 应显示 bootstrap 已关闭；原令牌重放必须失败。
7. 立即删除临时 drop-in 与 credential 文件，把 `SAAS_BOOTSTRAP_ENABLED` 恢复为 `0`，daemon reload 后重启 API，并再次确认 bootstrap 不可用。

已有用户的旧实例不走 bootstrap：迁移会在没有管理员时幂等提升最早用户。维护者应在受控窗口验证该用户身份、补改密码并复核其他活动会话。

## 版本化发布目录

- 每个 Release 解包到 `$RELEASES_ROOT/<version>`，目录内只包含受签名清单约束的代码和静态资源；数据库、租户目录、Cookie、任务、日志、备份与密钥必须始终位于 `$STATE_ROOT` 或独立秘密存储。
- `$CURRENT_LINK` 只能是指向 `$RELEASES_ROOT` 直接子目录的符号链接。API、consumer 和 nginx 静态 alias 都从该链接读取，避免切换期间混用两棵代码树。
- Python 运行时位于 `$RUNTIME_ROOT`。自动更新拒绝运行依赖变化，不在更新过程中执行 `pip install`；依赖升级必须由维护者审阅并作为独立部署步骤完成。
- release 目录在切换完成后视为只读。API 只能写 `$UPDATE_STAGING` 和 0600 意图，不能写 `$RELEASES_ROOT`、执行 `systemctl` 或修改 `$CURRENT_LINK`。

## 签名 Release 制品

发布源的 GitHub owner、repository、API host 和允许通道固定在服务端；浏览器不能提交 URL、仓库或重定向目标。更新器只识别每个语义版本 Release 中按固定名称匹配的以下三项：

- `xianyu-saas-<version>.tar.gz`；
- `xianyu-saas-<version>.manifest.json`；
- `xianyu-saas-<version>.manifest.sig`。

manifest 必须覆盖制品版本、文件路径、大小和 SHA-256，并使用部署时固定的 Ed25519 公钥验证 detached signature。下载和解包会拒绝重定向、版本降级、路径穿越、绝对路径、重复文件、软/硬链接、设备文件、超限文件/文件数/解包体积、运行数据路径以及未批准的依赖变化。私有仓库只读凭据仅由 API 密钥存储提供，不能进入数据库、Release、页面、日志或 updater 子进程环境。

## 独立 updater

1. 安装并启用 `xianyu-saas-updater.path`；它只监听 `$UPDATE_INTENTS/intent.json`，触发一次性 `xianyu-saas-updater.service`。
2. updater 以独立 root oneshot 运行，但 systemd 将可写路径收敛到 `$INSTALL_ROOT` 与 `$STATE_ROOT`；API/consumer 仍以非特权账号运行。
3. updater 先取得 `$STATE_ROOT` 内的文件锁，再重新验证意图权限、nonce、候选路径、版本、manifest 哈希、全部文件哈希和 Ed25519 签名，不能信任 API 阶段的历史结果。
4. 切换前记录 Worker 期望状态，并用 SQLite backup API 创建一致性备份和完整性检查；备份文件权限为 `0600`。迁移只运行目标 release 自带的数据库迁移入口，且必须保持向前兼容。
5. updater 停止 API 与 consumer，把候选 release 安装到独立版本目录，以临时 symlink 加 `os.replace` 原子替换 `$CURRENT_LINK`，再启动服务。
6. 健康检查依次验证 `/health`、`/api/ready`、未登录 `/api/me`、`/api/version/public` 与静态首页资源版本。任何一步失败都会停止部分启动的服务、原子切回旧 release、重启并复核旧版本健康。
7. 成功后清除已消费意图、恢复 Worker 期望状态并清理旧版本。系统保留当前版本在内的最近 3 个 release，并保留最近 5 份 SQLite 备份；生产备份仍应另行纳入加密、异机和恢复演练策略。

## 迁移纪律

- 代码回滚不会回滚运行数据，因此数据库迁移只能增加向前/向后兼容的表、列或索引；不能在同一发布中删除旧代码仍需的数据或执行不可逆重写。
- 需要破坏性数据迁移时，必须停止自动更新、建立经恢复演练的独立备份，并走单独维护计划；不得通过普通 Release 意图执行。
- updater 的 SQLite backup 是发布前安全点，不替代持续备份。恢复备份前必须先停所有写入进程，并在隔离副本验证完整性和目标版本兼容性。

## 发布顺序

### 首次安装或依赖变更

1. 在隔离目录校验签名制品和 manifest，把候选代码安装为 `$RELEASES_ROOT/<version>`；排除 `.env`、虚拟环境、数据库、运行态、日志和测试产物。
2. 在 `$RUNTIME_ROOT` 安装并审阅 API、consumer 与 Worker 依赖；依赖锁变化不能交给自动 updater。
3. 建立 `$CURRENT_LINK` 的临时符号链接并原子替换正式链接，安装 API、consumer、updater path/service、Worker 与 nginx 模板。
4. 对 nginx 候选配置执行 `nginx -t`，确认内部路径不会被公开代理，管理/认证/更新端点具备独立限流与请求体上限。
5. 重新加载 nginx，再按顺序启动 API、任务消费者和需要恢复的 Worker；首次空库按 bootstrap 章节完成管理员初始化。
6. 检查 systemd 的 `ActiveState`、`SubState`、重启次数、资源限制和脱敏日志，并检查 `/health`、`/api/ready`、首页、版本化静态资源、匿名鉴权响应和桌面/移动页面。

### 日常签名更新

1. 管理员在“项目说明 → 版本与更新”检查版本；检查结果只读取固定 Release 源。
2. 下载操作在 staging 完成签名、manifest、SHA-256、归档和依赖校验，不会立即切换运行代码。
3. 应用或回滚前重新输入管理员密码，取得与具体 action 绑定、短期且一次性的确认令牌；API 只写更新意图。
4. updater path 自动触发独立 oneshot，完成备份、原子切换、健康检查和必要时自动回滚。
5. 管理员重新登录后核对版本、更新状态、安全审计和 Worker 期望状态；真实闲鱼、模型和履约仍需独立受控验收。

## 上线闸门

- `npm test`、`git diff --check` 和仓库脱敏合同全绿；
- 源码、静态资源和候选部署树的哈希一致；
- nginx 限流、请求体上限、代理信任、内部路径 404 和安全响应头存在；
- API 只监听受信任的本机代理或明确的内网地址；
- Cookie、Token、账号、买家正文、库存、数据库和日志没有进入 Git、命令输出或发布包；
- 真实平台和真实履约验收有独立记录，并明确区分已验证与未验证。

## 回滚

- 健康检查失败时，updater 自动把 `$CURRENT_LINK` 原子切回更新前版本并复核旧版本健康；这只回滚代码链接，不回滚数据库或租户运行数据。
- 人工回滚只能选择 updater 列出的已安装、更低语义版本，仍要求管理员密码二次确认、一次性意图、文件锁、备份和完整健康检查；不能由浏览器提交任意路径。
- 如果旧版本不再与当前数据库兼容，不得强行切换。应停止写入、在隔离环境验证备份恢复和兼容性，再执行单独的灾难恢复计划。
- Git bundle/snapshot 只作为源码与发布制品的补充恢复手段；不要在开发工作树执行破坏性 reset，也不要把不同时间点的运行态数据库、库存、账号目录或 Cookie 混合。
- 回滚后复核角色与注册开关、bootstrap 已消费状态、会话撤销、静态资源版本、consumer/Worker 期望状态及只读数据完整性，并明确记录哪些检查尚未在真实平台完成。

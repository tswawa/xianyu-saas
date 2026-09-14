# 生产部署指南

本文档介绍如何在服务器或本地主机上部署 xianyu-saas，支持 **Docker Compose（推荐部署方式）**、**Linux systemd 原生服务** 以及 **Windows 宿主机环境（Docker 容器）**。

## 部署方式一：Docker Compose（推荐）

该方式适用于各类 Linux 服务器与本地容器环境，环境依赖自包含，升级与维护流程清晰。

### 1. 基础环境
- 安装 Docker 20.10+ 及 Docker Compose v2。

### 2. 获取代码
```bash
git clone https://github.com/tswawa/xianyu-saas.git /srv/xianyu-saas
cd /srv/xianyu-saas
```

### 3. 一键安装并验收（推荐）
```bash
sudo bash deploy/docker-install.sh
```

脚本在全新安装时按固定顺序完成：缺失时从示例创建 `config/saas.env`（0600，同时通过 `SAAS_ENV_FILE` 作为服务 env_file，`--env-file` 可同时替换插值与容器环境来源）；渲染 `docker-compose.yml` + `docker-compose.updates.yml` 并构建镜像；把仓库内置的 `deploy/update-signing.pub` 复制为容器内 root 拥有的本地副本（`./.local/docker-install/`）；仅为全新 `./data` 目录设置容器用户属主（非递归，不改动已有数据）；启动应用与独立更新器；等待容器和 `/health` 健康后执行一次性更新器登记；最后在应用容器内调用 `platform_update.update_capabilities()` 验收真实更新就绪能力。

- **信任来源**：只信任当前源码检出自带的公钥；脚本不会从发布资产下载替换公钥。请通过 HTTPS 克隆官方仓库并核对来源后再运行；已登记部署若要更换公钥必须先人工处理，脚本不会静默替换。
- **幂等与升级保护**：检测到受管安装（更新器容器、受管镜像或更新器私有卷）后不再构建；已由网页更新过的受管镜像不会被重建或降级。受管重跑只校验并启动既有容器（停止的容器用 `docker start` 保留原镜像与配置），缺失或部分状态会明确报错，不会补建、重新登记或删除任何东西。
- **重跑不应用新配置**：受管重跑不会读取当前检出的 compose/env 变更应用到运行容器；调整端口、挂载、环境等需要人工维护。本地 compose 只用于解析项目与执行 `config`/`exec`。
- **Linux 与本地引擎**：安装脚本只支持 Linux Bash 与本地 Linux Docker 引擎；不支持 `DOCKER_HOST`/远程 `DOCKER_CONTEXT`，请使用真实 Linux 路径（Windows 请用 WSL Linux 环境）。
- **超时与诊断**：容器与更新就绪都有上限（默认 300 秒，可用 `--timeout` 调整）；失败信息包含安全的原因码，详细排查使用 `docker compose logs`。

安装成功后可访问：
- **Web 控制台访问入口**：`http://127.0.0.1:4173/xianyu-saas/`
- **控制面健康检查接口**：`http://127.0.0.1:8096/health`

根据实际网络拓扑编辑 `config/saas.env`（安装脚本只在缺失时创建）：
- `SAAS_PUBLIC_ORIGIN`：本机测试使用 `http://127.0.0.1:4173`。经由 Nginx 等反向代理对外提供服务时，填写浏览器实际访问的完整来源（例如 `https://xianyu.example.com`，包含协议与端口，不带末尾路径）；
- `SAAS_TRUSTED_HOSTS`：填写允许的 Host 列表，多个以英文逗号分隔（例如 `127.0.0.1:4173,xianyu.example.com`）；
- `SAAS_COOKIE_SECURE`：对外启用 HTTPS 时设置为 `1`；纯 HTTP 内部调试设置为 `0`；
- `SAAS_AI_MASTER_KEY`：填写用于服务端加密保存 API Key 的主密钥（32 字节随机值做标准 Base64 编码，编码后长度 44 字符，可通过命令 `python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` 生成）；
- 受管安装的重复执行只校验并启动既有容器，不会把当前检出或 `config/saas.env` 的新配置应用到运行容器；调整端口、挂载、环境等需要人工维护（必要时按维护窗口重建，并保留数据与更新器私有状态）。

### 4. 手工源码开发与旧式启动
不使用独立更新器、仅做本地源码开发时，仍可按原方式手工启动（此路径不安装网页升级能力）：
```bash
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```
- **业务数据持久化**：SQLite 数据库、店铺会话、配置文件与卡密库存统一挂载在宿主机 `./data` 目录，容器更新与镜像重建不丢失数据。

服务默认绑定宿主机回环地址。如果需要远程访问，请通过受控反向代理（如 Nginx）暴露服务，并确保反代配置透传真实的 Host 与 Origin 请求头，浏览器访问地址必须与 `SAAS_PUBLIC_ORIGIN` 完全一致。

### 5. 查看日志、暂停应用与恢复
```bash
# 查看实时日志（config/saas.env 仅 root 可读，建议 sudo）
sudo docker logs -f xianyu-saas

# 暂停应用，保留原容器、镜像和配置
sudo docker stop xianyu-saas
# 恢复既有受管容器并确认更新能力
sudo bash deploy/docker-install.sh
```

---

## 部署方式二：Linux systemd 原生守护进程

适用于需要由宿主机 systemd 直接接管进程生命周期、集中采集日志的生产环境。

### 1. 系统要求与运行环境
- 操作系统：Ubuntu 22.04 LTS / 24.04 LTS 或 Debian 12；
- 软件要求：Python 3.10+（包含 `python3-venv`）、Git 2.40+、Nginx；
- 前端静态文件直接由 Nginx 分发，生产运行时无需安装 Node.js、npm 或 Chromium。

### 2. 版本化路径布局与虚拟环境

仓库中 `deploy/systemd/` 下的服务单元模板采用版本化目录布局规范（通过 `current` 软链接指向活动版本代码、独立的运行环境目录及系统持久状态目录）。部署时须按照模板规范建立对应目录结构与虚拟环境映射，不能将普通开发目录直接套用模板：

```bash
set -euo pipefail

# 在已检出的 Git 仓库根目录执行，获取选定 commit 的完整哈希作为部署标识
DEPLOY_ID="$(git rev-parse HEAD)"
RELEASE_DIR="/srv/xianyu-saas/releases/${DEPLOY_ID}"

# 仅适用于首次全新部署：检查发布目录不存在，避免意外覆盖已有版本
if [ -e "${RELEASE_DIR}" ]; then
  echo "发布目录已存在: ${RELEASE_DIR}" >&2
  exit 1
fi
mkdir -p "${RELEASE_DIR}" /srv/xianyu-saas/runtime

# 仅导出 Git 跟踪的源码至发布目录，避免带入本地配置、未跟踪文件或构建产物
git archive --format=tar HEAD | tar -x -C "${RELEASE_DIR}"

# 首次全新部署建立当前版本软链接（确认 current 既不存在实体也不存在软链接，不使用 -f 强行覆盖）
CURRENT_LINK="/srv/xianyu-saas/current"
if [ -e "${CURRENT_LINK}" ] || [ -L "${CURRENT_LINK}" ]; then
  echo "现役软链接或路径已存在: ${CURRENT_LINK}，请勿在运行期间直接覆盖" >&2
  exit 1
fi
ln -s "${RELEASE_DIR}" "${CURRENT_LINK}"

# 创建后端生产运行虚拟环境并安装依赖
python3 -m venv /srv/xianyu-saas/runtime/backend-venv
/srv/xianyu-saas/runtime/backend-venv/bin/python -m pip install -r /srv/xianyu-saas/current/backend/requirements.txt

# 创建 Worker 生产运行虚拟环境并安装依赖
python3 -m venv /srv/xianyu-saas/runtime/worker-venv
/srv/xianyu-saas/runtime/worker-venv/bin/python -m pip install -r /srv/xianyu-saas/current/worker/requirements.txt

# 建立与控制面和 Worker 运行预期一致的 .venv 软链接
ln -sfn /srv/xianyu-saas/runtime/backend-venv /srv/xianyu-saas/current/backend/.venv
ln -sfn /srv/xianyu-saas/runtime/worker-venv /srv/xianyu-saas/current/worker/.venv
```

生产运行仅安装上述运行依赖，不要在生产服务器执行 `scripts/bootstrap-dev.sh` 或全量前端测试命令。

### 3. 配置 systemd 服务与 Nginx 反代

参考仓库中的模板完成服务配置：
- `deploy/systemd/` 提供了控制面 API、任务处理守护单元与更新监听模板；服务单元中的 `WorkingDirectory`、`ExecStart`、`EnvironmentFile` 与状态目录须与实际安装路径保持严格一致，并在环境配置中指定 `SAAS_BOT_ROOT=/srv/xianyu-saas/current/worker`；
- `deploy/nginx/` 提供了静态资源托管与 API 反向代理配置模板；
- 各种部署模式下的业务数据与加密密钥位置均以实际配置文件与环境挂载为准。

---

## 部署方式三：Windows 环境部署

在 Windows 系统上，请使用 Docker Desktop 运行：

- **推荐方式**：安装 Windows 版 Docker Desktop 并启用 Linux 容器模式，在 WSL2 的 Linux 环境（真实 Linux 路径）中克隆源码并执行 `sudo bash deploy/docker-install.sh`，按方式一的流程启动与验收容器。安装脚本只支持本地 Linux Docker 引擎与 Linux 路径语义，不支持在 PowerShell 或 Git Bash 中直接运行；
- **运行限制说明**：Windows 宿主机通过 Docker Linux 容器运行完整后端；Worker 进程与控制面依赖 Linux 的 `resource` 模块、`/proc` 状态接口、`setsid` 会话隔离、`fcntl` 文件锁与 `prlimit`（`RLIMIT_AS`）资源配额限制，不支持 Windows 原生直接运行依赖上述特性的全部服务，请使用容器化方案运行。

---

## 系统升级与版本维护

### 版本识别规则与发布资产说明
控制台界面显示当前运行进程加载的代码版本、构建元数据以及从服务端缓存获取的最新发布信息。系统在服务端默认每 6 小时（21600 秒，可通过 `SAAS_UPDATE_CHECK_INTERVAL_SECONDS` 调整）异步探测一次发布版本，多用户多标签页共享数据库缓存，不重复请求 GitHub；浏览器在页面可见时每 5 分钟读取本地缓存，发现新版本后在版本徽标变黄提示。自动探测仅作发现，绝不触发静默安装。

**已发布版本（v0.2.2 历史事实）** 包含 8 项官方附件：
1. `xianyu-saas-0.2.2.tar.gz`：系统核心运行包，依据更新器白名单打包；
2. `xianyu-saas-0.2.2.manifest.json`：发布清单文件，记录包内文件尺寸与哈希；
3. `xianyu-saas-0.2.2.manifest.sig`：发布清单的 Ed25519 数字签名；
4. `xianyu-saas-0.2.2-source.zip`：完整安全源码包（包含 Dockerfile、Compose 模板、文档与许可证），供容器构建或手动部署；
5. `xianyu-saas-0.2.2.update-signing.pub`：本次发布对应的验证公钥副本；
6. `release-notes.md`：版本发布说明；
7. `artifacts.json`：资产元数据与签名公钥指纹；
8. `SHA256SUMS`：包含 6 项内容资产与 `artifacts.json` 共 7 项文件的 SHA-256 校验和清单，不包含自身散列。

**后续正式版本（0.3.0+ 规划）** 扩展为 10 项官方附件：
在原有 8 项基础上增加对应版本的 `VERSION.docker.manifest.json` 与 `VERSION.docker.manifest.sig`。其中 `artifacts.json` 记录 8 项内容资产元数据，`SHA256SUMS` 覆盖除自身外的全部 9 项文件。Docker 升级清单将完整源码 ZIP 与 Dockerfile/构建脚本绑定至受信任的签名、版本号、提交散列与文件哈希。

### Docker 容器升级方式

#### 1. 传统手动源码构建升级（通用）
若未启用独立更新器组件，Web 容器无权操纵宿主机 Docker 守护进程，无法直接在网页中执行升级。管理员执行手动升级的标准步骤：
1. 对目标容器实际挂载的 `/data` 业务数据（bind 目录或具名卷）进行离线冷备份；
2. 获取目标版本的源码包 `xianyu-saas-VERSION-source.zip` 并解压，或拉取对应 Git 标签；
3. 执行本地构建并重启服务：
   ```bash
   docker compose up -d --build --wait
   ```
4. 若存在本地覆盖配置文件，显式附加参数：
   ```bash
   docker compose -f docker-compose.yml -f .local/docker-compose.local.yml up -d --build --wait
   ```
5. **注意事项**：业务数据存储以应用容器实际 `/data` 挂载为准（依据部署环境可能是本地 bind 挂载或具名卷 named volume，例如本机覆盖配置采用的具名卷），升级时严格保留该数据卷，禁止使用 `-v` 参数清理容器。因镜像基于本地源码构建，不可使用 `docker compose pull` 更新业务代码。

#### 2. 独立 Docker 更新器组件
为支持网页管理员受控升级与可信校验，项目通过独立更新器覆盖配置 `docker-compose.updates.yml` 提供受控升级。推荐安装路径是 `deploy/docker-install.sh`：它统一完成镜像构建、公钥预装、一次性登记与就绪验收，重复执行不会重建或降级受管镜像。2026-09-14 已在独立 Linux Docker Engine 中验收实际应用的完整构建安装、网页资源与更新能力、运行中重跑和停止后恢复；另以签名测试应用验收升级、启动失败回退、配置和新增数据保留。该记录不代表真实业务店铺或线上发布验收。以下内容说明底层机制与手工接入边界；旧版（v0.2.0 与 v0.2.2）不具备本次受控更新能力，首次接入需要已经运行的新版应用容器与独立更新器。

- **架构、工具链与权限隔离**：
  - **工具链版本规范**：更新器容器内 Docker CLI 保持 28.3.3，Buildx 保持 0.26.1（CLI 与 Buildx 均未升级）；仅通过单独官方固定的 `docker:29.8.0-cli` 构建阶段引入官方 Compose 5.5.1，并在镜像构建时严格校验插件版本；
  - **官方 Compose 执行层分工**：由官方 Compose 负责候选镜像构建、目标单应用服务（`--no-deps`）的停止、重建与失败时切回旧镜像；更新器保留签名校验、维护排空、执行状态及健康检查，不再复制全量业务数据或运行数据迁移预演。配置检查复用同一份运行配置摘要，不另存容器快照指纹；
  - **覆盖配置顺序规则**：`docker-compose.updates.yml` 必须置于所有用户本地覆盖文件之后最后叠加（`docker compose -f docker-compose.yml -f <用户覆盖配置> -f docker-compose.updates.yml ...`）。该文件仅补充受控更新所需字段，不强制 `container_name`，保留用户原有端口、数据卷、资源配额与 `extra_hosts`；文件内 `volumes: !override` 会替换前面的挂载列表，最终配置必须同时保有应用数据挂载（`/data`）、更新 IPC 卷与受信公钥，严禁将包含 `!override` 的自定义配置置于 `docker-compose.updates.yml` 之后；
  - **公钥安全配置**：必须配置 `SAAS_UPDATE_PUBLIC_KEY_HOST_FILE` 指向宿主机预先可信安装的公钥文件，且该文件在容器内必须是 root 拥有、非符号链接且不可被 group/other 改写；系统仅信任本地预装公钥，绝不自动信任远端随行下载的公钥。`deploy/docker-install.sh` 默认把仓库内置的 `deploy/update-signing.pub` 复制成受管副本再挂载，因此操作者无需手工准备；手工接入时请自行保证上述属主与权限要求；
  - **Socket 挂载与权限事实**：Web 应用容器绝不挂载 Docker socket。仅独立更新器辅助容器（`xianyu-updater`）挂载宿主机 `/var/run/docker.sock`；挂载声明中的 `read_only: true` 属于文件系统挂载属性，更新器仍可通过 UNIX socket 通信调用 Docker 守护进程的高权限管理 API，更新器属于受信任的高权限核心组件；
  - **网络与权限收敛**：更新器容器不向外暴露任何网络端口与管理 API，使用 `network_mode: bridge` 保障 Buildx 客户端出站访问公开镜像仓库鉴权，丢弃多余 Linux 权限，Web 应用仅向受限的共享 IPC 卷提交请求。

- **受控初始化与配置登记（Docker 子命令 `initialize`）**：
  - **安装脚本已内置**：`deploy/docker-install.sh` 会使用同一组 `-p`、`--project-directory`、`--env-file` 与配置文件顺序自动执行下述登记，并在登记后等待真实更新就绪；下列手工命令用于自定义布局或人工排查，必须与安装脚本保持相同的参数与文件顺序；
  - **登记原理与凭据保护**：更新器依赖宿主机当前生效的完整 Compose 项目配置开展受控编排。解析后的配置 JSON 可能包含数据库密码或业务凭据，**严禁将其打印到终端、提交至代码仓库或记录到公开日志**。登记过程通过标准输入将原始字节流直接传递至更新器私有卷中安全保存，宿主机无需常驻额外守护进程、镜像仓库或管理面板；
  - **参数一致性要求**：调用端必须提供完整且两侧完全一致的 `-p <项目名>`、工作目录、`--env-file` 及所有配置文件列表（`updates` 覆盖文件位于最后），不得猜测项目名称或数据卷名称。输入配置与当前运行容器不匹配时（首次登记时同样执行严格比对），命令将直接报错中止。环境变量插值仅读取命令当前运行环境及 CLI `--env-file`，单纯服务内部的 `env_file` 指令不作为 `${...}` 的插值来源；
  - **标准登记执行命令**：
    Dockerfile 默认工作目录已固定为更新器脚本所在目录，健康检查统一使用镜像自带 Python 解释器。执行初始化登记时，必须使用统一的 Bash 参数数组复用到 `config` 与 `exec` 两端。原项目名不能推测，必须通过占位变量显式提供；若有外部环境文件或本地覆盖配置，必须按原顺序完整加入，且 `docker-compose.updates.yml` 必须置于最后。执行时直接在镜像默认工作目录调用 `python docker_updater.py initialize`（注意：这是 Docker 专用的 `initialize` 子命令，与 systemd 的 `--initialize` 区分；**不得覆盖该默认工作目录**，不发明新脚本或额外参数，命令不打印配置 JSON 避免凭据外泄）：
    ```bash
    # 原部署项目名不能猜，必须由操作者显式提供：
    : "${COMPOSE_PROJECT_NAME:?请先设置原部署项目名}"

    COMPOSE_ARGS=(
      -p "$COMPOSE_PROJECT_NAME"
      # 若存在外部环境变量文件，按原配置显式加入：
      # --env-file .env
      -f docker-compose.yml
      # 若存在本地覆盖配置，必须按原顺序逐个加入：
      # -f .local/docker-compose.local.yml
      -f docker-compose.updates.yml  # updates 覆盖配置必须置于最后
    )

    docker compose "${COMPOSE_ARGS[@]}" config --format json | \
      docker compose "${COMPOSE_ARGS[@]}" exec -T xianyu-updater python docker_updater.py initialize
    ```
  - **非 Linux 宿主的输入约束**：Windows 等非 Linux 宿主必须通过 WSL2 的 Linux 环境与真实 Linux 路径执行，不要依赖 PowerShell/Windows 文本管道转换（避免文本转码与回车换行损坏）；`deploy/docker-install.sh` 已在 Linux 内完成登记管道，手工命令仅在自定义布局时使用，同样应在 Linux 环境中执行；若宿主机缺少 Docker Compose 插件，应按照官方文档安装，不可使用无法提供 compose 子命令的伪装配置；
  - **首次接入与配置漂移边界**：`initialize` 属于一次性受控登记命令，若更新器私有卷中已存在任何登记记录、操作日志（journal）或更新历史，命令均直接返回冲突并拒绝执行；系统未提供 `refresh` 或 `reconfigure` 命令。一旦部署配置、数据卷、网络或挂载身份发生未登记的配置漂移，系统将暂停网页升级并转交维护者排查重新接入；处理过程中**必须完整保留原私有状态、历史记录与备份，严禁删除私有卷强行绕过**；

- **单应用依赖边界与操作限制**：
  - **依赖支持边界**：依赖限制代码已完成严格收敛。升级操作只更新目标服务（`--no-deps`），为避免静默忽略依赖条件，仅允许应用服务对当前更新器声明 `xianyu-updater: condition: service_started, required: true, restart: false` 依赖；其他任意依赖策略、额外迁移任务、其他联动容器的健康/完成条件在登记前均会被明确拒绝；系统整体升级安全依靠持久化状态与失败恢复，不宣称 Compose 具备原子事务能力；不支持 Swarm、Kubernetes、自定义 hooks 或复杂容器拓扑；
  - **运维操作红线**：严禁在更新期间执行 `down -v`、`renew-anon-volumes`、`remove-orphans`、`prune` 或并行的外部 Compose/tag/update 变更。若检测到外部配置漂移，或在恢复阶段遇到数据结构不兼容，系统主动保持维护状态并完整保留现场错误日志转人工排查，绝不静默覆盖运维人员提交的新配置。

- **升级执行流程与数据回滚红线**：
  1. 管理员在工作台发起升级，系统下载目标版本源码包与签名清单，核验 Ed25519 签名与 SHA-256 散列；
  2. 管理员输入当前登录密码完成身份再次确认，并勾选停机维护风险后提交升级（HTTP 202 仅代表受理）；
  3. 更新器核验登记配置一致性，基于完整源码包执行本地构建，旧容器在构建完成前保持对外服务；
  4. 比较当前与目标镜像的 `UPDATE_DATA_VERSION`，仅相同的正整数允许自动更新和回退；标记缺失或不同会在停服前拒绝操作，需维护者按发布说明人工升级。这是发布者对数据库、配置及凭据格式的兼容性声明，不代替迁移审查；
  5. 进入维护状态排空流量，停止旧容器，官方 Compose 使用原配置和同一 `/data` 挂载重建目标应用并检查就绪；不再执行全量冷备或数据预演，日常备份由部署者负责；
  6. **回滚与容器重建事实**：升级失败时使用旧镜像**重新创建旧版本容器**（容器 ID 可以改变，原配置与数据卷保持）；**不恢复或覆盖业务数据**，宿主机源码目录亦不自动覆盖。

### systemd 独立更新器升级
若采用 systemd 原生部署，系统升级由运行在活动代码软链接（`current`）之外的独立更新服务承载（由 root 运行，避免与日常应用共享运行权限）。历史旧版本（v0.2.0/v0.2.2）不会因新版本发布自动具备网页升级能力，必须由系统管理员在宿主机完成一次性受控基线接入与初始化。

#### 1. 首次受控接入标准操作时序

操作必须由管理员以 root 身份在宿主机按以下四个严格先后的步骤执行，要求在同一受控 shell 中按顺序执行。为保证更新器各阶段配置一致并满足布局校验（`validate_layout`），先定义统一工作目录与账户变量，并通过 shell 参数扩展强制设置版本变量（以 `/srv` 规范为例）：

```bash
set -euo pipefail
APP_ROOT=/srv/xianyu-saas
BUNDLE_ROOT=$APP_ROOT/updater
STATE_ROOT=/srv/xianyu-saas-data
UPDATE_ROOT=/srv/xianyu-saas-updates
UPDATER_STATE_ROOT=/srv/xianyu-saas-updater
APP_UID=$(id -u xianyu-saas)
[ -n "$APP_UID" ] && [ "$APP_UID" -gt 0 ] || { echo "无法解析 xianyu-saas 账户 UID"; exit 1; }
: "${VERSION:?请先设置已获批准且支持维护协议的真实已签名版本}"
```

1. **安装独立更新器组件（Bundle）**：
   - 从经审批的可信本地安装介质，将更新脚本及配套受信任模块安装至活动软链接之外的独立维护目录（例如 `$BUNDLE_ROOT`，即 `/srv/xianyu-saas/updater/`，也就是 `SAAS_UPDATER_BUNDLE_ROOT`），属主设为 root:root；
   - 组件固定包含 8 个核心代码文件：`deploy/updater/updater.py` 与 `backend/{account_storage.py, db.py, docker_update_protocol.py, platform_update.py, runtime_settings.py, update_maintenance.py, version.py}`，以及本地预装公钥副本 `deploy/update-signing.pub`；
   - 严禁从未经校验的网络源直接下载或提取上述核心代码；更新器统一使用生产运行虚拟环境的 Python 解释器（例如 `$APP_ROOT/runtime/backend-venv/bin/python`）。所有运行模式均必须由 root 执行（已通过便携合约与 AST 静态校验，真实 Linux 运行端验收仍在等待隔离环境）。

2. **离线导入受信基线版本（`--import-trusted-baseline`）**：
   - 准备管理员选定的真实已签名兼容版本（`VERSION`）的三个离线发布资产文件：归档包、清单与数字签名。注意：历史版本（v0.2.0 与 v0.2.2）均不含签名覆盖的 `MAINTENANCE_PROTOCOL=1`，无法通过新基线导入；本命令示例供后续兼容版本正式发布或获得经批准的签名介质后使用，切勿将未发布的 0.3.0 或历史旧版 0.2.2 当作现成可用基线；
   - 资产文件必须放置于受 root 保护的本地目录，属主为 root，文件权限设为 `0600`，文件基名须严格符合命名规范：`xianyu-saas-$VERSION.tar.gz`、`xianyu-saas-$VERSION.manifest.json`、`xianyu-saas-$VERSION.manifest.sig`；
   - 命令行必须显式传入刚好三个绝对路径参数（`--import-trusted-baseline ARCHIVE MANIFEST SIGNATURE`）。更新器启动时会立即解析完整配置（`Config.from_env()`）并执行目录布局校验（`validate_layout()`）；若仅传入发布目录与公钥，其余路径将回退至内置默认值导致 `releases.parent` 与 `current.parent` 不一致而报错中止。因此离线导入必须通过 `sudo env -i` 提供与后续初始化完全一致的完整路径配置：
     ```bash
     sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
       SAAS_UPDATE_APP_UID="$APP_UID" \
       SAAS_UPDATER_BUNDLE_ROOT="$BUNDLE_ROOT" \
       SAAS_CURRENT_ROOT="$APP_ROOT/current" \
       SAAS_CURRENT_LINK="$APP_ROOT/current" \
       SAAS_RELEASES_DIR="$APP_ROOT/releases" \
       SAAS_STATE_DIR="$STATE_ROOT" \
       SAAS_UPDATE_STAGING_DIR="$UPDATE_ROOT/staging" \
       SAAS_UPDATE_INTENT_FILE="$UPDATE_ROOT/intent.json" \
       SAAS_UPDATER_STATE_DIR="$UPDATER_STATE_ROOT" \
       SAAS_UPDATE_PUBLIC_KEY_FILE="$BUNDLE_ROOT/deploy/update-signing.pub" \
       "$APP_ROOT/runtime/backend-venv/bin/python" "$BUNDLE_ROOT/deploy/updater/updater.py" \
       --import-trusted-baseline \
       "$APP_ROOT/import/xianyu-saas-$VERSION.tar.gz" \
       "$APP_ROOT/import/xianyu-saas-$VERSION.manifest.json" \
       "$APP_ROOT/import/xianyu-saas-$VERSION.manifest.sig"
     ```
   - **导入语义与安全性**：更新器严格使用预先安装的本地公钥（系统绝不使用随资产附带的公钥）核验 Ed25519 签名与 SHA-256 文件散列，并以纯 AST 静态语法解析确认候选版本维护协议声明为 `MAINTENANCE_PROTOCOL=1`（绝不执行候选代码）。校验通过后原子解包至 `$APP_ROOT/releases/$VERSION/` 目录。该操作具备等幂性，**绝不自动切换 `current` 软链接、不重启服务、不初始化业务数据库，亦不触碰任何业务数据**。若已存在不同内容的同版本目录，命令安全失败并报错中止，切勿强行绕过。

3. **人工维护切换现役软链接**：
   - 管理员协调业务停机维护窗口，停止运行中的旧版服务；
   - 严禁使用非原子的 `ln -sfn`（目标已存在或为目录时存在嵌套软链接及非原子覆盖风险）。
   - 若尚未创建现役软链接（全新部署）：
     ```bash
     ln -s "$APP_ROOT/releases/$VERSION" "$APP_ROOT/current"
     ```
   - 若已存在现役软链接：
     1. 核验 `$APP_ROOT/current` 确为符号链接；若为实体目录或普通文件则立即中止，人工排查并禁止强制覆盖：
        ```bash
        [ -L "$APP_ROOT/current" ] || { echo "$APP_ROOT/current 未检测为符号链接，拒绝操作"; exit 1; }
        ```
     2. 记录旧目标路径以便需要时人工回退恢复：
        ```bash
        OLD_TARGET=$(readlink "$APP_ROOT/current")
        echo "当前版本软链接指向: $OLD_TARGET"
        ```
     3. 在同一父目录下创建临时软链接，并通过 `mv -T --` 原子重命名替换现役软链接（使用 `&&` 串联执行，若临时链接已存在导致 `ln` 失败，绝不将遗留临时路径覆盖至 `current`）：
        ```bash
        ln -s "$APP_ROOT/releases/$VERSION" "$APP_ROOT/.current.tmp" && mv -T -- "$APP_ROOT/.current.tmp" "$APP_ROOT/current"
        ```
   - 确保此时 `current` 软链接所指版本已具备维护协议能力并与导入基线一致。

4. **执行一次性更新器初始化（`--initialize`）**：
   - 现役版本对齐后，以 root 身份执行初始化。必须通过 `sudo env -i` 显式提供与步骤 2 导入及服务 `EnvironmentFile` 完全一致的可信路径及参数（避免 `sudo` 默认清理环境变量导致系统回退至非预期的默认路径）：
     ```bash
     sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
       SAAS_UPDATE_APP_UID="$APP_UID" \
       SAAS_UPDATER_BUNDLE_ROOT="$BUNDLE_ROOT" \
       SAAS_CURRENT_ROOT="$APP_ROOT/current" \
       SAAS_CURRENT_LINK="$APP_ROOT/current" \
       SAAS_RELEASES_DIR="$APP_ROOT/releases" \
       SAAS_STATE_DIR="$STATE_ROOT" \
       SAAS_UPDATE_STAGING_DIR="$UPDATE_ROOT/staging" \
       SAAS_UPDATE_INTENT_FILE="$UPDATE_ROOT/intent.json" \
       SAAS_UPDATER_STATE_DIR="$UPDATER_STATE_ROOT" \
       SAAS_UPDATE_PUBLIC_KEY_FILE="$BUNDLE_ROOT/deploy/update-signing.pub" \
       "$APP_ROOT/runtime/backend-venv/bin/python" "$BUNDLE_ROOT/deploy/updater/updater.py" \
       --initialize
     ```
   - **初始化门禁与可信记录**：`--initialize` 将核验 `current` 指向版本的签名清单与 `MAINTENANCE_PROTOCOL=1` 协议支持，配置意图目录属主为 root、权限设为 sticky `01770`（所属组赋给应用 UID 对应 GID），创建公开状态目录（权限 `0755`）并原子写入受信接入记录（`status/initialization.json`）。该记录严格包含六个字段（`schema`、`protocol`、`public_key_sha256`、`bundle_sha256`、`entrypoint_sha256`、`initialized_at`），无周期心跳或过期失效；控制面要求其必须与 API 信任公钥、独立 bundle 固定 8 文件规范哈希及实际 entrypoint 完全匹配。**只有初始化成功生成可信接入记录后，系统控制面与前端工作台才放行后续的版本升级操作**（门禁代码已完成落地生效，本机仅通过静态与便携测试，真实 Linux root 运行动态验收仍待验证）；初始化过程绝不修改业务数据的所属权限。
   - 启动更新监听单元（`xianyu-saas-updater.path`）与业务服务。

#### 2. 专用路径、服务模板与环境一致性规范
- **公钥与 Bundle 路径一致性**：更新器使用的 `SAAS_UPDATE_PUBLIC_KEY_FILE` 必须与控制面 API 绑定的公钥完全一致（二进制内容与公钥指纹匹配）。systemd 部署中公钥与 unit 默认配置一致，指向独立 bundle 内预装的 `deploy/update-signing.pub`（例如 `/srv/xianyu-saas/updater/deploy/update-signing.pub`），新增环境变量 `SAAS_UPDATER_BUNDLE_ROOT` 明确指定独立 bundle 根目录；Docker 部署模式下宿主机公钥文件路径可独立配置（例如 `/etc/xianyu-saas/update-signing.pub`），两者互不混淆；
- **服务模板同步修改**：`deploy/systemd/` 目录下的服务模板（`xianyu-saas.service`、`xianyu-saas-updater.service`、`xianyu-saas-updater.path`）中的 `WorkingDirectory`、`ExecStart`、`RequiresMountsFor`、`ReadWritePaths` 以及配置文件中的目录变量（含 `SAAS_UPDATER_BUNDLE_ROOT`、`SAAS_UPDATE_PUBLIC_KEY_FILE` 等），必须与实际安装目录严格对应同步修改，严禁仅修改命令行示例而保留不匹配的服务单元配置；
- **静态维护协议核验**：更新器在后续处理升级意图时，继续在验签与散列校验通过后，通过纯 AST 静态语法解析读取候选版本的 `MAINTENANCE_PROTOCOL=1` 常量，绝不为协议检查而预先执行未受信任的候选代码；
- **回滚与迁移策略**：当前 systemd 更新器采用严格保守策略，一旦涉及未经验证的数据库结构或破坏性数据变化，立即中断并交由人工处理；自动回滚仅回退代码软链接与运行环境，绝不覆盖业务数据。升级请求必须经系统管理员在网页中输入当前登录密码完成身份再次确认，并勾选确认停机维护风险后方可提交；系统不包含第二认证因素，密码与临时确认令牌不存入浏览器持久存储。注意：真实 Linux/systemd 生产环境升级与回滚端到端验收目前仍在等待隔离验证环境，不可提前标记为已通过。

---

## 本地网络代理、Ollama 与 Fake-IP 说明

- **本地 Ollama 回环例外**：系统网络安全策略默认拦截私网 IP 与 HTTP 接口。仅当模型协议选择 `ollama_chat` 且配置了 `SAAS_AI_ALLOW_OLLAMA_LOCAL=1` 时，服务端放行运行服务视角的 `localhost` / `127.0.0.1` 回环地址并同时允许 HTTP 请求。普通私有网络地址仍被拦截，`SAAS_AI_ALLOW_HTTP_LOCAL=1` 仅允许 HTTP 协议而不改变地址拦截规则；容器运行时的回环地址属于容器网络命名空间，与宿主机环境相互独立，系统不放行局域网私网 IP 或 `host.docker.internal`。
- **代理客户端 Fake-IP 拦截**：在配置统一模型连接时，若界面测试连接提示 `dns_fake_ip` 错误，说明本地代理返回了 `198.18.0.0/15` 网段的 Fake-IP 地址，触发了服务内置的安全拦截。应在代理客户端的 DNS 配置中将大模型域名加入直连或排除名单（fake-ip-filter），确保返回真实公网 IP 地址。禁止将本机临时固定 IP 映射写入通用部署配置或发布包。
- **安全底线**：切勿为了解决网络提示而关闭系统的私网防御（SSRF）或跳过 TLS 证书校验。

---

## 首次运行与账号初始化

1. **默认无令牌注册（`SAAS_BOOTSTRAP_ENABLED=0`）**：
   - 首次启动且数据库为空时，在前端登录界面直接点击「创建首个管理员账号」；
   - 该操作不受 `SAAS_ALLOW_REGISTRATION=0` 限制，无需命令行介入；
   - 系统原子创建首个 `admin` 账号及默认店铺基础配置目录；
   - 首次管理员注册与后续用户注册支持补齐空目录与纯默认配置残留，严格拒绝业务数据、未知/损坏/非默认配置及符号链接；两入口接入并发回滚保护（仅清理本次新建且未被认领的目录；SQLite 写事务串行化，已有事务或数据库异常时保守保留存储目录）；
   - **安全提示**：空数据库部署完成后，任何可访问者都能注册首个管理员。请务必在完成部署后立即完成初始化注册，再将端口或反代向外部开放。
2. **后续注册开关**：
   - 首个管理员注册完毕后，后续注册用户仅具备普通店主（`owner`）角色；
   - 必须同时开启环境变量布尔开关 `SAAS_ALLOW_REGISTRATION=1` 与管理后台「开放注册」开关，前端才会开放用户注册入口；首次管理员与后续注册开关权限边界保持独立不变。
3. **运维令牌模式（`SAAS_BOOTSTRAP_ENABLED=1`）**：
   - 如需强制仅允许持有令牌的运维初始化，配置 `SAAS_BOOTSTRAP_TOKEN_FILE` 与受信 IP 限制；
   - 在受信网络下通过特定令牌初始化，完成后关闭该开关并删除令牌。

---

## 生产安全加固清单

1. **反向代理与 HTTPS**：使用 Nginx 等反向代理配置 SSL 证书（HTTPS），对外仅开放 443 端口；
2. **控制公开注册**：初始化完成后保持 `SAAS_ALLOW_REGISTRATION=0`，按需在管理后台由管理员手动创建用户；
3. **定期冷备份**：定期离线备份 SQLite 数据库文件（如 `saas.db`）、各店铺配置目录及运行数据；若主密钥来源（如环境配置文件 `config/saas.env` 或独立密钥文件）保存在挂载目录之外，必须一并单独离线备份；
4. **妥善保管主密钥**：生产环境中的 `SAAS_AI_MASTER_KEY` 严禁泄露，丢失将导致所有已保存的 API Key 无法解密恢复。

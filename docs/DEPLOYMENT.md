# 生产部署指南

本文档介绍两种部署方式：**Docker Compose（推荐）**与 **Ubuntu 原生安装**。Windows 用户使用 Docker Desktop 运行同一套 Docker 部署。

## 部署方式一：Docker Compose（推荐）

该方式适用于各类 Linux 服务器与本地容器环境，环境依赖自包含，升级与维护流程清晰。

### 1. 基础环境
- 系统需具备 `curl`、`unzip`，以及支持 Engine API v1.47 的 Linux Docker Engine 与 Compose 插件（不支持远程 Docker 上下文）。

### 2. 获取 Release 安装包
从 GitHub Releases 下载正式版源码安装包：
```bash
VERSION=0.4.6
curl -fLO "https://github.com/tswawa/xianyu-saas/releases/download/v${VERSION}/xianyu-saas-${VERSION}-source.zip"
unzip -q "xianyu-saas-${VERSION}-source.zip"
cd "xianyu-saas-${VERSION}"
```

请使用项目发布的 `xianyu-saas-<version>-source.zip`。GitHub 自动生成的 “Source code” 归档缺少发布构建信息，不是这里的安装包。

当前 v0.4.6 安装包内置更新启动器，Docker 与 Ubuntu 原生部署均可在网页更新并自动重启。更早版本为已停用的历史测试版，不再提供安装附件。

### 3. 一键安装并验收（推荐）
```bash
sudo bash deploy/docker-install.sh
```

脚本在全新安装时按固定顺序完成：
1. 缺失时从示例创建 `config/saas.env`（权限 0600，同时作为 Compose 的 env_file）；
2. 渲染 `docker-compose.yml` 并在本地构建镜像（不拉取远程预构建镜像）；
3. 把仓库内置的 `deploy/update-signing.pub` 复制为容器内 root 拥有的本地副本（权限 0644）；
4. 准备 `./data` 目录及可写代码存储 `./data/app-code`（非递归设置属主，不改动已有业务数据）；
5. 启动应用容器（内置 `docker/launcher.sh` 启动器统一监督 API、任务消费者及静态前端）；
6. 等待容器运行与 `/health` 健康；
7. 验收内置文件更新就绪能力。

- **默认内置更新机制**：系统默认采用内置启动器与文件更新机制（`docker/launcher.sh` + `backend/file_update.py`），日常代码升级直接在 `./data/app-code` 中切换并重启进程，无需特权 sidecar 容器或独立更新器登记。
- **历史独立更新器兼容**：早期设计的独立特权更新器（`docker-compose.updates.yml`）及更新器登记逻辑在系统中保留向后兼容，但不作为默认推荐路径。
- **信任来源**：只信任源码包内置的公钥（`deploy/update-signing.pub`）；脚本绝不从外部自动下载替换公钥。首次下载源码包本身不验证外置签名，后续网页升级时由内置更新器自动核验签名。
- **脚本重跑与升级边界**：检测到既有受管安装后，脚本只校验并启动既有容器（`docker start` 保留原镜像与配置），不会重新构建镜像，也不会替换或升级已有镜像。因此，**旧版未接入内置启动器的 Docker 容器无法通过重新运行脚本自动迁移**。若需升级基础镜像或切换至新机制，需由维护者在维护窗口停止旧容器后重新构建启动。
- **重跑不加载新配置**：已安装实例，重复运行安装脚本或执行 `docker start` 均不会把当前目录或 `config/saas.env` 的新配置应用到容器。修改环境变量、端口映射或数据挂载后，`docker start` 或 `docker compose restart` 都不会加载新容器配置。更改配置需由维护者在维护窗口按实际运行镜像、挂载关系人工重建容器。
- **运行环境限制**：安装脚本只支持 Linux Bash 与本地 Linux Docker 引擎；不支持远程 Docker 上下文，请在真实 Linux 文件系统路径中执行（Windows 请用 WSL2 的 Linux 环境）。
- **超时与诊断**：容器启动与更新就绪验收默认超时为 300 秒（可用 `--timeout` 参数调整）；排查可查看 `docker compose logs`。

安装成功后访问：
- **Web 控制台访问入口**：`http://127.0.0.1:4173/xianyu-saas/`
- **控制面健康检查接口**：`http://127.0.0.1:8096/health`

根据实际网络拓扑编辑 `config/saas.env`（安装脚本只在缺失时创建）：
- `SAAS_PUBLIC_ORIGIN`：本机测试使用 `http://127.0.0.1:4173`。经由反向代理对外服务时填写浏览器实际访问完整来源（如 `https://xianyu.example.com`）；
- `SAAS_TRUSTED_HOSTS`：允许的 Host 列表，英文逗号分隔（如 `127.0.0.1:4173,xianyu.example.com`）；
- `SAAS_COOKIE_SECURE`：对外启用 HTTPS 时设为 `1`，纯 HTTP 测试设为 `0`；
- `SAAS_AI_MASTER_KEY`：服务端加密存储 API Key 的主密钥（32 字节随机二进制经标准 Base64 编码，长度 44 字符，生成命令：`python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"`）；
- **配置生效说明**：修改 `config/saas.env` 后，`docker compose restart` 或 `docker start` 均不会加载新配置，安装脚本重跑也不会自动应用。更改配置需由维护者按实际运行镜像、挂载和更新器登记关系人工重建容器。

### 4. 日常暂停、恢复与查看日志
```bash
# 查看实时日志
sudo docker logs -f xianyu-saas

# 暂停应用（保留容器实例、镜像和挂载数据）
sudo docker stop xianyu-saas

# 恢复既有受管容器并确认更新就绪能力
sudo bash deploy/docker-install.sh
```

### 5. 手动 Compose 启动（自定义编排）
若不通过安装脚本，也可以直接使用标准 Docker Compose 命令启动：
```bash
cp config/saas.env.docker.example config/saas.env
docker compose up -d --build
```
- 默认 Dockerfile 构建的镜像同样内置监督启动器与文件更新机制；业务数据及可写代码存储保存在宿主机 `./data` 目录（容器内映射 `/data`）。

---

## 部署方式二：Ubuntu 原生安装（systemd 守护进程）

适用于 Ubuntu 22.04、24.04 或 Debian 12 系统，支持 x86_64 与 ARM64（aarch64）两种硬件架构，由 systemd 直接管理系统进程。

### 1. 系统要求
- 操作系统：Ubuntu 22.04 LTS、24.04 LTS 或 Debian 12；
- 系统核心组件：systemd、systemd-analyze、useradd；
- 下载命令：curl；
- 运行权限：root 执行权限；
- 说明：直接下载运行的 manager 二进制文件不会自动调用 apt 安装基础工具；若使用仓库提供的 `deploy/install.sh` 在线脚本进行安装，脚本才会协助检查并安装依赖。

### 2. 首次安装

从官方 GitHub Releases 下载对应架构的安装器，执行 `install` 命令。以下命令安装 0.4.6，ARM64 机器请将 `ARCH` 改为 `aarch64`：

```bash
VERSION=0.4.6
ARCH=x86_64  # ARM64 改为 aarch64
curl -fLO "https://github.com/tswawa/xianyu-saas/releases/download/v${VERSION}/xianyu-saas-${VERSION}-linux-${ARCH}"
chmod +x "xianyu-saas-${VERSION}-linux-${ARCH}"
sudo "./xianyu-saas-${VERSION}-linux-${ARCH}" install --version "$VERSION"
```

也可以通过仓库内的在线安装脚本自动识别架构并安装：
```bash
sudo bash deploy/install.sh --version 0.4.6
```

- **访问地址**：`http://127.0.0.1:8096/xianyu-saas/`
- **数据与配置路径**：业务数据保存在 `/var/lib/xianyu-saas`，环境配置文件位于 `/etc/xianyu-saas.env`，可写代码存储位于 `/var/lib/xianyu-saas/app-code`。
- **管理器机制说明**：管理器文件本身是引导管理程序，不是包含全量依赖的离线大包；执行 `install` 时会在本机自动下载、验证对应架构的签名运行时包（`.tar.gz`、`.manifest.json`、`.manifest.sig`），并自动部署配置由监督启动器运行的 systemd 服务（`xianyu-saas.service`）。

### 3. 日常维护与服务控制

安装完成后，直接使用系统注册的稳定管理命令控制服务生命周期：

```bash
# 查看服务状态
sudo xianyu-saas status

# 暂停服务（例行维护，保留配置与数据）
sudo xianyu-saas stop

# 启动与恢复服务
sudo xianyu-saas start

# 重启服务
sudo xianyu-saas restart

# 环境自检与状态诊断
sudo xianyu-saas doctor
```

### 4. Nginx 反向代理配置
参考仓库中 `deploy/nginx/` 模板配置反向代理与静态资源分发，对外通过 HTTPS 暴露 443 端口，并将请求代理到本地 API 端口（默认 8096）。

---

## Windows 上使用 Docker Desktop

在 Windows 系统上，请使用 Docker Desktop 运行：

- **使用方式**：安装 Windows 版 Docker Desktop 并启用 Linux 容器模式，在 WSL2 的 Linux 环境（真实 Linux 文件系统路径）中下载解压 Release 源码包并执行 `sudo bash deploy/docker-install.sh`，按方式一的流程启动与验收容器。安装脚本只支持本地 Linux Docker 引擎与 Linux 路径语义，不支持在 Windows PowerShell 或 Git Bash 中直接运行；
- **运行限制说明**：Windows 宿主机通过 Docker Linux 容器运行完整后端；Worker 进程与控制面依赖 Linux 的 `resource` 模块、`/proc` 状态接口、`setsid` 会话隔离、`fcntl` 文件锁与 `prlimit`（`RLIMIT_AS`）资源配额限制，不支持 Windows 原生直接运行依赖上述特性的全部服务。

---

## 系统升级与版本维护

0.4.6 是当前维护起点，之前版本不再提供功能修复。已接入内置更新功能的 v0.4.5 可以从网页升级到 0.4.6，现有账号、店铺配置与业务数据保留；旧版明确手动停止的店铺不会因本次升级而自动启动。

0.4.6 新增了薄荷人设等配置，旧版不能完整识别。升级并使用新配置后，不要主动降级到旧版；启动失败时恢复上一版本代码的机制不等于任意版本之间的数据回滚。

### 版本识别与发布更新机制
控制台界面显示当前运行进程加载的代码版本、构建元数据以及从服务端缓存获取的最新发布信息。系统在服务端默认每 6 小时（21600 秒，可通过 `SAAS_UPDATE_CHECK_INTERVAL_SECONDS` 调整）异步探测一次发布版本，全站共享数据库缓存与租约，不重复请求 GitHub；浏览器在页面可见时每 5 分钟读取本地缓存，发现新版本后在版本徽标变黄提示。自动探测仅作发现，绝不触发静默安装。

- **首次安装与受管更新分工**：首次安装请使用官方 Release 发布的安装包（Docker 为 `xianyu-saas-<version>-source.zip`，Ubuntu 原生为对应架构的管理器无后缀可执行文件）；已安装实例后续直接在网页控制台进行受控升级。
- **重复运行安装脚本**：已安装实例，重复执行 `deploy/docker-install.sh` 仅校验并启动既有容器（`docker start` 保留原镜像与配置），不会替换镜像，也不会作为升级手段或自动应用本地新的环境变量与 Compose 变更。
- **业务数据保护边界**：网页文件更新和失败回退只切换代码，保留现有数据库、店铺数据与配置，不另做数据库备份。Docker 数据位于 `./data`；Ubuntu 默认数据位于 `/var/lib/xianyu-saas`，配置位于 `/etc/xianyu-saas.env`。通过原生安装器升级完整运行环境时，安装事务另有数据库备份。两种部署都需要日常备份。

### 默认内置文件更新机制（推荐）

系统新增 `backend/file_update.py` 与 `docker/launcher.sh` 监督启动器，默认内置在 Docker 容器与 Ubuntu 官方部署中，无需用户手动组装独立更新器容器或编写 systemd 维护单元。

#### 1. 代码目录与运行隔离
- **Docker 部署**：可写代码存储目录为 `/data/app-code`（映射在宿主机 `./data/app-code`）；基础运行环境（Python 虚拟环境、系统库）与受信任公钥（`/app/update-signing.pub`，由 root 只读拥有）固定在容器镜像内部；
- **Ubuntu 原生部署**：可写代码存储目录为 `/var/lib/xianyu-saas/app-code`；运行环境与可信公钥（`/etc/xianyu-saas/update-signing.pub`）固定在代码存储目录之外；
- **数据与凭据保护**：业务数据库（`saas.db`）、租户与店铺数据（`tenants/`）、主密钥（`ai-master-key`）及配置文件（`config/saas.env` 或 `/etc/xianyu-saas.env`）均位于代码存储目录之外，切换代码时保留；新版本继续使用这些数据与配置。

#### 2. 网页受控更新流程
1. **检查与下载**：管理员登录 Web 控制台，打开「系统更新」窗口查看版本信息，点击「更新」开始下载目标版本的官方签名源码包（`xianyu-saas-<version>-source.zip` 及其签名清单）；
2. **下载进度与验签**：窗口按实际进度显示连接更新源、已下载/总大小及百分比（未知大小不虚构百分比）；下载途中关闭窗口不会重复发起任务，再次打开可读取当前进度；后台使用本地受信公钥核验 Ed25519 签名，并校验下载包的 SHA-256；
3. **停服前兼容性检查**：系统比对目标版本与当前版本的 `UPDATE_DATA_VERSION` 以及 Python 依赖要求（`requirements.txt`）；若存在不兼容声明，在停止服务前直接拒绝更新并给出明确提示；
4. **密码确认与切换重启**：更新包校验就绪后，由管理员输入当前登录密码进行身份核验并确认；由监督启动器停止当前服务，将 `current` 软链接切换至新版本目录并启动服务；
5. **健康检查与自动回滚**：新版本启动后，启动器等待 `/health` 接口就绪并核对版本；若新版本在超时时间内未通过健康检查，启动器尝试将 `current` 软链接切回上一版本并重启；
6. **边界说明**：自动回滚仅针对应用代码目录的软链接切换，系统不自动备份或回滚业务数据库；
7. **镜像免重构**：Docker 部署中的普通代码更新直接在可写代码存储中切换，**无需重新构建 Docker 镜像**；
8. **运行状态说明**：切换版本会短暂中断服务；重启后使用现有数据库与店铺配置，重新建立运行连接。网页显示更新完成后刷新页面。

### 依赖或底层运行环境变动时的升级

文件更新检测到运行依赖或 `UPDATE_DATA_VERSION` 不兼容时，会在停服前拒绝升级。需要更换底层环境的版本，应按其发布说明维护运行环境；仅重装运行环境不能代替不兼容数据的迁移。

- **Docker 部署**：
  从 GitHub Releases 下载新版本源码包。维护者应记录现有挂载与环境配置，在维护窗口使用新版本重建镜像和容器，并继续挂载原业务数据。重跑 `deploy/docker-install.sh` 只会检查并启动已有容器，不会完成运行环境升级。具体迁移步骤以目标版本发布说明为准。
- **Ubuntu 原生部署**：
  下载新版本管理器或通过在线安装脚本重新执行安装：
  ```bash
  sudo ./xianyu-saas install --version <新版本号>
  # 或使用在线脚本：
  sudo bash deploy/install.sh --version <新版本号>
  ```
  管理器将下载新版独立运行时包并刷新 systemd 服务。

### 旧安装迁移

v0.4.6 以前的发布附件已撤下，不再通过旧版安装器或 v0.4.0 基线包进行安装与修复。已接入内置启动器的 v0.4.5 仍可在网页更新到 v0.4.6；未接入的历史环境按以下方式处理。

- **旧版 Docker 实例升级**：既有旧版 Docker 安装未接入内置启动器时，**重新运行安装脚本不会自动迁移或替换已有容器镜像**（脚本重跑仅执行 `docker start` 启动既有容器）。在维护窗口停止旧容器，保留业务数据挂载与环境配置，使用 v0.4.6 或后续正式版源码安装包重新构建应用容器，并接回原有数据；
- **Ubuntu 旧版实例修复**：下载 v0.4.6 或后续正式版安装器，显式指定目标版本（例如 `install --version 0.4.6`；在线脚本为 `sudo bash deploy/install.sh --version 0.4.6`）。安装器会核验旧安装的签名与服务模板；无法通过核验的历史布局需保留原数据、主密钥与环境配置后人工迁移，不能依靠已撤下的旧附件恢复安装。
- **旧版独立特权更新器兼容**：早期设计的独立更新器组件（`docker-compose.updates.yml` 与 systemd 独立更新服务）代码在系统中继续保留向后兼容，但已不再作为推荐路径。

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
   - 系统创建首个 `admin` 账号及默认店铺基础配置目录；
   - 首次管理员注册与后续用户注册支持安全处理空目录与默认配置残留，拒绝未知或损坏配置，发生异常时自动清理未认领目录并保留既有数据；
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

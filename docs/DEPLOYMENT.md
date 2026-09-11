# 生产部署指南

本文档介绍如何在服务器或本地主机上部署 xianyu-saas，支持 **Docker Compose（推荐部署方式）**、**Linux systemd 原生服务** 以及 **Windows 宿主机环境（Docker 容器）**。

## 部署方式一：Docker Compose（推荐）

该方式适用于各类 Linux 服务器与本地容器环境，环境依赖自包含，升级与维护流程清晰。

### 1. 基础环境
- 安装 Docker 20.10+ 及 Docker Compose v2。

### 2. 获取代码与配置环境
```bash
git clone https://github.com/tswawa/xianyu-saas.git /srv/xianyu-saas
cd /srv/xianyu-saas

# 复制生产环境变量模板
cp config/saas.env.docker.example config/saas.env
```

根据实际网络拓扑编辑 `config/saas.env`：
- `SAAS_PUBLIC_ORIGIN`：本机测试使用 `http://127.0.0.1:4173`。经由 Nginx 等反向代理对外提供服务时，填写浏览器实际访问的完整来源（例如 `https://xianyu.example.com`，包含协议与端口，不带末尾路径）；
- `SAAS_TRUSTED_HOSTS`：填写允许的 Host 列表，多个以英文逗号分隔（例如 `127.0.0.1:4173,xianyu.example.com`）；
- `SAAS_COOKIE_SECURE`：对外启用 HTTPS 时设置为 `1`；纯 HTTP 内部调试设置为 `0`；
- `SAAS_AI_MASTER_KEY`：填写用于服务端加密保存 API Key 的主密钥（32 字节随机值做标准 Base64 编码，编码后长度 44 字符，可通过命令 `python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` 生成）；

### 3. 构建与启动容器
```bash
docker compose up -d --build
```

- **Web 控制台访问入口**：`http://127.0.0.1:4173/xianyu-saas/`
- **控制面健康检查接口**：`http://127.0.0.1:8096/health`
- **业务数据持久化**：SQLite 数据库、店铺会话、配置文件与卡密库存统一挂载在宿主机 `./data` 目录，容器更新与镜像重建不丢失数据。

服务默认绑定宿主机回环地址。如果需要远程访问，请通过受控反向代理（如 Nginx）暴露服务，并确保反代配置透传真实的 Host 与 Origin 请求头，浏览器访问地址必须与 `SAAS_PUBLIC_ORIGIN` 完全一致。

### 4. 日志查看与停止服务
```bash
# 查看实时日志
docker compose logs -f

# 停止容器运行
docker compose down
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

- **Docker Desktop Compose 运行（推荐方式）**：安装 Windows 版 Docker Desktop 并启用 Docker Compose v2，按照方式一的 Docker Compose 流程启动与维护容器；
- **运行限制说明**：Windows 宿主机通过 Docker Linux 容器运行完整后端；Worker 进程与控制面依赖 Linux 的 `resource` 模块、`/proc` 状态接口、`setsid` 会话隔离、`fcntl` 文件锁与 `prlimit`（`RLIMIT_AS`）资源配额限制，不支持 Windows 原生直接运行依赖上述特性的全部服务，请使用容器化方案运行。

---

## 系统升级与版本维护

### 版本识别规则与发布资产说明
控制台界面显示当前运行进程加载的代码版本、构建元数据以及从 GitHub Releases 查询到的最新发布信息。网络不可达、未发现更新版本或缺少发布制品时，界面会给出对应状态提示。

GitHub Releases 随新版本（例如 `v0.2.2`）自动发布 8 项官方附件：
1. `xianyu-saas-0.2.2.tar.gz`：系统核心运行包，依据更新器白名单打包；
2. `xianyu-saas-0.2.2.manifest.json`：发布清单文件，记录包内文件尺寸与哈希；
3. `xianyu-saas-0.2.2.manifest.sig`：发布清单的 Ed25519 数字签名；
4. `xianyu-saas-0.2.2-source.zip`：完整安全源码包（包含 Dockerfile、Compose 模板、文档与许可证），供容器构建或手动部署；
5. `xianyu-saas-0.2.2.update-signing.pub`：本次发布对应的验证公钥副本；
6. `release-notes.md`：版本发布说明；
7. `artifacts.json`：资产元数据与签名公钥指纹；
8. `SHA256SUMS`：包含 6 项内容资产与 `artifacts.json` 共 7 项文件的 SHA-256 校验和清单，不包含自身散列。

**下载用途说明**：
- 容器部署或手动部署请下载完整源码包 `xianyu-saas-0.2.2-source.zip`；
- `xianyu-saas-0.2.2.tar.gz`、`.manifest.json` 与 `.manifest.sig` 专供配置了签名校验的 systemd 更新器自动验证与解压；
- GitHub 界面自动生成的源码压缩包（Source code zip/tar.gz）缺少上述构建校验元数据，建议优先使用官方附件。

### Docker 源码构建升级流程
Web 界面仅用于展示新版本提示与更新摘要，无权操纵宿主机 Docker 守护进程，Docker 部署者无法直接在网页中点击完成容器升级。管理员执行升级的标准步骤：

1. 对 `./data` 业务数据目录进行离线冷备份；
2. 获取目标版本的源码（解压新的 `xianyu-saas-0.2.2-source.zip` 或拉取对应 Git 标签）；
3. 使用项目原有的 Compose 文件（以及本地覆盖配置，例如 `.local/docker-compose.local.yml`）执行本地构建与平滑重启：
   ```bash
   docker compose up -d --build --wait
   ```
4. 若存在本地覆盖配置文件，必须显式附加参数：
   ```bash
   docker compose -f docker-compose.yml -f .local/docker-compose.local.yml up -d --build --wait
   ```
5. **升级注意事项**：Docker 默认的 `./data` 属于宿主机目录绑定挂载（bind mount），升级操作会自动保留已有数据卷，但仍不建议在已有数据上执行带有 `-v` 删卷参数的清理命令；镜像是基于本地源码构建生成的，不能使用 `docker compose pull` 来更新业务代码。

### systemd 签名发布包升级
若将部署模式标记为 `SAAS_DEPLOYMENT_MODE=systemd`，只有在满足以下全部前置条件时才开放管理界面升级流程：
- 部署目录结构规范，存在受信任的版本目录与 `current` 软链接；
- 配置了受信发布公钥：首次使用签名更新前，须将项目公钥存放在服务端受保护的路径，并在环境配置中设置 `SAAS_UPDATE_PUBLIC_KEY_FILE` 的绝对路径。公钥文件属主应为 root 或服务运行用户，权限建议设为 `644` 或 `400`（禁止属组与其他用户写入）。签名私钥仅由发布端保管，绝不下发至安装端；
- 公钥可取自仓库中的 `deploy/update-signing.pub` 或 Release 附件中的 `xianyu-saas-0.2.2.update-signing.pub`。公钥指纹可在 `artifacts.json` 中的 `public_key_fingerprint` 查看（该指纹基于原始 32 字节 Ed25519 公钥二进制计算 SHA-256 并带有 `sha256:` 前缀，直接对 PEM 文本文件计算哈希与该指纹不同）；全部下载文件可通过 `SHA256SUMS` 校验完整性；
- 拥有可写的暂存目录（staging）与升级意图文件（intent file）；
- 宿主机加载了 `xianyu-saas-updater.service` 并激活了 `xianyu-saas-updater.path` 监听。

升级执行前必须通过签名公钥校验，并要求管理员在后台二次确认。独立更新服务、目录规范与依赖兼容性检查均适用，普通源码部署切勿伪装为签名发布模式，系统不承诺所有源码部署均可直接通过网页更新。

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

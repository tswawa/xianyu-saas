# 维护者发布指南

本文档为项目维护者提供版本发布、签名打包、CI 流水线及公开发布资产的核对规范。

## 1. 版本号一致性要求

准备发布新版本时，需确保以下文件中的版本号保持一致：

- `package.json` 中的 `version` 字段；
- `package-lock.json` 中的根 `version` 与 `packages[""]["version"]`；
- `backend/version.py` 中的 `VERSION` 常量；
- 前端静态资产标识：`backend/version.py` 的 `ASSET_VERSION`、`frontend/assets/app.js` 的 `ASSET_VERSION` 以及 `frontend/index.html` 中各静态资源链接的 `?v=` 参数。

`backend/version.py` 中的 `UPDATE_DATA_VERSION`：
- 用于更新器在停服前检查业务数据格式（SQLite 数据库与存储目录）的兼容声明；
- 数据库结构或存储格式不兼容时必须递增，并于发布说明写明人工迁移指引；
- 数值相同才能进入文件更新流程；数值不匹配或缺失时，更新器拒绝自动更新并保持停机前状态；新配置是否能被旧版读取仍需单独核对，不能仅凭此值承诺降级兼容；
- 内置文件更新不自动备份数据库，维护前建议做好冷备份（Docker 业务数据在 `./data`，Ubuntu 在 `/var/lib/xianyu-saas`）；仅在使用旧原生更新器执行完整运行环境升级时包含数据库备份步骤。

---

## 2. 自动化发布流水线

项目的持续发布由 GitHub Actions 工作流 [`.github/workflows/release.yml`](../.github/workflows/release.yml) 承载，在推送 `v*` 标签时触发。流水线分为三个串行阶段：

### 阶段一：CI 自动化验证 (`validate`)
复用 `.github/workflows/ci.yml`，执行自动化单元测试与仓库合规检查。

### 阶段二：原生运行时构建 (`standalone`)
通过矩阵任务在 Ubuntu 24.04（x86_64）与 Ubuntu 24.04 ARM（aarch64）原生构建机上并行执行：
1. **构建引导管理器**：在 Debian 12 容器（`python:3.12.14-slim-bookworm`）内使用 PyInstaller 构建对应架构的无后缀引导管理器可执行文件（`xianyu-saas`），并执行 `internal self-check`；
2. **下载锁定依赖 Wheel**：读取 `deploy/runtime/backend.lock.json` 与 `worker.lock.json`，核验哈希并下载锁定的预编译依赖包；
3. **构建运行时归档包**：运行 `scripts/build-standalone.py`，组合 Python 运行时、项目代码、依赖包及管理器；
4. **运行时冒烟测试**：在宿主环境验证依赖完整性（`pip check`、核心模块导入、`ldd` 动态链接库检查）；
5. **打包并上传未签名中间产物**：打包为 `standalone-<target>.tar` 供发布阶段使用。

### 阶段三：签名、验证与发布 (`publish`)
在 Ubuntu 24.04 上执行最终打包与发布：
1. 校验 Release Tag 与 `package.json` 版本号匹配；
2. 解压两架构的未签名 standalone 中间产物；
3. 执行发布契约测试：`docker-update-protocol-contract.py`、`standalone-build-contract.py`、`release-bundle-contract.py`；
4. 调用 `scripts/build-release.py` 执行签名与打包；
5. 调用 `scripts/verify-public-release.py` 在本地验证完整制品签名与清单一致性；
6. 使用 GitHub CLI 创建 Draft Release，上传 14 项发布资产；
7. 将远端资产下载至隔离临时目录，再次运行 `scripts/verify-public-release.py` 验证远端上传资产的完整性；
8. 验证全部通过后，将 Draft 状态切换为正式发布（`draft=false`）。

---

## 3. 签名密钥规范与安全约束

系统采用 Ed25519 算法对发布资产清单进行数字签名，保证更新分发链路的真实性与完整性。

### 密钥规范
- **环境变量**：`RELEASE_SIGNING_KEY`；
- **私钥格式**：32 字节 Ed25519 随机种子经标准 Base64 编码（长度 44 字符，非 URL-safe 变体）；
- **公钥文件**：PEM 格式公钥保存在仓库内 [`deploy/update-signing.pub`](../deploy/update-signing.pub)。

### 安全约束
- GitHub Actions 流水线从 Repository Secret（`RELEASE_SIGNING_KEY`）读取私钥；
- 代码仓库与发布资产中严禁包含任何私钥明文；
- 验签公钥随安装包内置，程序不自动从远程下载替换本地受信公钥，也不提供动态替换公钥的途径。

---

## 4. 本地构建与验证命令

维护者在本地离线排查或构建发布包时，可使用以下命令：

```bash
# 准备环境变量（32 字节 Base64 编码私钥种子）
export RELEASE_SIGNING_KEY="..."

# 执行构建并签名（输出目录必须在 .local/releases/ 下）
python scripts/build-release.py \
  --ref HEAD \
  --standalone-input-root .local/standalone-inputs \
  --output ".local/releases/<version>" \
  --notes-output ".local/releases/<version>-release-notes.md"

# 验证发布资产完整性与数字签名
python scripts/verify-public-release.py \
  --directory ".local/releases/<version>" \
  --version "<version>" \
  --commit "$(git rev-parse HEAD)" \
  --public-key deploy/update-signing.pub
```

---

## 5. 公开发布资产结构（共 14 项）

正式发布 Release 包含以下 14 项公开文件。签名和清单由安装程序与更新程序自动核验，正文仅突出三个供用户下载的入口：

| 序号 | 文件名 | 归属类别 | 用途说明 |
| --- | --- | --- | --- |
| 1 | `xianyu-saas-<version>-source.zip` | Docker | **用户入口 1**：Docker 源码安装包（含安装脚本与构建依赖） |
| 2 | `xianyu-saas-<version>.docker.manifest.json` | Docker | Docker 升级描述清单（绑定源码哈希与构建输入） |
| 3 | `xianyu-saas-<version>.docker.manifest.sig` | Docker | Docker 升级清单的 Ed25519 数字签名 |
| 4 | `xianyu-saas-<version>.manifest.json` | Docker | 运行时文件清单（由 Docker 清单通过哈希认证） |
| 5 | `xianyu-saas-<version>-linux-x86_64` | Ubuntu x86_64 | **用户入口 2**：x86_64 引导管理器（无后缀可执行程序） |
| 6 | `xianyu-saas-<version>-linux-x86_64.tar.gz` | Ubuntu x86_64 | x86_64 原生独立运行时压缩包 |
| 7 | `xianyu-saas-<version>-linux-x86_64.manifest.json` | Ubuntu x86_64 | x86_64 运行时清单文件 |
| 8 | `xianyu-saas-<version>-linux-x86_64.manifest.sig` | Ubuntu x86_64 | x86_64 运行时清单的 Ed25519 数字签名 |
| 9 | `xianyu-saas-<version>-linux-aarch64` | Ubuntu ARM64 | **用户入口 3**：ARM64 引导管理器（无后缀可执行程序） |
| 10 | `xianyu-saas-<version>-linux-aarch64.tar.gz` | Ubuntu ARM64 | ARM64 原生独立运行时压缩包 |
| 11 | `xianyu-saas-<version>-linux-aarch64.manifest.json` | Ubuntu ARM64 | ARM64 运行时清单文件 |
| 12 | `xianyu-saas-<version>-linux-aarch64.manifest.sig` | Ubuntu ARM64 | ARM64 运行时清单的 Ed25519 数字签名 |
| 13 | `artifacts.json` | 发布索引 | 全量发布资产目录（Schema 2）、公钥指纹与散列汇总 |
| 14 | `artifacts.json.sig` | 发布索引 | `artifacts.json` 的 Ed25519 数字签名 |

> **已清理的旧资产与更新通道说明**：外置独立公钥、独立 release-notes.md 与 SHA256SUMS 等重复校验附件已由 `artifacts.json` 及其签名归并；旧版源码 OTA（tar.gz 及其独立签名）通道停止发布，不再提供自动更新协议，既有依赖旧源码 OTA 的历史实例需由维护者人工迁移。

---

## 6. 发布说明呈现与历史版本维护规范

当前维护起点为 **0.4.6**。更早版本停止维护，但保留已有实例升级所需的发布资产与迁移信息。v0.4.6 的新增人设配置不能被 v0.4.5 完整识别，发布说明不得承诺使用新配置后可任意降级。

- **正文组织顺序**：发布说明正文按「变更在前、三个下载入口与文档链接在后」组织。先清晰列出本版本的变更要点，随后通过 `## 下载` 提供 Docker 源码包、Ubuntu x86_64 引导管理器、Ubuntu ARM64 引导管理器 3 个入口及 `README.md` 文档链接；
- **正式发布与测试收敛**：正式发布不再附加成对的测试发布；测试发布期间验证的有效改动统一并入正式版本更新说明中；
- **历史发布清理前备份**：在清理或删除 GitHub 历史 Release 前，必须完整备份对应 Release 的元数据（JSON）以及全部附件资产；
- **保留关键迁移基线**：保留安装器仍会下载的基线版本及附件（如 `v0.4.0`），不能只按发布时间清理；
- **已删发布的安装器**：旧安装器需要读取自身版本的远端清单；对应 Release 删除后，重新安装或修复需下载最新版安装器。已安装实例发现后续版本和本地回退不依赖旧 Release，但升级仍受原有部署能力、依赖与数据兼容条件限制。

---

## 7. 运行端与维护者边界说明

- **安装入口与更新入口分离**：普通用户首次安装使用上述 3 个入口文件；已安装实例后续直接通过 Web 控制台执行网页更新；
- **重复执行安装脚本**：`deploy/docker-install.sh` 具备幂等性，对已存在容器仅做校验和启动，不覆盖镜像，也不自动应用新的环境变量；
- **回滚与数据持久化**：新版本启动失败时，启动器尝试切回上一版本代码并重启；回退后仍使用现有业务数据库与配置。文件更新不自动备份或还原数据库；
- **历史环境**：未接入内置启动器的旧安装需由维护者人工迁移。

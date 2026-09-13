# 维护者发布指南

本文档为项目维护者提供版本发布、签名打包与资产分发的操作规程。

## 1. 版本号一致性要求

在准备发布新版本时，需确保以下文件中的版本与资产标识保持一致：

- `package.json` 中的 `version` 字段；
- `package-lock.json` 中的根 `version` 以及 `packages[""]["version"]`；
- `backend/version.py` 中的 `VERSION` 常量；
- 前端静态资产版本号：`backend/version.py` 中的 `ASSET_VERSION`、`frontend/assets/app.js` 中的 `ASSET_VERSION` 以及 `frontend/index.html` 中各静态资源链接的 `?v=` 查询串。

Docker 网页更新还会检查 `backend/version.py` 的 `UPDATE_DATA_VERSION`：相同的正整数表示这些版本能够双向读写同一份业务数据。数据库、配置或凭据格式发生不兼容变更时必须递增，不能重复使用旧值，并在发布说明中写明人工迁移步骤。标记缺失或不同会拒绝自动更新和回退；这是发布者的兼容性声明，不是对迁移脚本的自动证明。Docker 更新器不再自动冷备或预演全量业务数据，日常备份仍由部署者负责。

## 2. 发布前检查与门禁流程

1. **更新日志维护**：在 `CHANGELOG.md` 中添加对应版本的正式章节（例如 `## [<version>] - YYYY-MM-DD`），准确记录本次版本的新增功能、变更事项与问题修复；
2. **本地测试验证**：运行全套测试套件并确保全部通过：
   ```bash
   npm test
   python tests/repository-contract.py
   git diff --check
   ```
3. **主分支 CI 验证**：将代码提交并推送到 `main` 分支，等待 GitHub Actions 的 `ci.yml` 工作流完整执行通过；
4. **推送版本标签**：确认主分支 CI 通过后，创建并推送对应的版本标签：
   ```bash
   VERSION=0.2.2
   git tag "v${VERSION}"
   git push origin "v${VERSION}"
   ```

## 3. 自动化发布流水线

GitHub Actions 工作流 [`.github/workflows/release.yml`](../.github/workflows/release.yml) 在检测到 `v*` 格式的标签推送时触发：

1. **流水线复用**：首先调用 `.github/workflows/ci.yml` 复用全套自动化测试；
2. **签名打包**：测试通过后，在 Ubuntu 环境下调用 `scripts/build-release.py`，从当前 commit 对应的 Git blob 构建签名发布包；
3. **草稿发布与校验**：通过 GitHub CLI（`gh`）先创建 Draft Release，上传全套 8 项发布产物并校验文件非空，确认无误后正式发布。

## 4. 签名密钥规范与安全存储

系统采用 Ed25519 算法对发布归档清单进行数字签名，确保 OTA 更新分发链路的完整性。

### 密钥规范
- **环境变量名称**：`RELEASE_SIGNING_KEY`；
- **私钥格式**：32 字节 Ed25519 随机种子经标准 Base64 编码（长度为 44 字符的标准 Base64 字符串，不采用 URL-safe 变体）；
- **公钥文件**：公钥以 PEM 格式保存在公开仓库的 [`deploy/update-signing.pub`](../deploy/update-signing.pub) 中。

### 安全约束
- 自动发版流水线直接读取 GitHub Actions Secret（`RELEASE_SIGNING_KEY`）；
- 当前维护者若在本地保存离线凭据副本，可采用 Windows DPAPI 加密存储（绑定当前 Windows 操作系统账户，不提供跨机器恢复保证）。该加密方式属于当前维护者的本机保管方案，其他环境遵循各自平台的密钥安全规范即可；
- 公开仓库与发布源码包严禁包含任何真实私钥或示例私钥文件；严禁擅自修改已发布的公钥。

## 5. 本地构建与打包命令

在维护者本地或离线环境中执行签名打包时，使用以下命令：

```bash
VERSION=0.2.2
python scripts/build-release.py --ref HEAD --output ".local/releases/${VERSION}"
```

构建脚本执行以下约束校验：
- 从环境变量 `RELEASE_SIGNING_KEY` 读取 Base64 编码的私钥种子；
- 从指定 commit（例如 `HEAD`）内部读取 `deploy/update-signing.pub`，验证公私钥严格配对；
- 检查已跟踪的工作区状态，若存在未提交的已跟踪修改则中止打包；
- 输出目录必须位于已配置忽略的 `.local/releases/` 路径下，若目标目录已存在且非空则拒绝覆盖。

## 6. 发布产物清单

### 已发布版本（v0.2.2 历史事实）
历史发布的 `v0.2.2` 包含 8 项标准官方产物：
1. `xianyu-saas-<version>.tar.gz`：OTA 升级归档，适配 systemd 更新器白名单规范；
2. `xianyu-saas-<version>.manifest.json`：归档清单，记录 OTA 包元数据与文件哈希；
3. `xianyu-saas-<version>.manifest.sig`：对 `manifest.json` 的 Ed25519 签名；
4. `xianyu-saas-<version>-source.zip`：完整源码包，包含对应 Git commit 的全部源码、Docker 构建文件与文档；
5. `xianyu-saas-<version>.update-signing.pub`：构建时从 commit 中提取的签名公钥副本；
6. `release-notes.md`：从 `CHANGELOG.md` 中提取的当前版本更新说明；
7. `artifacts.json`：构建元数据汇总文件，包含公钥指纹以及 6 项内容资产的大小与校验和；
8. `SHA256SUMS`：包含 6 项内容资产与 `artifacts.json` 共 7 项文件的 SHA-256 校验清单，不包含自身散列。

### 后续正式版本（0.3.0+ 规划）
未来正式版本扩展为 10 项标准产物（新增 2 项 Docker 升级签名资产）：
1. `xianyu-saas-<version>.tar.gz`
2. `xianyu-saas-<version>.manifest.json`
3. `xianyu-saas-<version>.manifest.sig`
4. `xianyu-saas-<version>-source.zip`
5. `xianyu-saas-<version>.docker.manifest.json`：Docker 升级清单，将完整源码包与构建输入绑定至版本、提交散列、源码哈希与尺寸；
6. `xianyu-saas-<version>.docker.manifest.sig`：对 `docker.manifest.json` 的 Ed25519 签名；
7. `xianyu-saas-<version>.update-signing.pub`
8. `release-notes.md`
9. `artifacts.json`：汇总 8 项内容资产元数据（含上述 2 项 Docker 资产）；
10. `SHA256SUMS`：覆盖除自身外的全部 9 项文件的 SHA-256 校验清单。

### 指纹与完整性校验规则
- `artifacts.json` 中的 `public_key_fingerprint` 定义为原始 32 字节 Ed25519 公钥二进制数据的 SHA-256 哈希（格式为 `sha256:<hex>`）。该指纹计算对象为解码后的原始公钥字节，不采用 PEM 文本文件的散列；
- 下载发布资产的使用者可通过标准工具核验资产完整性：
  ```bash
  sha256sum -c SHA256SUMS
  ```

## 7. 运行端更新与环境约束

- **systemd 部署模式**：运行端首次接入须由 root 使用生产虚拟环境 Python，依序完成独立更新器组件安装、离线基线导入（`--import-trusted-baseline` 传入刚好三个绝对资产路径，经本地预装公钥验签与 AST 静态语法核验 `MAINTENANCE_PROTOCOL=1`，不自动切换 current/不触碰业务数据）、人工切换现役软链接，以及通过显式受控环境变量执行 `--initialize`（配置 sticky `01770` 专用 IPC 目录并原子写入严格六字段的可信接入记录 `initialization.json`，校验 API 公钥、独立 bundle 固定 8 文件与 entrypoint，作为控制面放行升级的门禁）；实际路径需与服务模板及环境变量严格同步。本机仅静态与便携测试通过，真实 Linux/systemd 端到端动态验收仍在等待隔离验证环境；
- **Docker 部署模式**：通用方式仍支持管理员在宿主机通过 `docker compose up -d --build` 手动构建升级；新规划的 `docker-compose.updates.yml` 独立更新器引入官方 Compose 5.5.1 执行层（Docker CLI 28.3.3 与 Buildx 0.26.1 保持固定），依赖 Docker Engine API v1.47，作为受信任的高权限组件挂载宿主机 Docker socket（`read_only` 属于文件系统挂载属性，更新器仍可借由 UNIX socket 调用高权限 Docker 引擎管理 API 完成镜像构建与重编排），Web 容器不挂载 socket。首次接入通过标准输入原字节向 `initialize` 子命令登记完整 Compose 配置，回滚支持重新创建旧版容器（容器 ID 可变，原配置与数据卷保持，绝不覆盖业务数据）。升级期间不支持并行执行外部容器变更，检测到配置漂移时保留维护现场转人工排查。所有真实 Linux/Docker 环境端到端验收仍在等待隔离引擎环境；
- **生产环境验收**：代码与打包阶段的离线测试不能代替真实闲鱼账号会话、真实第三方大模型接口以及真实订单履约流程的现场验证；
- **部署与权限参考**：完整的生产环境配置、数据卷挂载及权限要求参见 [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)。

# 维护者发布指南

本文档为项目维护者提供版本发布、签名打包与资产分发的操作规程。

## 1. 版本号一致性要求

在准备发布新版本时，需确保以下文件中的版本与资产标识保持一致：

- `package.json` 中的 `version` 字段；
- `package-lock.json` 中的根 `version` 以及 `packages[""]["version"]`；
- `backend/version.py` 中的 `VERSION` 常量；
- 前端静态资产版本号：`backend/version.py` 中的 `ASSET_VERSION`、`frontend/assets/app.js` 中的 `ASSET_VERSION` 以及 `frontend/index.html` 中各静态资源链接的 `?v=` 查询串。

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
   VERSION=0.2.0
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
VERSION=0.2.0
python scripts/build-release.py --ref HEAD --output ".local/releases/${VERSION}"
```

构建脚本执行以下约束校验：
- 从环境变量 `RELEASE_SIGNING_KEY` 读取 Base64 编码的私钥种子；
- 从指定 commit（例如 `HEAD`）内部读取 `deploy/update-signing.pub`，验证公私钥严格配对；
- 检查已跟踪的工作区状态，若存在未提交的已跟踪修改则中止打包；
- 输出目录必须位于已配置忽略的 `.local/releases/` 路径下，若目标目录已存在且非空则拒绝覆盖。

## 6. 发布产物清单

每次正式发布包含以下 8 项标准产物：

1. `xianyu-saas-<version>.tar.gz`：OTA 升级归档，适配 systemd 更新器白名单规范，内部无顶层包装目录，解压即为项目文件结构；
2. `xianyu-saas-<version>.manifest.json`：归档清单，记录 OTA 包元数据以及内部每个文件的相对路径、大小、SHA-256 校验和与可执行权限；
3. `xianyu-saas-<version>.manifest.sig`：对 `manifest.json` 全文的标准 Base64 格式 Ed25519 签名；
4. `xianyu-saas-<version>-source.zip`：完整源码包，包含对应 Git commit 跟踪的全部源代码、Docker 构建文件与文档，包含 `xianyu-saas-<version>/` 顶层目录，不含运行时数据、私有配置与临时文件；
5. `xianyu-saas-<version>.update-signing.pub`：构建时从 commit 中提取的签名公钥副本；
6. `release-notes.md`：从 `CHANGELOG.md` 中提取的当前版本更新说明；
7. `artifacts.json`：构建元数据汇总文件，包含版本号、commit 哈希、公钥指纹以及 6 项内容资产（`tar.gz`、`manifest.json`、`manifest.sig`、`source.zip`、`pub`、`release-notes.md`）的大小与 SHA-256 校验和；
8. `SHA256SUMS`：包含 6 项内容资产与 `artifacts.json` 共 7 项文件的 SHA-256 校验清单，不包含自身散列。

### 指纹与完整性校验规则
- `artifacts.json` 中的 `public_key_fingerprint` 定义为原始 32 字节 Ed25519 公钥二进制数据的 SHA-256 哈希（格式为 `sha256:<hex>`）。该指纹计算对象为解码后的原始公钥字节，不采用 PEM 文本文件的散列；
- 下载发布资产的使用者可通过标准工具核验资产完整性：
  ```bash
  sha256sum -c SHA256SUMS
  ```

## 7. 运行端更新与环境约束

- **systemd 部署模式**：运行端首次配置并显式信任公钥（`deploy/update-signing.pub`）后，方可启用带签名校验的自动更新服务；
- **Docker 部署模式**：当前 Docker 运行模式仅提供新版本检测与后台提示，容器更新需由管理员在宿主机通过 `docker compose build` 与 `docker compose up -d` 完成，工作台网页不提供接管宿主机升级容器的功能；
- **生产环境验收**：代码与打包阶段的离线测试不能代替真实闲鱼账号会话、真实第三方大模型接口以及真实订单履约流程的现场验证；
- **部署与权限参考**：完整的生产环境配置、数据卷挂载及权限要求参见 [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)。

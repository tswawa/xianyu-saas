# 公开发布自检清单

在对外发布新版本或向开源社区提交代码前，请按照本清单逐项核对，确保代码整洁、安全合规。

## 1. 敏感信息与安全自查
- [ ] 工作区与 Git 提交历史中无真实 API Key、密码、Token、平台 Cookie 或私人凭据；
- [ ] 代码与测试夹具中无真实买家信息、真实订单号、商业卡密明文或私人网盘链接；
- [ ] `.gitignore` 与 `.dockerignore` 正常生效，`data/` 目录、本地运行日志及临时文件未被提交；
- [ ] 真实生产环境配置与私有凭据不纳入 Git，仅保留合法的示例与模板配置，无私有域名、生产私有路径或特定主机硬编码路径。

## 2. 开源合规与许可证检查
- [ ] 根目录 `LICENSE`（GPL-3.0-only）完整；
- [ ] `LICENSING.md`、`worker/NOTICE.md` 与字体开源许可证（`frontend/assets/OFL-NotoSansSC.txt`）齐全规范；
- [ ] 文档包含免责声明，明确提示使用者遵守平台规则与法律法规。

## 3. 自动化门禁测试
- [ ] 运行并通过全套单元测试套件：`npm test`（专项测试不能代替全套测试）；
- [ ] 运行并通过仓库合规检查：`python3 tests/repository-contract.py`；
- [ ] 运行并通过代码语法与编译检查：`npm run test:syntax`；
- [ ] 运行并通过核心契约测试套件：`npm run test:api`、`npm run test:auth`、`npm run test:settings`、`npm run test:resources`；
- [ ] 运行并通过 Worker 单元测试：`npm run test:worker`；
- [ ] 检查代码格式与空白符差异：`git diff --check`。

## 4. 管理员初始化与环境配置验证
- [ ] 环境变量格式：`SAAS_AI_MASTER_KEY` 采用 32 字节随机二进制经标准 Base64 编码（44 字符，示例：`base64.b64encode(os.urandom(32)).decode()`），不能配置任意非规范字符串；
- [ ] 默认模式验证：在 `SAAS_BOOTSTRAP_ENABLED=0` 且数据库为空时，首位管理员可通过网页界面直接注册；布尔开关 `SAAS_ALLOW_REGISTRATION=0` 不影响首位管理员注册；初始化与后续注册支持补齐空目录与纯默认配置残留，拒绝业务数据与符号链接，具备并发回滚保护；
- [ ] 引导模式验证：在 `SAAS_BOOTSTRAP_ENABLED=1` 时，首位管理员注册需通过 `SAAS_BOOTSTRAP_TOKEN_FILE` 提供的一次性服务引导令牌完成；
- [ ] 后续注册限制：系统已有管理员后，新用户注册同时受布尔开关环境变量 `SAAS_ALLOW_REGISTRATION` 与系统管理后台注册开关控制。

## 5. 容器与部署验证
- [ ] Docker 镜像构建正常：`docker compose build`；
- [ ] 容器启动后健康检查正常响应：`GET /health` 返回 200；
- [ ] 挂载数据卷 `./data` 能持久化数据库与各店铺配置文件；
- [ ] Windows 部署说明明确通过 Docker Linux 容器运行完整服务，不宣称 Windows 原生直接运行依赖 `fcntl` 等内核特性的全部服务。

## 6. 版本发布与制品完整性检查
- [ ] 版本号一致性：`package.json`、`package-lock.json` 与 `backend/version.py` 版本号一致（如 `0.2.2`）；
- [ ] 签名密钥分离：签名私钥由 GitHub Actions secret `RELEASE_SIGNING_KEY` 托管，公钥保存于 `deploy/update-signing.pub`，严禁将私钥打入任何安装包或源码；
- [ ] 自动化打包工作流：明确 `main` 分支 CI 成功后方可推送版本标签（不能预先勾选成功或提前推签）；标签推送后触发 `.github/workflows/release.yml`，复用 CI 验证并自动执行 `scripts/build-release.py` 生成发布草稿；
- [ ] 发行资产齐全（共 8 项）：`xianyu-saas-0.2.2.tar.gz`、`xianyu-saas-0.2.2.manifest.json`、`xianyu-saas-0.2.2.manifest.sig`、`xianyu-saas-0.2.2-source.zip`、`xianyu-saas-0.2.2.update-signing.pub`、`release-notes.md`、`artifacts.json`、`SHA256SUMS`；
- [ ] 资产用途明确：`source.zip` 包含完整安全源码，用于 Docker 或手动部署；`tar.gz` + `manifest.json` + `manifest.sig` 专供签名 systemd 更新器；
- [ ] 校验和与公钥指纹：`SHA256SUMS` 覆盖 6 项内容资产与 `artifacts.json` 共 7 项制品（不包含自身散列）；`artifacts.json` 中的 `public_key_fingerprint` 为原始 32 字节 Ed25519 公钥二进制的 SHA-256 摘要（带 `sha256:` 前缀，与直接计算 PEM 文本哈希不同）。

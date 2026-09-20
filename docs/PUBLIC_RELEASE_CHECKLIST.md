# 公开发布自检清单

维护者在整批构建完成、正式发布版本前按本清单逐项核对。详细构建流程与技术约束见 [`docs/RELEASING.md`](RELEASING.md)。

## 1. 敏感信息与安全合规
- [ ] 工作区与 Git 提交历史中无真实 API Key、密码、Token、平台 Cookie 或私人凭据；
- [ ] 代码与测试夹具中无真实买家信息、真实订单号、商业卡密明文或私人网盘链接；
- [ ] `.gitignore` 与 `.dockerignore` 正常生效，`data/` 目录、本地运行日志及临时文件未被提交；
- [ ] 仅保留合法的示例与模板配置，无私有域名或特定主机硬编码路径。

## 2. 开源合规与许可证
- [ ] 根目录 `LICENSE`（GPL-3.0-only）完整；
- [ ] `LICENSING.md`、`worker/NOTICE.md` 与字体开源许可证（`frontend/assets/OFL-NotoSansSC.txt`）齐全规范；
- [ ] 上游署名与第三方许可说明准确，未混入未经授权的代码或素材。

## 3. 版本号一致性
- [ ] `package.json`、`package-lock.json` 与 `backend/version.py` 版本号一致；
- [ ] `backend/version.py`、`frontend/assets/app.js` 与 `frontend/index.html` 静态资产版本标识一致；
- [ ] `backend/version.py` 的 `UPDATE_DATA_VERSION` 声明与当前业务数据读写兼容性一致。

## 4. 门禁验证与发布流水线
- [ ] 整批改动完成后在本地集中验证：按本批改动风险针对性运行相关测试，并执行 `git diff --check` 确认无格式或空白问题；
- [ ] 推送待发布的 `v*` 标签后，由 `.github/workflows/release.yml` 的 `validate` 阶段统一执行完整 CI 检查，且 `validate` 与 `standalone` 阶段全部通过。

## 5. 当前正式版的 14 项发布资产完整性与签名
- [ ] Docker 资产 4 项完整（`source.zip`、`.docker.manifest.json`、`.docker.manifest.sig`、`.manifest.json`）；
- [ ] Ubuntu x86_64 资产 4 项完整（管理器、`.tar.gz`、`.manifest.json`、`.manifest.sig`）；
- [ ] Ubuntu ARM64 资产 4 项完整（管理器、`.tar.gz`、`.manifest.json`、`.manifest.sig`）；
- [ ] 发布索引 2 项完整（`artifacts.json`、`artifacts.json.sig`）；
- [ ] 流水线中 `scripts/verify-public-release.py` 在本地打包后及远端上传后二次下载核验均成功通过。

## 6. 发布页面与说明核对
- [ ] 发布正文只包含用户可感知的功能、修复与必要升级信息，不含文档整理、截图制作、测试执行或内部协作记录；
- [ ] GitHub Release 正文按「变更在前、三个下载入口与文档链接在后」排布，包含版本核心变更说明；
- [ ] 包含三个明确的用户下载入口（Docker `source.zip`、Ubuntu x86_64 管理器、Ubuntu ARM64 管理器）及 `README.md` 文档链接，不重复冗长命令；
- [ ] 确认当前正式版 Release 为 `draft=false`、`prerelease=false`，且为 Latest；
- [ ] v0.4.6 以前的历史 Release 均标记为测试版（`prerelease=true`），上传附件为空；正文指向当前正式版，无失效下载入口。

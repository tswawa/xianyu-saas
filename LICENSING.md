# 许可证说明

本文件说明仓库中的许可证边界，不构成法律意见。发布或再分发前，请确认代码、字体、图片和其他素材具有相应授权。

## 默认许可证

除文件另有明确说明外，本仓库原创代码、测试、文档和配置模板按 **GNU General Public License v3.0 only**（GPL-3.0-only）提供，完整文本见根目录 [`LICENSE`](LICENSE)。

根 `package.json` 的 `private: true` 只表示项目不应被误发布到 npm，不改变源码许可证。

## `worker/` 的来源

`worker/` 包含基于 [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) 修改的代码，继续遵循 GPL-3.0。上游许可证全文见 [`worker/LICENSE`](worker/LICENSE)，来源和修改范围见 [`worker/NOTICE.md`](worker/NOTICE.md)。

## 第三方资产和依赖

- `frontend/assets/OFL-NotoSansSC.txt` 与生成的 `ui-sans-generated.woff2` 按随附的 SIL Open Font License 1.1 处理。
- 第三方依赖遵循各自上游许可证；发布前应审阅锁定版本的许可证和 NOTICE 要求。
- 新增图片、字体、图标、示例数据或其他外部素材时，贡献者必须在 Pull Request 中说明来源、许可证和再分发条件。
- 未明确获得授权的第三方素材不属于本项目的发布内容，不能作为示例或构建产物提交。

## 贡献者责任

提交内容的贡献者必须拥有相应权利，并同意该内容按本仓库适用的许可证再分发。贡献不得包含凭据、真实业务数据或未授权的第三方材料。

## 平台与合规边界

本项目是第三方平台自动化工具，不代表闲鱼、淘宝、阿里巴巴或任何模型服务商。使用者应自行遵守平台规则、服务条款、隐私法规和数据保护要求，并负责评估账号安全与数据合规风险。
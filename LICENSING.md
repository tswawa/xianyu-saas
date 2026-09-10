# 许可证说明

本文档概述仓库中的许可证边界与第三方素材授权情况，仅供参考，不构成法律意见。分发前请确认代码、字体与相关素材的授权合规性。

## 原创代码许可证

除特定文件另有说明外，本仓库原创代码、测试、文档与配置文件模板遵循 **GNU General Public License v3.0 only**（GPL-3.0-only）开源许可，许可证全文见根目录 [`LICENSE`](LICENSE)。

根目录 `package.json` 中标注的 `"private": true` 用于避免包误发布至 npm 公共源，不影响项目源码本身的开源许可协议。

## `worker/` 组件来源与协议

`worker/` 目录中的代码基于开源项目 [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) 进行修改和重构，继续遵循 GPL-3.0 许可证。来源、修改范围与上游版权声明见 [`worker/NOTICE.md`](worker/NOTICE.md)，许可证文本见 [`worker/LICENSE`](worker/LICENSE)。

## 字体与第三方资产

- `frontend/assets/OFL-NotoSansSC.txt` 与字体文件 `frontend/assets/ui-sans-generated.woff2` 遵循 SIL Open Font License 1.1（OFL）许可证；
- 第三方 Python 与 Node.js 运行时依赖遵循各自上游项目的开源许可证；
- 贡献者提交外部素材时，应说明来源、许可证要求与再分发条件。

## 贡献者要求

贡献者提交的内容须拥有相应合法权利，并同意按本仓库适用的开源许可证进行分发。提交内容严禁包含私人凭据、商业密钥、真实业务数据或未经授权的第三方资产。

## 免责声明与平台合规

本项目属于独立的第三方自动化工具，与闲鱼、淘宝、阿里巴巴集团或各模型服务商无任何官方关联或认证关系。使用者应自行遵守所在司法管辖区法律法规以及平台服务协议，并承担相关合规与使用责任。

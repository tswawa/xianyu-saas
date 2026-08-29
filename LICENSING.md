# 许可证说明

本文件是仓库许可证边界说明，不构成法律意见。发布或再分发前，请由项目负责人确认自己拥有相应代码、字体和素材的授权。

## 仓库默认许可证

除文件另有明确说明外，本仓库原创代码、测试、文档和配置模板均按 **GNU General Public License v3.0 only**（GPL-3.0-only）提供，完整许可证文本见根目录 [`LICENSE`](LICENSE)。

根 `package.json` 保留 `private: true`，只表示本项目不应被误发布到 npm；它不改变源码许可证。

## `worker/` 上游代码

`worker/` 包含基于 [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) 的修改版本，继续遵循 GPL-3.0。上游许可证全文见 [`worker/LICENSE`](worker/LICENSE)，来源与本地修改记录见 [`worker/NOTICE.md`](worker/NOTICE.md)。

## 第三方资产

- `frontend/assets/OFL-NotoSansSC.txt` 与生成的 `ui-sans-generated.woff2` 属于字体资产，按其随附的 SIL Open Font License 1.1 条款处理；字体不能被本仓库 GPL 文本替代或重新授权。
- 依赖包各自遵循其上游许可证；安装依赖后应通过包元数据和项目锁定版本进行审计。
- `蒸馏/` 中的内部分析截图、演示材料和其他未明确授权的第三方素材不是公开再分发许可的一部分。未来将仓库改为公开前，必须从工作区和 Git 历史中清除这些内容。

## 贡献者许可

向本仓库提交代码即表示贡献者有权提交该内容，并同意其在本仓库适用的 GPL-3.0-only 条款下被再分发。若贡献包含第三方代码或资产，必须在 Pull Request 中说明来源、许可证和保留的 NOTICE 文件。

## 平台与合规边界

本项目是第三方平台自动化工具，不代表任何平台官方立场。使用者必须自行遵守闲鱼、淘宝、阿里巴巴及所在地区的服务条款、隐私法规和数据保护要求，不得将真实 Cookie、Token、API Key、订单、买家消息或库存提交到仓库、Issue 或 Pull Request。

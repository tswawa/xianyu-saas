# xianyu-saas

管理闲鱼店铺的自托管工作台。集中处理买家消息、商品、客服规则和虚拟商品发货，也可以通过店铺助手调整日常配置。

[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue.svg)](LICENSE)

[下载安装](#安装) · [网页更新](#网页更新) · [更多截图](#更多截图) · [使用文档](#文档)

![工作台概览](docs/assets/readme/overview.png)

本文所有截图均使用当前界面和离线演示数据，不代表真实店铺的运营结果。

## 功能

| 功能 | 可以做什么 |
| --- | --- |
| 多店铺管理 | 扫码接入店铺、切换账号、查看连接与运行状态；不同店铺分别保存配置和业务数据。 |
| 客服工作台 | 查看会话、人工回复和发送图片；人工介入后，暂时停止该会话的自动回复。 |
| 关键词与 AI 客服 | 配置店铺或商品回复规则、客服风格和知识内容；用对话沙盘试答，再保存启用。 |
| 商品与自动发货 | 为商品绑定卡密、网盘链接或文字资料，核验订单后按配置发货，查看库存和订单状态。 |
| 店铺助手 | 通过对话调整当前店铺的问答、知识和发货配置，并查看执行结果。助手不直接执行真实发货或退款。 |
| 模型与运行设置 | 配置 OpenAI 兼容、Claude、Gemini 或 Ollama 连接；管理员可设置运行数量和内存限制。 |

## 安装

当前正式版为 **[0.4.5](https://github.com/tswawa/xianyu-saas/releases/tag/v0.4.5)**。选择一种部署方式即可。

| 部署方式 | 下载 |
| --- | --- |
| Docker（推荐） | [源码安装包](https://github.com/tswawa/xianyu-saas/releases/download/v0.4.5/xianyu-saas-0.4.5-source.zip) |
| Ubuntu x86_64 | [安装器](https://github.com/tswawa/xianyu-saas/releases/download/v0.4.5/xianyu-saas-0.4.5-linux-x86_64) |
| Ubuntu ARM64 | [安装器](https://github.com/tswawa/xianyu-saas/releases/download/v0.4.5/xianyu-saas-0.4.5-linux-aarch64) |

安装请使用上表文件。其他清单与签名由程序处理；GitHub 自动生成的 “Source code” 归档不是这里的源码安装包。

### Docker

需要 Linux Docker Engine 与 Compose 插件。Windows 用户请先启动 Docker Desktop，再在 WSL2 的 Linux 文件系统中运行以下命令。

```bash
VERSION=0.4.5
curl -fLO "https://github.com/tswawa/xianyu-saas/releases/download/v${VERSION}/xianyu-saas-${VERSION}-source.zip"
unzip -q "xianyu-saas-${VERSION}-source.zip"
cd "xianyu-saas-${VERSION}"
sudo bash deploy/docker-install.sh
```

服务器本机访问：`http://127.0.0.1:4173/xianyu-saas/`。业务数据保存在安装目录的 `data/`，配置在 `config/saas.env`。

Docker 默认仅映射本机端口。远程访问、域名与 HTTPS 配置见[部署指南](docs/DEPLOYMENT.md)。重复运行安装脚本只会检查并启动已有容器；网页更新见下方说明。

### Ubuntu 原生安装

支持 Ubuntu 22.04、24.04 和 Debian 12，需要 systemd。根据处理器架构选择 `x86_64` 或 `aarch64`。

```bash
VERSION=0.4.5
ARCH=x86_64  # ARM64 改为 aarch64
curl -fLO "https://github.com/tswawa/xianyu-saas/releases/download/v${VERSION}/xianyu-saas-${VERSION}-linux-${ARCH}"
chmod +x "xianyu-saas-${VERSION}-linux-${ARCH}"
sudo "./xianyu-saas-${VERSION}-linux-${ARCH}" install --version "$VERSION"
```

服务器本机访问：`http://127.0.0.1:8096/xianyu-saas/`。业务数据在 `/var/lib/xianyu-saas`，配置在 `/etc/xianyu-saas.env`。使用 `sudo xianyu-saas status` 查看状态，其他管理命令见[部署指南](docs/DEPLOYMENT.md)。

### 首次使用

1. 打开工作台，创建首个管理员账号。请先完成初始化，再向公网开放访问。
2. 在「店铺管理」添加店铺，用闲鱼 App 扫码连接。
3. 在「系统设置 → 模型连接」配置模型并测试连接；只使用关键词规则时可先跳过。
4. 配置客服内容和回复规则，在沙盘中试答后保存并开启客服。
5. 如需自动发货，为商品绑定发货资料或卡密池，再启用对应配置。

沙盘不会发送真实闲鱼消息；正式客服和自动发货开启后会影响店铺业务，请先核对配置。

## 网页更新

管理员点击顶部版本号检查更新。有新版本时：

1. 点击「更新」，等待下载和校验；窗口会显示下载大小与进度。
2. 下载期间可以关闭窗口，再次打开后继续查看。
3. 校验完成后输入管理员密码确认，程序切换版本并重启。

![网页更新与下载进度（演示数据）](docs/assets/readme/update.png)

普通代码更新无需重建 Docker 镜像。新版本启动失败时会尝试恢复上一版本；更新不会自动备份或还原数据库，请自行保管业务数据和配置备份。

未接入更新组件的旧安装需要先迁移；依赖或数据格式不兼容时，文件更新会被拒绝。重新安装或修复请使用正式版安装器，具体操作见[部署与升级说明](docs/DEPLOYMENT.md)。

## 更多截图

<details>
<summary>展开查看店铺、客服、商品、发货和设置页面</summary>

### 店铺管理
![店铺管理](docs/assets/readme/shops.png)

### 客服与人工接管
![客服工作台](docs/assets/readme/customer-service.png)

### AI 客服与沙盘
![AI 客服设置](docs/assets/readme/ai-config.png)

### 商品、卡密与订单
![商品管理](docs/assets/readme/goods.png)
![卡密库存](docs/assets/readme/cards.png)
![订单列表](docs/assets/readme/orders.png)

### 店铺助手
![店铺助手](docs/assets/readme/operations.png)

### 模型与运行设置
![模型连接](docs/assets/readme/settings.png)
![运行限制](docs/assets/readme/resources.png)

### 移动端
![移动端概览](docs/assets/readme/overview-mobile.png)
![移动端客服](docs/assets/readme/customer-service-mobile.png)

</details>

## 文档

| 需要了解的内容 | 文档 |
| --- | --- |
| 安装、远程访问、配置、备份与旧版迁移 | [部署指南](docs/DEPLOYMENT.md) |
| 用户角色、店铺归属与权限 | [权限说明](docs/ACCESS_MODEL.md) |
| AI 客服、规则与沙盘的工作方式 | [客服说明](docs/AI-CUSTOMER-SERVICE-REQUIREMENTS.md) |
| 本地开发与贡献代码 | [贡献指南](CONTRIBUTING.md) · [Ubuntu 开发环境](docs/NEW_UBUNTU_HANDOFF.md) |
| 进程、数据流与 Worker | [系统架构](docs/ARCHITECTURE.md) · [Worker 说明](worker/README.md) |
| 版本变化与发布维护 | [更新记录](CHANGELOG.md) · [发布指南](docs/RELEASING.md) |
| 漏洞报告与社区协作 | [安全政策](SECURITY.md) · [行为准则](CODE_OF_CONDUCT.md) |

## 许可证与使用说明

项目采用 [GPL-3.0-only](LICENSE) 许可。上游代码与第三方组件说明见 [LICENSING.md](LICENSING.md) 和 [worker/NOTICE.md](worker/NOTICE.md)。

本项目是独立的第三方店铺管理工具，与闲鱼官方无关联。使用时请遵守平台规则，合理设置请求频次，并自行评估账号与业务风险。

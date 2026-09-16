# xianyu-saas

闲鱼多店铺客服与自动发货工作台，支持部署在自己的服务器上。店铺连接、买家会话、商品资料、回复规则和发货记录都可以在同一个后台管理。

项目适合需要同时管理多个闲鱼账号、处理重复咨询，或销售卡密、兑换码、网盘资料等虚拟商品的卖家。可以只使用关键词回复，也可以接入大模型，让客服结合商品信息和店铺知识回答问题；发货按商品单独配置。

[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue.svg)](LICENSE)

[功能介绍](#功能介绍) · [下载安装](#安装) · [首次使用](#首次使用) · [网页更新](#网页更新) · [更多截图](#更多截图) · [开发与文档](#开发与文档)

![工作台概览](docs/assets/readme/overview.png)

## 功能介绍

### 多店铺管理

在工作台添加店铺后，用闲鱼 App 扫码完成登录。通过店铺切换器，可以分别查看每个账号的商品、买家会话、订单和客服配置。

- 每个店铺分别运行，配置、商品缓存与会话记录按店铺保存。
- 概览页展示连接状态、买家消息、自动回复、发货结果与待办事项。
- 查看各店铺进程的 CPU、内存占用和运行时间，了解当前运行情况。
- 店铺掉线后可重新扫码接入，后台核验新登录状态后替换原连接。

### 关键词与 AI 客服

常见问题可以直接配置关键词话术。买家咨询某个商品时，系统先匹配该商品的专属规则，再匹配全店通用规则；未命中时，可由 AI 客服结合店铺设定、商品信息和知识内容生成回复。

- **关键词规则**：按关键词包含关系匹配，支持标准、保守和激进三种策略。标准策略按规则顺序回复，保守策略将长消息留给后续处理，激进策略优先采用更长、更具体的关键词。
- **客服风格**：设置客服称谓、回复口吻、长度与表情习惯，也可选择现有风格预设。
- **商品知识**：补充商品介绍、规格、使用方法和常见问答；也可以粘贴说明文本，由模型整理成知识条目后保存。
- **回复节奏**：设置营业时间、打字延迟和消息冷却，按店铺安排自动回复时段。
- **对话沙盘**：直接用正在编辑的客服内容进行多轮试答，查看不同问题的回复效果，边调整边预览。

### 客服会话与人工接管

客服工作台集中展示买家会话与消息记录。需要人工处理时，接管对应对话即可发送文字和图片；接管期间暂停该会话的自动回复，页面显示剩余接管时间。

图片支持按顺序排队发送，一次最多选择 8 张。接管只作用于当前会话，其他买家的自动回复和已经核验的订单发货继续运行。接管时间结束后，系统按店铺配置恢复自动回复。

### 商品管理与自动发货

同步店铺商品后，可以在商品页查看发货资料的绑定情况，并为不同商品选择对应模板。订单付款后，系统核对平台订单状态、商品、买家与卖家信息，再执行配置好的发货动作。

| 发货类型 | 使用方式 |
| --- | --- |
| 卡密与兑换码 | 导入卡密池，按订单购买数量预留并分配卡密，支持 1–50 件，记录每张卡密的发放状态。 |
| 网盘资料 | 为商品配置分享链接与提取码，订单核验通过后发送对应资料。 |
| 固定文字 | 保存使用教程、操作说明等文字内容，支持单件订单自动发送；多件订单进入人工复核。 |

卡密库存、资料模板和订单记录可以分别查看，便于补充库存、调整商品绑定及跟踪发货结果。自动发货也可配合纯关键词客服使用。

### 店铺助手

店铺助手提供对话式配置入口。选中店铺后，可以通过聊天为目标商品补充客服知识、修改关键词规则，或把已有卡密池、网盘资料绑定到商品。

例如，可以让助手给某个商品补充一条使用说明，再继续调整这条说明的表达。会话保留上下文，每次操作都会显示执行结果，也支持停止生成、重试和继续追问。

### 模型连接与账号管理

在「系统设置 → 模型连接」统一填写接口地址、模型名称和 API Key，测试连接后保存。同一用户管理的店铺共用这份连接，供 AI 客服、知识整理和店铺助手使用。

- 支持 OpenAI Chat Completions 兼容接口、OpenAI Responses、Anthropic Claude、Google Gemini 和 Ollama。
- 每个用户管理自己的店铺、业务数据和模型配置。
- 管理员可管理用户、开放注册、调整店铺运行数量与内存限制，并执行版本更新。

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

## 首次使用

1. **创建管理员账号**：首次打开工作台时，按页面提示填写用户名和密码，完成初始化。
2. **接入店铺**：在「店铺管理」添加店铺，用闲鱼 App 扫码连接，然后选择要配置的店铺。
3. **连接模型**：在「系统设置 → 模型连接」填写模型信息并测试连接。只使用关键词回复时，可以直接配置规则。
4. **准备客服内容**：设置客服风格和营业时间，补充常见问答与商品知识，用沙盘试答后保存并开启客服。
5. **设置关键词规则**：将价格、使用方法、资料内容等高频问题整理成关键词与回复话术，分别配置到商品或全店规则中。
6. **配置自动发货**：导入卡密或添加网盘、文字资料，为商品绑定对应模板，并启用自动发货。
7. **日常查看**：通过概览页了解运行情况，在客服工作台处理需要人工接管的对话，在订单与库存页面查看发货记录和剩余卡密。

如果需要多人分别管理自己的店铺，可以在部署配置和系统设置中开启用户注册，具体说明见[账号与权限](docs/ACCESS_MODEL.md)。

## 网页更新

管理员点击顶部版本号检查更新。有新版本时：

1. 点击「更新」，等待下载和校验；窗口会显示下载大小与进度。
2. 下载期间可以关闭窗口，再次打开后继续查看。
3. 校验完成后输入管理员密码确认，程序切换版本并重启。

![网页更新与下载进度](docs/assets/readme/update.png)

普通代码更新无需重建 Docker 镜像。新版本启动失败时会尝试恢复上一版本；文件更新保留现有业务数据和配置，不自动备份或还原数据库。

旧版安装的迁移、依赖或数据格式变化时的升级方式，见[部署与升级说明](docs/DEPLOYMENT.md)。

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

## 开发与文档

项目使用 FastAPI 提供后端服务，前端由 HTML、CSS 和原生 JavaScript 构成。每个店铺通过独立的 Python Worker 连接闲鱼，处理消息、回复规则与发货任务。

在 Linux 或 WSL2 中搭建开发环境：

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas
./scripts/bootstrap-dev.sh
npm run dev
```

```text
frontend/   工作台页面、样式与交互
backend/    API、账号管理、AI 客服与任务调度
worker/     闲鱼连接、消息处理与发货
config/     环境配置模板
deploy/     安装脚本与服务配置
scripts/    开发、构建与发布工具
tests/      自动化测试
docs/       部署、使用与架构文档
```

| 需要了解的内容 | 文档 |
| --- | --- |
| 安装、远程访问、配置、备份与旧版迁移 | [部署指南](docs/DEPLOYMENT.md) |
| 用户角色、店铺归属与权限 | [权限说明](docs/ACCESS_MODEL.md) |
| AI 客服、规则与沙盘的工作方式 | [客服说明](docs/AI-CUSTOMER-SERVICE-REQUIREMENTS.md) |
| 本地开发与贡献代码 | [贡献指南](CONTRIBUTING.md) · [Ubuntu 开发环境](docs/NEW_UBUNTU_HANDOFF.md) |
| 进程、数据流与 Worker | [系统架构](docs/ARCHITECTURE.md) · [Worker 说明](worker/README.md) |
| 版本变化与发布维护 | [更新记录](CHANGELOG.md) · [发布指南](docs/RELEASING.md) |
| 漏洞报告与社区协作 | [安全政策](SECURITY.md) · [行为准则](CODE_OF_CONDUCT.md) |

## 许可证

项目采用 [GPL-3.0-only](LICENSE) 许可。上游代码与第三方组件说明见 [LICENSING.md](LICENSING.md) 和 [worker/NOTICE.md](worker/NOTICE.md)。

# xianyu-saas

> 闲鱼多店铺一站式自动化运营工作台：多账号进程隔离与商品同步，规则+AI双引擎客服（多风格人设/话术知识库/沙盘调优），官方双接口验单秒发虚拟卡密与网盘资源，配备多店统一工作台与可视化运营看板。

[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

---

## 核心特性

### 1. 规则 + AI 智能客服（多风格人设与知识库）
- **多风格客服人设，自由定制**：
  - 🎭 **开箱预设风格**：内置元气软萌、热情亲切、严谨专业等多种预设人格，适应不同商品品类与买家交流风格；
  - 🛠️ **深度自由定制**：支持自由配置客服名称、买家称呼（“老板 / 亲 / 朋友”）、回复语气（活泼 / 克制 / 专业）、回复长短及表情频率，并可一键保存至模板库；
  - 💡 **AI 智能提炼知识**：直接粘贴零散的商品介绍文案，点击“AI 帮我整理”，系统自动梳理为结构化的商品客服知识。
- **双层规则匹配，精准优先**：
  - **商品专属规则 > 全店通用规则**：买家从特定宝贝进店咨询时，优先匹配该商品的专属问答；未命中才退回全店通用规则；
  - **三大响应策略（`AUTOMATION_STRATEGY`）**：
    - `standard`（标准）：命中首个关键词规则即刻秒回；
    - `conservative`（保守）：买家发来超过 240 字长文自动判定为复杂问题，跳过规则直接交由 AI 或人工，避免断章取义；
    - `aggressive`（激进）：同时命中多条规则时，优先选用关键词最长（匹配度最高）的精准话术。
- **拟人仿真与防封机制**：
  - 随机回复延迟（0~60 秒），模拟真人打字节奏；
  - 消息触发防刷冷却，防止短时间内被恶意买家连续刷屏触发平台风控；
  - 支持营业时间限制（如 09:00 - 23:30），非工作时间自动静默休息。
- **多模型接入与代码级安全门禁**：
  - 原生支持 **OpenAI 兼容接口、Claude、Google Gemini、本地 Ollama**；
  - 自动感知商品的**实时价格、库存、规格 SKU 与上下架状态**；
  - **安全过滤硬闸门**：代码层内置敏感词拦截（严禁微信/QQ/电话等站外引流词，防封店）、防虚假发货承诺（严禁 AI 擅自承诺“已发货/已退款”），以及 90% 相似度防复读熔断；
  - **内置连续对话沙盘**：在后台直接模拟买家多轮问答，实时查看引用的知识与回复决策后再上线。

### 2. 虚拟商品自动秒发货（官方双接口防骗验单）
- **三大自动发货类型**：
  - **兑换码 / 卡密池**：支持单笔拍下 1~50 件，按购买件数自动从库存池提取对应数量的卡密发放，事务加锁防超卖、防重发；
  - **网盘资源**：买家付款后，自动私信下发百度网盘、阿里网盘等分享链接与提取码；
  - **固定资料**：自动发送固定的安装教程、激活指南或下载说明。
- **官方双接口交叉验单（防假截图与未付款诈骗）**：
  - 绝不凭买家一句话就发货！系统监听到付款事件后，调用平台官方双接口核验：严格比对买家 ID、卖家身份、商品 ID 与订单真实状态（必须为待发货 `status == 2`）；
  - 一旦出现库存不足或验单异常，立即拦截并自动转入后台人工审核待办。

### 3. 多店铺多账号物理隔离
- **独立进程与数据沙盒**：每个闲鱼号拥有独立的运行态、凭据加密、本地数据库与专属 Worker 进程，单店掉线或异常绝不牵连其他店铺；
- **官方扫码直连**：后台直接生成闲鱼官方授权二维码，手机闲鱼扫码即可快速绑定。

### 4. 客服工作台与人工接管
- **接管防抢话**：人工在后台发消息或输入接管指令后，系统进入**人工接管冷却倒计时**，期间机器人彻底静默，避免机器人与客服抢着插嘴；
- **图片发送队列**：支持一次性粘贴或拖入最多 8 张图片；图片按顺序单张发送并确认协议 ACK，文字最后发送；发送中断时只重试失败的分段。

---

## 界面预览

| 运营概览 | 店铺管理 |
|:---:|:---:|
| ![运营概览](docs/assets/readme/overview.png) | ![店铺管理](docs/assets/readme/shops.png) |

| 客服会话工作台 | AI 设置与沙盘测试 |
|:---:|:---:|
| ![客服会话](docs/assets/readme/customer-service.png) | ![AI设置](docs/assets/readme/ai-config.png) |

| 卡密库存池 | 订单与自动发货状态 |
|:---:|:---:|
| ![卡密库存池](docs/assets/readme/cards.png) | ![订单列表](docs/assets/readme/orders.png) |

---

## 快速上手

### 方式一：Docker 一键部署（推荐）

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

# 复制配置文件
cp config/saas.env.docker.example config/saas.env

# 启动容器
docker compose up -d --build
```

- **管理后台**：`http://localhost:4173/xianyu-saas/`
- **数据持久化**：数据库与各店铺配置文件默认保存在本地 `./data` 目录。

查看日志或停止：
```bash
docker compose logs -f
docker compose down
```

### 方式二：Linux 本地源码开发

环境要求：Linux、Python 3.10+、Node.js 20+、npm 10+。

```bash
git clone https://github.com/tswawa/xianyu-saas.git
cd xianyu-saas

./scripts/bootstrap-dev.sh
npx playwright install --with-deps chromium
npm run dev
```

### 首次登录：创建管理员账号

为了防止公网被他人随意扫描并抢先注册，系统默认**关闭了公开网页注册**。服务首次启动后，请在服务器终端执行对应命令创建首位管理员（Admin）账号：

- **Docker 部署环境**：
  ```bash
  docker compose exec xianyu-saas backend/.venv/bin/python -c "from db import DB; db = DB('/data/saas.db'); db.create_user('admin', 'Admin12345678!', role='admin'); print('管理员创建成功！')"
  ```
- **Linux 源码环境**：
  ```bash
  PYTHONPATH=backend python3 -c "from db import DB; db = DB('data/saas.db'); db.create_user('admin', 'Admin12345678!', role='admin'); print('管理员创建成功！')"
  ```

> 💡 **提示**：默认账号为 `admin`，密码为 `Admin12345678!`（系统安全策略要求密码长度**不少于 12 位**）。创建完成后即可在登录页输入登录，登录后可在后台随时修改密码。

---

## 4 步日常使用流程

1. **登录与绑定店铺**：首次启动先通过上述命令创建管理员账号登录后台；进入「店铺管理」，添加店铺并通过闲鱼 App 扫码登录；
2. **配置智能客服**：进入「智能客服中心」，选择合适的人格风格或自定义客服，填入你的大模型 API Key，在沙盘模拟测试效果；
3. **设置高频规则**：添加商品专属或全店通用的问答规则（优先走规则秒回，省 Token 且零延迟）；
4. **绑定自动发货**：在「自动化发货」中导入卡密池或填入网盘链接，绑定对应商品，买家付款后全自动秒发。

---

## 核心环境变量说明

编辑 `config/saas.env` 可定制系统行为：

| 变量名 | 说明 | 建议值/默认值 |
| --- | --- | --- |
| `SAAS_PUBLIC_ORIGIN` | 工作台对外访问域名或 IP | `http://127.0.0.1:4173` |
| `SAAS_COOKIE_SECURE` | Cookie 是否强制 HTTPS | 本地设 `0`，线上生产环境设 `1` |
| `SAAS_AI_MASTER_KEY` | 加密模型 API Key 的主密钥 | 生产环境务必填写强随机字符串 |
| `SAAS_MAX_BOTS` | 允许同时运行的最大店铺 Worker 数 | 根据服务器性能调整（默认 10） |
| `SAAS_ALLOW_REGISTRATION` | 是否允许公开注册账号 | 私有自用建议设为 `0` |

完整模板见 [`config/saas.env.example`](config/saas.env.example) 和 [`config/saas.env.docker.example`](config/saas.env.docker.example)。

---

## 目录结构

```text
frontend/             前端静态工作台（HTML/CSS/JS 单页）
backend/              FastAPI 控制面、AI 客服引擎与任务调度
worker/               闲鱼消息接入、多店铺独立 Worker 进程与自动发货
config/               环境变量模板
deploy/               Nginx、systemd 与更新器模板
docker/               Docker 镜像入口与容器化脚本
scripts/              开发初始化与本地启动脚本
tests/                自动化回归测试与合规检查
docs/                 架构设计与部署指南
```

---

## 相关文档

| 文档 | 说明 |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 系统组件边界、数据流与架构设计 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 生产环境部署（Docker / systemd） |
| [`docs/NEW_UBUNTU_HANDOFF.md`](docs/NEW_UBUNTU_HANDOFF.md) | Ubuntu 开发环境完整配置指南 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 参与贡献与本地代码门禁 |
| [`SECURITY.md`](SECURITY.md) | 安全机制与漏洞报告 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本更新日志 |

---

## 免责声明

本项目仅供技术研究与学习交流，与阿里巴巴集团、淘宝或闲鱼官方无关。请遵守相关平台使用条款，在法律与平台规则允许的范围内合理使用。

## 许可证

本项目基于 [GPL-3.0-only](LICENSE) 发布。`worker/` 的上游来源与说明见 [`worker/NOTICE.md`](worker/NOTICE.md)。

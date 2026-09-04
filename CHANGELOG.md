# 更新记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按 [Semantic Versioning](https://semver.org/lang/zh-CN/) 管理。

## [未发布]

### 新增

- 整站容器化：根目录 `Dockerfile`、`docker-compose.yml`、`docker/entrypoint.sh` 与 `config/saas.env.docker.example`，Windows 与 macOS 可通过容器运行。
- 静态页面服务器支持 `SAAS_DEV_WEB_HOST` 配置监听地址，默认仍为 `127.0.0.1`。

### 变更

- README 改为面向使用者的结构：功能特性表格、界面截图、容器与源码两种安装方式。
- `docs/ARCHITECTURE.md` 补充平台依赖与容器组件说明，纠正 Docker 模板位置。
- `docs/DEPLOYMENT.md` 增加容器部署章节，并明确其与 systemd 版本化发布互斥。
- `docs/NEW_UBUNTU_HANDOFF.md` 增加容器开发路径，改用 `npm run dev` 单命令启动。

### 移除

- 删除浏览器连接助手扩展 `connector-extension/`、预构建扩展 ZIP 及其合同测试：店铺连接已统一走服务端官方扫码，前端不再发起扩展握手。
- 删除后端 `/api/bot/connector/handoff` 与 `/api/bot/connector/cookies` 端点、`connector_handoff` 模块，以及来源校验对连接端点的豁免，缩减可接收登录态的入口。
- 删除 nginx 中对应的 connector location 与限流 zone。
- 删除 `worker/tutorial_text.py` 及其测试：该文件是维护者个人转售业务的成品营销文案（含个人域名与自定折扣定价），此前随 worker 镜像分发，自建部署后会出现在发给买家的文本里；其测试还把具体价格断言成了合同。兑换码履约文本改为「配置话术优先 + 通用回落」，与网盘、资料两类发货行为一致。
- 删除试用码链路：`send_trial_code` 无生产调用点，却依赖 `delivery_store` 的 7 个方法、`trial_claims` 表、启动恢复扫描和一条库存类型；它服务的是特定业务规则，不属于通用客服能力。库存类型收窄为仅兑换码。既有数据库的 `trial_claims` 表保持原样不删除，兑换码发货与库存隔离不受影响。

### 修复

- `蒸馏/` 目录下的本地开发图片虽已在忽略规则中声明，但因先于规则入库仍被跟踪，现已脱离版本控制。

容器镜像构建与启动尚未纳入自动化门禁，首次部署需自行验证。当前仓库仍以内部构建方式维护，未承诺公共 Release 节奏。

## [0.1.0] - 2026-08-28

这是当前自用工作台的内部构建基线，不代表真实平台或生产环境已完成验收。

### 产品范围

- 完成六个业务域：运营概览、智能客服、履约中心、订单管理、店铺管理和项目说明。
- 以店铺账号为隔离边界，贯穿前端请求、控制面、商品、会话、任务、配置和 Worker 运行态。
- 保留轻量静态前端，把权限、连接、任务状态、恢复和履约证明放在服务端负责。

### 新增

- 增加服务端官方二维码店铺连接、账号切换、状态恢复和异步店铺同步任务。
- 增加 SQLite jobs/leases、Worker 期望状态、消费者重试/死信和重启恢复合同。
- 增加统一收件箱、会话搜索、未读状态、人工接管和账号级人工回复 outbox。
- 增加人工回复发送期租约、稳定消息标识、有限重试和协议 ACK 记录。
- 增加自然语言内容驱动的 AI 客服模型，统一沙盘和真实自动回复链路。
- 增加五种 provider 适配格式：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、Google Gemini 和 Ollama Chat。
- 增加 AI 草稿来源、精确配置版本、规则/设置指纹和发送前再次复核。
- 增加商品客服内容、兑换码/网盘资源配置和经过订单证明的受控履约流程。
- 增加 nginx、systemd、日志轮转和开发部署文档模板。
- 增加 GitHub CI、Dependabot、CODEOWNERS、Issue/PR 模板、贡献指南和许可证边界文档。

### 安全

- 浏览器不接收或保存闲鱼 Cookie、平台 Token；模型 Key 仅在用户主动测试或保存连接时短暂提交，已保存值不回显，应用不写入浏览器存储。
- 控制文件缺失或损坏时 fail-closed；只有显式注册或新建店铺初始化可以播种默认文件。
- AI 只能生成客服文本或人工接管决策，不能授权发货、改订单或读取其他店铺资料。
- 自动履约必须核验订单号、商品、买家、卖家、状态和数量，不能由买家文字、商品标题或模型输出授权。
- AI provider 连接采用受控解析、固定已验证地址、Host/SNI 校验、禁止重定向和响应大小限制。
- 生产注册、浏览器日志输出和测试模式采用保守默认值；敏感运行态不进入 Git。

### 验证范围

- 离线合同覆盖仓库脱敏、API、账号隔离、AI provider、异步任务、Worker 恢复、人工回复、部署配置和桌面/移动 UI。
- 当前完整门禁使用 `npm test`，并辅以 `python3 tests/repository-contract.py` 和 `git diff --check`。
- 本地开发实例已验证 API 存活/就绪、未登录鉴权响应、静态资源一致性以及桌面/390px 布局无横向溢出。

### 未包含的生产承诺

以下事项需要维护者在独立受控窗口中完成，不能由离线合同替代：

- 真实闲鱼账号扫码、验证码、安全认证、风控恢复和实时消息；
- 真实第三方模型连接、模型质量和供应商策略验证；
- 真实订单、库存、发货和平台 ACK 的业务验收；
- nginx/systemd 生产主机加载、域名、TLS、密钥和运行态权限配置。

当前仓库为私有内部构建，未发布 Git 标签或 GitHub Release，因此本文件不提供版本间的比较链接。

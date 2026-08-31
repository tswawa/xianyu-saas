# DeepWhale 闲鱼客服项目长期记忆

更新日期：`2026-08-31`。本文件是项目稳定事实的主索引，只记录可安全进入私有源码仓库的内容，不包含凭据、生产数据或完整操作过程。

- 项目指令：[`AGENTS.md`](AGENTS.md)
- 详细维护流程：[`MEMORY_OPERATIONS.md`](MEMORY_OPERATIONS.md)
- 通用维护流程：`~/.codex/memory/MEMORY_OPERATIONS.md`
- 详细搭建：[`docs/NEW_UBUNTU_HANDOFF.md`](../docs/NEW_UBUNTU_HANDOFF.md)
- 生产边界：[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)

状态标记使用 `verified`、`reported`、`unverified`、`superseded`、`expired`。优先级为 `P0 > P1 > P2 > P3 > P4`；当前状态发生变化时更新日期并移除旧的“当前”结论。

## P0：安全与真实性

- [verified | review: 2026-08-16] 仓库是 DeepWhale 闲鱼客服单仓库：控制台/API 在根目录，自动客服在 `worker/`。
- [verified | review: 2026-08-16] 真实 Cookie、Token、Key、验证码、库存、网盘资料、买家/订单正文、数据库和日志必须留在 Git 之外。
- [verified | review: 2026-08-16] 普通跨域跳转无法让 `deepwhale.chat` 读取 `goofish.com` 的 HttpOnly Cookie；主登录方案是服务端生成官方二维码并在服务端保留短期登录态。
- [verified | review: 2026-08-16] 真实订单不可盲测；自动履约必须同时核验订单号、商品、买家、卖家、状态和数量。
- [verified | review: 2026-08-16] 开发完成后 push 精确 commit；生产端拉取、测试、建立 Git 回滚点后部署，开发机不直接写生产目录。
- [verified | implementation + contracts | review: 2026-08-27 | expires: next provider transport change] 真实 AI provider 出站请求只允许一次受控 DNS 解析；连接必须复用已验证地址并保留原始主机名的 Host、SNI 与 TLS 证书校验，禁止重定向并限制响应体，避免连接阶段二次解析导致 DNS 重绑定。
- [verified | implementation + full offline suite | review: 2026-08-28 | expires: next tenant initialization change] 账号级 `reply_rules.json` 与 `automation_settings.json` 在普通访问及 Worker 启动时缺失或损坏均 fail-closed，不得静默重建；状态查询安全降级为自动化关闭和 `automation_available=false`。补齐有效私有文件后 Worker 可按文件签名热恢复。仅注册或新建店铺的显式初始化可播种，初始化失败必须删除已创建的账号记录和私有目录，不能留下幽灵账号；完整 `npm test` 中 Worker 225 项通过。
- [verified | implementation + regression contracts | review: 2026-08-28 | expires: next automatic-reply send path change] AI 草稿必须持久化来源和精确 `config_revision`，每次实际发送前复核自动化状态、AI enabled 与该版本；固定规则草稿绑定规则文件和自动化设置指纹。生成后、延迟后、重试或重启恢复时任一版本变化都取消旧正文，缺少 provenance/revision 的旧 AI 草稿也不得发送。
- [verified | implementation + full offline suite | review: 2026-08-28 | expires: next production deployment contract change] 生产默认关闭公开注册和浏览器日志输出；浏览器写请求必须通过来源校验。API 同时提供存活与数据库就绪探针，systemd 只信任本机代理转发头；Worker 默认使用无 traceback 诊断的 `INFO` 日志，账号日志具备有界轮转。控制面 token、完成任务和死信任务使用 TTL、索引及分批清理，避免无界增长。
- [verified | implementation + targeted auth/admin contracts | review: 2026-08-31 | expires: next authentication/bootstrap contract change] 空 `users` 表不会自动开放公开注册；首位管理员只可经显式启用、受信任来源和 `X-Bootstrap-Token` 一次性 credential 创建，数据库只保存摘要，并在同一事务中完成首位 `admin` 与消费状态，重放、并发和重启均不能重新开放。公开注册固定为部署上限、数据库开关和已有用户三条件 AND；旧实例无管理员时幂等提升最早用户，最后一个启用管理员不可降级或停用。
- [verified | implementation + targeted security/admin contracts | review: 2026-08-31 | expires: next password/session/audit contract change] 密码采用版本化 PBKDF2 并在旧格式登录成功后惰性升级，未知用户执行等成本假哈希；登录失败锁定、审计、管理员确认和更新状态均持久化且有界清理，改密/停用/角色变化会撤销会话。平台管理员权限不授予读取其他用户 Cookie、会话正文、订单、库存或买家数据的能力。
- [verified | user decision | review: 2026-08-17] 每次错误必须记录表现、根因、修正、预防规则和验证方式；长期可复用规则进入项目 memory，一次性过程只进 daily log，且不得包含凭据或用户数据。
- [correction | verified | review: 2026-08-29] GCM 的账户列表不是浏览器授权证据；只有对目标私有仓库执行不泄露凭据的认证只读操作成功，才能确认当前凭据可用，并且不得把它表述为刚刚完成了浏览器授权。

## P1：架构边界

- [verified | review: 2026-08-16] `frontend/` 只负责静态 UI，生产由 nginx 提供，API 使用同源 `/xianyu-saas/api/`。
- [verified | review: 2026-08-16] `backend/` 是 FastAPI 控制面，生产只监听 `127.0.0.1:8096`；浏览器会话使用 Secure/HttpOnly/SameSite Cookie。
- [verified | review: 2026-08-22] `worker/` 处理闲鱼消息与履约；`rules` 不创建 AI 客户端，`rules_ai` 只拿生命周期内的内部 AI 凭据。两种模式不再按会员或旧 `expires_at` 字段切换。
- [verified | implementation + contracts | review: 2026-08-28] `/internal/v1/ai/reply`、`/internal/v1/ai/ready` 与兼容 `/internal/v1/chat/completions` 只允许本机 worker，nginx 不公开 `/internal/`；请求还必须通过账号作用域短期令牌与 `X-Shop-Account` 绑定校验。
- [verified | implementation + targeted update/deploy contracts | review: 2026-08-31 | expires: next release-updater contract change] 版本更新采用“固定 GitHub Release 源 + Ed25519 签名 manifest + SHA-256 + 独立 staging/API 意图 + root updater”分层：API 不执行 `systemctl`、不修改 `current`、不使用 `git pull`；updater 取得文件锁并二次验签，使用 SQLite backup、临时 symlink 与 `os.replace` 原子切换，健康失败自动回滚，运行数据始终位于 release 目录之外，保留当前在内最近 3 个版本。
- [verified | review: 2026-08-16] 店铺商品由服务端验证的闲鱼登录态同步，前端不要求用户填写专业商品配置。

## P2：产品决策

- [verified | user decision | review: 2026-08-22] 第一阶段采用自用形态：前端不显示会员、套餐、支付、续费、升级或免费/会员身份；登录账号可使用现有全部经营功能与 AI，旧订阅字段仅保留兼容。
- [verified | user decision + implementation | review: 2026-08-31 | expires: next account/update product decision] 平台角色只使用 `admin` 与 `owner`，允许多个管理员并保护最后管理员；注册开关使用部署上限与数据库开关 AND 规则。版本、更新、账号权限与安全记录复用“项目说明”子标签，不新增第七个业务域；所有登录用户可看版本，只有管理员可管理账号、审计和更新。
- [verified | source: user decision | review: 2026-08-16] 产品复杂度优先放在后端逻辑（账号隔离、任务调度、幂等、履约和恢复），前端保持一体化、少入口、低学习成本；未经确认不因竞品宣传增加大量前端开关。
- [verified | user decision | review: 2026-08-17] 处理本项目任何新事情前，必须先查看全局 memory、项目 memory 和当天记录；用户明确要求与纠偏结果要回写 memory，后续交付以最新记忆和用户最新决定为准。
- [verified | user decision + implementation + full offline suite | review: 2026-08-28 | expires: next AI content/provider contract change] AI 客服产品模型固定为“店主自然语言内容 + 当前店铺信息 + 实时商品事实 + 当前会话上下文”；JSON 只作账号私有存储与内部协议，普通用户不编辑 JSON、提示词、结构化知识库或“草稿/发布”状态。沙盘与真实自动回复共用控制面统一引擎，worker 通过 `/internal/v1/ai/reply` 生成受限决策，并在每次实际发送前用 `/internal/v1/ai/ready` 复核精确 `config_revision`。上游继续支持 `openai_chat_completions`、`openai_responses`、`anthropic_messages`、`google_gemini`、`ollama_chat` 五种固定格式；长期密钥仅在控制面加密保存，固定规则仍优先，AI 不授权履约。完整 `npm test` 通过，Worker 为 225 项，静态资源为 `20260828-01`；未连接真实第三方模型或闲鱼，未进行生产发布。
- [verified | implementation + full offline suite | review: 2026-08-28 | expires: next public AI content contract change] 公开 AI 配置和商品内容接口只返回当前内容驱动模型需要的字段，不暴露旧 `draft`、`published`、`history` 或发布修订字段；商品整理只返回自然语言预览并明确 `saved: false`，生成动作不保存、不发布。旧 `/publish` 仅作兼容，新前端不调用。
- [superseded | user decision | review: 2026-08-22] 旧决策“会员只解锁 AI 类能力”已被“第一阶段自用、全部现有功能不按订阅门控”替代。
- [verified | user decision | review: 2026-08-18] UI 以用户提供的 Gemini 演示 `gemini-code-1787037585951.html` 为最终视觉/信息架构规格，按阶段把现有页面完全对齐，并为演示中每个可交互控件补充真实逻辑；不能用假数据或仅 alert 占位。
- [superseded | user decision | review: 2026-08-22] 旧的套餐、支付链接和续费方案已取消；不得在自用前端重新加入会员、套餐、支付、升级或续费入口。
- [superseded | user decision | review: 2026-08-18] 旧决策“免费账号只展示日/周卡、会员才展示全部套餐卡”已被上一条替代。
- [superseded | user decision | review: 2026-08-18] 旧决策“模板管理与兑换码激活 UI 已取消”已被“按 Gemini 演示恢复模板/卡密等页面并接入真实逻辑”替代，落地阶段以最新实现为准。
- [verified | source: implementation + contracts | review: 2026-08-17] 人工回复要求当前会话先人工接管；新回复进入账号私有持久 outbox，由当前账号 worker 以发送期租约、稳定 UUID 和最多 10 次重试发送。该链路是至少一次投递加稳定 UUID，不承诺平台端 exactly-once；界面只把协议 ACK 表达为“闲鱼已接收”，不得写成买家已读或最终送达，旧草稿不会自动发送。

## P3：当前开发状态

- [verified | implementation + full offline suite + local refresh | review: 2026-08-31 | expires: next auth/admin/update release change] 注册与管理员安全、版本 `0.1.0` 展示、签名 Release staging、独立 updater、项目说明内的版本/账号/安全 UI 及部署模板已落地，静态资源版本为 `20260831-01`。完整 `npm test` 退出码为 0，覆盖认证、更新、API、控制面、账号隔离、部署、225 项 Worker 测试和桌面/390px UI；`git diff --check`、资源版本调整后的 repository/syntax/API/UI 合同均通过。本机 API、consumer、Web 已从当前工作区刷新，`/health`、`/api/ready`、工作台及 JS/CSS 返回 200，未登录 `/api/me` 返回 401，公开注册和 bootstrap 默认关闭；JS/CSS 引用版本与工作区一致。真实 GitHub 签名制品、公钥/只读凭据、systemd/nginx updater、真实账号/订单/履约和生产发布均未验收；当前改动未提交、未推送、未生产发布。
- [verified | user request + Opus 5.0 implementation + full offline suite + visual screenshots + local refresh | review: 2026-08-30 | expires: next startup/docs UI change] 启动欢迎幕、极光聚光灯和入口按钮已从首屏移除；“项目说明”经 Opus 5.0 二次优化为一张连续说明书，正文仅含“项目概览”“日常操作与异常处理”“技术与安全边界”三个小节，六大业务域嵌入概览表格，作者 GitHub 与项目仓库链接固定在文末页脚。UI 合同已覆盖启动幕负向断言、链接安全属性、桌面/390px 无横向溢出、移动端表格标题横向排版和截图；完整 `npm test` 与 `npm run test:ui` 通过（Worker 225 项与 UI 合同），桌面/390px 截图已复核。本机 API、consumer、Web 已从当前工作区刷新，`/health`、`/api/ready` 返回 200，未登录 `/api/me` 返回 401，工作台引用并加载资源版本 `20260830-02`，首屏无启动幕。真实闲鱼、真实模型、真实订单和 nginx/systemd 生产发布仍未验收，未提交或推送 Git。
- [verified | implementation + full offline suite + local refresh | review: 2026-08-28 | expires: next provider/rules/public-AI/worker-send/production-hardening change] 商品 AI 内容驱动、账号配置 fail-closed 与发送前版本复核已完成；provider 连接固定一次受控 DNS 解析。生产安全收口新增默认关闭注册和浏览器日志、浏览器写来源校验、数据库就绪探针、控制面 token/终态任务有界保留、可信代理限制、Worker 安全日志默认值及账号日志轮转。完整 `npm test` 通过（Worker 225 项，UI 覆盖桌面/390px），`git diff --check` 与仓库合同通过；本机 API、consumer、Web 已从当前工作区刷新，API 存活/数据库就绪、工作台和 HTML 引用的 JS/CSS/字体/SVG 均返回 200 且内容与工作区一致，资源版本为 `20260828-01`。仅离线/模拟与本机未登录验收，未连接真实 provider 或闲鱼，未执行 nginx/systemd 生产发布，未提交 Git。
- [verified | remote sync + branch cleanup + read-only verification | review: 2026-08-29 | expires: next remote-history or release-policy change] 私有远端 `github.com/tswawa/xianyu-saas` 的 `main` 先由初始提交 `d2982a5` 普通非强制 fast-forward 到完整快照 `eb23196`，后以直接子提交 `802247c` 删除 `.github/dependabot.yml`；远端历史未改写。12 个自动生成的 Dependabot 分支已删除，GitHub API 终检远端仅剩 `main`；版本更新分支已通过删除配置停用，自动安全修复原本未启用。因本地克隆是 shallow + partial 且浅边界 `8857f06` 的真实父提交 `c29f6dd` 缺失，直接推送本地提交图会被 GitHub index-pack 拒绝；成功方案是在隔离的完整仓库中 fetch 远端基线、导入当前 HEAD 树快照并以基线为父创建单一提交，再普通推送。本地工作仓库历史未被改写，HEAD 仍为 `63b9220`，未修改 git config。发布核验：远端快照与源 HEAD 的 144 个允许跟踪路径 mode/blob 哈希一致，独立重克隆后逐字节无差异且无多余文件；运行态秘密均不在远端，秘密模式扫描 0 命中。被 `.gitignore` 忽略的 `蒸馏/` 第三方截图未进入新远端快照，但仍存在于本地旧历史 `350c3c8`，切 public 前必须净化全历史。
- [verified | repository governance + full offline suite + themed commits + local refresh | review: 2026-08-29 | expires: next release-policy or dependency-layout change] 私有仓库入口已补齐 GPL-3.0-only 许可证边界、贡献指南、行为准则、Issue/PR 模板、CODEOWNERS、EditorConfig、Git 属性、架构/公开发布清单与开发依赖分层；CI 固定在 main push/PR 运行并启用并发取消，运行依赖与测试依赖分离。`npm test` 通过（225 项 Worker 与 UI 合同），`python3 tests/repository-contract.py` 和 `git diff --check` 通过。当前改动已分为 `12d808d`（实现/测试）与 `f1fba43`（治理/发布基础设施）两条新提交，并建立已验证的本地 Git bundle；本机开发栈已从 `f1fba43` 重启，API 存活/就绪为 200、未登录 `/api/me` 为预期 401、HTML 引用的静态资源与工作区一致，桌面/390px 无横向溢出。Dependabot 配置曾在本地旧 HEAD 中建立，后按用户“远端只保留 main”决定从远端删除；后续快照发布不得重新带回。未安装 GitHub CLI，未执行 nginx/systemd 生产发布。远端当前状态见上一条 `802247c` 记录。
- [verified | README + remote sync + full offline suite + local health | review: 2026-08-29 | expires: next product/navigation or release-policy change] README 已按真实产品结构重写为四个日常主域（运营概览、智能客服、订单管理、履约中心）和两个侧栏辅助入口（店铺管理、项目说明），加入两张脱敏测试截图；`npm test` 通过（Worker 225 项与 UI 合同），README 相对链接、图片属性、`python3 tests/repository-contract.py`、`git diff --check` 和本机 Web/API 健康检查通过。通过 GCM 登录后，GitHub 私有远端 `main` 已以非强制方式从 `802247c` 更新到直接子提交 `f3431c0`，提交范围严格为 README 与两张截图；写入流程校验分支指针、父提交、变更文件和本地 blob 哈希。重新完成 GCM 官方浏览器认证后，独立只读 API 进程再次确认远端 `main`、父提交、三文件清单和逐文件内容哈希全部一致。因 Git smart-HTTP 仍偶发 TLS 中断，本地 `origin/main` 跟踪引用暂时停在 `802247c`，后续判断远端当前状态应以已验证的 API `f3431c0` 为准，直到 fetch 成功刷新；真实闲鱼、模型、订单和 nginx/systemd 仍未验收。
- [verified | README Gemini revision + full offline suite + local health | review: 2026-08-30 | expires: next README/product-validation change] README 已写入用户提供的 Gemini 文案重构稿，保留两张脱敏界面图、技术架构、本地开发、测试、安全、文档索引、贡献、许可证与免责声明，并明确多店铺隔离属于应用层作用域。用户稿中关于真实扫码/实时消息、真实订单履约和 Nginx/systemd 生产部署的表述已据证据收敛为待受控或生产验收；店铺切换入口、沙盘统一引擎、商品同步时点和 AI 安全不回复边界已按当前实现校正。README 已检查不含“不是”“而是”“不等同于”“并非”等指定禁用表达，20 个相对链接无缺失，仓库合同和 `git diff --check` 通过；`npm test` 退出码为 0，Worker 225 项与 UI 合同通过。本机 Web、API 存活和数据库就绪为 200，未登录 `/api/me` 为 401，JS/CSS 资源版本均为 `20260828-01`；README 不属于运行时资源，未重启本机开发栈。当前修改未提交、未推送，远端 `f3431c0` 仍是上一版 README；真实闲鱼、模型、订单和 Nginx/systemd 生产部署仍未验收。

- [verified | user correction + implementation + full local quality gate + local refresh | review: 2026-08-24 | expires: next login/sync state-machine change] 扫码重连不再要求先暂停自动客服：候选登录验证成功后才短暂停止旧 worker，保留 `desired_running` 并自动恢复。真实本机排查发现店铺检测任务因 `sync_cooldown` 失败，而账号状态已是 `cookie_expired`；前端遗漏该真实错误码后错误兜底为“需要检测”。现已将登录失效明确显示为“已断开 · 登录失效”，冷却/网络/平台错误显示具体检测结果；consumer 遇到冷却会延后超过冷却窗口再试，不再数秒内耗尽 3 次重试。同步成功仍清理旧授权错误并恢复运行意图。完整 `npm test` 通过（含 216 项 Worker 测试和桌面/390px UI 合同），`git diff --check` 通过；本地 API、Web、Web→API 均返回 200，新 consumer 存活，页面资源 `20260824-01` 与工作区一致。真实账号当前登录失效状态来自本机元数据，但本次未替用户重新扫码；nginx/systemd 生产发布仍未执行。
- [verified | user decision + implementation + full local quality gate | review: 2026-08-23 | expires: next product model change] 第一阶段自用模式已在源码中落地：登录账号可使用现有经营与 AI 能力，旧订阅字段仅保留兼容，前端无会员/VIP/套餐/支付/续费/升级入口。正式前端已按 `xianyusaas迭代演示UI-1.html` 重构为运营概览、智能客服、履约中心、订单管理、店铺管理、项目说明六个业务域及对应子标签，继续使用真实账号作用域 API；消息与会话搜索覆盖完整历史，买家消息统计、快捷短语以及风险与待办处理状态保持账号隔离。本地资源版本为 `20260824-01`。完整 `npm test`、桌面/平板/390px 浏览器合同和截图人工复核均通过；本地提交为 `c8a4501`，尚未按 nginx/systemd 生产方式部署，生产 AI、真实账号消息与真实履约未验收。
- [verified | screenshot-driven correction + contracts + local refresh | review: 2026-08-26 | expires: next provider adapter change] OpenAI Responses 连接探测的输出预算已从 8 提高到 256，避免推理兼容模型因预算过低只返回未完成内容；响应归一化优先接受原生 Responses 结构，并兼容部分网关返回的 Chat Completions 形状成功体。专项 AI/API 合同、完整 `npm test`（含 216 项 Worker 和桌面/390px UI）及 `git diff --check` 通过，本机 API/Web/Web→API/consumer 已刷新健康；用户截图中的真实上游仍需使用原凭据重新点击“测试连接”确认，未记录或读取密钥。
- [verified | user correction + implementation + full local quality gate + local refresh | review: 2026-08-26 | expires: next AI credential-storage change] 上游模型连接成功与服务端长期凭据加密保存是两个独立阶段；此前旧本机 `config/saas.env` 缺少后来新增的 AI 主密钥时，会在上游成功后返回 `credential_store_unavailable`。本机 `dev-api.sh` 现仅在未显式配置主密钥且不存在既有加密连接时，生成并持久复用 Git 忽略的 `.local/ai-master-key`，要求当前用户所有且权限不宽于 `0600`；若已有密文则拒绝替换主密钥。生产仍必须显式提供固定主密钥。完整 `npm test`、开发密钥迁移合同、仓库合同与 `git diff --check` 通过，本机 API/Web/Web→API/consumer 已刷新健康；诊断和验证未读取本机 env、进程环境或任何密钥内容。
- [superseded | product-model replacement | review: 2026-08-28] 2026-08-26 的“结构化商品知识 + 草稿/发布”交互已被自然语言内容驱动模型替代；其有界上下文、整理预览不落盘和安全解析经验继续保留，但不能再作为当前产品状态。
- [superseded | user correction + local refresh | review: 2026-08-24] 不能把历史烟测或仍在运行的旧开发进程表述为“当前本地已部署”。收到纠正后已停止旧 API、consumer、Web 和两个账号 worker，并从提交 `c8a4501` 重新启动完整本地开发栈；API、Web、Web→API 均返回 200，页面确认加载 `20260823-04`，consumer 存活，两个账号 worker 已恢复。该状态是当前本机可访问成品，不等于 nginx/systemd 生产部署。
- [verified | local export | review: 2026-08-22 | expires: next frontend handoff] Gemini UI 交接 ZIP 位于 Git 忽略的 `.local/exports/xianyu-saas-frontend-gemini-2026-08-22.zip`，SHA-256 为 `8b1a1dad6ffcd2deee330eded9b569a7a950b46b884a0140e89696d6be6c658c`。白名单仅含前端 HTML/JS/CSS/SVG/字体/许可证和 `GEMINI_UI_BOUNDARIES.md`，已排除后端、测试、连接器 ZIP、配置、凭据与运行数据。
- [verified | rich-media + manual image chain contracts | review: 2026-08-22 | expires: next message schema change] 聊天入站文字/图片不再强制依赖 `reminderUrl` 或当前消息内的商品字段；缺少商品 ID 时可从已有会话上下文有界回退，人工接管仍入库并只跳过自动回复。图片和其他富媒体标准化为 `content_type`/`media_json`，图片占位不会被方括号系统消息过滤器误删。人工图片发送使用具体 MIME 类型和平台公开图片结构，ACK 聊天记录不保存本地临时路径；前端兼容数组与 JSON 字符串媒体，只渲染 HTTPS 图片并过滤内部路径、文件名和原始媒体 JSON。已通过 31 项相关 Worker 定向测试、人工回复与收件箱合同、Playwright 桌面/390px UI 合同、语法检查和 `git diff --check`；真实闲鱼协议 ACK、对端图片显示、真实图片上传和生产部署仍未验证。
- [verified | implementation + offline contracts | review: 2026-08-22 | expires: next platform-auth protocol change] 闲鱼授权链路已重构为 Session、MTOP Token、WebSocket 三层状态；`risk_control` 只表示需要安全验证并进入 `NEEDS_HUMAN` 熔断，不映射为永久账号受限，只有平台明确返回发布/违规限制时才显示“部分能力受限”。HTTP 登录、Token、MTOP 与 WebSocket 请求统一使用 Chrome 133 指纹；长期 `cookies.txt` 保持只读，仅 `_m_h5_tk`、`_m_h5_tk_enc` 可写入账号私有短期 Cookie 文件。Token 刷新具备单飞、调用预算和退避；瞬时失败不破坏已注册 WebSocket，风控后停止自动平台请求。授权状态使用扁平 v2，并兼容旧 v1 与早期嵌套 v2，状态文件不含平台凭据。
- [verified | full local quality gate | review: 2026-08-22 | expires: next release] 授权与韧性定向测试 40 项、五项关键行为专项验证、后端/API/UI 合同及完整 `npm test` 均通过；完整套件包含 216 项 Worker 测试和 Playwright 桌面/390px UI 合同，`git diff --check` 通过。验证只使用 mock、离线合同和本地测试，未连接真实闲鱼、未发送真实业务请求、未部署、未重启生产服务、未提交 Git。
- [verified | implementation + offline contracts; reported live acceptance | review: 2026-08-23 | expires: next QR login protocol change] 非默认店铺二维码链路已绑定创建会话时的 `account_key`：二维码 SVG 改为带 `X-Shop-Account` 的同源 fetch，校验 SVG 类型/大小后以 Blob URL 展示；状态轮询、完成、取消和页面退出清理均固定使用同一店铺键，不再因当前活动店铺变化而回退 `default`。扫码后错误已拆分为 `qr_query_failed`、`login_confirm_failed`、`mtop_context_failed`、`qr_cookie_incomplete` 四个安全阶段，响应和前端文案不包含 Cookie、Token 或上游原文。用户报告另一机器部署二维码修复后真实扫码已恢复。顶部和店铺栏“添加店铺”入口现统一切换到店铺页、滚动并聚焦真实添加表单，避免点击无反馈；本地资源版本为 `20260823-02`。二维码/API/语法/UI 和此前完整 `npm test` 通过，完整套件含 216 项 Worker 测试；`20260823-02` 仅通过语法、UI 与 `git diff --check`，尚未部署、未重启生产服务、未提交 Git。

- [verified | source: contracts and local health check | review: 2026-08-22 | expires: next architecture change] P0 控制面基础已落地：`shop_accounts` 默认账号兼容层、SQLite jobs/leases、worker 期望状态、同步持久租约、账号元数据接口和按账号过滤的 attention；API 重启恢复已验证 PID 身份校验、`rules` Worker 认领、`rules_ai` Worker 令牌替换和账号级恢复租约。
- [verified | source: account-isolation contract | review: 2026-08-16 | expires: next architecture change] 商品快照、Cookie、规则/履约配置、会话库、履约库和 worker 环境路径已按账号目录隔离；相同商品/订单/任务键在两个账号下互不覆盖，`X-Shop-Account` 由服务端校验并贯穿 API 读写。
- [verified | source: API/worker contracts | review: 2026-08-16 | expires: next architecture change] 托管 AI 内部令牌的账号作用域已贯穿 worker 请求头和 loopback 代理；错账号、缺少作用域或停用账号都会被拒绝，作用域不向上游模型转发。
- [verified | source: `npm test`, async consumer contracts and local process check | review: 2026-08-16 | expires: next architecture change] 独立 `backend/job_consumer.py` 已运行并白名单消费已保存 Cookie 的 `shop_sync` 刷新任务；任务载荷只保存指纹，API 的 `Prefer: respond-async` 路径提供账号范围内的入队/轮询摘要，SQLite 租约支持续期、重试和死信。扫码完成与手动 Cookie 替换仍保留同步路径；其他任务类型尚无消费者。
- [verified | source: UI contract and implementation | review: 2026-08-17 | expires: next product-plan revision] 店铺管理第一步已实现：左侧独立“店铺管理”导航进入店铺列表，可添加备注、切换店铺并按用户名保存当前内部标识；顶部只读展示当前店铺，不再承担多店铺切换。切换会重新加载商品、规则、会话、订单和 worker 状态，请求由服务端校验 `X-Shop-Account`。浏览器只保存标识，不保存 Cookie/Token。
- [verified | source: API/worker implementation and UI contract | review: 2026-08-17 | expires: next product-plan revision] 自动化策略第一步已实现：账号级保守/标准/积极预设和总开关，worker 读取同一账号目录设置；关闭总开关会停止该账号 worker。事件级策略版本和商品覆盖仍未完成；会话人工接管已有第一步控制接口。
- [verified | source: implementation + API/account-isolation/UI contracts | review: 2026-08-23 | expires: next attention model change] 运营概览的风险与待办看板聚合当前账号/店铺的真实连接状态、worker 状态、失败/重试后台任务、人工回复重试和人工审核队列；公开响应只包含稳定预警 ID、标题、说明、严重级别、数量、动作和处理状态等有界字段。处理记录绑定 `user_id + account_id + item_id` 并持久化，可再次点击恢复待处理；稳定 ID 按账号和来源生成，指纹纳入来源、计数及运行意图，内容或计数变化会清除旧处理状态并重新提醒。同类后台任务按 `job_kind` 区分。该看板是派生展示数据，不是履约事实源。
- [verified | source: implementation + UI contracts | review: 2026-08-28 | expires: next product-plan revision] 前端使用账号、商品和请求三层 generation/scope：账号切换、退出、会话或商品切换会使旧请求失效，所有迟到响应在写入前复核当前账号键、商品键和代次；整理预览、编辑器内容、busy 状态和清理动作也受同一边界保护，避免旧请求污染新作用域。该保护只解决浏览器状态一致性，不替代后端账号作用域校验。
- [verified | source: inbox/manual-reply/worker/UI contracts | review: 2026-08-17 | expires: next product-plan revision] 统一收件箱已实现会话搜索、未读游标/筛选、账号范围校验、已读命令、人工接管/恢复和持久人工回复 outbox；接管写入账号独立 worker `manual_modes`，回复正文只在账号私有聊天库，状态查询和中心 jobs 不返回正文。worker 发送期续租，过期 owner 不能回写，以稳定 UUID 和最多 10 次重试发送，并在平台协议 ACK 后原子写入聊天历史；ACK 落库前崩溃只会恢复同一 UUID。最近 2000 条确认记录保留完整 outbox，较旧记录压缩为最多 50000 条无正文 tombstone。买家已读回执、平台 UUID 去重、跨设备实时推送和完整事件级审计仍未完成或验证。
- [verified | source: contracts + local health check | review: 2026-08-22 | expires: next product-plan revision] 运营统计第一步已实现：登录账号可读取当前店铺 1/7/30 天的消息、自动回复、人工接管、履约成功/待处理和未读聚合；人工回复不计自动回复，回复 `retry/manual_review` 纳入当前账号 attention 和兼容 summary，已解决人工审核不再计入待处理，工作台只增加今日四项数字。接口不返回正文、库存、凭据或业务标识；回复率、平均处理耗时和完整事件审计仍未完成。
- [verified | source: product-batch contract + UI contract + `npm test` | review: 2026-08-17 | expires: next product-plan revision] 商品批量资料第一步已实现：当前账号已验证商品可预览并提交统一资料或暂停资料，preview token 绑定账号、商品快照、配置和请求；提交只改选中商品，保留未选资料及兑换码/网盘配置，不回显资料正文；同一 token 的无变更重放幂等成功，批量上限与商品页统一为 500。批量导入/导出、商品变更检测和定时同步仍未完成。
- [verified | user correction | review: 2026-08-17 | expires: next product-plan revision] 竞品蒸馏计划中的新功能和新界面必须单独标注实现状态；当前已实现 P0 后端基础、异步刷新、账号切换第一步、策略预设、统一收件箱与人工回复发送第一步、运营统计第一步和商品批量资料第一步。完整事件审计、买家已读回执、完整运营统计、批量导入/导出和真实生产验收仍未完成；不能用本地服务健康或离线合同通过来宣称这些产品功能已完成。
- [superseded | user decision | review: 2026-08-22] 旧的会员价值、套餐定价和支付状态方案已被第一阶段自用模式替代；旧数据库订阅字段保留兼容，但前后端不再提供套餐购买或续期入口。
- [reported | local configuration correction | review: 2026-08-16 | expires: controlled database migration] 本地 API 与 consumer 应通过开发脚本加载 `config/saas.env`，使用可写的 `.local/tenants`；当前为保留用户本地账号，暂时显式使用既有 `backend/saas.db`，待受控迁移完成后再统一切换数据库。直接裸启动会回退生产路径并导致扫码验证成功后保存失败。
- [verified | source: code review and implementation | review: 2026-08-16 | expires: next architecture change] `tenant_configs` 的旧用户级列仅作为 default 兼容镜像，新账号规则以账号文件为准。
- [reported | source: README/handoff | review: 2026-08-16 | expires: next release] 现网基线仍是前端资源 `20260816-10`。
- [verified | source: README + UI contract | review: 2026-08-18 | expires: next release] 仓库待部署版为 `20260817-20`：包含官方二维码连接、账号切换、策略预设、统一收件箱、人工回复发送第一步、运营统计、商品批量资料第一步、分层套餐展示、店铺管理移动端操作图标和商家显示屏；现网仍未据此验收或部署。
- [verified | source: `npm test`, `git diff --check`, local Node fetch + Playwright smoke | review: 2026-08-18 | expires: next release] `20260817-20` 已完成本地离线合同和无凭据页面冒烟；首页与 JS/CSS/字体资源返回 200，未登录 API 按预期返回 401，桌面 1440px 与移动 390px 无横向溢出。API、Web、consumer 当前仅是本机开发进程，不等同于生产部署。
- [reported | source: README/handoff | review: 2026-08-16 | expires: next release] 登录会话具备主动 TTL 清理、Session 关闭、流式响应上限、最小轮询间隔、全局上游并发闸门和两阶段消费；同步失败可复用已确认登录直接重试。
- [verified | user correction + implementation | review: 2026-08-17 | expires: next connection-flow change] 普通店铺管理页已移除浏览器连接组件、手动 Cookie 和“其他连接方式”入口；服务端官方二维码是唯一展示的连接流程。兼容扩展资产仍留在仓库边界内，不作为普通用户入口。
- [unverified | review: 2026-08-16 | expires: next release] 真实账号扫码、验证码/安全认证、真实商品同步和真实履约尚未在本开发版中执行，不能写成已验证。

## 2026-08-17 UI 产品化改版收口

- [verified | implementation + browser screenshots | review: 2026-08-17] 会员页已固定为“套餐付款区 -> 会员权益 -> 免费版已包含”的顺序；免费账号只显示日卡/周卡，会员账号显示日卡/周卡/月卡/年卡，权益说明紧凑且不在套餐卡重复。
- [verified | implementation + browser screenshots | review: 2026-08-17] 店铺多账号切换已从顶部上下文移到左侧独立“店铺管理”页面；页面提供当前标识、状态、切换和添加店铺，顶部仅展示当前上下文。
- [verified | implementation + browser screenshots | review: 2026-08-17] 自动规则页已改为紧凑的状态栏、三段式回复策略、关键词/付款资料摘要列表和单条展开编辑；同类编辑器互斥展开，暂停资料保留原配置，避免误删。
- [verified | command] `npm test`、`npm run test:syntax`、`npm run test:ui`、`npm run test:repository` 和 `git diff --check` 均通过；真实浏览器覆盖桌面与 390px 移动视口，截图人工抽查无横向溢出或明显重叠。
- [verified | local health] Node `fetch` 检查本机 Web `127.0.0.1:4173/xianyu-saas/`、API `127.0.0.1:8096/health`、JS/CSS/字体均返回 200；API、Web、consumer 是开发进程，不等于生产部署。
- [unverified | boundary] 真实闲鱼扫码、违规账号恢复、商品发布、真实支付回调和生产部署仍未验收。

## 2026-08-18 UI 与本地验收收口

- [verified | implementation + browser screenshots | review: 2026-08-18 | expires: next release] 待部署资源版本统一为 `20260817-20`；店铺管理、商家显示屏、会员分层套餐和自动规则收口改动已落在 HTML/CSS/JS，而非只有文案或缓存参数变化。
- [verified | UI contract | review: 2026-08-18 | expires: next product-plan revision] UI 合同覆盖辅助文字和未连接状态对比度、输入 placeholder、移动店铺/自动规则操作的至少 `44px` 触控尺寸、自动规则单一启停语义、桌面/移动截图和布局稳定性。
- [verified | test evidence | review: 2026-08-18 | expires: next release] `npm run test:syntax`、`npm run test:ui`（连续 3 次）、`npm test`（最终退出码 0）和 `git diff --check` 通过；测试启动时会清理旧的误导性 UI 截图产物。
- [verified | local health | review: 2026-08-18 | expires: next release] 本机 Web `http://127.0.0.1:4173/xianyu-saas/`、API `http://127.0.0.1:8096/health` 和静态资源检查返回 200；这些开发进程不等同于生产上线。
- [verified | implementation + `npm test` + browser screenshots（多模态视觉验收）| review: 2026-08-18 | expires: next release] 前端 DOM 已整体重写为 `data-panel` 面板体系（home/shops/goods/auto-reply/chat/orders/analytics/vip/settings），导航经 `data-view` 切换并保留旧路径别名；静态资源 cache-busting bump 到 `20260818-01`。`tests/ui-check.mjs` 已按新选择器与文案整体重写，全量 `npm test` 退出码 0，21 张桌面/移动截图经多模态模型逐张验收，无布局错乱、横向溢出、文字截断、图标缺失或占位符残留。

## 2026-08-18 Gemini 演示吸收：去小字 UI 收口

- [verified | user decision + implementation | review: 2026-08-18 | expires: next product-plan revision] 用户提供 Gemini 演示 `gemini-code-1787037585951.html` 作为 UI 方向：页面标题和卡片标题不再堆解释性小字（包括“账号、连接状态和店铺操作集中在一处”已删除），动态状态说明仍保留；二维码连接按钮改为整行主按钮，不再配侧面小字。
- [verified | implementation | review: 2026-08-18 | expires: next release] 商品卡片支持后端提供的 https 商品图（失败自动回退为商品首字占位），首页商品展示从 4 个扩到 6 个，会员顶栏徽章增加皇冠图标；商品同步提示只在受限/过期等异常状态出现，正常状态不显示多余状态条。静态资源 cache-busting bump 到 `20260818-02`。
- [verified | test evidence | review: 2026-08-18 | expires: next release] UI 合同新增“页头无注解小字、店铺页禁用文案不出现、连接动作无侧面提示”断言；`npm run test:ui` 与全量 `npm test` 退出码 0，`git diff --check` 通过。本会话模型不支持读图，本次视觉验收以 DOM 合同、对比度与无横向溢出断言为准，未宣称人工/多模态截图验收。
- [boundary] 只改动 `frontend/`、`tests/ui-check.mjs` 与 README 版本说明；未改变 API、账号作用域、免费/会员权限或店铺连接流程。

## 2026-08-18 用户复核：侧栏滚动/整体偏小/付费 AI 与策略区

- [correction | user feedback + implementation | review: 2026-08-18 | expires: next release] 左侧导航之前使用 `overflow-y:auto`，矮视口下会出现底部滚动；已改为 `position:sticky + height:100vh + overflow:visible`，并新增 1366×768 无滚动回归断言。
- [correction | user feedback + implementation | review: 2026-08-18 | expires: next release] 100% 缩放下整体偏小：正文/标题/按钮/徽章/卡片内边距整体放大一档，内容最大宽度 1280→1520，侧栏 64→68、顶栏 64→68，统计卡和商品卡同步放大；移动端 44px 触控尺寸合同保持通过。
- [verified | user decision + implementation | review: 2026-08-18 | expires: next product-plan revision] “智能回复”页的 AI 智能客服卡片是付费功能：免费账号整卡隐藏，不再显示“开通会员”提示或启动按钮；会员账号保留卡片和启停控制。实现入口为 `data-member-only` 统一可见性逻辑。
- [verified | implementation | review: 2026-08-18 | expires: next release] 回复策略从三枚小分段按钮升级为带说明的三张预设卡（保守/标准/积极），说明文案完整显示，选中态黄色高亮；`strategy-presets` 仍只写现有 `automation_settings.json` 的 strategy/enabled，未扩展未落地的字段。
- [verified | visual evidence | review: 2026-08-18] 本轮视觉验收使用 DSH harness 模型路由 `opencode-go/minimax-m3`：直接 RPC 创建临时会话并传截图，模型确认侧栏无滚动条、100% 缩放下不再明显偏小、策略卡清晰且无截断、免费页无“AI 智能客服”卡片。
- [verified | test evidence | review: 2026-08-18 | expires: next release] `npm run test:ui`、全量 `npm test` 退出码 0，`git diff --check` 通过；静态资源 cache-busting bump 到 `20260818-03`，README 同步更新。

## 2026-08-18 Gemini 演示第二轮吸收：页面信息架构补全

- [verified | user feedback + vision review | review: 2026-08-18 | expires: next product-plan revision] 用户指出第一轮只做减法不够完整。用 `opencode-go/minimax-m3` 逐页对比 Gemini 演示与当前截图后，按“现有数据可实现”的边界落地第二轮，不新增后端字段、不展示伪造数据。
- [verified | implementation | review: 2026-08-18 | expires: next release] 顶栏增加“当前店铺”上下文标签；六个工作台页标题增加对应图标；所有卡片标题增加 `section-icon`；统计卡增加副文案（如“今日已接收”“付款后自动发送”）。
- [verified | implementation | review: 2026-08-18 | expires: next release] 会员控制台新增“最近订单”模块：复用 `state.orders` 渲染最多 4 条真实订单行，免费账号隐藏；自动处理流程增加 1-4 序号徽章，末步绿色高亮。
- [verified | implementation | review: 2026-08-18 | expires: next release] 店铺卡按账号状态着色（过期/受限红描边），元信息改为浅灰信息块并加图标；过期/受限账号的“重新连接”升级为主按钮，“断开”使用浅红危险色。
- [verified | implementation | review: 2026-08-18 | expires: next release] 关键词规则行的触发词改为黄色关键词胶囊（最多 4 个 + 溢出计数），更接近演示的规则表表达；套餐卡按既有产品决策仍不重复权益文案。
- [verified | visual evidence | review: 2026-08-18 | expires: next release] `minimax-m3` 终检控制台/店铺/智能回复/商品四页：最近订单信息完整、流程末步高亮、店铺卡状态层次清晰、关键词胶囊可见、免费页无 AI 卡片；模型建议的趋势环比、真实商品图、更多高频词示例为后续事项，未在本轮虚构实现。
- [boundary | expires: next release] 本轮只改 `frontend/`、`tests/ui-check.mjs`、README 与项目 memory；后端契约、账号作用域、免费/会员权限和连接流程均未改变。静态资源 cache-busting 为 `20260818-04`。


## 错误改进：移动端布局测量竞态

- [correction | test evidence | review: 2026-08-18] 店铺面板切换后立即读取按钮尺寸，偶发得到 `0×0`，导致移动触控尺寸合同误报失败。
- [cause] 面板切换触发 CSS 动画和布局更新，验收脚本在面板真正可见、动画结束前测量。
- [fix] UI 合同先等待活动面板动画完成、面板可见且目标按钮尺寸非零，再执行 `44px` 触控尺寸断言和截图；旧截图名称加入启动清理名单。
- [prevention] 所有异步面板的尺寸/截图断言必须等待布局稳定，禁止用一次即时测量代表最终布局。
- [verification] 修正后 `npm run test:ui` 连续 3 次通过，`npm test` 最终退出码为 0。

## P4：验证与交付入口

- [verified | local startup smoke | review: 2026-08-23] `scripts/dev-all.sh` 和 `npm run dev` 会并行启动 API、consumer、Web，打印工作台/API 地址，并在 Ctrl+C 或任一服务退出时统一清理；`scripts/dev-api.sh` 使用当前虚拟环境解释器执行 `python -m uvicorn`，避免仓库迁移后 console-script 绝对 shebang 指向旧目录。固定隔离目录烟测确认 API 健康检查、consumer 存活、Web 静态 JS/CSS 和 Web→API 代理均正常，结束后服务与测试状态已清理；不等同于生产部署。
- 一键：`npm test`
- API/扫码合同：`npm run test:api`
- bootstrap、角色、密码与管理员合同：`npm run test:auth`
- 签名 Release、独立 updater、原子切换与回滚合同：`npm run test:update`
- 控制面持久状态与保留清理：`npm run test:p0`
- AI provider 与客服合同：`npm run test:ai`
- 异步同步 consumer：`npm run test:consumer`、`npm run test:async`
- worker：`npm run test:worker`
- 浏览器流程：`npm run test:ui`
- worker 恢复：`npm run test:recovery`
- 店铺授权后自动启动：`npm run test:auto-worker`
- 账号目录：`npm run test:storage`
- 双账号隔离：`npm run test:isolation`
- 统一收件箱：`npm run test:inbox`
- 人工回复 outbox：`npm run test:manual-reply`
- 运营统计：`npm run test:analytics`
- 商品批量资料：`npm run test:product-batch`
- 仓库敏感信息：`npm run test:repository`
- 修改后按风险运行必要验证，并用 `git diff --check` 检查补丁；命令通过不等于真实账号或生产验收完成。
- [verified | local environment: 2026-08-17 | expires: `curl` installed] 当前本机没有 `curl`；本项目 HTTP 健康检查先用 Node.js 标准 `fetch`，调用可选外部命令前先检查是否存在。

## P5：历史与清理规则

- 被新发布编号或新产品决定替代的条目标记 `superseded`，移入归档后删除本文件中的旧引用。
- 达到失效条件的条目标记 `expired`；无追溯价值时删除，敏感内容不归档。
- 当前没有已确认可归档的项目主记忆；阶段性过程写入全局 daily log，不在此堆积。

## 2026-08-17 UI 视觉刷新验收

- [verified | implementation + browser screenshots | review: 2026-08-17] 视觉刷新已实际落地：浅色窄侧栏、浅色高密度工作区、商品单列紧凑列表、会员价值/权益/套餐/支付状态层级、统一收件箱密度和 390px 移动适配；截图与 `蒸馏/` 成熟经营后台素材的布局方向一致。
- [verified | command] `npm test` 通过（含 161 项 worker 测试和 Playwright 桌面/移动合同）；`npm run test:syntax`、`npm run test:repository`、`git diff --check` 均通过。
- [verified | local health] Node `fetch` 检查本机 Web/API/JS/CSS/字体均返回 200，未登录 `/api/me` 返回 401；当前 Web `127.0.0.1:4173`、API `127.0.0.1:8096` 和 consumer 仍是开发进程，不等于 systemd 或生产部署。

## 错误改进：UI 截图落在过渡帧

- [correction | test evidence] 首轮桌面截图在 `panel-in` 动画结束前采集，主工作区文字呈半透明，容易误判为视觉刷新失败；移动截图因已有等待未复现。
- [cause] UI 合同只等待面板出现和无溢出，没有等待活动面板的 CSS 动画完成。
- [fix] `tests/ui-check.mjs` 新增 `waitForPanelSettled()`，在桌面工作台、商品、会员和收件箱截图前等待 `panel-in` 动画的 `finished` Promise，再保留短暂稳定间隔。
- [prevention] 所有用于 UI 验收的截图必须在活动面板动画完成后采集；截图人工抽查同时覆盖桌面与 390px 移动视口。
- [verification] 重新运行 `npm run test:ui` 和完整 `npm test` 均退出码 0；最新截图已人工确认文字对比度、布局密度和移动端无横向溢出。

## 错误改进：店铺连接操作区布局

- [correction | browser screenshot: 2026-08-17] 店铺管理桌面端的连接按钮被拉成高柱，提示文案逐字竖排。
- [cause] 连接按钮设置为 100% 宽后仍与提示共用横向 flex；提示被压窄，父级 stretch 又把按钮高度撑大。
- [fix] 连接操作区改为单列 grid，并增加桌面 DOM 尺寸契约。
- [prevention] 任何全宽按钮旁的状态提示必须验证布局方向、按钮高度和提示最小可读宽度。
- [verification] `npm run test:ui` 通过；桌面截图确认按钮保持紧凑、提示横向可读。

## 错误改进：店铺切换移动端按钮

- [correction | browser screenshot: 2026-08-17] 390px 店铺列表隐藏“切换”文字后只剩无意义色块，图标和无障碍名称缺失。
- [cause] 移动 CSS 只隐藏按钮文字，没有为压缩状态提供替代图标或 `aria-label`。
- [fix] 切换/当前按钮加入箭头或勾选图标，并补充 `aria-label/title`；移动合同验证图标存在。
- [prevention] 响应式隐藏文字时必须提供可识别图标和可访问名称，不能留下空色块。
- [verification] `npm run test:ui` 通过；移动截图确认按钮显示箭头/勾选图标。

## 2026-08-18 Gemini 完全对齐：权限扁平 + 模板/卡密落地

- [verified | user decision + implementation | review: 2026-08-18] 免费权限已扩展到除 `automation.ai` 外的全部现有权限；前端导航对免费用户开放客服对话/订单/统计，套餐卡全部时长可见。提交 `c54643b`。
- [verified | implementation + contracts | review: 2026-08-18] 新增发货模板 API：`GET/PUT /api/bot/templates`、`DELETE /api/bot/templates/{id}`，数据源为账号 `products_config.json` 的 redeem/pan types，不返回 payload 原文；新增卡密池 API：`GET/PUT /api/bot/cards`，数据源为 `redeem_codes.json`，只返回统计与池元数据。
- [verified | implementation + UI contract | review: 2026-08-18] 前端新增 `templates`/`cards` 面板与导航（共 9 项），模板/卡密编辑弹窗与真实请求，免费可操作；UI 合同同步扩展，`npm run test:templates-cards` 与 `npm run test:ui` 通过。
- [verified | test evidence | review: 2026-08-18] 全量 `npm test` 退出码 0；静态资源 cache-busting bump 到 `20260818-05`。

## 2026-08-18 Gemini 对齐阶段三：AI 会员定位 + 支付结算 + 卡密池补全

- [verified | user decision + implementation | review: 2026-08-18] “智能回复”页已移除 AI 客服卡片，与 Gemini 演示一致；AI 启停移入客服对话头部：免费用户只看到“开通 AI 客服”入口，会员看到开启/暂停控制。
- [verified | implementation | review: 2026-08-18] 会员结算栏新增微信/支付宝支付方式选择；未配置支付链接时按钮仍置灰，真实链接配置后直接跳转，不做模拟支付。
- [verified | implementation + contracts | review: 2026-08-18] 卡密池允许创建空池并持久化池名/备注（`card_pool.json`），空码列表表示仅改元数据、保留现有库存；前端新增左侧“新建卡密池”内联表单和“管理发货模板”入口。
- [verified | vision evidence | review: 2026-08-18] `minimax-m3` 终检：卡密页已具备内联新建表单 + 5 统计卡 + 列表；AI 已从智能回复页移除；会员页结算栏含套餐合计、支付方式与置灰按钮。
- [verified | test evidence | review: 2026-08-18] 全量 `npm test` 退出码 0；静态资源 cache-busting bump 到 `20260818-06`。

## 2026-08-18 Gemini 对齐收口：视觉终检

- [verified | implementation | review: 2026-08-18] 模板卡绑定数量改为 quiet eyebrow；流程步骤改为实心编号圆点、末步绿色高亮；套餐卡周期改为胶囊、价格字号 32px。
- [verified | vision evidence | review: 2026-08-18] `minimax-m3` 10 页终检：模板页达到可上线，卡密页基本达到上线；首页与会员页剩余项为真实商品图/真实支付配置等外部依赖，不属于本次可伪造范围。
- [verified | test evidence | review: 2026-08-18] 全量 `npm test` 退出码 0，`git diff --check` 通过，资源版本 `20260818-07`；提交 `80e8ddb`。
- [boundary | next release] 真实支付链接配置、真实商品图接入、生产部署与真实账号验收仍为后续事项。

## 2026-08-21/22 Worker 测试 OOM 风险收口

- [resolved | implementation + bounded full suite | review: 2026-08-22] `test_initial_token_and_rotated_cookie_precede_websocket_handshake` 已不再全局替换 `main.asyncio.sleep`；测试通过可终止的假 WebSocket 驱动退出，避免 `token_refresh_loop()` 高速空转和 mock 调用累积。
- [verified | local quality gate] `npm run test:worker` 在测试脚本自带的 3 GiB Worker 地址空间限制和超时下完成 216 项；完整 `npm test` 在不限制外层 Node 进程时退出码为 0。
- [prevention] 资源限制应施加到 Worker 子进程，不要给整套 `npm test` 的外层 Node/WASM 运行时套同一低地址空间上限；后者会让 UI 初始化 OOM，不能据此判定代码失败。

## 错误改进：人工回复输入区被消息内容撑出

- [correction | user report + browser contract | review: 2026-08-22] 人工接管发送消息后，文本框和发送按钮曾被挤出可视区域。
- [cause] `.chat-window` 是 CSS Grid 子项但缺少 `min-height: 0`，消息内容增长时按内容高度撑开；父级 `overflow: hidden` 将底部回复表单裁掉。
- [fix] `.chat-window` 增加 `min-height: 0`，`.reply-form` 使用 `flex: 0 0 auto` 并保持最小高度可收缩；浏览器合同新增发送后表单、文本框和发送按钮布局盒断言。
- [prevention] 纵向聊天布局中，滚动消息区必须允许收缩，底部输入区必须显式固定为不参与收缩的 flex 子项。
- [verification] `npm run test:ui`、相关 Node 语法检查和 `git diff --check` 通过；真实账号/生产环境未验证。

# 闲鱼客服 SaaS 权限与服务边界

更新日期：2026-08-18

## 套餐判定

- `member`：`expires_at` 晚于当前时间。
- `free`：新注册、未激活或会员已到期。
- 套餐由后端根据订阅时间计算；前端只展示结果，不参与授权决策。
- 会员只解锁 AI 类能力（当前为 `automation.ai`，后续的 AI 一键操作与定制服务也走会员）；订单、统计、模板、卡密、高级履约等经营功能对免费用户可用。

## 权限矩阵

| 权限 | 免费 | 会员 | 说明 |
| --- | --- | --- | --- |
| `shop.configure` | 是 | 是 | 保存店铺连接信息；敏感内容只写不读 |
| `products.manage` | 是 | 是 | 权限名沿用旧合同，实际只读已验证 Cookie 自动同步的商品快照 |
| `automation.rules` | 是 | 是 | 关键词包含匹配的固定回复规则 |
| `fulfillment.basic` | 是 | 是 | 当前店铺商品的固定文字/链接资料发送，数量固定为 1 |
| `fulfillment.manage` | 是 | 是 | 管理卡券库存、模板与自动履约 |
| `records.read` | 是 | 是 | 查看对话与订单记录 |
| `runtime.logs` | 是 | 是 | 查看托管机器人运行日志 |
| `analytics.read` | 是 | 是 | 查看运营统计 |
| `automation.ai` | 否 | 是 | 启动平台 AI 智能客服 |

停止机器人是安全操作，所有已登录用户均可执行，避免会员到期后无法关闭进程。

## API 边界

- `/api/me` 返回账号、套餐和权限数组。
- `/api/membership/plans` 返回全部时长卡种（日/周/月/年）；套餐用于开通会员 AI 能力，不是订单、统计或履约功能的门槛。前端只负责展示，不能通过请求参数绕过该限制。
- `/api/config` 只返回平台 AI 的可用状态和套餐信息，不返回或接收模型地址、模型名、API Key。
- `POST /api/bot/login/start`、同源二维码和状态接口只暴露随机登录 ID、状态和剩余时间；二维码查询材料、Cookie 和 Token 只存在于短期服务端内存。
- `GET /api/bot/login/{id}/status` 只确认扫码状态，不执行最长 55 秒的商品同步。确认后由 `POST /api/bot/login/complete` 单独同步；同步失败释放两阶段消费锁供直接重试，成功保存后才销毁登录会话。
- `POST /api/bot/connector/handoff` 只接受当前 HttpOnly 登录会话并签发短期内存凭证；每个账号最多保留 3 个活动凭证，凭证有过期和尝试次数上限，成功后立即销毁。
- `POST /api/bot/connector/cookies` 不读取普通 SaaS 会话，只接受上述 handoff。连接助手从绑定的闲鱼官方标签页读取 Cookie 后直接调用该接口，页面只接收 ACK、状态和安全错误码，不接收 Cookie、handoff token 或扩展 session。
- `PUT /api/bot/cookies` 先校验 Cookie，再保存敏感内容并生成账号绑定的商品快照；校验失败不会替换原连接。
- `POST /api/bot/shop/sync` 使用已保存 Cookie 只读同步店铺昵称和商品列表。快照必须匹配当前 Cookie 账号指纹，不能混用旧账号缓存或手工商品配置。
- 免费账号可使用真实商品快照、关键词回复、固定资料发送、卡券/模板、对话订单、运行日志和经营统计；`automation.ai`（AI 智能客服）仍由后端逐接口校验会员权限。
- Cookie 检测失败返回稳定的 `detail.code` 与中文 `detail.message`；`risk_control` 显示需要安全验证，`cookie_expired` 显示失效并要求重新获取，状态写入租户 `shop_sync_state.json`（不含 Cookie 内容，权限 `0600`）。
- Chrome 连接助手是折叠的兼容方式，只声明 `cookies` 和 DeepWhale/闲鱼白名单 host 权限，不使用 `chrome.storage`；Cookie 查询固定为闲鱼 MTOP URL 并绑定目标标签页唯一 `cookieStoreId`，提交固定为 DeepWhale connector endpoint 且使用 `credentials: "omit"`。
- 会员履约配置必须使用数字商品 ID；服务端会拒绝重复/无效 ID 和缺少网盘资源标签的配置。新租户先使用空映射，AI 可运行但不会猜测或自动发货。
- 更换 Cookie 对应的卖家账号时，旧履约映射会被原子清空，避免把上一账号的商品绑定到新账号。
- `/api/activate`、`/api/admin/codes` 兑换码流程已停用并返回 404；会员有效期仍可由管理员延长，后续支付链接接入后替换管理员流程。
- `/api/bot/conversations` 返回会话摘要；`/api/bot/messages?chat_id=...` 只返回选中买家的消息。人工回复接口只保存明确绑定会话的草稿，当前不会直接向闲鱼发送。
- `GET/PUT /api/bot/templates` 与 `DELETE /api/bot/templates/{id}` 管理 redeem/pan 发货模板；只返回模板元数据与商品绑定，不回显 payload 原文。
- `GET/PUT /api/bot/cards` 管理当前账号兑换码池元数据与统计；批量导入只接受码列表，不回传任何 Cookie/Token。
- 管理接口继续使用独立的服务器端管理认证，不复用普通用户套餐权限。

## 平台 AI 边界

- 浏览器和租户配置永远不持有上游模型凭据。
- 托管机器人启动时只获得进程生命周期内有效的内部随机凭据。
- 内部 `/internal/v1/chat/completions` 代理验证随机凭据、账号作用域、账号启用状态和实时会员状态，再使用服务器环境中的模型配置转发请求；作用域头不会转发给上游。
- nginx 不代理 `/internal/`；该路径只允许本机托管机器人访问。
- 机器人停止、退出或异常回收后立即撤销内部凭据。

## 前后端部署边界

- `frontend/` 是独立静态产物，由 nginx 从 `/var/www/xianyu-saas-ui/` 提供。
- `backend/` 只运行 FastAPI API 和 loopback 内部服务，不再提供 HTML、CSS、JavaScript 或字体。
- 公网只代理 `/xianyu-saas/api/` 到后端；静态资源与 API 使用同源路径，但部署和进程完全分离。

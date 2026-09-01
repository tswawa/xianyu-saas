# 安全约定

本项目处理店铺登录态、会话和订单状态。发现安全问题时，请优先私下报告，给维护者留出修复时间，不要在公开 Issue、讨论或 Pull Request 中披露可复用的攻击细节。

## 私下报告漏洞

- 优先使用 GitHub Security Advisories 的私下报告入口：<https://github.com/tswawa/xianyu-saas/security/advisories/new>。
- 如果目标仓库暂未启用该功能，请通过维护者账号 [@tswawa](https://github.com/tswawa) 私下联系，并只提供脱敏后的复现步骤。
- 报告中不要包含 Cookie、Token、API Key、密码、真实订单号、买家信息、库存、数据库或未经脱敏的日志。
- 维护者会先确认收到报告，再评估影响范围、修复方式和是否需要发布安全公告；不要在公开渠道催促或复制敏感内容。

## 不进入仓库的内容

- GitHub、模型平台、支付平台或内部 API 的任何 Token、Key、密码。
- 闲鱼 Cookie、账号标识、验证码、二维码查询材料或服务端登录态。
- 兑换码、试用码、网盘链接、自动发货资料和库存。
- 买家消息、订单正文、生产数据库、日志、环境文件和备份。
- 容器数据卷 `data/`（含 SQLite 数据库、店铺目录与 AI 主密钥）。

环境变量只提交 `*.example` 模板。生产值保存在部署主机权限为 `0600` 的环境文件中；源码开发机使用未跟踪的 `config/saas.env` 和 `worker/.env`，容器部署同样使用未跟踪的 `config/saas.env`。

## 提交前

```bash
python3 tests/repository-contract.py
git diff --check
git status --short
```

只暂存本次确认过的路径，不使用不加审阅的 `git add .`。发现凭据进入提交历史后，应立即撤销或轮换凭据；删除工作区文件不能让已经提交的凭据失效。

## 测试边界

默认测试必须离线或使用本地模拟服务。真实闲鱼账号、真实订单和真实发货只允许在负责人明确安排的受控窗口中验证，不能用生产订单做盲测。

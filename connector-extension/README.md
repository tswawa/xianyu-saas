# 闲鱼连接助手

这是一个独立的 Chrome Manifest V3 扩展，只负责把闲鱼官网中已经存在的登录会话
交给 DeepWhale 闲鱼客服工作台。它不接收登录凭据，不代替用户登录，也不把登录
信息交给网页脚本。

## 安装

1. 在 Chrome 打开 `chrome://extensions/`。
2. 开启“开发者模式”，点击“加载已解压的扩展程序”。
3. 选择本目录 `connector-extension/`。
4. 回到 `https://deepwhale.chat/xianyu-saas/`，从工作台发起店铺连接。

工作台会先向自己的后端申请一次性连接凭证，再把凭证交给扩展。扩展只会新开
闲鱼官方登录页；登录、验证码和安全验证都由用户在闲鱼官方页面完成。
扩展检测到非空的 `unb` 与 `_m_h5_tk` 后，直接通过固定的 SaaS 连接端点提交登录
状态，网页脚本不会收到 Cookie 内容。

## 页面消息合同

消息版本固定为 `1`。工作台发起连接时必须发送完整且无额外字段的消息：

```js
window.postMessage({
  source: "deepwhale-xianyu-saas",
  protocol: 1,
  type: "DW_XIANYU_CONNECT_START",
  requestId: crypto.randomUUID(),
  handoffToken
}, "https://deepwhale.chat");
```

扩展的所有页面响应都使用 `source: "deepwhale-xianyu-connector"`、`protocol: 1`，
并原样回显 `requestId`。响应类型如下：

- `DW_XIANYU_CONNECT_ACK`：扩展已接受当前请求。
- `DW_XIANYU_CONNECT_STATUS`：`status` 为 `login_opened`、
  `waiting_for_login`、`checking_login`、`verification_required`、`submitting`
  或 `connected`。
- `DW_XIANYU_CONNECT_ERROR`：只包含安全的 `code` 与用户可读 `message`。

用户也可以随时发送只含固定公共字段的取消消息：

```js
window.postMessage({
  source: "deepwhale-xianyu-saas",
  protocol: 1,
  type: "DW_XIANYU_CONNECT_CANCEL",
  requestId
}, "https://deepwhale.chat");
```

连接端点成功后扩展回传 `connected` 并结束内存会话。安全验证或登录状态异常会
回传 `verification_required` 并继续观察官方标签页；只有 Cookie 指纹发生变化才会
再次提交。网络或平台错误会结束本次连接并显示安全错误码。连接从首次发起起最多
保留 5 分钟。

如果扩展未安装，页面不会收到扩展消息，工作台应在自己的等待窗口内显示安装入口。
官方页面被关闭或离开官方域名时，会返回对应错误。

## 权限与生命周期

- `cookies`：只按精确 MTOP URL 读取闲鱼 Cookie，并绑定新开官方标签页所属的
  `cookieStoreId`；无法唯一确认 store 时拒绝读取。
- Host permissions：只包含 SaaS 固定路径和闲鱼域名。
- 不使用 `chrome.storage`；连接凭证、Cookie 和指纹只存在于 service worker 内存。
- 内容脚本活动期间约每 20 秒发送一次内部心跳，帮助 MV3 worker 保持活动。
- 心跳不会刷新截止时间；每次连接从首次发起起最多保留 5 分钟。
- service worker 只向固定的 `https://deepwhale.chat/xianyu-saas/api/bot/connector/cookies`
  发起 `POST`，使用 `credentials: "omit"`，不读取 DeepWhale 登录 Cookie，也不调用
  其他网络端点。

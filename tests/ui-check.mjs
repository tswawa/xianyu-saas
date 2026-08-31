import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const requireFromRepo = createRequire(path.join(repoRoot, "package.json"));
const { chromium } = requireFromRepo("playwright");

const staticRoot = path.join(repoRoot, "frontend");
const resultRoot = path.join(repoRoot, "test-results");
fs.mkdirSync(resultRoot, { recursive: true });
for (const staleName of [
  "local-live-desktop.png", "local-live-mobile.png", "shop-connector-missing-desktop.png",
  "dashboard-free-desktop.png", "shop-accounts-desktop.png", "shop-accounts-mobile.png",
  "products-free-desktop.png", "automation-free-desktop.png", "automation-free-mobile.png",
  "batch-delivery-preview-desktop.png", "batch-delivery-mobile.png",
  "shop-qr-login-desktop.png", "shop-qr-login-mobile.png",
  "membership-free-desktop.png", "membership-free-mobile.png", "mobile-nav-open.png",
  "overview-member-signals.png", "overview-member-signals-mobile.png", "overview-member-signals-mobile-viewport.png",
  "chat-member-desktop.png", "chat-member-mobile.png",
  "shops-desktop.png", "shops-mobile.png", "goods-free-desktop.png",
  "auto-reply-free-desktop.png", "auto-reply-free-mobile.png",
  "vip-free-desktop.png", "vip-free-mobile.png",
  "home-member-desktop.png", "home-member-mobile.png", "home-member-mobile-viewport.png",
  "analytics-member-desktop.png", "orders-member-desktop.png",
  "ai-config-desktop.png", "ai-config-mobile.png", "ai-templates-mobile.png",
  "ai-generated-preview-desktop.png", "ai-generated-preview-mobile.png",
  "docs-manual-desktop.png", "docs-manual-mobile.png",
]) {
  fs.rmSync(path.join(resultRoot, staleName), { force: true });
}

const selfUsePermissions = ["shop.configure", "products.manage", "automation.rules", "automation.ai", "fulfillment.basic", "fulfillment.manage", "records.read", "runtime.logs", "analytics.read"];
const productFixtures = [
  { id: "100001", title: "DeepSeek 完整使用教程与常见问题处理", description: "从安装到调用的完整步骤，适合第一次使用的店主。", price_display: "¥6.5", source: "cookie", updated_at: "2026-08-15T10:02:00" },
  { id: "100002", title: "聊天表情包", description: "日常聊天素材，付款后自动交付。", price_display: "¥0.01", source: "cookie", updated_at: "2026-08-15T10:01:00" },
  { id: "100003", title: "店铺自动化指南", description: "整理店铺经营中的常用设置。", price_display: "¥12", source: "cookie", updated_at: "2026-08-15T10:00:00" },
  ...Array.from({ length: 19 }, (_item, index) => ({
    id: String(100004 + index),
    title: `数字商品资料包 ${index + 4}`,
    description: `第 ${index + 4} 个已同步数字商品，用于模板批量绑定回归。`,
    price_display: `¥${index + 4}`,
    source: "cookie",
    updated_at: "2026-08-15T09:59:00",
  })),
];
const overflowTemplateItemIds = productFixtures.map((item) => item.id);
const defaultAiStoreConfig = {
  store_content: "",
  persona_preset: "friendly",
  persona_name: "",
  tone: "friendly",
  buyer_address: "亲",
  reply_length: "short",
  emoji_level: "low",
  forbidden_claims: "",
  handoff_rules: "",
};
const fixtures = {
  authCapabilities: { registration_enabled: true, bootstrap_available: false, password_min_length: 12 },
  bootstrapRequests: [],
  passwordRequests: [],
  me: {
    username: "owner-demo", expires_at: 0, active: false, plan: "free", plan_label: "免费",
    role: "owner", role_label: "店主", is_admin: false, permissions: selfUsePermissions, platform_permissions: [],
  },
  version: {
    version: "0.1.0",
    commit: "ui-contract",
    build_time: "2026-08-31T00:00:00Z",
    asset_version: "20260831-01",
    update_channel: "stable",
    release_notes: "本地说明 <img src=x onerror=window.__releaseNotesInjected=true>",
    latest_update: null,
  },
  adminSettings: {
    registration: { environment_allowed: true, database_open: false, users_exist: true, effective: false },
    update_channel: "stable",
  },
  adminUsers: [
    { id: 1, username: "admin-demo", role: "admin", role_label: "管理员", enabled: true, locked: false, session_count: 1, created_at: 1788134400, password_changed_at: 1788134400 },
    { id: 2, username: "owner-demo", role: "owner", role_label: "店主", enabled: true, locked: true, session_count: 2, created_at: 1788134400, password_changed_at: 1788134400 },
  ],
  auditEvents: [
    { id: 1, event_type: "auth.login_succeeded", actor_user_id: 1, target_type: "user", target_id: "1", outcome: "success", source_hash: "source-hash", metadata: {}, created_at: 1788134400 },
  ],
  updateStatus: {
    current: { version: "0.1.0", commit: "ui-contract", build_time: "2026-08-31T00:00:00Z", asset_version: "20260831-01", update_channel: "stable" },
    latest_update: null,
    rollback_versions: ["0.0.9"],
  },
  updateRequests: [],
  adminUserRequests: [],
  adminSettingRequests: [],
  adminConfirmRequests: [],
  bot: {
    running: false,
    cookies_set: true,
    connected: true,
    sync_status: "verified",
    cookie_status: { code: "verified", label: "已验证", message: "Cookie 已验证（不显示内容）", action: "可随时重新检测店铺商品" },
    products_set: true,
    product_count: productFixtures.length,
    running_total: 0,
    shop_name: "海风数字店",
    last_sync_at: "2026-08-15T10:00:00+0800",
    products_truncated: false,
    codes_locked: false,
    ai_locked: false,
    rules_locked: false,
    basic_fulfillment_locked: false,
    automation_mode: "rules",
  },
  shopAccounts: [
    { id: 1, key: "default", platform: "xianyu", name: "海风数字店", status: "ready", enabled: true, last_error_code: "", last_verified_at: "2026-08-15T10:00:00+0800", last_sync_at: "2026-08-15T10:00:00+0800" },
  ],
  shopAccountHeaders: [],
  shopActionRequests: [],
  shopAccountPatchRequests: [],
  shopAccountDeleteRequests: [],
  attention: [],
  summary: { messages_total: 12, orders_total: 4, delivered_total: 3, attention_total: 1, last_activity: "08-15 15:20" },
  analytics: {
    totals: { messages_total: 5, auto_replies_total: 3, fulfillment_success_total: 2, fulfillment_failed_total: 1 },
    buckets: [
      { date: "2026-08-13", messages_total: 2 },
      { date: "2026-08-14", messages_total: 0 },
      { date: "2026-08-15", messages_total: 5 },
    ],
  },
  config: { bot_running: false, reply_rules: [], platform_ai: { managed: false, available: false } },
  automation: {
    rules: [{ id: "rule-1", name: "使用咨询", item_id: "100001", enabled: true, keywords: ["怎么用"], match: "contains", reply: "请告诉我具体想了解哪一步使用方法。" }],
    deliveries: [{ item_id: "100001", enabled: true, delivery: "material", material: "使用说明：https://example.com/guide" }],
    running: false,
    desired_running: false,
    strategy: "standard",
    enabled: true,
    first_reply: "你好，在的，请问想了解商品的哪一方面？",

    fallback_reply: "这个问题我稍后人工为您解答。",
    delay_min_seconds: 2,
    delay_max_seconds: 3,
    trigger_cooldown_seconds: 2,
    manual_takeover_cooldown_seconds: 30,
    business_hours_enabled: false,
    business_start: "09:00",
    business_end: "23:30",
  },
  products: productFixtures,
  ai: {
    status: { enabled: false, running: false, connection_verified: false, error_code: "" },
    connection: { provider: "openai_chat_completions", base_url: "", model: "", api_key_configured: false, status: "unconfigured", revision: 0, key_revision: 0 },
    config: { draft: structuredClone(defaultAiStoreConfig), published: null, status: "draft", revision: 0 },
    templates: [],
    products: productFixtures.map((item) => ({
      item_id: item.id,
      facts: { item_id: item.id, title: item.title, description: item.description, price: item.price_display, stock: "", status: "在售", skus: [] },
      knowledge_status: "unconfigured",
      snapshot_fingerprint: `snapshot-${item.id}`,
    })),
    knowledge: {},
    versions: {},
  },
  aiRequests: [],
  aiPreviewRequests: [],
  aiPreviewResponseDelays: [],
  aiExtractResponses: [],
  aiExtractResponseDelays: [],
  aiKnowledgeResponseDelays: [],
  aiConnectionTestDelayMsByAccount: {},
  messages: [
    { role: "user", content: "你好，这个商品怎么使用？", time: "2026-08-15 15:17", chat_id: "chat-1", item_id: "100001", content_type: "rich", media: JSON.stringify([{ type: "image", url: "https://cdn.example/buyer.png", label: "买家图片", path: "manual_reply_private.png" }, { type: "emoji", label: "开心表情" }]) },
    { role: "assistant", content: "付款后会发送完整说明，有问题可以继续问我。", time: "2026-08-15 15:18", chat_id: "chat-1", item_id: "100001" },
    { role: "user", content: "另一个买家的问题", time: "2026-08-15 15:19", chat_id: "chat-2", item_id: "100002" },
  ],
  conversations: [
    { chat_id: "chat-2", item_id: "100002", buyer_label: "买家 · 0002", preview: "另一个买家的问题", time: "2026-08-15 15:19", message_count: 1, unread: true, manual_mode: false },
    { chat_id: "chat-1", item_id: "100001", buyer_label: "买家 · 0001", preview: "付款后会发送完整说明", time: "2026-08-15 15:18", message_count: 2, unread: false, manual_mode: false },
  ],
  orders: [
    { order_key: "202608150001", status: "delivered", item_id: "DeepSeek 完整教程", quantity: 1, paid_amount: "6.50", delivered_at: "08-15 15:12", created_at: "08-15 15:11" },
    { order_key: "202608150002", status: "manual_review", item_id: "聊天表情包", quantity: 1, paid_amount: "0.01", delivered_at: "", created_at: "08-15 15:14" },
  ],
  templates: [
    { id: "tpl-1", name: "卡密自动发货模板", description: "感谢购买！系统将自动发送兑换码。", delivery: "account", price: "", item_ids: overflowTemplateItemIds.concat(["999999"]), enabled: true, item_count: overflowTemplateItemIds.length + 1, payload_set: true },
    { id: "tpl-2", name: "网盘资源模板", description: "付款后发送网盘链接与提取码。", delivery: "pan", price: "", item_ids: ["100002", "100003"], resource_match: ["网盘资源模板"], enabled: true, item_count: 2, payload_set: true },
  ],
  templateRequests: [],
  cards: {
    pool: { id: "pool-1", name: "默认卡密池", note: "全自动发货绑定中", total: 120, available: 85, used: 32, enabled: true },
    stats: { pools: 1, total: 120, available: 85, reserved: 3, used: 32 },
  },
  cardRequests: [],
  cardGetRequests: [],
  productGetRequests: [],
  automationPuts: [],
  automationPutDelayMsByAccount: {},
  botStartModes: [],
  botStops: [],
  botStatusRequests: 0,
  botStatusResponseDelays: [],
  authorizationHeaders: [],
  cookieSaves: 0,
  qrLoginCounter: 0,
  qrLogins: new Map(),
  qrStarts: 0,
  qrConnects: 0,
  qrCancels: 0,
  qrNextMode: "expired",
  qrStartDelayMs: 0,
  qrSyncFailures: 0,
  qrStageFailures: 0,
  qrStageCancelNotFound: 0,
  handoffCounter: 0,
  handoffTokens: new Set(),
  cookieFailureCode: "",
  accountData: {},
  loaderResponseDelayMs: { products: {}, automation: {}, orders: {}, cards: {}, ai: {} },
  messageResponseDelayMsByChat: {},
  messageRequests: [],
  quickReplies: [
    { id: "welcome", title: "在的", content: "你好，在的，请问需要了解什么？" },
    { id: "delivery", title: "发货说明", content: "付款后系统会按当前商品配置自动处理。" },
  ],
  quickReplyRequests: [],
  manualReplies: [],
  manualReplyRequests: [],
  manualImageRequests: [],
  manualReplyPolls: 0,
  manualReplyPollsById: new Map(),
  manualReplyPollModes: new Map(),
  manualReplyPollMode: "success",
  manualReplyPostDelayMs: 0,
  manualReplyPostMode: "success",
  manualReplyPostFailures: 0,
  manualReplyPollNotFoundResponses: 0,
  inboxReadCommands: [],
  inboxTakeoverCommands: [],
  inboxReadDelayMs: 0,
  inboxTakeoverDelayMs: 0,
  inboxTakeoverMode: "success",
  inboxTakeoverFailures: 0,
  batchPreviews: [],
  batchCommits: [],
  batchPreviewToken: "",
};

function json(res, value, status = 200, headers = {}) {
  const body = JSON.stringify(value);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(body), ...headers });
  res.end(body);
}

function scopedFixture(req, key, fallback) {
  const accountKey = String(req.headers["x-shop-account"] || "default");
  const account = fixtures.accountData[accountKey];
  const value = account && Object.prototype.hasOwnProperty.call(account, key) ? account[key] : fallback;
  return { accountKey, value };
}

function accountForRequest(req, accountKey = "") {
  const key = String(accountKey || req.headers["x-shop-account"] || "default");
  return fixtures.shopAccounts.find((item) => item.key === key) || null;
}

function scopedBot(req) {
  return scopedFixture(req, "bot", fixtures.bot);
}

function scopedAi(req) {
  return scopedFixture(req, "ai", fixtures.ai);
}

function takeLoaderDelay(kind, accountKey) {
  const delays = fixtures.loaderResponseDelayMs[kind] || {};
  const delay = Number(delays[accountKey] || 0);
  delete delays[accountKey];
  return delay;
}

function createServer() {
  let loggedIn = false;
  return http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/xianyu-saas" || url.pathname === "/xianyu-saas/") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(fs.readFileSync(path.join(staticRoot, "index.html")));
      return;
    }
    if (url.pathname.startsWith("/xianyu-saas/assets/")) {
      const assetsRoot = path.join(staticRoot, "assets");
      const file = path.join(assetsRoot, url.pathname.slice("/xianyu-saas/assets/".length));
      if (!file.startsWith(assetsRoot) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
      const extension = path.extname(file);
      const contentType = extension === ".css" ? "text/css" : extension === ".js" ? "text/javascript" : extension === ".svg" ? "image/svg+xml" : extension === ".woff2" ? "font/woff2" : extension === ".zip" ? "application/zip" : "application/octet-stream";
      res.writeHead(200, { "content-type": contentType, "cache-control": "no-store" });
      res.end(fs.readFileSync(file));
      return;
    }
    if (!url.pathname.startsWith("/xianyu-saas/api/")) { res.writeHead(404); res.end(); return; }
    if (req.headers.authorization) fixtures.authorizationHeaders.push(req.headers.authorization);
    const apiPath = url.pathname.slice("/xianyu-saas".length);
    let rawBody = "";
    req.on("data", (chunk) => { rawBody += chunk; });
    req.on("end", () => {
      const contentType = String(req.headers["content-type"] || "").toLowerCase();
      const payload = contentType.includes("application/json") && rawBody ? JSON.parse(rawBody) : {};
      if (apiPath === "/api/auth/capabilities" && req.method === "GET") {
        return json(res, fixtures.authCapabilities);
      }
      if (apiPath === "/api/auth/bootstrap" && req.method === "POST") {
        fixtures.bootstrapRequests.push({
          payload,
          token: String(req.headers["x-bootstrap-token"] || ""),
          browserIntent: String(req.headers["x-saas-browser-intent"] || ""),
          url: req.url,
        });
        fixtures.authCapabilities = { ...fixtures.authCapabilities, bootstrap_available: false };
        return json(res, { ok: true });
      }
      if (apiPath === "/api/auth/login" && req.method === "POST") {
        loggedIn = true;
        return json(res, { ok: true }, 200, { "set-cookie": "xianyu_saas_session=mock-session; Path=/xianyu-saas/; HttpOnly; SameSite=Strict" });
      }
      if (apiPath === "/api/auth/register" && req.method === "POST") return json(res, { ok: true });
      if (apiPath === "/api/auth/logout" && req.method === "POST") {
        loggedIn = false;
        return json(res, { ok: true }, 200, { "set-cookie": "xianyu_saas_session=; Path=/xianyu-saas/; Max-Age=0; HttpOnly" });
      }
      if (!loggedIn) return json(res, { detail: "未登录" }, 401);
      if (req.headers["x-shop-account"]) fixtures.shopAccountHeaders.push(String(req.headers["x-shop-account"]));
      if (apiPath === "/api/me") return json(res, fixtures.me);
      if (apiPath === "/api/auth/password" && req.method === "POST") {
        fixtures.passwordRequests.push(payload);
        return json(res, { ok: true, other_sessions_revoked: true });
      }
      if (apiPath === "/api/version" && req.method === "GET") return json(res, fixtures.version);
      if (apiPath.startsWith("/api/admin/") && fixtures.me?.is_admin !== true) {
        return json(res, { detail: { code: "admin_required", message: "需要管理员权限" } }, 403);
      }
      if (apiPath === "/api/admin/settings" && req.method === "GET") return json(res, fixtures.adminSettings);
      if (apiPath === "/api/admin/settings" && req.method === "PUT") {
        fixtures.adminSettingRequests.push(payload);
        if (typeof payload.registration_open === "boolean") {
          fixtures.adminSettings.registration.database_open = payload.registration_open;
          fixtures.adminSettings.registration.effective = fixtures.adminSettings.registration.environment_allowed && payload.registration_open && fixtures.adminSettings.registration.users_exist;
        }
        if (["stable", "beta"].includes(payload.update_channel)) {
          fixtures.adminSettings.update_channel = payload.update_channel;
          fixtures.version.update_channel = payload.update_channel;
          fixtures.updateStatus.current.update_channel = payload.update_channel;
        }
        return json(res, fixtures.adminSettings);
      }
      if (apiPath === "/api/admin/users" && req.method === "GET") {
        return json(res, { users: fixtures.adminUsers, next_cursor: null });
      }
      const adminUserMatch = apiPath.match(/^\/api\/admin\/users\/(\d+)$/);
      if (adminUserMatch && req.method === "PATCH") {
        const user = fixtures.adminUsers.find((item) => String(item.id) === adminUserMatch[1]);
        if (!user) return json(res, { detail: { code: "user_not_found", message: "账号不存在" } }, 404);
        fixtures.adminUserRequests.push({ action: "patch", userId: user.id, payload });
        if (["owner", "admin"].includes(payload.role)) {
          user.role = payload.role;
          user.role_label = payload.role === "admin" ? "管理员" : "店主";
        }
        if (typeof payload.enabled === "boolean") user.enabled = payload.enabled;
        return json(res, { user });
      }
      const adminUnlockMatch = apiPath.match(/^\/api\/admin\/users\/(\d+)\/unlock$/);
      if (adminUnlockMatch && req.method === "POST") {
        const user = fixtures.adminUsers.find((item) => String(item.id) === adminUnlockMatch[1]);
        if (!user) return json(res, { detail: { code: "user_not_found", message: "账号不存在" } }, 404);
        fixtures.adminUserRequests.push({ action: "unlock", userId: user.id });
        user.locked = false;
        return json(res, { ok: true });
      }
      const adminSessionsMatch = apiPath.match(/^\/api\/admin\/users\/(\d+)\/sessions\/revoke$/);
      if (adminSessionsMatch && req.method === "POST") {
        const user = fixtures.adminUsers.find((item) => String(item.id) === adminSessionsMatch[1]);
        if (!user) return json(res, { detail: { code: "user_not_found", message: "账号不存在" } }, 404);
        fixtures.adminUserRequests.push({ action: "revoke", userId: user.id });
        const revoked = Math.max(Number(user.session_count || 0) - (user.username === fixtures.me.username ? 1 : 0), 0);
        user.session_count = user.username === fixtures.me.username ? 1 : 0;
        return json(res, { ok: true, sessions_revoked: revoked });
      }
      if (apiPath === "/api/admin/audit" && req.method === "GET") {
        return json(res, { events: fixtures.auditEvents, next_cursor: null });
      }
      if (apiPath === "/api/admin/updates" && req.method === "GET") return json(res, fixtures.updateStatus);
      if (apiPath === "/api/admin/updates/check" && req.method === "POST") {
        fixtures.updateRequests.push({ action: "check", payload });
        fixtures.updateStatus.latest_update = {
          version: "0.2.0", channel: fixtures.adminSettings.update_channel, status: "available",
          release_notes: "Release 0.2.0 <script>window.__releaseNotesInjected=true</script>", error_code: "", updated_at: 1788134400,
        };
        return json(res, {
          available: true, current_version: fixtures.version.version, version: "0.2.0",
          channel: fixtures.adminSettings.update_channel, published_at: "2026-08-31T00:00:00Z",
          release_notes: fixtures.updateStatus.latest_update.release_notes,
        });
      }
      if (apiPath === "/api/admin/updates/download" && req.method === "POST") {
        fixtures.updateRequests.push({ action: "download", payload });
        fixtures.updateStatus.latest_update = {
          ...fixtures.updateStatus.latest_update,
          version: String(payload.version || "0.2.0"), status: "staged", updated_at: 1788134401,
        };
        return json(res, {
          version: fixtures.updateStatus.latest_update.version,
          channel: fixtures.adminSettings.update_channel,
          status: "staged",
          release_notes: fixtures.updateStatus.latest_update.release_notes,
        });
      }
      if (apiPath === "/api/admin/confirm" && req.method === "POST") {
        fixtures.adminConfirmRequests.push(payload);
        return json(res, { confirmation_token: "ui-one-time-confirmation", expires_in: 180 });
      }
      if (["/api/admin/updates/apply", "/api/admin/updates/rollback"].includes(apiPath) && req.method === "POST") {
        const action = apiPath.endsWith("/apply") ? "apply" : "rollback";
        fixtures.updateRequests.push({ action, payload });
        fixtures.updateStatus.latest_update = {
          ...(fixtures.updateStatus.latest_update || {}),
          version: String(payload.version || ""), channel: fixtures.adminSettings.update_channel,
          status: action === "apply" ? "apply_requested" : "rollback_requested", error_code: "", updated_at: 1788134402,
        };
        return json(res, { queued: true, action, version: String(payload.version || "") }, 202);
      }
      if (apiPath === "/api/bot/accounts" && req.method === "GET") return json(res, { accounts: fixtures.shopAccounts });
      if (apiPath === "/api/bot/accounts" && req.method === "POST") {
        const name = String(payload.name || "").trim();
        const account = {
          id: fixtures.shopAccounts.length + 1,
          key: `shop-ui-${fixtures.shopAccounts.length + 1}`,
          platform: "xianyu",
          name,
          status: "unconfigured",
          enabled: true,
          last_error_code: "",
          last_verified_at: null,
          last_sync_at: null,
        };
        fixtures.shopAccounts.push(account);
        fixtures.accountData[account.key] = {
          products: [],
          automation: { rules: [], deliveries: [], running: false, rules_set: false, deliveries_set: false, strategy: "standard", enabled: true },
          conversations: [],
          quickReplies: [],
          orders: [],
          ai: {
            status: { enabled: false, running: false, connection_verified: false, error_code: "" },
            connection: { provider: "openai_chat_completions", base_url: "", model: "", api_key_configured: false, status: "unconfigured", revision: 0, key_revision: 0 },
            config: { draft: structuredClone(defaultAiStoreConfig), published: null, status: "draft", revision: 0 },
            templates: [],
            products: [],
            knowledge: {},
            versions: {},
          },
        };
        return json(res, { ok: true, account });
      }
      const accountPathMatch = apiPath.match(/^\/api\/bot\/accounts\/([^/]+)$/);
      if (accountPathMatch && req.method === "PATCH") {
        const key = decodeURIComponent(accountPathMatch[1]);
        const account = accountForRequest(req, key);
        if (!account || account.enabled === false) return json(res, { detail: "店铺不存在" }, 404);
        const name = String(payload.name || "").trim();
        fixtures.shopAccountPatchRequests.push({ key, name });
        account.name = name || account.name;
        return json(res, { ok: true, account: { ...account } });
      }
      if (accountPathMatch && req.method === "DELETE") {
        const key = decodeURIComponent(accountPathMatch[1]);
        const account = accountForRequest(req, key);
        if (!account || account.enabled === false) return json(res, { detail: "店铺不存在" }, 404);
        if (key === "default") return json(res, { detail: "默认店铺不能删除" }, 409);
        fixtures.shopAccountDeleteRequests.push(key);
        account.enabled = false;
        account.status = "disabled";
        return json(res, { ok: true, account: { ...account } });
      }
      if (apiPath === "/api/config") return json(res, fixtures.config);
      if (apiPath === "/api/bot/status") {
        const scoped = scopedBot(req);
        const account = fixtures.shopAccounts.find((item) => item.key === scoped.accountKey) || fixtures.shopAccounts[0];
        const unconfigured = account?.status === "unconfigured";
        const bot = unconfigured
          ? { ...scoped.value, cookies_set: false, connected: false, sync_status: "unconfigured", cookie_status: { code: "unconfigured", label: "未连接", message: "尚未连接闲鱼店铺", action: "连接后自动识别店铺和商品" }, products_set: false, product_count: 0, catalog_state: "not_started" }
          : { ...scoped.value };
        const requestNumber = ++fixtures.botStatusRequests;
        const response = { ...bot, shop_name: account.name || bot.shop_name, account: { ...account, name: account.name || bot.shop_name }, account_id: account.id };
        const headers = { "x-ui-bot-status-request": String(requestNumber) };
        const delay = Number(fixtures.botStatusResponseDelays.shift() || 0);
        if (delay > 0) {
          setTimeout(() => json(res, response, 200, headers), delay);
          return;
        }
        return json(res, response, 200, headers);
      }
      if (apiPath === "/api/bot/attention" && req.method === "GET") {
        const pendingTotal = fixtures.attention.filter((item) => !item.resolved).length;
        return json(res, {
          ok: true,
          items: fixtures.attention,
          total: fixtures.attention.length,
          pending_total: pendingTotal,
          resolved_total: fixtures.attention.length - pendingTotal,
        });
      }
      const attentionMatch = apiPath.match(/^\/api\/bot\/attention\/(att_[0-9a-f]{24})$/);
      if (attentionMatch && req.method === "PUT") {
        const item = fixtures.attention.find((candidate) => candidate.id === attentionMatch[1]);
        if (!item) return json(res, { detail: "预警事项不存在" }, 404);
        item.resolved = Boolean(payload.resolved);
        item.resolved_at = item.resolved ? 1787443200 : null;
        const pendingTotal = fixtures.attention.filter((candidate) => !candidate.resolved).length;
        return json(res, {
          ok: true,
          items: fixtures.attention,
          total: fixtures.attention.length,
          pending_total: pendingTotal,
          resolved_total: fixtures.attention.length - pendingTotal,
        });
      }
      if (apiPath === "/api/bot/summary" && req.method === "GET") {
        return json(res, fixtures.summary);
      }
      if (apiPath === "/api/bot/analytics" && req.method === "GET") {
        return json(res, fixtures.analytics);
      }
      if (apiPath === "/api/bot/login/start" && req.method === "POST") {
        const loginId = `qr-ui-${String(++fixtures.qrLoginCounter).padStart(32, "0")}`;
        const accountKey = String(req.headers["x-shop-account"] || "default");
        fixtures.qrLogins.set(loginId, { polls: 0, mode: fixtures.qrNextMode, accountKey });
        fixtures.qrNextMode = "success";
        fixtures.qrStarts += 1;
        const delay = fixtures.qrStartDelayMs;
        fixtures.qrStartDelayMs = 0;
        if (delay) {
          setTimeout(() => json(res, { login_id: loginId, status: "waiting", expires_in: 150 }), delay);
          return;
        }
        return json(res, { login_id: loginId, status: "waiting", expires_in: 150 });
      }
      const qrMatch = apiPath.match(/^\/api\/bot\/login\/([A-Za-z0-9_-]+)\/qr\.svg$/);
      if (qrMatch && req.method === "GET") {
        const login = fixtures.qrLogins.get(qrMatch[1]);
        if (!login || login.accountKey !== String(req.headers["x-shop-account"] || "default")) {
          return json(res, { detail: { code: "login_not_found", message: "登录会话不存在" } }, 404);
        }
        const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="white"/><path d="M8 8h28v28H8zm56 0h28v28H64zM8 64h28v28H8zm42-14h12v12H50zm20 20h12v12H70z"/></svg>';
        res.writeHead(200, { "content-type": "image/svg+xml", "cache-control": "no-store", "content-length": Buffer.byteLength(svg) });
        res.end(svg);
        return;
      }
      const qrStatusMatch = apiPath.match(/^\/api\/bot\/login\/([A-Za-z0-9_-]+)\/status$/);
      if (qrStatusMatch && req.method === "GET") {
        const login = fixtures.qrLogins.get(qrStatusMatch[1]);
        if (!login || login.accountKey !== String(req.headers["x-shop-account"] || "default")) {
          return json(res, { detail: { code: "login_not_found", message: "登录会话不存在" } }, 404);
        }
        login.polls += 1;
        if (login.mode === "mtop_context_failed") {
          fixtures.qrLogins.delete(qrStatusMatch[1]);
          fixtures.qrStageFailures += 1;
          return json(res, { detail: { code: "mtop_context_failed", message: "扫码确认成功，但登录上下文初始化失败，请刷新二维码重试。", retryable: true } }, 502);
        }
        if (login.mode === "expired") return json(res, { login_id: qrStatusMatch[1], status: "expired", expires_in: 0 });
        if (login.polls === 1) return json(res, { login_id: qrStatusMatch[1], status: "waiting", expires_in: 148 });
        if (login.polls === 2) return json(res, { login_id: qrStatusMatch[1], status: "scanned", expires_in: 146 });
        return json(res, { login_id: qrStatusMatch[1], status: "confirmed", expires_in: 90 });
      }
      if (apiPath === "/api/bot/login/complete" && req.method === "POST") {
        const login = fixtures.qrLogins.get(payload.login_id);
        if (!login || login.accountKey !== String(req.headers["x-shop-account"] || "default")) {
          return json(res, { detail: { code: "login_not_found", message: "登录会话不存在" } }, 404);
        }
        if (login.mode === "sync_fail_once" && !login.syncFailed) {
          login.syncFailed = true;
          fixtures.qrSyncFailures += 1;
          return json(res, { detail: { code: "network_error", message: "暂时无法连接闲鱼", retryable: true } }, 503);
        }
        fixtures.qrLogins.delete(payload.login_id);
        fixtures.qrConnects += 1;
        fixtures.bot.cookies_set = true;
        fixtures.bot.connected = true;
        fixtures.bot.sync_status = "verified";
        fixtures.bot.cookie_status = { code: "verified", label: "已验证", message: "登录状态已验证", action: "可随时重新检测店铺商品" };
        const account = accountForRequest(req);
        if (account) {
          account.status = "ready";
          account.last_error_code = "";
          account.last_verified_at = "2026-08-15T10:02:00+0800";
          account.last_sync_at = "2026-08-15T10:02:00+0800";
          if (!account.name) account.name = fixtures.bot.shop_name;
          fixtures.accountData[account.key] = {
            products: fixtures.products,
            automation: fixtures.automation,
            conversations: fixtures.conversations,
            quickReplies: structuredClone(fixtures.quickReplies),
            orders: fixtures.orders,
            cards: {
              pool: { ...fixtures.cards.pool, name: "备用店卡密池" },
              stats: { ...fixtures.cards.stats },
            },
            ai: {
              status: { enabled: false, running: false, connection_verified: false, error_code: "" },
              connection: { provider: "openai_chat_completions", base_url: "", model: "", api_key_configured: false, status: "unconfigured", revision: 0, key_revision: 0 },
              config: { draft: structuredClone(defaultAiStoreConfig), published: null, status: "draft", revision: 0 },
              templates: [],
              products: fixtures.products.map((item) => ({ item_id: item.id, title: `备用店 · ${item.title}`, price_display: item.price_display, knowledge_status: "unconfigured", snapshot_fingerprint: `backup-${item.id}` })),
              knowledge: {},
              versions: {},
            },
          };
        }
        return json(res, { login_id: payload.login_id, status: "connected", connected: true, shop_name: fixtures.bot.shop_name, product_count: fixtures.products.length });
      }
      const qrCancelMatch = apiPath.match(/^\/api\/bot\/login\/([A-Za-z0-9_-]+)\/cancel$/);
      if (qrCancelMatch && req.method === "POST") {
        const login = fixtures.qrLogins.get(qrCancelMatch[1]);
        if (!login || login.accountKey !== String(req.headers["x-shop-account"] || "default")) {
          if (!login) fixtures.qrStageCancelNotFound += 1;
          return json(res, { detail: { code: "login_not_found", message: "登录会话不存在" } }, 404);
        }
        fixtures.qrLogins.delete(qrCancelMatch[1]);
        fixtures.qrCancels += 1;
        return json(res, { ok: true });
      }
      if (apiPath === "/api/bot/connector/handoff" && req.method === "POST") {
        const token = `handoff-contract-${++fixtures.handoffCounter}`;
        fixtures.handoffTokens.add(token);
        return json(res, { ok: true, handoff_token: token, expires_at: Date.now() / 1000 + 600 });
      }
      if (apiPath === "/api/bot/connector/cookies" && req.method === "POST") {
        if (!fixtures.handoffTokens.has(payload.handoff_token)) return json(res, { detail: { code: "handoff_invalid", message: "连接请求已失效" } }, 401);
        fixtures.handoffTokens.delete(payload.handoff_token);
        if (typeof payload.cookies !== "string" || !payload.cookies.includes("unb=") || !payload.cookies.includes("_m_h5_tk=")) {
          return json(res, { detail: { code: "cookie_incomplete", message: "登录信息不完整" } }, 400);
        }
        fixtures.cookieSaves += 1;
        fixtures.bot.cookies_set = true;
        fixtures.bot.connected = true;
        fixtures.bot.sync_status = "verified";
        fixtures.bot.cookie_status = { code: "verified", label: "已验证", message: "登录状态已验证", action: "可随时重新检测店铺商品" };
        fixtures.bot.last_sync_at = "2026-08-15T10:01:00+0800";
        return json(res, { ok: true, connected: true, shop_name: fixtures.bot.shop_name, product_count: fixtures.products.length });
      }
      if (apiPath === "/api/bot/ai/status" && req.method === "GET") {
        const scoped = scopedAi(req);
        return json(res, structuredClone(scoped.value.status));
      }
      if (apiPath === "/api/bot/ai/connection" && req.method === "GET") {
        const scoped = scopedAi(req);
        const response = structuredClone(scoped.value.connection);
        const delay = takeLoaderDelay("ai", scoped.accountKey);
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/ai/connection/test" && req.method === "POST") {
        const scoped = scopedAi(req);
        fixtures.aiRequests.push({ method: "POST", kind: "connection-test", accountKey: scoped.accountKey, payload: structuredClone(payload) });
        const finish = () => {
          const provider = String(payload.provider || "openai_chat_completions");
          const reusableKey = scoped.value.connection.api_key_configured === true && String(scoped.value.connection.provider || "openai_chat_completions") === provider;
          if (!String(payload.base_url || "").startsWith("https://")) return json(res, { detail: { code: "unsafe_url", message: "连接地址不安全" } }, 400);
          if (!String(payload.model || "").trim()) return json(res, { detail: { code: "model_not_found", message: "模型不存在" } }, 404);
          if (provider !== "ollama_chat" && !String(payload.api_key || "").trim() && !reusableKey) return json(res, { detail: { code: "authentication_failed", message: "API Key 无效" } }, 401);
          return json(res, { ok: true, status: "success", provider, verification_token: `verify-${scoped.accountKey}-${provider}-${scoped.value.connection.key_revision}`, tested_at: "2026-08-26T10:00:00+08:00" });
        };
        const delay = Number(fixtures.aiConnectionTestDelayMsByAccount[scoped.accountKey] || 0);
        delete fixtures.aiConnectionTestDelayMsByAccount[scoped.accountKey];
        if (delay > 0) setTimeout(finish, delay);
        else finish();
        return;
      }
      if (apiPath === "/api/bot/ai/connection" && req.method === "PUT") {
        const scoped = scopedAi(req);
        if (!String(payload.verification_token || "").startsWith(`verify-${scoped.accountKey}-`)) return json(res, { detail: { code: "verification_required", message: "请先测试连接" } }, 409);
        const provider = String(payload.provider || "openai_chat_completions");
        const replacingKey = Boolean(String(payload.api_key || "").trim());
        const sameProvider = String(scoped.value.connection.provider || "openai_chat_completions") === provider;
        scoped.value.connection = {
          provider,
          base_url: String(payload.base_url || ""),
          model: String(payload.model || ""),
          api_key_configured: replacingKey || (sameProvider && scoped.value.connection.api_key_configured === true),
          status: "verified",
          verified: true,
          revision: Number(scoped.value.connection.revision || 0) + 1,
          key_revision: Number(scoped.value.connection.key_revision || 0) + (replacingKey ? 1 : 0),
          last_tested_at: "2026-08-26T10:00:00+08:00",
        };
        scoped.value.status = { ...scoped.value.status, connection_verified: true, error_code: "" };
        fixtures.aiRequests.push({ method: "PUT", kind: "connection", accountKey: scoped.accountKey, payload: structuredClone(payload) });
        const safe = structuredClone(scoped.value.connection);
        return json(res, { ok: true, connection: safe });
      }
      if (apiPath === "/api/bot/ai/connection/key" && req.method === "DELETE") {
        const scoped = scopedAi(req);
        if (payload.confirm !== true) return json(res, { detail: "需要确认删除" }, 400);
        scoped.value.connection = { ...scoped.value.connection, api_key_configured: false, status: "unconfigured", verified: false, key_revision: Number(scoped.value.connection.key_revision || 0) + 1 };
        scoped.value.status = { ...scoped.value.status, enabled: false, running: false, connection_verified: false };
        fixtures.aiRequests.push({ method: "DELETE", kind: "key", accountKey: scoped.accountKey, payload: structuredClone(payload) });
        return json(res, { ok: true });
      }
      if (apiPath === "/api/bot/ai/config" && req.method === "GET") {
        const scoped = scopedAi(req);
        return json(res, { config: structuredClone(scoped.value.config), presets: { catgirl: {} } });
      }
      if (apiPath === "/api/bot/ai/config" && req.method === "PUT") {
        const scoped = scopedAi(req);
        const revision = Number(scoped.value.config.revision || 0) + 1;
        const config = structuredClone(payload.config || {
          store_content: payload.store_content,
          persona_preset: payload.persona_preset,
          persona_name: payload.persona_name,
          tone: payload.tone,
          buyer_address: payload.buyer_address,
          reply_length: payload.reply_length,
          emoji_level: payload.emoji_level,
          forbidden_claims: payload.forbidden_claims,
          handoff_rules: payload.handoff_rules,
        });
        const content = String(config.store_content ?? config.common_knowledge ?? "").trim();
        if (!/[A-Za-z0-9\u3400-\u9FFF]/.test(content)) return json(res, { detail: { code: "empty_content", message: "店铺与客服说明不能为空" } }, 400);
        scoped.value.config = { draft: config, published: { revision, published_at: "2026-08-26T10:00:00Z", config: structuredClone(config) }, status: "saved", revision };
        scoped.value.status = { ...scoped.value.status, enabled: true };
        fixtures.aiRequests.push({ method: "PUT", kind: "config", accountKey: scoped.accountKey, payload: structuredClone(payload) });
        return json(res, { ok: true, config: structuredClone(scoped.value.config) });
      }
      if (apiPath === "/api/bot/ai/templates" && req.method === "GET") {
        const scoped = scopedAi(req);
        return json(res, { templates: structuredClone(scoped.value.templates || []) });
      }
      if (apiPath === "/api/bot/ai/templates" && req.method === "POST") {
        const scoped = scopedAi(req);
        const name = String(payload.name || "").trim();
        if (!name) return json(res, { detail: "请输入模板名称" }, 400);
        const templates = scoped.value.templates || (scoped.value.templates = []);
        let saved = templates.find((item) => item.name.toLowerCase() === name.toLowerCase());
        if (saved) {
          saved.name = name;
          saved.config = structuredClone(payload.config || {});
          saved.updated_at = "2026-08-25T10:02:00Z";
        } else {
          saved = {
            id: `ai-tpl-${scoped.accountKey}-${templates.length + 1}`,
            name,
            config: structuredClone(payload.config || {}),
            created_at: "2026-08-25T10:01:00Z",
            updated_at: "2026-08-25T10:01:00Z",
          };
          templates.push(saved);
        }
        fixtures.aiRequests.push({ method: "POST", kind: "template", accountKey: scoped.accountKey, payload: structuredClone(payload) });
        return json(res, { ok: true, template: structuredClone(saved) });
      }
      const aiTemplateDeleteMatch = apiPath.match(/^\/api\/bot\/ai\/templates\/([^/]+)$/);
      if (aiTemplateDeleteMatch && req.method === "DELETE") {
        const scoped = scopedAi(req);
        const templateId = decodeURIComponent(aiTemplateDeleteMatch[1]);
        const templates = scoped.value.templates || (scoped.value.templates = []);
        const index = templates.findIndex((item) => String(item.id) === templateId);
        if (index < 0) return json(res, { detail: "客服模板不存在" }, 404);
        templates.splice(index, 1);
        fixtures.aiRequests.push({ method: "DELETE", kind: "template", accountKey: scoped.accountKey, templateId });
        return json(res, { ok: true });
      }
      if (apiPath === "/api/bot/ai/products" && req.method === "GET") {
        const scoped = scopedAi(req);
        return json(res, { products: structuredClone(scoped.value.products) });
      }
      const aiKnowledgeMatch = apiPath.match(/^\/api\/bot\/ai\/products\/([^/]+)\/knowledge$/);
      if (aiKnowledgeMatch && req.method === "GET") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiKnowledgeMatch[1]);
        const product = scoped.value.products.find((item) => String(item.item_id) === itemId);
        if (!product) return json(res, { detail: "商品不存在" }, 404);
        const current = scoped.value.knowledge[itemId] || {
          item_id: itemId,
          status: "unconfigured",
          revision: 0,
          draft: null,
          published: null,
          disabled: false,
          history: [],
          facts: structuredClone(product.facts),
        };
        return json(res, { knowledge: structuredClone(current) });
      }
      if (aiKnowledgeMatch && req.method === "PUT") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiKnowledgeMatch[1]);
        const current = scoped.value.knowledge[itemId] || { item_id: itemId, revision: 0 };
        const content = String(payload.content ?? payload.knowledge?.content ?? payload.knowledge?.summary ?? "").trim();
        if (!/[A-Za-z0-9\u3400-\u9FFF]/.test(content)) return json(res, { detail: { code: "empty_content", message: "商品补充内容不能为空" } }, 400);
        scoped.value.knowledge[itemId] = { item_id: itemId, status: "saved", knowledge_status: "saved", revision: Number(current.revision || 0) + 1, content, published: { content } };
        const product = scoped.value.products.find((item) => String(item.item_id) === itemId);
        if (product) product.knowledge_status = "saved";
        fixtures.aiRequests.push({ method: "PUT", kind: "knowledge", accountKey: scoped.accountKey, itemId, payload: structuredClone(payload) });
        const response = { ok: true, knowledge: structuredClone(scoped.value.knowledge[itemId]) };
        const delay = Number(fixtures.aiKnowledgeResponseDelays.shift() || 0);
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      const aiVersionsMatch = apiPath.match(/^\/api\/bot\/ai\/products\/([^/]+)\/versions$/);
      if (aiVersionsMatch && req.method === "GET") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiVersionsMatch[1]);
        return json(res, { versions: structuredClone(scoped.value.versions[itemId] || []) });
      }
      const aiExtractMatch = apiPath.match(/^\/api\/bot\/ai\/products\/([^/]+)\/extract$/);
      if (aiExtractMatch && req.method === "POST") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiExtractMatch[1]);
        const product = scoped.value.products.find((item) => String(item.item_id) === itemId);
        if (!product) return json(res, { detail: "商品不存在" }, 404);
        fixtures.aiRequests.push({ method: "POST", kind: "extract", accountKey: scoped.accountKey, itemId, payload: structuredClone(payload) });
        const queued = fixtures.aiExtractResponses.shift();
        const source = String(payload.content ?? payload.source_text ?? "").trim();
        const response = queued || { content: `适用人群：第一次使用该商品的买家。\n使用方式：${source}\n售后说明：遇到争议或无法确认的情况转人工。`, saved: false, active: false };
        const delay = Number(fixtures.aiExtractResponseDelays.shift() || 0);
        if (delay > 0) {
          setTimeout(() => json(res, structuredClone(response)), delay);
          return;
        }
        return json(res, structuredClone(response));
      }
      const aiPublishMatch = apiPath.match(/^\/api\/bot\/ai\/products\/([^/]+)\/publish$/);
      if (aiPublishMatch && req.method === "POST") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiPublishMatch[1]);
        const current = scoped.value.knowledge[itemId];
        if (!current || payload.confirm !== true) return json(res, { detail: "请先保存草稿并确认发布" }, 409);
        current.status = "published";
        current.published = structuredClone(current.draft);
        current.revision += 1;
        scoped.value.versions[itemId] = [{ revision: current.revision, status: "published", label: `已发布 revision ${current.revision}`, updated_at: "2026-08-24T10:05:00+08:00" }].concat(scoped.value.versions[itemId] || []);
        const product = scoped.value.products.find((item) => String(item.item_id) === itemId);
        if (product) product.knowledge_status = "published";
        fixtures.aiRequests.push({ method: "POST", kind: "publish", accountKey: scoped.accountKey, itemId, payload: structuredClone(payload) });
        return json(res, { ok: true, knowledge: structuredClone(current) });
      }
      const aiDisableMatch = apiPath.match(/^\/api\/bot\/ai\/products\/([^/]+)\/disable$/);
      if (aiDisableMatch && req.method === "POST") {
        const scoped = scopedAi(req);
        const itemId = decodeURIComponent(aiDisableMatch[1]);
        const current = scoped.value.knowledge[itemId] || { revision: 0, draft: { item_id: itemId } };
        current.status = "disabled";
        current.revision += 1;
        scoped.value.knowledge[itemId] = current;
        const product = scoped.value.products.find((item) => String(item.item_id) === itemId);
        if (product) product.knowledge_status = "disabled";
        fixtures.aiRequests.push({ method: "POST", kind: "disable", accountKey: scoped.accountKey, itemId, payload: structuredClone(payload) });
        return json(res, { ok: true, knowledge: structuredClone(current) });
      }
      if (apiPath === "/api/bot/ai/preview" && req.method === "POST") {
        const scoped = scopedAi(req);
        fixtures.aiPreviewRequests.push({ accountKey: scoped.accountKey, payload: structuredClone(payload) });
        const question = String(payload.current_question || payload.buyer_message || "").trim();
        const reply = question.includes("价格")
          ? "当前实时价格是 ¥6.5，具体以商品页面显示为准。"
          : question.includes("使用")
            ? "付款后可按商品补充内容中的步骤使用，遇到问题可以继续问我。"
            : question.includes("售后")
              ? "售后问题需要结合具体情况确认，如涉及退款或争议我会转人工处理。"
              : `我已收到你的当前问题：${question}`;
        const response = { reply, sources: ["realtime_facts", "store_content", "product_content", ...(Array.isArray(payload.history) && payload.history.length ? ["conversation"] : [])], knowledge_status: "saved", safety_status: "已通过安全检查" };
        const delay = Number(fixtures.aiPreviewResponseDelays.shift() || 0);
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/automation" && req.method === "GET") {
        const scoped = scopedFixture(req, "automation", fixtures.automation);
        const delay = takeLoaderDelay("automation", scoped.accountKey);
        if (delay > 0) {
          setTimeout(() => json(res, scoped.value), delay);
          return;
        }
        return json(res, scoped.value);
      }
      if (apiPath === "/api/automation" && req.method === "PUT") {
        const scoped = scopedFixture(req, "automation", fixtures.automation);
        const automation = scoped.value;
        fixtures.automationPuts.push({ accountKey: scoped.accountKey, payload: structuredClone(payload) });
        if (payload.rules !== undefined) {
          const invalidRule = !Array.isArray(payload.rules) || payload.rules.some((rule) => {
            const itemId = String(rule?.item_id || "");
            return !String(rule?.name || "").trim() || !Array.isArray(rule?.keywords) || !rule.keywords.length || !String(rule?.reply || "").trim() || (itemId && !/^\d+$/.test(itemId));
          });
          if (invalidRule) return json(res, { detail: "回复规则格式无效" }, 400);
          automation.rules = payload.rules;
        }
        if (payload.deliveries !== undefined) automation.deliveries = payload.deliveries;
        automation.strategy = payload.strategy || automation.strategy;
        automation.enabled = payload.enabled ?? automation.enabled;
        automation.first_reply = payload.first_reply ?? automation.first_reply;
        automation.fallback_reply = payload.fallback_reply ?? automation.fallback_reply;
        automation.delay_min_seconds = payload.delay_min_seconds ?? automation.delay_min_seconds;
        automation.delay_max_seconds = payload.delay_max_seconds ?? automation.delay_max_seconds;
        automation.trigger_cooldown_seconds = payload.trigger_cooldown_seconds ?? automation.trigger_cooldown_seconds;
        automation.manual_takeover_cooldown_seconds = payload.manual_takeover_cooldown_seconds ?? automation.manual_takeover_cooldown_seconds;
        automation.business_hours_enabled = payload.business_hours_enabled ?? automation.business_hours_enabled;
        automation.business_start = payload.business_start ?? automation.business_start;
        automation.business_end = payload.business_end ?? automation.business_end;
        if (payload.enabled === false) {
          const bot = scopedBot(req).value;
          if (bot.running) {
            fixtures.botStops.push({ accountKey: scoped.accountKey, mode: bot.automation_mode || "rules", reason: "automation_disabled" });
            bot.running = false;
            bot.running_total = 0;
          }
        }
        automation.rules_set = automation.rules.length > 0;
        automation.deliveries_set = automation.deliveries.length > 0;
        const response = { ok: true, automation: structuredClone(automation) };
        const delay = Number(fixtures.automationPutDelayMsByAccount[scoped.accountKey] || 0);
        delete fixtures.automationPutDelayMsByAccount[scoped.accountKey];
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/products/batch/preview" && req.method === "POST") {
        const itemIds = Array.isArray(payload.item_ids) ? payload.item_ids.map(String) : [];
        const byId = new Map(fixtures.automation.deliveries.map((item) => [String(item.item_id), item]));
        const changeCount = itemIds.filter((itemId) => {
          const current = byId.get(itemId);
          if (payload.enabled === false) return Boolean(current && current.enabled !== false);
          return !current || current.enabled === false || current.material !== payload.material;
        }).length;
        fixtures.batchPreviewToken = `batch-preview-${fixtures.batchPreviews.length + 1}`;
        fixtures.batchPreviews.push({ itemIds, enabled: payload.enabled !== false });
        return json(res, { ok: true, preview: {
          preview_token: fixtures.batchPreviewToken,
          selected_count: itemIds.length,
          change_count: changeCount,
          unchanged_count: itemIds.length - changeCount,
        } });
      }
      if (apiPath === "/api/bot/products/batch/commit" && req.method === "POST") {
        if (!fixtures.batchPreviewToken || payload.preview_token !== fixtures.batchPreviewToken) {
          return json(res, { detail: "商品或自动规则已变化，请重新检查" }, 409);
        }
        const itemIds = Array.isArray(payload.item_ids) ? payload.item_ids.map(String) : [];
        const selected = new Set(itemIds);
        const byId = new Map(fixtures.automation.deliveries.map((item) => [String(item.item_id), { ...item }]));
        for (const itemId of itemIds) {
          const current = byId.get(itemId);
          if (payload.enabled === false) {
            if (current) byId.set(itemId, { ...current, enabled: false });
          } else {
            byId.set(itemId, { item_id: itemId, enabled: true, delivery: "material", material: payload.material });
          }
        }
        fixtures.automation.deliveries = Array.from(byId.values());
        fixtures.automation.deliveries_set = fixtures.automation.deliveries.some((item) => item.enabled !== false);
        fixtures.batchCommits.push({ itemIds: Array.from(selected), enabled: payload.enabled !== false });
        fixtures.batchPreviewToken = "";
        return json(res, { ok: true, automation: fixtures.automation });
      }
      if (apiPath === "/api/bot/products" && req.method === "GET") {
        const scoped = scopedFixture(req, "products", fixtures.products);
        fixtures.productGetRequests.push(scoped.accountKey);
        const requestNumber = fixtures.productGetRequests.length;
        const response = { products: structuredClone(scoped.value) };
        const headers = { "x-ui-product-request": String(requestNumber) };
        const delay = takeLoaderDelay("products", scoped.accountKey);
        if (delay > 0) {
          setTimeout(() => json(res, response, 200, headers), delay);
          return;
        }
        return json(res, response, 200, headers);
      }
      if (apiPath === "/api/bot/templates" && req.method === "GET") {
        return json(res, { templates: fixtures.templates });
      }
      if (apiPath === "/api/bot/templates" && req.method === "PUT") {
        const template = payload.template && typeof payload.template === "object" ? { ...payload.template } : {};
        const scopedProducts = scopedFixture(req, "products", fixtures.products);
        const accountCatalog = fixtures.accountData[scopedProducts.accountKey]?.productCatalog;
        const validProducts = Array.isArray(accountCatalog) ? accountCatalog : scopedProducts.value;
        const validItemIds = new Set((Array.isArray(validProducts) ? validProducts : []).map((item) => String(item.id || "")));
        const submittedItemIds = Array.isArray(template.item_ids) ? template.item_ids.map(String) : [];
        if (submittedItemIds.some((itemId) => !validItemIds.has(itemId))) {
          return json(res, { detail: "商品配置只能绑定当前店铺已识别的商品" }, 400);
        }
        const existingIndex = fixtures.templates.findIndex((item) => String(item.id) === String(template.id || ""));
        const nextTemplateId = () => {
          const max = fixtures.templates.reduce((best, item) => {
            const match = String(item.id || "").match(/^tpl-(\d+)$/);
            return match ? Math.max(best, Number(match[1])) : best;
          }, 0);
          return `tpl-${max + 1}`;
        };
        const saved = existingIndex >= 0
          ? { ...fixtures.templates[existingIndex], ...template, id: fixtures.templates[existingIndex].id, item_count: Array.isArray(template.item_ids) ? template.item_ids.length : fixtures.templates[existingIndex].item_count }
          : { id: nextTemplateId(), ...template, enabled: template.enabled !== false, item_count: Array.isArray(template.item_ids) ? template.item_ids.length : 0 };
        if (existingIndex >= 0) fixtures.templates[existingIndex] = saved;
        else fixtures.templates = fixtures.templates.concat([saved]);
        fixtures.templateRequests.push({ method: "PUT", template: saved, accountKey: req.headers["x-shop-account"] || "default" });
        return json(res, { ok: true, template: saved });
      }
      const templateDeleteMatch = apiPath.match(/^\/api\/bot\/templates\/([^/]+)$/);
      if (templateDeleteMatch && req.method === "DELETE") {
        const id = decodeURIComponent(templateDeleteMatch[1]);
        const before = fixtures.templates.length;
        fixtures.templates = fixtures.templates.filter((item) => String(item.id) !== id);
        fixtures.templateRequests.push({ method: "DELETE", id, accountKey: req.headers["x-shop-account"] || "default" });
        return json(res, { ok: true, removed: before !== fixtures.templates.length });
      }
      if (apiPath === "/api/bot/cards" && req.method === "GET") {
        const scoped = scopedFixture(req, "cards", fixtures.cards);
        fixtures.cardGetRequests.push(scoped.accountKey);
        const response = structuredClone(scoped.value);
        const delay = takeLoaderDelay("cards", scoped.accountKey);
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/cards" && req.method === "PUT") {
        const name = String(payload.name || "").trim();
        const note = String(payload.note || "").trim();
        const codes = Array.isArray(payload.codes) ? payload.codes : [];
        const newCodes = codes.filter((item) => item && String(item.code || "").trim()).length;
        const pool = {
          id: fixtures.cards.pool.id,
          name,
          note,
          total: fixtures.cards.pool.total + newCodes,
          available: fixtures.cards.pool.available + newCodes,
          used: fixtures.cards.pool.used,
          enabled: true,
        };
        const stats = {
          pools: 1,
          total: pool.total,
          available: pool.available,
          reserved: fixtures.cards.stats.reserved,
          used: pool.used,
        };
        fixtures.cards = { pool, stats };
        fixtures.cardRequests.push({ name, note, codes, accountKey: req.headers["x-shop-account"] || "default" });
        return json(res, { ok: true, pool, stats });
      }
      if (apiPath === "/api/bot/cookies" && req.method === "PUT") {
        if (fixtures.cookieFailureCode) {
          const code = fixtures.cookieFailureCode;
          const details = {
            risk_control: { message: "闲鱼需要安全验证，请先在浏览器完成安全验证后再试", label: "需要安全验证" },
            cookie_expired: { message: "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie", label: "Cookie 已失效" },
            account_restricted: { message: "闲鱼限制了当前账号的部分操作，暂时不能发布商品", label: "部分能力受限" },
            sync_cooldown: { message: "操作太频繁，请稍后再检测", label: "操作太频繁" },
          }[code] || { message: "Cookie 检测失败，请稍后重试", label: "检测失败" };
          return json(res, { detail: { code, message: details.message, label: details.label } }, code === "risk_control" || code === "cookie_expired" || code === "account_restricted" ? 422 : 400);
        }
        fixtures.cookieSaves += 1;
        fixtures.bot.cookies_set = true;
        fixtures.bot.connected = true;
        fixtures.bot.sync_status = "verified";
        fixtures.bot.cookie_status = { code: "verified", label: "已验证", message: "Cookie 已验证（不显示内容）", action: "可随时重新检测店铺商品" };
        fixtures.bot.last_sync_at = "2026-08-15T10:01:00+0800";
        return json(res, { ok: true, connected: true, shop_name: fixtures.bot.shop_name, product_count: fixtures.products.length });
      }
      if (apiPath === "/api/bot/shop/sync" && req.method === "POST") {
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const account = accountForRequest(req, accountKey);
        fixtures.shopActionRequests.push({ action: "check", key: accountKey });
        if (fixtures.cookieFailureCode) {
          const code = fixtures.cookieFailureCode;
          const details = {
            risk_control: { message: "闲鱼需要安全验证，请先在浏览器完成安全验证后再试", label: "需要安全验证" },
            cookie_expired: { message: "Cookie 已失效，请重新登录闲鱼后复制完整 Cookie", label: "Cookie 已失效" },
            account_restricted: { message: "闲鱼限制了当前账号的部分操作，暂时不能发布商品", label: "部分能力受限" },
            sync_cooldown: { message: "操作太频繁，请稍后再检测", label: "操作太频繁" },
          }[code] || { message: "Cookie 检测失败，请稍后重试", label: "检测失败" };
          fixtures.bot.connected = false;
          fixtures.bot.sync_status = code;
          fixtures.bot.cookie_status = { code, label: details.label, message: details.message, action: "处理后重新检测" };
          if (account) {
            account.status = code === "account_restricted" ? "restricted" : code === "cookie_expired" ? "expired" : "degraded";
            account.last_error_code = code;
          }
          return json(res, { detail: { code, message: details.message, label: details.label } }, code === "risk_control" || code === "cookie_expired" || code === "account_restricted" ? 422 : 400);
        }
        fixtures.bot.connected = true;
        fixtures.bot.sync_status = "verified";
        fixtures.bot.cookie_status = { code: "verified", label: "已验证", message: "Cookie 已验证（不显示内容）", action: "可随时重新检测店铺商品" };
        fixtures.bot.auth_code = "ok";
        fixtures.bot.auth_phase = "WS_REGISTERED";
        fixtures.bot.needs_human = false;
        fixtures.bot.reauthorization_required = false;
        if (account) {
          account.status = "ready";
          account.last_error_code = "";
          account.last_verified_at = "2026-08-15T10:03:00+0800";
          account.last_sync_at = "2026-08-15T10:03:00+0800";
        }
        return json(res, { ok: true, connected: true, shop_name: fixtures.bot.shop_name, product_count: fixtures.products.length });
      }
      const readConversationMatch = apiPath.match(/^\/api\/bot\/conversations\/([^/]+)\/read$/);
      if (readConversationMatch && req.method === "POST") {
        const chatId = decodeURIComponent(readConversationMatch[1]);
        const scoped = scopedFixture(req, "conversations", fixtures.conversations);
        const conversation = scoped.value.find((item) => item.chat_id === chatId);
        if (!conversation) return json(res, { detail: { code: "not_found", message: "会话不存在" } }, 404);
        fixtures.inboxReadCommands.push(chatId);
        const updated = { ...conversation, unread: payload.read === false, unread_count: payload.read === false ? 1 : 0 };
        const delay = Number(fixtures.inboxReadDelayMs || 0);
        fixtures.inboxReadDelayMs = 0;
        if (delay > 0) {
          setTimeout(() => json(res, { ok: true, conversation: updated }), delay);
          return;
        }
        Object.assign(conversation, updated);
        return json(res, { ok: true, conversation: updated });
      }
      const takeoverConversationMatch = apiPath.match(/^\/api\/bot\/conversations\/([^/]+)\/takeover$/);
      if (takeoverConversationMatch && req.method === "POST") {
        const chatId = decodeURIComponent(takeoverConversationMatch[1]);
        const scoped = scopedFixture(req, "conversations", fixtures.conversations);
        const conversation = scoped.value.find((item) => item.chat_id === chatId);
        if (!conversation) return json(res, { detail: { code: "not_found", message: "会话不存在" } }, 404);
        fixtures.inboxTakeoverCommands.push({ chatId, enabled: payload.enabled !== false });
        const updated = { ...conversation, manual_mode: payload.enabled !== false };
        const delay = Number(fixtures.inboxTakeoverDelayMs || 0);
        const mode = fixtures.inboxTakeoverMode;
        fixtures.inboxTakeoverDelayMs = 0;
        fixtures.inboxTakeoverMode = "success";
        if (mode === "failure") {
          fixtures.inboxTakeoverFailures += 1;
          const reject = () => json(res, { detail: { code: "takeover_unavailable", message: "人工接管切换失败" } }, 503);
          if (delay > 0) setTimeout(reject, delay);
          else reject();
          return;
        }
        if (delay > 0) {
          setTimeout(() => json(res, { ok: true, conversation: updated }), delay);
          return;
        }
        Object.assign(conversation, updated);
        return json(res, { ok: true, conversation: updated });
      }
      if (apiPath === "/api/bot/quick-replies" && req.method === "GET") {
        const scoped = scopedFixture(req, "quickReplies", fixtures.quickReplies);
        return json(res, { quick_replies: structuredClone(scoped.value) });
      }
      if (apiPath === "/api/bot/quick-replies" && req.method === "PUT") {
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const quickReplies = Array.isArray(payload.quick_replies) ? structuredClone(payload.quick_replies) : [];
        fixtures.quickReplyRequests.push({ accountKey, quickReplies });
        if (fixtures.accountData[accountKey]) fixtures.accountData[accountKey].quickReplies = quickReplies;
        else fixtures.quickReplies = quickReplies;
        return json(res, { ok: true, quick_replies: quickReplies });
      }
      if (apiPath === "/api/bot/conversations" && req.method === "GET") {
        const scoped = scopedFixture(req, "conversations", fixtures.conversations);
        const search = String(url.searchParams.get("search") || "").trim().toLowerCase();
        const conversations = search ? scoped.value.filter((item) => [item.buyer_label, item.preview, item.item_id].join(" ").toLowerCase().includes(search)) : scoped.value;
        return json(res, { conversations });
      }
      if (apiPath === "/api/bot/messages" && req.method === "GET") {
        const selected = url.searchParams.get("chat_id");
        const search = String(url.searchParams.get("search") || "").trim().toLowerCase();
        const messages = selected ? fixtures.messages.filter((item) => item.chat_id === selected) : fixtures.messages.filter((item) => item.chat_id === "chat-2");
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const allMessages = [...messages, ...fixtures.manualReplies.filter((item) => item.account_key === accountKey && (!selected || item.chat_id === selected))];
        const matchedMessages = search ? allMessages.filter((item) => String(item.content || "").toLowerCase().includes(search)).map((item) => ({ ...item, matched: true })) : allMessages;
        const response = { messages: matchedMessages, match_count: search ? matchedMessages.length : 0, search };
        fixtures.messageRequests.push({ chatId: selected || "", accountKey, search });
        const delay = Number(fixtures.messageResponseDelayMsByChat[selected] || 0);
        delete fixtures.messageResponseDelayMsByChat[selected];
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/messages/image" && req.method === "POST") {
        fixtures.manualImageRequests.push({
          chatId: String(url.searchParams.get("chat_id") || ""),
          contentType,
          fileName: String(req.headers["x-file-name"] || ""),
          bytes: Buffer.byteLength(rawBody),
        });
        return json(res, {
          ok: true,
          media: {
            type: "image",
            url: "",
            path: "manual_reply_test.jpg",
            alt: "测试图片",
            label: "测试图片",
            name: "reply.jpg",
            mime: "image/jpeg",
          },
        });
      }
      if (apiPath === "/api/bot/messages/reply" && req.method === "POST") {
        const selected = payload.chat_id || "chat-2";
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const replyId = String(req.headers["idempotency-key"] || "");
        const message = { role: "assistant_manual", content: payload.content, content_type: Array.isArray(payload.media) && payload.media.length ? "image" : "text", media: Array.isArray(payload.media) ? payload.media : [], time: "2026-08-15 15:20", chat_id: selected, item_id: selected === "chat-1" ? "100001" : "100002", reply_id: replyId, outbox_id: fixtures.manualReplies.length + 1, delivery_status: "queued", status: "queued", account_key: accountKey };
        fixtures.manualReplyRequests.push({ chatId: selected, content: payload.content, media: Array.isArray(payload.media) ? payload.media : [], replyId, accountKey });
        const delay = Number(fixtures.manualReplyPostDelayMs || 0);
        fixtures.manualReplyPostDelayMs = 0;
        if (fixtures.manualReplyPostMode === "failure") {
          fixtures.manualReplyPostFailures += 1;
          const reject = () => json(res, { detail: { code: "reply_unavailable", message: "回复暂时无法提交，请稍后重试" } }, 503);
          if (delay > 0) setTimeout(reject, delay);
          else reject();
          return;
        }
        fixtures.manualReplies.push(message);
        fixtures.manualReplyPollModes.set(replyId, fixtures.manualReplyPollMode);
        const accept = () => json(res, { ok: true, accepted: true, saved: true, delivered: false, platform_acknowledged: false, reply: { reply_id: replyId, status: "queued", attempts: 0, platform_acknowledged: false }, message });
        if (delay > 0) setTimeout(accept, delay);
        else accept();
        return;
      }
      const manualReplyStatusMatch = apiPath.match(/^\/api\/bot\/messages\/reply\/([^/]+)$/);
      if (manualReplyStatusMatch && req.method === "GET") {
        const replyId = decodeURIComponent(manualReplyStatusMatch[1]);
        const message = fixtures.manualReplies.find((item) => item.reply_id === replyId);
        if (!message) return json(res, { detail: "not found" }, 404);
        const mode = fixtures.manualReplyPollModes.get(replyId) || "success";
        if (mode === "not_found") {
          fixtures.manualReplyPollNotFoundResponses += 1;
          return json(res, { detail: "not found" }, 404);
        }
        fixtures.manualReplyPolls += 1;
        const replyPolls = Number(fixtures.manualReplyPollsById.get(replyId) || 0) + 1;
        fixtures.manualReplyPollsById.set(replyId, replyPolls);
        message.delivery_status = mode === "manual_review" ? "manual_review" : mode === "pending" ? "sending" : replyPolls === 1 ? "retry" : "acknowledged";
        message.status = message.delivery_status;
        return json(res, { reply: { reply_id: replyId, status: message.delivery_status, attempts: replyPolls, platform_acknowledged: message.delivery_status === "acknowledged" } });
      }
      if (apiPath === "/api/bot/orders" && req.method === "GET") {
        const scoped = scopedFixture(req, "orders", fixtures.orders);
        const response = { orders: scoped.value };
        const delay = takeLoaderDelay("orders", scoped.accountKey);
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/start" && req.method === "POST") {
        const scoped = scopedBot(req);
        const mode = payload.mode === "rules_ai" ? "rules_ai" : "rules";
        fixtures.botStartModes.push({ accountKey: scoped.accountKey, mode });
        scoped.value.running = true;
        scoped.value.running_total = 1;
        scoped.value.automation_mode = mode;
        return json(res, { ok: true, reason: "started" });
      }
      if (apiPath === "/api/bot/stop" && req.method === "POST") {
        const scoped = scopedBot(req);
        fixtures.botStops.push({ accountKey: scoped.accountKey, mode: scoped.value.automation_mode || "rules", reason: "explicit" });
        scoped.value.running = false;
        scoped.value.running_total = 0;
        return json(res, { ok: true, reason: "stopped" });
      }
      return json(res, { detail: "not found" }, 404);
    });
  });
}

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server.address().port;
}

async function close(server) {
  await new Promise((resolve) => server.close(resolve));
}

async function assertNoOverflow(page, label) {
  const result = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, bodyWidth: document.body.scrollWidth, scrollX, scrollLeft: document.scrollingElement?.scrollLeft || 0 }));
  const offenders = result.scrollWidth <= result.width && result.bodyWidth <= result.width ? [] : await page.evaluate(() => Array.from(document.querySelectorAll("*"))
    .map((node) => ({ node, rect: node.getBoundingClientRect() }))
    .filter(({ rect }) => rect.right > innerWidth + 1 || rect.left < -1)
    .sort((a, b) => Math.max(b.rect.right - innerWidth, -b.rect.left) - Math.max(a.rect.right - innerWidth, -a.rect.left))
    .slice(0, 8)
    .map(({ node, rect }) => ({ tag: node.tagName, id: node.id, className: String(node.className || "").slice(0, 100), left: Math.round(rect.left), right: Math.round(rect.right), width: Math.round(rect.width), hidden: node.hidden })));
  assert.ok(result.scrollWidth <= result.width && result.bodyWidth <= result.width && result.scrollX === 0, `${label} must not overflow: ${JSON.stringify({ ...result, offenders })}`);
}

async function measuredContrast(page, selector, pseudo = "") {
  return page.locator(selector).evaluate((node, pseudoSelector) => {
    const channels = (value) => {
      const match = String(value || "").match(/[\d.]+/g) || [];
      return [Number(match[0] || 0), Number(match[1] || 0), Number(match[2] || 0), match[3] == null ? 1 : Number(match[3])];
    };
    const luminance = (rgb) => {
      const linear = rgb.slice(0, 3).map((channel) => {
        const value = channel / 255;
        return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    };
    const foreground = channels(getComputedStyle(node, pseudoSelector || null).color);
    let background = [255, 255, 255, 1];
    for (let current = node; current; current = current.parentElement) {
      const candidate = channels(getComputedStyle(current).backgroundColor);
      if (candidate[3] > 0) {
        background = candidate;
        break;
      }
    }
    const lighter = Math.max(luminance(foreground), luminance(background));
    const darker = Math.min(luminance(foreground), luminance(background));
    return { ratio: (lighter + 0.05) / (darker + 0.05), foreground, background };
  }, pseudo);
}

async function waitForPanelSettled(page) {
  await page.evaluate(async () => {
    const targets = [
      document.querySelector('[data-panel]:not([hidden])'),
      document.querySelector("#sidebar"),
      document.querySelector(".sidebar-scrim"),
    ].filter(Boolean);
    const animations = targets.flatMap((node) => node.getAnimations()).filter((animation) => {
      const endTime = animation.effect?.getComputedTiming?.().endTime;
      return Number.isFinite(endTime);
    });
    await Promise.all(animations.map((animation) => animation.finished.catch(() => undefined)));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function captureScreenshot(page, options) {
  const viewport = page.viewportSize();
  if (viewport) await page.mouse.move(Math.max(1, viewport.width - 2), Math.max(1, viewport.height - 2));
  await page.locator("#toastRegion .toast").last().waitFor({ state: "detached", timeout: 4000 }).catch(() => undefined);
  await waitForPanelSettled(page);
  await page.screenshot(options);
}

async function openView(page, view) {
  await page.evaluate((targetView) => document.querySelector(`[data-view="${targetView}"]`)?.click(), view);
  await page.waitForSelector(`[data-panel="${view}"]:not([hidden])`);
}

async function run() {
  const server = createServer();
  const port = await listen(server);
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  const failedResponses = [];
  let expectedCookieProbeConsole = 0;
  let expectedCookieProbeResponses = 0;
  let expectedQrFailureConsole = 0;
  let expectedQrFailureResponses = 0;
  let expectedQrStageFailureConsole = 0;
  let expectedQrStageFailureResponses = 0;
  let expectedQrStageCancelConsole = 0;
  let expectedQrStageCancelResponses = 0;
  let expectedManualReplyFailureConsole = 0;
  let expectedManualReplyFailureResponses = 0;
  let expectedManualReplyNotFoundConsole = 0;
  let expectedManualReplyNotFoundResponses = 0;
  try {
    const bootstrapToken = "bootstrap-ui-contract-token-0123456789abcdef";
    fixtures.authCapabilities = { registration_enabled: false, bootstrap_available: true, password_min_length: 12 };
    const bootstrapPage = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    await bootstrapPage.goto(`http://127.0.0.1:${port}/xianyu-saas/`, { waitUntil: "networkidle" });
    assert.equal(await bootstrapPage.locator("#bootstrapTab").isVisible(), true, "trusted first-admin state must expose bootstrap only");
    assert.equal(await bootstrapPage.locator("#registerTab").isVisible(), false, "bootstrap must not imply public registration");
    await bootstrapPage.click("#bootstrapTab");
    assert.match(await bootstrapPage.locator("#authTitle").textContent(), /首个管理员/);
    await bootstrapPage.fill("#authUsername", "bootstrap-admin");
    await bootstrapPage.fill("#authPassword", "Bootstrap-Pass-123!");
    await bootstrapPage.fill("#bootstrapToken", bootstrapToken);
    await bootstrapPage.click("#authSubmit");
    await bootstrapPage.waitForFunction(() => document.querySelector("#authTitle")?.textContent === "登录工作台");
    assert.equal(fixtures.bootstrapRequests.length, 1, "bootstrap UI must submit exactly once");
    assert.deepEqual(fixtures.bootstrapRequests[0].payload, { username: "bootstrap-admin", password: "Bootstrap-Pass-123!" });
    assert.equal(fixtures.bootstrapRequests[0].token, bootstrapToken, "bootstrap token must use the dedicated header");
    assert.equal(fixtures.bootstrapRequests[0].browserIntent, "browser-write", "bootstrap remains subject to browser write checks");
    assert.equal(fixtures.bootstrapRequests[0].url.includes(bootstrapToken), false, "bootstrap token must never enter the URL");
    assert.equal(JSON.stringify(fixtures.bootstrapRequests[0].payload).includes(bootstrapToken), false, "bootstrap token must never enter JSON");
    assert.equal((await bootstrapPage.locator("body").innerHTML()).includes(bootstrapToken), false, "bootstrap token must be cleared from rendered DOM");
    await bootstrapPage.close();
    fixtures.authCapabilities = { registration_enabled: true, bootstrap_available: false, password_min_length: 12 };

    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    await page.route("https://cdn.example/**", (route) => route.fulfill({
      status: 200,
      contentType: "image/png",
      body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
    }));
    page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
    page.on("console", (message) => {
      const expectedAnonymousProbe = message.type() === "error" && message.text().includes("status of 401");
      const expectedCookieProbe = message.type() === "error"
        && (message.text().includes("status of 400") || message.text().includes("status of 422"))
        && expectedCookieProbeConsole < expectedCookieProbeResponses;
      const expectedQrFailure = message.type() === "error"
        && message.text().includes("status of 503")
        && expectedQrFailureConsole < fixtures.qrSyncFailures;
      const expectedQrStageFailure = message.type() === "error"
        && message.text().includes("status of 502")
        && expectedQrStageFailureConsole < fixtures.qrStageFailures;
      const expectedQrStageCancel = message.type() === "error"
        && message.text().includes("status of 404")
        && expectedQrStageCancelConsole < fixtures.qrStageCancelNotFound;
      const expectedManualReplyFailure = message.type() === "error"
        && message.text().includes("status of 503")
        && expectedManualReplyFailureConsole < fixtures.manualReplyPostFailures;
      const expectedManualReplyNotFound = message.type() === "error"
        && message.text().includes("status of 404")
        && expectedManualReplyNotFoundConsole < fixtures.manualReplyPollNotFoundResponses;
      if (expectedCookieProbe) expectedCookieProbeConsole += 1;
      if (expectedQrFailure) expectedQrFailureConsole += 1;
      if (expectedQrStageFailure) expectedQrStageFailureConsole += 1;
      if (expectedQrStageCancel) expectedQrStageCancelConsole += 1;
      if (expectedManualReplyFailure) expectedManualReplyFailureConsole += 1;
      if (expectedManualReplyNotFound) expectedManualReplyNotFoundConsole += 1;
      if (message.type() === "error" && !expectedAnonymousProbe && !expectedCookieProbe && !expectedQrFailure && !expectedQrStageFailure && !expectedQrStageCancel && !expectedManualReplyFailure && !expectedManualReplyNotFound) errors.push(`console: ${message.text()}`);
    });
    page.on("response", (response) => {
      const expectedAnonymousProbe = response.status() === 401 && response.url().endsWith("/api/me");
      const expectedCookieProbe = fixtures.cookieFailureCode
        && response.status() >= 400
        && response.url().endsWith("/api/bot/shop/sync");
      const expectedQrFailure = response.status() === 503 && response.url().endsWith("/api/bot/login/complete");
      const expectedQrStageFailure = response.status() === 502
        && response.url().includes("/api/bot/login/")
        && response.url().endsWith("/status");
      const expectedQrStageCancel = response.status() === 404
        && response.url().includes("/api/bot/login/")
        && response.url().endsWith("/cancel");
      const expectedManualReplyFailure = response.status() === 503 && response.url().endsWith("/api/bot/messages/reply");
      const expectedManualReplyNotFound = response.status() === 404 && response.url().includes("/api/bot/messages/reply/");
      if (expectedCookieProbe) expectedCookieProbeResponses += 1;
      if (expectedQrFailure) expectedQrFailureResponses += 1;
      if (expectedQrStageFailure) expectedQrStageFailureResponses += 1;
      if (expectedQrStageCancel) expectedQrStageCancelResponses += 1;
      if (expectedManualReplyFailure) expectedManualReplyFailureResponses += 1;
      if (expectedManualReplyNotFound) expectedManualReplyNotFoundResponses += 1;
      if (response.status() >= 400 && !expectedAnonymousProbe && !expectedCookieProbe && !expectedQrFailure && !expectedQrStageFailure && !expectedQrStageCancel && !expectedManualReplyFailure && !expectedManualReplyNotFound) failedResponses.push(`${response.status()} ${response.url()}`);
    });

    await page.goto(`http://127.0.0.1:${port}/xianyu-saas/`, { waitUntil: "networkidle" });
    assert.deepEqual(errors, [], `initial page must bind without runtime errors: ${errors.join(" | ")}`);
    assert.equal(await page.locator("#introCurtain").count(), 0, "startup curtain must be removed from the first-load DOM");
    assert.equal(await page.locator("#enterWorkspaceButton").count(), 0, "startup entry button must be removed");
    assert.equal(await page.locator("#authScreen").isVisible(), true, "login screen should be visible without a startup curtain");
    await page.click("#registerTab");
    assert.match(await page.locator("#authTitle").textContent(), /创建/, "register tab must switch the auth title");
    await page.click("#loginTab");
    await assertNoOverflow(page, "desktop login");
    await page.fill("#authUsername", "owner-demo");
    await page.fill("#authPassword", "password-123");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await page.waitForSelector('[data-panel="home"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#homeStatCards .stat-card").length === 4 && document.querySelectorAll("#homeProductGrid .home-product-card").length === 6);

    // The self-use dashboard exposes all existing operations without subscription UI.
    assert.equal(await page.locator("#headerPlanBadge, #membershipCurrentBadge, #vipNavButton, #chatAiUpgrade, [data-panel=vip]").count(), 0, "membership and upgrade controls must not exist");
    assert.equal(await page.locator("#accountTabs").getAttribute("aria-label"), "当前店铺：海风数字店", "topbar tabs must expose the active shop");
    assert.equal(await page.locator("#accountTabs .account-tab.is-active .account-tab-name").textContent(), "海风数字店");
    assert.deepEqual(await page.locator("#sideNav .side-nav-item").allTextContents(), ["运营概览", "智能客服", "履约中心", "订单管理"], "primary navigation is grouped into business domains");
    assert.equal(await page.locator('#sideNav [data-view="chat"], #sideNav [data-view="goods"], #sideNav [data-view="orders"]').count(), 3, "chat, fulfillment and orders remain primary owner tools");
    assert.equal(await page.locator('[data-panel="chat"]:not([hidden]), [data-panel="goods"]:not([hidden]), [data-panel="orders"]:not([hidden])').count(), 0, "inactive panels stay hidden while the dashboard is active");
    assert.deepEqual(await page.locator(".sidebar-bottom [data-view] .side-nav-tooltip").allTextContents(), ["店铺管理", "项目说明"], "shop and documentation domains stay in the sidebar footer");
    assert.equal(await page.locator("#logoutButton").count(), 1, "sidebar footer keeps only the logout action");
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".side-nav")).overflowY), "visible", "the left navigation must never scroll");
    assert.ok(await page.evaluate(() => {
      const nav = document.querySelector(".side-nav");
      return nav && nav.scrollHeight <= nav.clientHeight + 1;
    }), "the left navigation must fit without a scrollable overflow");
    const statLabels = await page.locator("#homeStatCards .stat-card-label").allTextContents();
    const statValues = await page.locator("#homeStatCards .stat-card-value").allTextContents();
    assert.deepEqual(statLabels, ["买家咨询总数", "自动回复率", "履约自动发送", "异常与待办"]);
    assert.deepEqual(statValues, ["5", "60%", "2 笔", "1 项"], "self-use owners see the operations dashboard");
    assert.equal(await page.locator("#homeStatCards .stat-card-sub").count(), 4, "every stat card keeps a compact context line");
    assert.equal(await page.locator(".overview-grid-2col > .card-section").count(), 2, "overview keeps the trend and risk modules side by side");
    assert.equal(await page.locator("#analyticsChart .chart-bar").count(), fixtures.analytics.buckets.length, "overview renders the analytics trend");
    assert.deepEqual(await page.locator("#homeProductGrid .home-product-name").allTextContents(), fixtures.products.slice(0, 6).map((item) => item.title), "home shows up to six featured products");
    const visibleText = await page.locator("body").innerText();
    for (const removed of ["会员服务", "选择套餐", "会员权益", "立即开通", "开通 AI 客服", "续费", "升级", "模板管理", "兑换码", "卡券管理", "账号、连接状态和店铺操作集中在一处", "已识别的商品会自动整理成列表", "商品信息会自动整理，不需要填写复杂配置"]) {
      assert.equal(visibleText.includes(removed), false, `${removed} must be removed from the self-use workspace`);
    }
    assert.equal(await page.evaluate(() => localStorage.getItem("whale_token")), null, "access token must not use localStorage");
    assert.equal(await page.locator('[data-panel="home"] .page-head-copy p').count(), 0, "page headers keep a clean title without annotation microcopy");
    assert.equal(await page.locator('[data-panel="home"] .section-title p').count(), 0, "section titles keep a clean heading without annotation microcopy");

    // 项目说明保持为单一连续说明书，正文只保留三个小节，作者链接固定在文末页脚。
    await openView(page, "docs");
    await waitForPanelSettled(page);
    assert.equal(await page.locator(".docs-manual").count(), 1, "project docs must use one manual surface");
    assert.deepEqual(await page.locator(".docs-manual-section h3").allTextContents(), ["一、项目概览", "二、日常操作与异常处理", "三、技术与安全边界"], "project docs must keep exactly three body sections");
    assert.equal(await page.locator(".docs-manual-section").count(), 3, "project docs must not fragment into many feature panels");
    assert.deepEqual(await page.locator("#docsBusinessDomains tbody th").allTextContents(), ["运营概览", "智能客服", "履约中心", "订单管理", "店铺管理", "项目说明"], "six business domains must remain a compact manual table");
    assert.equal(await page.locator(".docs-github-card, .docs-grid-cards, .docs-card, .btn-github-portal, .docs-manual-notice, .docs-manual-kicker").count(), 0, "legacy documentation card layout must be removed");
    // 正式文档版式：外层不再是带阴影的卡片，标题和页脚使用实线分隔而不是色块。
    assert.deepEqual(await page.evaluate(() => {
      const manual = getComputedStyle(document.querySelector(".docs-manual"));
      return [manual.boxShadow, manual.borderTopWidth];
    }), ["none", "0px"], "the manual must read as a flat document instead of a stacked card");
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".docs-manual-footer")).backgroundColor), "rgba(0, 0, 0, 0)", "the closing signature must not become another tinted card");
    assert.equal(await page.locator(".docs-manual-table").count(), 2, "supporting facts stay in lightweight manual tables");
    assert.equal(await page.locator(".docs-manual-table caption").count(), 2, "every manual table keeps a caption");
    assert.equal(await page.locator(".docs-manual > .docs-manual-footer").evaluate((footer) => footer === footer.parentElement.lastElementChild), true, "author links must stay in the final manual footer");
    for (const [selector, href] of [["#docsAuthorGithub", "https://github.com/tswawa"], ["#docsProjectGithub", "https://github.com/tswawa/xianyu-saas"]]) {
      const link = page.locator(selector);
      assert.equal(await link.getAttribute("href"), href, `${selector} must point to the expected GitHub destination`);
      assert.equal(await link.getAttribute("target"), "_blank", `${selector} must open in a new tab`);
      assert.equal(await link.getAttribute("rel"), "noopener noreferrer", `${selector} must use a safe external-link policy`);
    }
    assert.equal(await page.locator(".docs-manual-footer").textContent().then((value) => value.includes("作者 GitHub") && value.includes("项目仓库")), true, "manual footer must label author and project links");
    // 作者信息只出现在文末，不能回到首屏或页头横幅。
    assert.equal(await page.locator('[data-panel="docs"] .page-head a[href*="github.com"], .docs-manual-head a[href*="github.com"]').count(), 0, "author links must not return to the top banner");
    assert.ok(await page.evaluate(() => {
      const manual = document.querySelector(".docs-manual");
      const footer = document.querySelector(".docs-manual-footer");
      return footer.getBoundingClientRect().top > manual.getBoundingClientRect().top + manual.getBoundingClientRect().height * 0.6;
    }), "the signature block must sit at the end of the manual");
    await assertNoOverflow(page, "project manual desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "docs-manual-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "project manual mobile");
    // 移动端表格改为分块堆叠，说明书正文不得出现横向滚动。
    assert.ok(await page.evaluate(() => Array.from(document.querySelectorAll(".docs-manual-table")).every((table) => table.scrollWidth <= table.clientWidth + 1)), "manual tables must not scroll horizontally at 390px");
    // 表格标题在窄视口必须保持横向排版，不能被压成逐字竖排。
    assert.ok(await page.evaluate(() => Array.from(document.querySelectorAll(".docs-manual-table-caption")).every((caption) => {
      const box = caption.getBoundingClientRect();
      const line = parseFloat(getComputedStyle(caption).fontSize) * 2.4;
      return box.width > 120 && box.height < line;
    })), "manual table captions must stay on a single horizontal line at 390px");
    await captureScreenshot(page, { path: path.join(resultRoot, "docs-manual-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });

    // All signed-in owners can inspect version metadata and change their own password,
    // while platform account, audit and update actions remain hidden.
    assert.equal(await page.locator('[data-docs-tab="accounts"]').isVisible(), false, "owners must not see platform account administration");
    assert.equal(await page.locator('[data-docs-tab="audit"]').isVisible(), false, "owners must not see platform audit records");
    await page.click('[data-docs-tab="version"]');
    await page.waitForSelector('[data-docs-panel="version"]:not([hidden])');
    await page.waitForFunction(() => document.querySelector("#currentVersionValue")?.textContent === "v0.1.0");
    assert.equal(await page.locator("#currentAssetVersionValue").textContent(), "20260831-01");
    assert.equal(await page.locator("#adminUpdateControls").isVisible(), false, "owners can read releases but cannot operate updates");
    assert.match(await page.locator("#versionReleaseNotes").textContent(), /<img src=x onerror=/, "release notes remain literal text");
    assert.equal(await page.locator("#versionReleaseNotes img, #versionReleaseNotes script").count(), 0, "release notes must not create executable nodes");
    assert.notEqual(await page.evaluate(() => window.__releaseNotesInjected), true, "release notes must never execute markup");
    await page.fill("#currentPasswordInput", "password-123");
    await page.fill("#newPasswordInput", "Owner-New-Pass-123!");
    const passwordChangeResponse = page.waitForResponse((response) => response.url().endsWith("/api/auth/password") && response.request().method() === "POST");
    await page.click('#passwordChangeForm button[type="submit"]');
    assert.equal((await passwordChangeResponse).status(), 200, "password change must reach the authenticated API");
    await page.waitForFunction(() => document.querySelector("#passwordChangeMessage")?.textContent.includes("其他会话已撤销"));
    assert.deepEqual(fixtures.passwordRequests.at(-1), { current_password: "password-123", new_password: "Owner-New-Pass-123!" });
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "owner version and password mobile");
    await page.setViewportSize({ width: 1440, height: 900 });

    await openView(page, "home");
    await page.waitForSelector('[data-panel="home"]:not([hidden])');
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "dashboard after project manual");

    await assertNoOverflow(page, "free dashboard desktop");
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.waitForTimeout(220);
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".side-nav")).overflowY), "visible", "the left navigation must never scroll on laptop viewports");
    assert.ok(await page.evaluate(() => {
      const nav = document.querySelector(".side-nav");
      return nav && nav.scrollHeight <= nav.clientHeight + 1;
    }), "the left navigation must fit at 1366x768 without overflow");
    await assertNoOverflow(page, "free dashboard 1366x768");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(220);
    await waitForPanelSettled(page);
    await captureScreenshot(page, { path: path.join(resultRoot, "dashboard-free-desktop.png"), fullPage: true });

    // Free owners can manage 发货模板: list renders, create/update/delete hit the API.
    assert.equal(await page.locator('[data-panel="goods"] [data-view="templates"]').count(), 1, "fulfillment tabs keep a templates entry");
    assert.equal(await page.locator('[data-panel="goods"] [data-view="cards"]').count(), 1, "fulfillment tabs keep a cards entry");
    await openView(page, "templates");
    await page.waitForFunction((count) => document.querySelectorAll("#templateGrid .template-card").length === count, fixtures.templates.length);
    assert.match(await page.locator('[data-panel="templates"] .page-head-copy h1').textContent(), /自动化履约中心/);
    assert.equal(await page.locator('[data-panel="templates"] [data-view="templates"]').getAttribute("aria-selected"), "true");
    assert.equal(await page.locator("#templateGrid .template-card").count(), 2, "templates fixture rows render as cards");
    const redeemTemplateCard = page.locator('[data-template-id="tpl-1"]');
    const panTemplateCard = page.locator('[data-template-id="tpl-2"]');
    assert.match(await redeemTemplateCard.textContent(), /卡密自动发货模板/);
    assert.match(await redeemTemplateCard.textContent(), /已绑定 23 个商品/);
    assert.match(await redeemTemplateCard.textContent(), /类型兑换码/);
    assert.match(await redeemTemplateCard.textContent(), /感谢购买！系统将自动发送兑换码。/);
    assert.match(await panTemplateCard.textContent(), /类型网盘资料/);
    assert.match(await panTemplateCard.textContent(), /付款后发送网盘链接与提取码。/);
    assert.doesNotMatch(await panTemplateCard.textContent(), /类型兑换码/, "pan resource_match tags must not be mistaken for a redeem pool");
    assert.equal(await redeemTemplateCard.locator("[data-template-edit]").count(), 1, "each template card has an edit button");
    await assertNoOverflow(page, "templates free desktop");
    await waitForPanelSettled(page);
    await captureScreenshot(page, { path: path.join(resultRoot, "templates-free-desktop.png"), fullPage: true });

    assert.equal(await page.locator('form[name="automationTemplateForm"] [name="delivery-type"]').inputValue(), "redeem", "the static template form must use the canonical redeem default");
    await page.click("#createTemplateButton");
    await page.waitForSelector("#templateEditorDialog[open]");
    assert.deepEqual(await page.locator("#templateCardPoolSelect option").allTextContents(), ["无（纯话术/网盘链接）", "默认卡密池"], "opening the template editor directly must await card-pool data");
    assert.equal(await page.inputValue("#templateDeliveryTypeInput"), "redeem", "the first editor open must keep the static canonical default");
    assert.equal(fixtures.cardGetRequests.filter((accountKey) => accountKey === "default").length, 1, "initial refresh, template entry, and editor open must share one card-pool GET");
    await page.fill("#templateNameInput", "新建网盘模板");
    await page.fill("#templateDeliveryInput", "保存后回显的网盘资料说明");
    await page.selectOption("#templateCardPoolSelect", "");
    assert.equal(await page.inputValue("#templateDeliveryTypeInput"), "pan", "clearing the pool explicitly switches the canonical type to pan");
    await page.check('#templateProductPicker [data-template-product="100003"]');
    const createPanTemplateResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await createPanTemplateResponse;
    await page.waitForFunction(() => document.querySelector("#templateEditorDialog")?.open !== true);
    await page.waitForFunction((count) => document.querySelectorAll("#templateGrid .template-card").length === count, fixtures.templates.length);
    const createdPanTemplate = fixtures.templateRequests.find((req) => req.method === "PUT" && req.template.name === "新建网盘模板")?.template;
    assert.equal(createdPanTemplate?.delivery, "pan", "creating without a card pool must submit canonical pan");
    assert.deepEqual(createdPanTemplate?.resource_match, ["新建网盘模板"]);
    assert.match(await page.locator(`[data-template-id="${createdPanTemplate.id}"]`).textContent(), /类型网盘资料/);
    assert.match(await page.locator(`[data-template-id="${createdPanTemplate.id}"]`).textContent(), /保存后回显的网盘资料说明/);

    await page.click("#createTemplateButton");
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.fill("#templateNameInput", "新建兑换码模板");
    await page.fill("#templateDeliveryInput", "保存后回显的兑换码说明");
    assert.equal(await page.inputValue("#templateCardPoolSelect"), "默认卡密池", "the canonical redeem default selects the available pool without user type changes");
    assert.equal(await page.inputValue("#templateDeliveryTypeInput"), "redeem");
    await page.check('#templateProductPicker [data-template-product="100001"]');
    const createRedeemTemplateResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await createRedeemTemplateResponse;
    await page.waitForFunction(() => document.querySelector("#templateEditorDialog")?.open !== true);
    await page.waitForFunction((count) => document.querySelectorAll("#templateGrid .template-card").length === count, fixtures.templates.length);
    const createdRedeemTemplate = fixtures.templateRequests.find((req) => req.method === "PUT" && req.template.name === "新建兑换码模板")?.template;
    assert.equal(createdRedeemTemplate?.delivery, "redeem", "creating with a card pool must submit canonical redeem");
    assert.equal(Object.prototype.hasOwnProperty.call(createdRedeemTemplate, "resource_match"), false);
    assert.match(await page.locator(`[data-template-id="${createdRedeemTemplate.id}"]`).textContent(), /类型兑换码/);
    assert.match(await page.locator(`[data-template-id="${createdRedeemTemplate.id}"]`).textContent(), /保存后回显的兑换码说明/);

    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    assert.equal(await page.inputValue("#templateNameInput"), "卡密自动发货模板", "template editor opens prefilled for existing rows");
    assert.equal(await page.inputValue("#templateDeliveryTypeInput"), "redeem", "legacy account delivery aliases must normalize to canonical redeem");
    assert.equal(await page.locator('#templateProductPicker [data-template-product]').count(), 20, "template picker keeps its 20-product render limit");
    assert.equal(await page.locator('#templateProductPicker [data-template-product="100021"], #templateProductPicker [data-template-product="100022"], #templateProductPicker [data-template-product="999999"]').count(), 0);
    await page.fill("#templateNameInput", "卡密自动发货模板（已编辑）");
    const editTemplateResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await editTemplateResponse;
    await page.waitForFunction(() => document.querySelector("#templateEditorDialog")?.open !== true);
    await page.waitForFunction(() => document.querySelector('[data-template-id="tpl-1"] h3')?.textContent.includes("已编辑"));
    const preservedOverflowTemplate = fixtures.templateRequests.find((req) => req.method === "PUT" && req.template.name === "卡密自动发货模板（已编辑）")?.template;
    assert.deepEqual(preservedOverflowTemplate?.item_ids, overflowTemplateItemIds, "editing must preserve all current catalog bindings beyond the rendered picker limit");
    assert.equal(preservedOverflowTemplate?.item_ids.includes("999999"), false, "stale bindings outside the current product catalog must be removed before PUT");
    assert.equal(preservedOverflowTemplate?.delivery, "redeem");

    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.uncheck('#templateProductPicker [data-template-product="100002"]');
    const removeVisibleBindingResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await removeVisibleBindingResponse;
    await page.waitForFunction(() => document.querySelector("#templateEditorDialog")?.open !== true);
    const updatedOverflowTemplate = fixtures.templateRequests.at(-1).template;
    assert.equal(updatedOverflowTemplate.item_ids.length, overflowTemplateItemIds.length - 1);
    assert.equal(updatedOverflowTemplate.item_ids.includes("100002"), false, "an explicitly unchecked visible binding must remain removed");
    assert.ok(overflowTemplateItemIds.slice(3).every((itemId) => updatedOverflowTemplate.item_ids.includes(itemId)), "unrendered bindings must remain intact when a visible binding is removed");

    // A same-account force refresh must take precedence over cached products.
    // Pair the new status metadata with the delayed product response so the old
    // truncated cache can never masquerade as a new complete catalog.
    const refreshedCatalogItem = { id: "100023", title: "刷新后补齐的第 23 个商品", description: "完整目录新增商品", price_display: "¥23", source: "cookie", updated_at: "2026-08-15T09:58:00" };
    const originalAtomicBot = structuredClone(fixtures.bot);
    const originalAtomicProducts = fixtures.products;
    const originalAtomicTemplate = structuredClone(fixtures.templates.find((item) => item.id === "tpl-1"));
    fixtures.bot.products_truncated = true;
    fixtures.templates.find((item) => item.id === "tpl-1").item_ids.push(refreshedCatalogItem.id);
    fixtures.templates.find((item) => item.id === "tpl-1").item_count += 1;
    const truncatedCacheTemplates = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "GET");
    const truncatedCacheProducts = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await Promise.all([truncatedCacheTemplates, truncatedCacheProducts]);
    await page.waitForFunction(() => document.querySelector('[data-template-id="tpl-1"]')?.textContent.includes("22 个商品"));

    fixtures.bot.products_truncated = false;
    fixtures.products = originalAtomicProducts.concat([refreshedCatalogItem]);
    fixtures.loaderResponseDelayMs.products.default = 350;
    const productGetsBeforeAtomicRefresh = fixtures.productGetRequests.filter((accountKey) => accountKey === "default").length;
    const completeStatusResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/status") && response.request().headers()["x-shop-account"] === "default");
    const delayedCompleteProductRequest = page.waitForRequest((request) => request.url().includes("/api/bot/products?limit=500") && request.headers()["x-shop-account"] === "default");
    const delayedCompleteProductResponse = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500") && response.request().headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await Promise.all([completeStatusResponse, delayedCompleteProductRequest]);
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForTimeout(30);
    assert.equal(await page.locator("#templateEditorDialog").getAttribute("open"), null, "editing during a force product refresh must await the in-flight request instead of using cached products");
    assert.equal(fixtures.productGetRequests.filter((accountKey) => accountKey === "default").length, productGetsBeforeAtomicRefresh + 1, "the editor must reuse the same-account force product request");
    await delayedCompleteProductResponse;
    await page.waitForSelector("#templateEditorDialog[open]");
    assert.equal(await page.locator(`[data-template-product="${refreshedCatalogItem.id}"]`).count(), 0, "the newly complete item remains outside the 20-row picker");
    await page.fill("#templateDeliveryInput", "完整目录刷新完成后保存");
    const atomicTemplateSave = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await atomicTemplateSave;
    await page.waitForSelector("#templateEditorDialog", { state: "hidden" });
    assert.equal(fixtures.templateRequests.at(-1).template.item_ids.includes(refreshedCatalogItem.id), true, "the binding outside the old cache must survive once it exists in the new complete catalog");

    fixtures.bot = originalAtomicBot;
    fixtures.products = originalAtomicProducts;
    fixtures.templates[fixtures.templates.findIndex((item) => item.id === "tpl-1")] = originalAtomicTemplate;
    const restoredAtomicTemplates = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await restoredAtomicTemplates;
    await page.waitForFunction((count) => document.querySelectorAll("#productGrid .product-row").length === count, fixtures.products.length);

    fixtures.loaderResponseDelayMs.cards.default = 350;
    const cardGetsBeforeForceRefresh = fixtures.cardGetRequests.filter((accountKey) => accountKey === "default").length;
    const delayedForceCardsRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/cards") && request.headers()["x-shop-account"] === "default");
    const delayedForceCardsResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards") && response.request().headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await delayedForceCardsRequest;
    await page.click("#createTemplateButton");
    await page.waitForTimeout(30);
    assert.equal(await page.locator("#templateEditorDialog").getAttribute("open"), null, "the editor must await a same-account force card-pool refresh instead of using cached pools");
    assert.equal(fixtures.cardGetRequests.filter((accountKey) => accountKey === "default").length, cardGetsBeforeForceRefresh + 1, "the editor must reuse the same-account force cards request");
    await delayedForceCardsResponse;
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.click('#templateEditorDialog [data-close-dialog="templateEditorDialog"]');

    const waitDefaultStatus = () => page.waitForResponse((response) => response.url().endsWith("/api/bot/status")
      && response.request().headers()["x-shop-account"] === "default");
    const waitStatusRequestNumber = (number) => page.waitForResponse((response) => response.url().endsWith("/api/bot/status")
      && response.headers()["x-ui-bot-status-request"] === String(number));
    const waitProductRequestNumber = (number) => page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500")
      && response.headers()["x-ui-product-request"] === String(number));

    // A newer status with a different completeness value supersedes the old
    // request and schedules one serialized follow-up paired to the new token.
    const differentTokenBot = structuredClone(fixtures.bot);
    const differentTokenProducts = fixtures.products;
    const differentTokenTemplate = structuredClone(fixtures.templates.find((item) => item.id === "tpl-1"));
    const originalDefaultAccountDataForToken = fixtures.accountData.default;
    const beyondCachedItemId = "100024";
    fixtures.templates.find((item) => item.id === "tpl-1").item_ids.push(beyondCachedItemId);
    fixtures.templates.find((item) => item.id === "tpl-1").item_count += 1;
    fixtures.accountData.default = {
      ...(originalDefaultAccountDataForToken || {}),
      productCatalog: differentTokenProducts.concat([{ id: beyondCachedItemId, title: "截断目录外合法商品" }]),
    };
    fixtures.bot.products_truncated = false;
    fixtures.loaderResponseDelayMs.products.default = 700;
    const differentBaseRequest = fixtures.productGetRequests.length;
    const differentOldStatus = waitDefaultStatus();
    const differentOldRequest = page.waitForRequest((request) => request.url().includes("/api/bot/products?limit=500") && request.headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await Promise.all([differentOldStatus, differentOldRequest]);
    fixtures.bot.products_truncated = true;
    const differentNewStatus = waitDefaultStatus();
    const differentFinalResponse = waitProductRequestNumber(differentBaseRequest + 2);
    await page.click("#refreshButton");
    await differentNewStatus;
    await differentFinalResponse;
    assert.equal(fixtures.productGetRequests.length, differentBaseRequest + 2, "a new true status must follow an in-flight false request with one serialized product GET");
    await page.waitForSelector('[data-template-edit="tpl-1"]');
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.fill("#templateDeliveryInput", "新 token 的截断快照保存");
    const differentTokenSave = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await differentTokenSave;
    await page.waitForSelector("#templateEditorDialog", { state: "hidden" });
    assert.equal(fixtures.templateRequests.at(-1).template.item_ids.includes(beyondCachedItemId), true, "the final true token must preserve an original binding outside the loaded cache");

    fixtures.bot = differentTokenBot;
    fixtures.products = differentTokenProducts;
    fixtures.templates[fixtures.templates.findIndex((item) => item.id === "tpl-1")] = differentTokenTemplate;
    if (originalDefaultAccountDataForToken === undefined) delete fixtures.accountData.default;
    else fixtures.accountData.default = originalDefaultAccountDataForToken;
    const restoreDifferentToken = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await restoreDifferentToken;

    // Equal booleans from different status generations still require a new
    // products response; token identity, not the boolean alone, owns the snapshot.
    const equalTokenProducts = fixtures.products;
    const equalTokenReplacement = equalTokenProducts.map((item, index) => index === 0 ? { ...item, title: "同布尔新 token 商品" } : item);
    fixtures.bot.products_truncated = false;
    fixtures.loaderResponseDelayMs.products.default = 700;
    const equalBaseRequest = fixtures.productGetRequests.length;
    const equalOldStatus = waitDefaultStatus();
    const equalOldRequest = page.waitForRequest((request) => request.url().includes("/api/bot/products?limit=500") && request.headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await Promise.all([equalOldStatus, equalOldRequest]);
    fixtures.products = equalTokenReplacement;
    const equalNewStatus = waitDefaultStatus();
    const equalFinalResponse = waitProductRequestNumber(equalBaseRequest + 2);
    await page.click("#refreshButton");
    await equalNewStatus;
    await equalFinalResponse;
    assert.equal(fixtures.productGetRequests.length, equalBaseRequest + 2, "equal completeness booleans with different tokens must not reuse the old request");
    await page.waitForFunction(() => document.querySelector("#productGrid .product-title")?.textContent === "同布尔新 token 商品");
    fixtures.products = equalTokenProducts;
    const restoreEqualToken = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500") && response.request().headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await restoreEqualToken;

    // Multiple status generations arriving during one old request collapse to
    // a single follow-up for the latest token. Intermediate metadata may not commit.
    const latestTokenBot = structuredClone(fixtures.bot);
    const latestTokenProducts = fixtures.products;
    const latestTokenTemplate = structuredClone(fixtures.templates.find((item) => item.id === "tpl-1"));
    const staleLatestTokenId = "999998";
    fixtures.templates.find((item) => item.id === "tpl-1").item_ids.push(staleLatestTokenId);
    fixtures.templates.find((item) => item.id === "tpl-1").item_count += 1;
    fixtures.bot.products_truncated = false;
    fixtures.loaderResponseDelayMs.products.default = 900;
    const latestBaseRequest = fixtures.productGetRequests.length;
    const latestOldStatus = waitDefaultStatus();
    const latestOldRequest = page.waitForRequest((request) => request.url().includes("/api/bot/products?limit=500") && request.headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await Promise.all([latestOldStatus, latestOldRequest]);
    fixtures.bot.products_truncated = true;
    fixtures.products = latestTokenProducts.map((item, index) => index === 0 ? { ...item, title: "中间 token 商品" } : item);
    const intermediateStatus = waitDefaultStatus();
    await page.click("#refreshButton");
    await intermediateStatus;
    fixtures.bot.products_truncated = false;
    fixtures.products = latestTokenProducts.map((item, index) => index === 0 ? { ...item, title: "最终 token 商品" } : item);
    const latestStatus = waitDefaultStatus();
    const latestFinalResponse = waitProductRequestNumber(latestBaseRequest + 2);
    await page.click("#refreshButton");
    await latestStatus;
    await latestFinalResponse;
    assert.equal(fixtures.productGetRequests.length, latestBaseRequest + 2, "multiple pending statuses must collapse to the old request plus one latest-token follow-up");
    await page.waitForFunction(() => document.querySelector("#productGrid .product-title")?.textContent === "最终 token 商品");
    await page.waitForSelector('[data-template-edit="tpl-1"]');
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.fill("#templateDeliveryInput", "只提交最终完整 token");
    const latestTokenSave = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await latestTokenSave;
    await page.waitForSelector("#templateEditorDialog", { state: "hidden" });
    assert.equal(fixtures.templateRequests.at(-1).template.item_ids.includes(staleLatestTokenId), false, "the latest false token must clean stale bindings instead of retaining intermediate true metadata");

    fixtures.bot = latestTokenBot;
    fixtures.products = latestTokenProducts;
    fixtures.templates[fixtures.templates.findIndex((item) => item.id === "tpl-1")] = latestTokenTemplate;
    const restoreLatestToken = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await restoreLatestToken;
    await page.waitForTimeout(50);

    // Refresh generation is assigned synchronously at invocation. An older
    // false status arriving after a newer true refresh must be ignored before
    // it can write state, register a catalog token, or start another products GET.
    const reverseBotSnapshot = structuredClone(fixtures.bot);
    const reverseProductsSnapshot = fixtures.products;
    const reverseTemplateSnapshot = structuredClone(fixtures.templates.find((item) => item.id === "tpl-1"));
    const reverseDefaultAccountData = fixtures.accountData.default;
    const reverseOutsideId = "100026";
    fixtures.templates.find((item) => item.id === "tpl-1").item_ids.push(reverseOutsideId);
    fixtures.templates.find((item) => item.id === "tpl-1").item_count += 1;
    fixtures.accountData.default = {
      ...(reverseDefaultAccountData || {}),
      productCatalog: reverseProductsSnapshot.concat([{ id: reverseOutsideId, title: "反序截断目录合法商品" }]),
    };
    fixtures.bot.products_truncated = false;
    fixtures.botStatusResponseDelays.push(900, 0);
    const reverseStatusBase = fixtures.botStatusRequests;
    const reverseProductBase = fixtures.productGetRequests.length;
    const reverseOldStatusRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/status") && request.headers()["x-shop-account"] === "default");
    const reverseOldStatusResponse = waitStatusRequestNumber(reverseStatusBase + 1);
    await page.click("#refreshButton");
    await reverseOldStatusRequest;
    fixtures.bot.products_truncated = true;
    const reverseNewStatusResponse = waitStatusRequestNumber(reverseStatusBase + 2);
    const reverseNewProductResponse = waitProductRequestNumber(reverseProductBase + 1);
    await page.click("#refreshButton");
    await Promise.all([reverseNewStatusResponse, reverseNewProductResponse]);
    await reverseOldStatusResponse;
    await page.waitForTimeout(60);
    assert.equal(fixtures.productGetRequests.length, reverseProductBase + 1, "a late older false status must not register a token or start a follow-up after the newer true refresh");
    await page.waitForSelector('[data-template-edit="tpl-1"]');
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    await page.fill("#templateDeliveryInput", "新 true refresh 先完成，旧 false 完全失效");
    const reverseTemplateSave = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await reverseTemplateSave;
    await page.waitForSelector("#templateEditorDialog", { state: "hidden" });
    assert.equal(fixtures.templateRequests.at(-1).template.item_ids.includes(reverseOutsideId), true, "the newer true refresh must remain authoritative after the old false status arrives");

    fixtures.bot = reverseBotSnapshot;
    fixtures.products = reverseProductsSnapshot;
    fixtures.templates[fixtures.templates.findIndex((item) => item.id === "tpl-1")] = reverseTemplateSnapshot;
    if (reverseDefaultAccountData === undefined) delete fixtures.accountData.default;
    else fixtures.accountData.default = reverseDefaultAccountData;
    const restoreReverseRefresh = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await restoreReverseRefresh;
    await page.waitForTimeout(50);

    // The same ordering rule applies when both statuses report the same
    // boolean. The old response still belongs to an obsolete refresh generation.
    const equalReverseProducts = fixtures.products;
    const equalReverseReplacement = equalReverseProducts.map((item, index) => index === 0 ? { ...item, title: "同布尔反序新刷新商品" } : item);
    fixtures.bot.products_truncated = true;
    const cacheTrueStatus = waitDefaultStatus();
    const cacheTrueProducts = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500") && response.request().headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await Promise.all([cacheTrueStatus, cacheTrueProducts]);
    fixtures.botStatusResponseDelays.push(900, 0);
    const equalReverseStatusBase = fixtures.botStatusRequests;
    const equalReverseProductBase = fixtures.productGetRequests.length;
    const equalReverseOldRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/status") && request.headers()["x-shop-account"] === "default");
    const equalReverseOldResponse = waitStatusRequestNumber(equalReverseStatusBase + 1);
    await page.click("#refreshButton");
    await equalReverseOldRequest;
    fixtures.products = equalReverseReplacement;
    const equalReverseNewResponse = waitStatusRequestNumber(equalReverseStatusBase + 2);
    const equalReverseNewProducts = waitProductRequestNumber(equalReverseProductBase + 1);
    await page.click("#refreshButton");
    await Promise.all([equalReverseNewResponse, equalReverseNewProducts]);
    await equalReverseOldResponse;
    await page.waitForTimeout(60);
    assert.equal(fixtures.productGetRequests.length, equalReverseProductBase + 1, "a late same-boolean status must not create another catalog token or products request");
    await page.waitForFunction(() => document.querySelector("#productGrid .product-title")?.textContent === "同布尔反序新刷新商品");
    fixtures.bot = reverseBotSnapshot;
    fixtures.products = equalReverseProducts;
    const restoreEqualReverse = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500") && response.request().headers()["x-shop-account"] === "default");
    await page.click("#refreshButton");
    await restoreEqualReverse;

    const deleteTemplateResponse = page.waitForResponse((response) => response.url().includes("/api/bot/templates/tpl-2") && response.request().method() === "DELETE");
    await page.click('[data-template-delete="tpl-2"]');
    await page.waitForSelector("#confirmDialog[open]");
    await page.click("#confirmAction");
    await deleteTemplateResponse;
    await page.waitForFunction((count) => document.querySelectorAll("#templateGrid .template-card").length === count, fixtures.templates.length);
    assert.ok(fixtures.templateRequests.some((req) => req.method === "DELETE" && req.id === "tpl-2"), "deleting a template must call DELETE /api/bot/templates/{id}");

    // Free owners can manage 卡密池: stats render and import/edit call the API.
    await openView(page, "cards");
    await page.waitForSelector('[data-panel="cards"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#cardsList .cards-row").length === 1 && document.querySelectorAll("#cardsStats .stat-card").length === 5);
    assert.deepEqual(await page.locator("#cardsStats .stat-card-label").allTextContents(), ["卡密池", "总库存", "可用", "预占", "已消耗"]);
    assert.equal(await page.locator("#cardsList .cards-row-name strong").textContent(), "默认卡密池");
    assert.match(await page.locator("#cardsList .cards-row").textContent(), /可用 85/);
    await assertNoOverflow(page, "cards free desktop");
    await waitForPanelSettled(page);
    await captureScreenshot(page, { path: path.join(resultRoot, "cards-free-desktop.png"), fullPage: true });

    await page.click("#importCardsButton");
    await page.waitForSelector("#cardsEditorDialog[open]");
    await page.fill("#cardsPoolNameInput", "默认卡密池");
    await page.fill("#cardsNoteInput", "批量测试备注");
    await page.fill("#cardsCodesInput", "CODE-001\nCODE-002\n  \nCODE-001");
    const importCardsResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards") && response.request().method() === "PUT");
    await page.click("#cardsEditorCommit");
    await importCardsResponse;
    await page.waitForFunction(() => document.querySelector("#cardsEditorDialog")?.open !== true);
    assert.equal(fixtures.cardRequests.length, 1, "importing cards must call PUT /api/bot/cards once");
    assert.deepEqual(fixtures.cardRequests[0].codes.map((item) => item.code), ["CODE-001", "CODE-002"], "card editor sends unique non-empty codes");
    await page.waitForFunction(() => document.querySelector("#cardsList .cards-row")?.textContent.includes("可用 87"));

    await page.click('[data-cards-edit="pool-1"]');
    await page.waitForSelector("#cardsEditorDialog[open]");
    assert.equal(await page.inputValue("#cardsPoolNameInput"), "默认卡密池", "card editor opens prefilled for existing pools");
    await page.fill("#cardsPoolNameInput", "运营卡密池");
    await page.fill("#cardsNoteInput", "运营备注");
    await page.fill("#cardsCodesInput", "");
    const editCardsResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards") && response.request().method() === "PUT");
    await page.click("#cardsEditorCommit");
    await editCardsResponse;
    await page.waitForFunction(() => document.querySelector("#cardsEditorDialog")?.open !== true);
    assert.ok(fixtures.cardRequests.some((req) => req.name === "运营卡密池"), "editing a pool must call PUT /api/bot/cards with the new name");
    await page.waitForFunction(() => document.querySelector("#cardsList .cards-row-name strong")?.textContent === "运营卡密池");
    await page.fill("#cardsCreateName", "新建卡密池");
    await page.fill("#cardsCreateNote", "还没有导入卡密");
    const createPoolResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards") && response.request().method() === "PUT");
    await page.click("#cardsCreateSubmit");
    await createPoolResponse;
    assert.ok(fixtures.cardRequests.some((req) => req.name === "新建卡密池" && Array.isArray(req.codes) && req.codes.length === 0), "gemini-style pool creation allows an empty pool");
    await page.waitForFunction(() => document.querySelector("#cardsList .cards-row-name strong")?.textContent === "新建卡密池");

    // 店铺管理 is a dedicated workspace. Creation uses a friendly name only,
    // while subsequent requests carry the opaque account scope.
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 1);
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card.is-current .shop-card-copy strong").textContent(), "海风数字店");
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card-main").getAttribute("aria-label"), "当前店铺", "current shop action needs an accessible name");
    assert.match(await page.locator("#shopAccountsPanelList .shop-card.is-current .badge").textContent(), /当前 · 已连接/);
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card-meta span").first().textContent(), `${fixtures.products.length} 个商品`);
    assert.equal(await page.locator("#shopAccountsCount").textContent(), "1 个");
    assert.equal(await page.locator('[data-account-delete="default"]').isDisabled(), true, "the default shop must be protected");
    assert.equal(await page.locator("#otherConnectionDetails, #legacyConnectorButton, #cookiesForm, #downloadConnector").count(), 0, "compatibility connection methods must be removed");
    assert.equal(await page.locator("#shopConnectionTitle").textContent(), "店铺已连接", "connected shops must show the management copy");
    assert.equal(await page.locator("#xianyuConnectButton span").textContent(), "重新连接店铺", "connected shops must not show the initial connect action");
    assert.equal(await page.locator("#shopConnectionBadge").textContent(), "已验证");
    assert.equal(await page.locator('[data-panel="shops"] .page-head-copy p').count(), 0, "shop page header must not carry annotation microcopy");
    assert.equal(await page.locator("#shopConnectionHint").count(), 0, "the QR connect action must stay clean without a side annotation");
    const shopsVisibleText = await page.locator("body").innerText();
    for (const removed of ["账号、连接状态和店铺操作集中在一处", "当前店铺会高亮显示，操作只影响选中的账号", "连接新的闲鱼账号，成功后会自动加入列表", "不安装额外组件，连接成功后自动读取店铺和商品"]) {
      assert.equal(shopsVisibleText.includes(removed), false, `${removed} must be removed from the shop workspace`);
    }
    assert.equal(await page.locator('[data-panel="shops"] .shop-add-heading p, [data-panel="shops"] .connector-trust p').count(), 0, "shop add/trust blocks must not carry annotation microcopy");
    const shopMetaContrast = await measuredContrast(page, "#shopAccountsPanelList .shop-card-copy small");
    assert.ok(shopMetaContrast.ratio >= 4.5, `shop card metadata contrast must be at least 4.5:1: ${JSON.stringify(shopMetaContrast)}`);
    const placeholderContrast = await measuredContrast(page, "#shopAccountPanelNameInput", "::placeholder");
    assert.ok(placeholderContrast.ratio >= 4.5, `input placeholder contrast must be at least 4.5:1: ${JSON.stringify(placeholderContrast)}`);
    await page.click("#openAddShopAccount");
    await page.waitForFunction(() => document.activeElement?.id === "shopAccountPanelNameInput");
    assert.equal(await page.locator('[data-panel="shops"]').isVisible(), true, "the page-level add-shop action must expose the add form");
    await page.click("#accountTabs [data-open-shop-add]");
    await page.waitForFunction(() => document.activeElement?.id === "shopAccountPanelNameInput");

    fixtures.qrNextMode = "success";
    await page.fill("#shopAccountPanelNameInput", "备用店");
    await page.click("#addShopAccountPanelForm button[type=submit]");
    await page.waitForSelector("#xianyuLoginDialog[open]");
    await page.waitForSelector("#xianyuQrImage:not([hidden])");
    await page.waitForSelector("#xianyuLoginDialog", { state: "hidden" });
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店");
    assert.equal(await page.evaluate(() => localStorage.getItem("xianyu-saas.active-account:owner-demo")), "shop-ui-2", "the newly connected shop becomes the active account");
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 2);
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card.is-current .shop-card-copy strong").textContent(), "备用店");

    const defaultAccount = fixtures.shopAccounts.find((item) => item.key === "default");
    const originalDefaultData = fixtures.accountData.default;
    const originalDefaultStatus = defaultAccount.status;
    const originalDefaultError = defaultAccount.last_error_code;
    defaultAccount.status = "expired";
    defaultAccount.last_error_code = "session_expired";
    fixtures.accountData.default = {
      ...(originalDefaultData || {}),
      bot: {
        ...fixtures.bot,
        running: false,
        connected: false,
        sync_status: "waiting_login",
        runtime_state: "waiting_login",
        desired_running: true,
        auth_code: "session_expired",
        reauthorization_required: true,
      },
    };
    const defaultAuthStatusLoad = page.waitForResponse((response) => response.url().endsWith("/api/bot/status")
      && response.request().headers()["x-shop-account"] === "default");
    const defaultCardsLoad = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "default");
    await page.click('#shopAccountsPanelList [data-account-switch="default"]');
    await Promise.all([defaultAuthStatusLoad, defaultCardsLoad]);
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");
    assert.equal(await page.locator("#shopConnectionTitle").textContent(), "店铺需要重新授权");
    assert.equal(await page.locator("#xianyuConnectButton span").textContent(), "重新扫码授权");
    assert.equal(await page.locator("#xianyuConnectButton").getAttribute("aria-label"), "重新扫码授权");
    assert.equal(await page.locator("#shopConnectionBadge").textContent(), "登录已失效");
    assert.match(await page.locator("#shopConnectionDescription").textContent(), /授权成功后，自动回复会自动恢复/);
    assert.equal(await page.locator("#cookieStatusNotice").isVisible(), true);
    assert.equal(await page.locator("#cookieStatusAction").textContent(), "重新扫码授权后自动恢复服务");
    assert.match(await page.locator("#shopAccountsPanelList .shop-card.is-current .badge").textContent(), /当前 · 已断开 · 登录失效/);
    defaultAccount.status = originalDefaultStatus;
    defaultAccount.last_error_code = originalDefaultError;
    if (originalDefaultData === undefined) delete fixtures.accountData.default;
    else fixtures.accountData.default = originalDefaultData;

    fixtures.loaderResponseDelayMs.cards["shop-ui-2"] = 900;
    fixtures.loaderResponseDelayMs.products["shop-ui-2"] = 900;
    const shopCardLoadsBefore = fixtures.cardGetRequests.filter((accountKey) => accountKey === "shop-ui-2").length;
    const slowShopCardsLoad = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    const slowShopProductsRequest = page.waitForRequest((request) => request.url().includes("/api/bot/products?limit=500")
      && request.method() === "GET"
      && request.headers()["x-shop-account"] === "shop-ui-2");
    const slowShopProductsLoad = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店");
    await slowShopProductsRequest;
    await openView(page, "templates");
    await page.waitForSelector('[data-template-edit="tpl-1"]');
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForTimeout(30);
    assert.equal(await page.locator("#templateEditorDialog").getAttribute("open"), null, "the editor must wait for the current product catalog before reconciling saved bindings");
    assert.equal(fixtures.cardGetRequests.filter((accountKey) => accountKey === "shop-ui-2").length, shopCardLoadsBefore + 1, "template entry and editor open must reuse the in-flight shop card-pool GET");
    await page.click('#sideNav [data-view="home"]');
    await slowShopProductsLoad;
    await slowShopCardsLoad;
    await page.waitForTimeout(50);
    assert.equal(await page.locator('[data-panel="home"]').isVisible(), true);
    assert.equal(await page.locator("#templateEditorDialog").getAttribute("open"), null, "leaving templates on the same account must invalidate a delayed editor open");

    await openView(page, "shops");
    await page.click('#shopAccountsPanelList [data-account-switch="default"]');
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");

    const truncatedItemId = "100501";
    const shopData = fixtures.accountData["shop-ui-2"];
    const originalShopBot = shopData.bot;
    const originalShopCatalog = shopData.productCatalog;
    const truncatedTemplate = fixtures.templates.find((item) => item.id === "tpl-1");
    const originalTruncatedTemplate = structuredClone(truncatedTemplate);
    shopData.bot = { ...fixtures.bot, products_truncated: true, product_count: 501 };
    shopData.productCatalog = fixtures.products.concat([{ id: truncatedItemId, title: "第 501 个合法商品" }]);
    truncatedTemplate.item_ids = Array.from(new Set(truncatedTemplate.item_ids.concat([truncatedItemId])));
    truncatedTemplate.item_count = truncatedTemplate.item_ids.length;

    const truncatedBotLoad = page.waitForResponse((response) => response.url().endsWith("/api/bot/status")
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    const truncatedProductsLoad = page.waitForResponse((response) => response.url().includes("/api/bot/products?limit=500")
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
    await Promise.all([truncatedBotLoad, truncatedProductsLoad]);
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店");
    await openView(page, "templates");
    await page.waitForSelector('[data-template-edit="tpl-1"]');
    await page.click('[data-template-edit="tpl-1"]');
    await page.waitForSelector("#templateEditorDialog[open]");
    assert.equal(await page.locator(`[data-template-product="${truncatedItemId}"]`).count(), 0, "the 501st binding is outside the loaded picker catalog");
    await page.fill("#templateDeliveryInput", "截断目录编辑后仍保留未加载绑定");
    const truncatedTemplateSave = page.waitForResponse((response) => response.url().endsWith("/api/bot/templates") && response.request().method() === "PUT");
    await page.click("#templateEditorCommit");
    await truncatedTemplateSave;
    await page.waitForSelector("#templateEditorDialog", { state: "hidden" });
    const truncatedRequest = fixtures.templateRequests.at(-1);
    assert.equal(truncatedRequest.accountKey, "shop-ui-2");
    assert.equal(truncatedRequest.template.item_ids.includes(truncatedItemId), true, "truncated catalogs must preserve legal original bindings beyond the loaded 500");

    fixtures.templates[fixtures.templates.findIndex((item) => item.id === "tpl-1")] = originalTruncatedTemplate;
    if (originalShopBot === undefined) delete shopData.bot;
    else shopData.bot = originalShopBot;
    if (originalShopCatalog === undefined) delete shopData.productCatalog;
    else shopData.productCatalog = originalShopCatalog;

    await openView(page, "shops");
    await page.click('#shopAccountsPanelList [data-account-switch="default"]');
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");
    await openView(page, "templates");
    await page.click("#createTemplateButton");
    await page.waitForSelector("#templateEditorDialog[open]");
    assert.deepEqual(await page.locator("#templateCardPoolSelect option").allTextContents(), ["无（纯话术/网盘链接）", "新建卡密池"], "switching accounts must not retain the previous shop pool data");
    assert.equal(await page.locator('#templateCardPoolSelect option:has-text("备用店卡密池")').count(), 0);
    await page.click('#templateEditorDialog [data-close-dialog="templateEditorDialog"]');
    await openView(page, "shops");
    const returnShopCardsLoad = page.waitForResponse((response) => response.url().endsWith("/api/bot/cards")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
    await returnShopCardsLoad;
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店");

    await page.click('[data-account-rename="shop-ui-2"]');
    await page.waitForSelector("#renameShopAccountForm:not([hidden])");
    await page.fill("#shopDisplayNameInput", "备用店（运营）");
    await page.click("#renameShopAccountForm button[type=submit]");
    await page.waitForFunction(() => document.querySelector("#renameShopAccountMessage")?.textContent.includes("名称已保存"));
    assert.equal(fixtures.shopAccountPatchRequests.at(-1)?.name, "备用店（运营）", "rename must call the scoped PATCH endpoint");
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card.is-current .shop-card-copy strong").textContent(), "备用店（运营）");
    const checkAccountResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/shop/sync") && response.request().method() === "POST");
    await page.click('[data-shop-action="check"][data-shop-key="shop-ui-2"]');
    await checkAccountResponse;
    assert.ok(fixtures.shopActionRequests.some((item) => item.action === "check" && item.key === "shop-ui-2"), "check must call the scoped sync endpoint");
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card.is-current .shop-card-copy strong").textContent(), "备用店（运营）");
    fixtures.qrNextMode = "expired";
    const reconnectStartsBefore = fixtures.qrStarts;
    const scopedReconnectQr = page.waitForResponse((response) => response.url().endsWith("/qr.svg")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.click('[data-shop-action="reconnect"][data-shop-key="shop-ui-2"]');
    await scopedReconnectQr;
    await page.waitForSelector("#xianyuLoginDialog[open]");
    await page.waitForSelector("#xianyuLoginStatus");
    await page.waitForFunction(() => document.querySelector("#xianyuLoginStatus")?.textContent.includes("已过期"));
    await page.click("#closeXianyuLogin");
    await page.waitForSelector("#xianyuLoginDialog", { state: "hidden" });
    assert.equal(fixtures.qrStarts, reconnectStartsBefore + 1, "row reconnect must open the official QR flow");
    assert.equal(fixtures.qrCancels, 1, "cancelling row reconnect must close its server session");
    // Isolate the full QR retry contract below from the setup sessions above.
    fixtures.qrStarts = 0;
    fixtures.qrConnects = 0;
    fixtures.qrCancels = 0;
    fixtures.qrSyncFailures = 0;
    await assertNoOverflow(page, "shops desktop");
    await waitForPanelSettled(page);
    await captureScreenshot(page, { path: path.join(resultRoot, "shops-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await waitForPanelSettled(page);
    const closedSidebarA11y = await page.evaluate(() => {
      const sidebar = document.querySelector("#sidebar");
      const menu = document.querySelector("#mobileMenu");
      return {
        inert: sidebar.inert,
        hasInert: sidebar.hasAttribute("inert"),
        ariaHidden: sidebar.getAttribute("aria-hidden"),
        expanded: menu.getAttribute("aria-expanded"),
      };
    });
    assert.deepEqual(closedSidebarA11y, { inert: true, hasInert: true, ariaHidden: "true", expanded: "false" }, "closed mobile navigation must be removed from the accessibility tree");

    await page.click("#mobileMenu");
    await page.waitForSelector("#sidebar.is-open");
    await waitForPanelSettled(page);
    assert.deepEqual(await page.evaluate(() => ({
      inert: document.querySelector("#sidebar").inert,
      hasInert: document.querySelector("#sidebar").hasAttribute("inert"),
      ariaHidden: document.querySelector("#sidebar").getAttribute("aria-hidden"),
      expanded: document.querySelector("#mobileMenu").getAttribute("aria-expanded"),
      activeElement: document.activeElement?.id || "",
    })), { inert: false, hasInert: false, ariaHidden: "false", expanded: "true", activeElement: "closeSidebar" }, "opening mobile navigation must expose it and move focus to the close button");

    await page.click("#closeSidebar");
    await waitForPanelSettled(page);
    assert.deepEqual(await page.evaluate(() => ({
      open: document.querySelector("#sidebar").classList.contains("is-open"),
      inert: document.querySelector("#sidebar").inert,
      ariaHidden: document.querySelector("#sidebar").getAttribute("aria-hidden"),
      expanded: document.querySelector("#mobileMenu").getAttribute("aria-expanded"),
      activeElement: document.activeElement?.id || "",
    })), { open: false, inert: true, ariaHidden: "true", expanded: "false", activeElement: "mobileMenu" }, "closing mobile navigation must restore focus to the menu button");

    await page.click("#mobileMenu");
    await page.waitForSelector("#sidebar.is-open");
    await waitForPanelSettled(page);
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    await waitForPanelSettled(page);
    assert.equal(await page.locator("#mobileMenu").getAttribute("aria-expanded"), "false", "choosing a mobile navigation item must close the drawer");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "mobileMenu", "closing from a navigation action must restore focus");
    await page.waitForFunction(() => {
      const panel = document.querySelector('[data-panel="shops"]');
      const list = document.querySelector("#shopAccountsPanelList");
      if (!panel || panel.hidden || !list) return false;
      const visibleButtons = Array.from(list.querySelectorAll(".shop-card-actions button")).filter((button) => {
        const style = getComputedStyle(button);
        return style.display !== "none" && style.visibility !== "hidden";
      });
      return visibleButtons.length >= 8 && visibleButtons.every((button) => {
        const rect = button.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      });
    }, null, { timeout: 5000 });
    await assertNoOverflow(page, "shops mobile");
    const mobileShopActionTargets = await page.locator("#shopAccountsPanelList .shop-card-actions button").evaluateAll((buttons) => buttons.filter((button) => {
      const style = getComputedStyle(button);
      return style.display !== "none" && style.visibility !== "hidden";
    }).map((button) => {
      const rect = button.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }));
    assert.ok(mobileShopActionTargets.length >= 8, "mobile shop rows must expose their account actions");
    assert.ok(mobileShopActionTargets.every(({ width, height }) => width >= 44 && height >= 44), `mobile shop actions must keep 44px touch targets: ${JSON.stringify(mobileShopActionTargets)}`);
    await waitForPanelSettled(page);
    await captureScreenshot(page, { path: path.join(resultRoot, "shops-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(250);
    assert.ok(fixtures.shopAccountHeaders.includes("shop-ui-2"), "account-scoped requests must include the selected account");
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("#workspace:not([hidden])");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(250);
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店（运营）", null, { timeout: 5000 });

    // An authenticated account with an empty catalog must not fall back to
    // the unconfigured "去连接店铺" state.
    const originalProducts = fixtures.products;
    const originalScopedProducts = fixtures.accountData["shop-ui-2"]?.products;
    const originalProductCount = fixtures.bot.product_count;
    const originalProductsSet = fixtures.bot.products_set;
    fixtures.products = [];
    if (fixtures.accountData["shop-ui-2"]) fixtures.accountData["shop-ui-2"].products = [];
    fixtures.bot.product_count = 0;
    fixtures.bot.products_set = false;
    fixtures.bot.catalog_state = "empty";
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("#workspace:not([hidden])");
    await page.click('#sideNav [data-view="goods"]');
    await page.waitForSelector('#productsEmpty:not([hidden])');
    assert.equal(await page.locator("#productsEmptyTitle").textContent(), "店铺已连接，暂时没有商品");
    assert.equal(await page.locator("#productsEmptyAction span").textContent(), "重新检测商品");
    assert.equal(await page.locator("#productsEmptyAction").getAttribute("data-sync-products"), "true");
    assert.equal((await page.locator("#productsEmpty").textContent()).includes("去连接店铺"), false);
    fixtures.products = originalProducts;
    if (fixtures.accountData["shop-ui-2"]) fixtures.accountData["shop-ui-2"].products = originalScopedProducts;
    fixtures.bot.product_count = originalProductCount;
    fixtures.bot.products_set = originalProductsSet;
    delete fixtures.bot.catalog_state;
    await page.reload({ waitUntil: "networkidle" });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(250);
    await page.click('#sideNav [data-view="goods"]');
    await page.waitForFunction((count) => document.querySelectorAll("#productGrid .product-row").length === count, fixtures.products.length);

    // 智能回复严格采用 Gemini 的全局设置 + 新增规则表单 + 五列规则表结构。
    await openView(page, "auto-reply");
    await page.waitForSelector('[data-panel="auto-reply"]:not([hidden])');
    assert.equal(await page.locator('[data-panel="auto-reply"] .page-head-copy p').count(), 0, "business-domain headers stay compact without duplicate subtitles");
    assert.match(await page.locator('[data-panel="auto-reply"] .page-head-copy h1').textContent(), /智能客服中心/);
    assert.equal(await page.locator('[data-panel="auto-reply"] [data-view="auto-reply"]').getAttribute("aria-selected"), "true");
    assert.equal(await page.locator("#automationEnabledToggle").count(), 1, "automation must expose exactly one enable switch");
    await page.click(".automation-enabled-row .setting-copy");
    assert.equal(await page.locator("#automationEnabledToggle").isChecked(), false, "clicking the visible switch copy toggles the checkbox");
    await page.click(".automation-enabled-row .setting-copy");
    assert.equal(await page.locator("#automationEnabledToggle").isChecked(), true);
    assert.equal(await page.locator("#startAutomationButton, #stopAutomationButton").count(), 0, "the reference page uses the enable switch and save action instead of extra run buttons");
    assert.equal(await page.locator("#replyRuleForm").count(), 1, "the create-rule form stays visible like the reference demo");
    assert.equal(await page.locator("#replyRuleTable thead th").count(), 5, "configured rules use a semantic five-column table");
    assert.equal(await page.locator("#replyRuleList .rule-row").count(), 1, "free user can see reply rules");
    assert.ok(await page.locator("#replyRuleList .rule-keyword").count() >= 1, "keyword rules render compact chips");
    assert.equal(await page.locator('[data-panel="auto-reply"] #deliveryRuleList, [data-panel="auto-reply"] #automationLogList').count(), 0, "delivery and runtime-log cards stay out of the reference auto-reply page");
    assert.equal(await page.locator(".page-head-copy h1 .page-head-icon").count(), 10, "every workspace panel title keeps an icon");
    assert.equal(await page.inputValue("#automationShopSelect"), "shop-ui-2", "automation scopes to the active shop");
    assert.equal(await page.inputValue("#automationFirstReply"), fixtures.automation.first_reply, "first-contact reply is loaded from the account settings");
    assert.equal(await page.inputValue("#automationFallbackReply"), fixtures.automation.fallback_reply, "fallback reply is loaded from the account settings");
    assert.equal(await page.inputValue("#automationDelayMin"), "2");
    assert.equal(await page.inputValue("#automationDelayMax"), "3");
    assert.equal(await page.inputValue("#automationTriggerCooldown"), "2");
    assert.equal(await page.inputValue("#automationManualCooldown"), "30");
    assert.equal(await page.locator("#replyRuleProductOptions option").count(), fixtures.products.length, "rule product ID suggestions come from real synced products");

    const measureAutomationLayout = () => page.evaluate(() => {
      const panel = document.querySelector('[data-panel="auto-reply"]');
      const layout = panel?.querySelector(".automation-layout");
      const left = layout?.querySelector(".automation-left");
      const right = layout?.querySelector(".automation-right");
      const panelRect = panel?.getBoundingClientRect();
      const leftRect = left?.getBoundingClientRect();
      const rightRect = right?.getBoundingClientRect();
      return {
        viewportWidth: innerWidth,
        panelWidth: panelRect?.width || 0,
        leftWidth: leftRect?.width || 0,
        rightWidth: rightRect?.width || 0,
        leftTop: leftRect?.top || 0,
        leftBottom: leftRect?.bottom || 0,
        rightTop: rightRect?.top || 0,
        rightLeft: rightRect?.left || 0,
        leftRight: leftRect?.right || 0,
      };
    });
    const layout1440 = await measureAutomationLayout();
    assert.ok(Math.abs(layout1440.panelWidth - 1152) <= 2, `1440px automation panel should cap near 1152px: ${JSON.stringify(layout1440)}`);
    assert.ok(Math.abs(layout1440.leftWidth / layout1440.rightWidth - 4.5 / 7.5) <= 0.03, `desktop automation columns should stay near 4.5:7.5: ${JSON.stringify(layout1440)}`);
    assert.ok(Math.abs(layout1440.leftTop - layout1440.rightTop) <= 1 && layout1440.rightLeft > layout1440.leftRight, "1440px automation layout must use two columns");

    await page.setViewportSize({ width: 1024, height: 900 });
    await waitForPanelSettled(page);
    const layout1024 = await measureAutomationLayout();
    assert.ok(Math.abs(layout1024.leftTop - layout1024.rightTop) <= 1 && layout1024.rightLeft > layout1024.leftRight, `1024px must retain two automation columns: ${JSON.stringify(layout1024)}`);

    await page.setViewportSize({ width: 1023, height: 900 });
    await waitForPanelSettled(page);
    const layout1023 = await measureAutomationLayout();
    assert.ok(Math.abs(layout1023.leftWidth - layout1023.rightWidth) <= 2 && layout1023.rightTop > layout1023.leftBottom, `1023px must collapse automation to one column: ${JSON.stringify(layout1023)}`);

    await page.setViewportSize({ width: 768, height: 900 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "auto-reply 768px");
    const ruleTableSemantics = await page.locator("#replyRuleTable").evaluate((table) => ({
      tagName: table.tagName,
      headDisplay: getComputedStyle(table.tHead).display,
      headers: Array.from(table.tHead?.querySelectorAll("th") || []).map((header) => ({ text: header.textContent.trim(), scope: header.getAttribute("scope") })),
      overflowX: getComputedStyle(table.closest(".rule-table-scroll")).overflowX,
    }));
    assert.equal(ruleTableSemantics.tagName, "TABLE");
    assert.equal(ruleTableSemantics.headDisplay, "table-header-group", "responsive rules must retain a semantic table header");
    assert.deepEqual(ruleTableSemantics.headers, ["规则", "关键词", "回复话术", "状态", "操作"].map((text) => ({ text, scope: "col" })));
    assert.equal(ruleTableSemantics.overflowX, "auto", "the rule table wrapper must allow internal horizontal scrolling");

    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "auto-reply 390px");
    const mobileRuleTableScroll = await page.locator(".rule-table-scroll").evaluate((wrapper) => {
      wrapper.scrollLeft = 96;
      return { clientWidth: wrapper.clientWidth, scrollWidth: wrapper.scrollWidth, scrollLeft: wrapper.scrollLeft, pageScrollX: scrollX };
    });
    assert.ok(mobileRuleTableScroll.scrollWidth > mobileRuleTableScroll.clientWidth, `mobile rule table should overflow only inside its wrapper: ${JSON.stringify(mobileRuleTableScroll)}`);
    assert.ok(mobileRuleTableScroll.scrollLeft > 0 && mobileRuleTableScroll.pageScrollX === 0, `mobile rule table should scroll internally without moving the page: ${JSON.stringify(mobileRuleTableScroll)}`);
    await page.locator(".rule-table-scroll").evaluate((wrapper) => { wrapper.scrollLeft = 0; });

    await page.setViewportSize({ width: 1440, height: 900 });
    await waitForPanelSettled(page);

    const deliveryCountBeforeRuleSave = fixtures.automation.deliveries.length;
    await page.fill("#replyRuleName", "价格咨询");
    await page.fill("#replyRuleItemId", "100002");
    await page.fill("#replyRuleKeywords", "价格,多少钱,优惠");
    await page.fill("#replyRuleReply", "你好，在的，标价即实价。");
    const createRuleResponse = page.waitForResponse((response) => response.url().endsWith("/api/automation") && response.request().method() === "PUT");
    await page.click("#saveReplyRuleButton");
    await createRuleResponse;
    assert.equal(await page.locator("#replyRuleList .rule-row").count(), 2);
    assert.equal(fixtures.automation.rules[1].name, "价格咨询");
    assert.equal(fixtures.automation.rules[1].item_id, "100002");
    assert.deepEqual(Object.keys(fixtures.automationPuts.at(-1).payload), ["rules"], "rule create must submit only the rules sub-resource");
    assert.equal(fixtures.automationPuts.at(-1).accountKey, "shop-ui-2");
    assert.equal(fixtures.automation.deliveries.length, deliveryCountBeforeRuleSave, "partial rule saves must not clear delivery settings");

    await page.locator("#replyRuleList [data-edit-rule]").nth(1).click();
    assert.equal(await page.inputValue("#replyRuleName"), "价格咨询");
    assert.equal(await page.inputValue("#replyRuleItemId"), "100002");
    await page.fill("#replyRuleReply", "标价即实价，直接拍下即可。");
    const editRuleResponse = page.waitForResponse((response) => response.url().endsWith("/api/automation") && response.request().method() === "PUT");
    await page.click("#saveReplyRuleButton");
    await editRuleResponse;
    assert.equal(await page.locator("#replyRuleList .rule-row").count(), 2);
    assert.equal(fixtures.automation.rules[1].reply, "标价即实价，直接拍下即可。");

    await page.locator("#replyRuleList [data-remove-rule]").nth(1).click();
    await page.waitForSelector("#confirmDialog[open]");
    const deleteRuleResponse = page.waitForResponse((response) => response.url().endsWith("/api/automation") && response.request().method() === "PUT");
    await page.click("#confirmAction");
    await deleteRuleResponse;
    assert.equal(await page.locator("#replyRuleList .rule-row").count(), 1);
    assert.equal(fixtures.automation.rules.length, 1, "rule deletion is persisted immediately");

    await page.fill("#automationFirstReply", "欢迎光临，请告诉我想了解商品的哪一方面。");
    await page.fill("#automationFallbackReply", "稍后店主会人工回复你。");
    await page.fill("#automationDelayMin", "4");
    await page.fill("#automationDelayMax", "6");
    await page.fill("#automationTriggerCooldown", "8");
    await page.fill("#automationManualCooldown", "45");
    await page.check("#automationBusinessHoursEnabled");
    await page.fill("#automationBusinessStart", "08:30");
    await page.fill("#automationBusinessEnd", "23:00");
    const globalPutCount = fixtures.automationPuts.length;
    const rulesStartCount = fixtures.botStartModes.length;
    const rulesStartResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/start") && response.request().method() === "POST");
    await page.click("#saveAutomationButton");
    await rulesStartResponse;
    await page.waitForFunction(() => document.querySelector("#automationMessage")?.textContent.includes("已开启"));
    assert.equal(fixtures.automationPuts.length, globalPutCount + 1);
    assert.equal(Object.prototype.hasOwnProperty.call(fixtures.automationPuts.at(-1).payload, "rules"), false, "global save must not overwrite rules");
    assert.equal(Object.prototype.hasOwnProperty.call(fixtures.automationPuts.at(-1).payload, "deliveries"), false, "global save must not overwrite deliveries");
    assert.equal(fixtures.botStartModes.length, rulesStartCount + 1);
    assert.equal(fixtures.botStartModes.at(-1).mode, "rules", "saving enabled global settings starts the deterministic rules worker");
    assert.equal(fixtures.automation.first_reply, "欢迎光临，请告诉我想了解商品的哪一方面。");
    assert.equal(fixtures.automation.fallback_reply, "稍后店主会人工回复你。");
    assert.equal(fixtures.automation.delay_min_seconds, 4);
    assert.equal(fixtures.automation.delay_max_seconds, 6);
    assert.equal(fixtures.automation.trigger_cooldown_seconds, 8);
    assert.equal(fixtures.automation.manual_takeover_cooldown_seconds, 45);
    assert.equal(fixtures.automation.business_hours_enabled, true);
    assert.equal(fixtures.automation.business_start, "08:30");
    assert.equal(fixtures.automation.business_end, "23:00");

    // Disabling global automation is persisted by the same PUT and the backend
    // stops the account's running deterministic rules worker.
    const rulesWorkerStopCount = fixtures.botStops.length;
    await page.click(".automation-enabled-row .setting-copy");
    assert.equal(await page.locator("#automationEnabledToggle").isChecked(), false);
    const disableAutomationPut = page.waitForResponse((response) => response.url().endsWith("/api/automation") && response.request().method() === "PUT");
    const disabledBotStatus = page.waitForResponse((response) => response.url().endsWith("/api/bot/status") && response.request().method() === "GET");
    await page.click("#saveAutomationButton");
    await disableAutomationPut;
    await disabledBotStatus;
    await page.waitForFunction(() => document.querySelector("#automationMessage")?.textContent.includes("已关闭"));
    assert.equal(fixtures.botStops.length, rulesWorkerStopCount + 1, "turning off automation must stop the running worker once");
    assert.deepEqual(fixtures.botStops.at(-1), { accountKey: "shop-ui-2", mode: "rules", reason: "automation_disabled" });
    assert.equal(fixtures.bot.running, false, "the deterministic rules worker must be stopped after disabling automation");

    // Saving enabled settings while the paid rules_ai worker is already active
    // must not replace it with, or additionally start, a deterministic worker.
    fixtures.bot.running = true;
    fixtures.bot.running_total = 1;
    fixtures.bot.automation_mode = "rules_ai";
    const aiStatusRefresh = page.waitForResponse((response) => response.url().endsWith("/api/bot/status") && response.request().method() === "GET");
    await page.click("#refreshButton");
    await aiStatusRefresh;
    await page.waitForFunction(() => document.querySelector("#chatAiStatus")?.textContent === "AI 已开启");
    await page.click(".automation-enabled-row .setting-copy");
    assert.equal(await page.locator("#automationEnabledToggle").isChecked(), true);
    const startsBeforeAiSettingsSave = fixtures.botStartModes.length;
    const aiSettingsPut = page.waitForResponse((response) => response.url().endsWith("/api/automation") && response.request().method() === "PUT");
    await page.click("#saveAutomationButton");
    await aiSettingsPut;
    await page.waitForFunction(() => document.querySelector("#automationMessage")?.textContent === "店铺配置已保存");
    assert.equal(fixtures.botStartModes.length, startsBeforeAiSettingsSave, "saving settings during rules_ai must not send a rules start request");
    assert.equal(fixtures.bot.automation_mode, "rules_ai");
    assert.equal(fixtures.bot.running, true);

    // A slow rules PUT from shop-ui-2 must neither issue a second concurrent
    // rules save nor overwrite the default shop after an immediate switch.
    const originalShopAutomation = fixtures.accountData["shop-ui-2"].automation;
    const originalDefaultAccountData = fixtures.accountData.default;
    const slowShopAutomation = {
      ...structuredClone(fixtures.automation),
      first_reply: "备用店首次回复",
      rules: [{ id: "shop-slow-rule", name: "备用店慢规则", item_id: "100002", enabled: true, keywords: ["备用"], match: "contains", reply: "这是备用店回复。" }],
    };
    const defaultAutomation = {
      ...structuredClone(fixtures.automation),
      first_reply: "默认店首次回复",
      rules: [{ id: "default-rule", name: "默认店规则", item_id: "100001", enabled: true, keywords: ["默认"], match: "contains", reply: "这是默认店回复。" }],
    };
    fixtures.accountData["shop-ui-2"].automation = slowShopAutomation;
    fixtures.accountData.default = { ...(originalDefaultAccountData || {}), automation: defaultAutomation };
    const slowShopReload = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await openView(page, "auto-reply");
    await slowShopReload;
    await page.waitForFunction(() => document.querySelector("#replyRuleList")?.textContent.includes("备用店慢规则"));

    await page.fill("#replyRuleName", "备用店延迟保存规则");
    await page.fill("#replyRuleItemId", "100003");
    await page.fill("#replyRuleKeywords", "延迟");
    await page.fill("#replyRuleReply", "这条规则会延迟返回。");
    fixtures.automationPutDelayMsByAccount["shop-ui-2"] = 900;
    const slowRulePutCount = fixtures.automationPuts.length;
    const slowRuleRequest = page.waitForRequest((request) => request.url().endsWith("/api/automation")
      && request.method() === "PUT"
      && request.headers()["x-shop-account"] === "shop-ui-2");
    const slowRuleResponse = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "PUT"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.click("#saveReplyRuleButton");
    await slowRuleRequest;
    assert.equal(await page.locator("#saveReplyRuleButton").isDisabled(), true, "the rules submit button must stay disabled during a rules PUT");
    assert.equal(await page.locator("#replyRuleList [data-edit-rule], #replyRuleList [data-remove-rule]").evaluateAll((buttons) => buttons.every((button) => button.disabled)), true, "same-type rule actions must stay disabled during a rules PUT");
    await page.locator("#replyRuleForm").evaluate((form) => {
      form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));
    });
    assert.match(await page.locator("#replyRuleMessage").textContent(), /同类设置正在保存/, "a concurrent rules submit must be rejected locally");

    const defaultAutomationLoad = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "default");
    await page.selectOption("#automationShopSelect", "default");
    await defaultAutomationLoad;
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");
    const defaultPanelLoad = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "default");
    await openView(page, "auto-reply");
    await defaultPanelLoad;
    await page.waitForFunction(() => document.querySelector("#replyRuleList")?.textContent.includes("默认店规则"));
    await page.fill("#replyRuleName", "默认店未保存草稿");
    const completedSlowRuleResponse = await slowRuleResponse;
    await completedSlowRuleResponse.finished();
    await page.waitForTimeout(20);
    assert.equal(await page.inputValue("#automationShopSelect"), "default", "the late shop-ui-2 response must not switch the active automation account");
    assert.equal(await page.inputValue("#automationFirstReply"), "默认店首次回复", "the late shop-ui-2 response must not replace default settings");
    assert.match(await page.locator("#replyRuleList").textContent(), /默认店规则/);
    assert.doesNotMatch(await page.locator("#replyRuleList").textContent(), /备用店慢规则|备用店延迟保存规则/);
    assert.equal(await page.inputValue("#replyRuleName"), "默认店未保存草稿", "the late response must not clear the new shop's rule form");
    assert.equal(fixtures.automationPuts.length, slowRulePutCount + 1, "only the original slow rules PUT may reach the backend");

    fixtures.accountData["shop-ui-2"].automation = originalShopAutomation;
    if (originalDefaultAccountData === undefined) delete fixtures.accountData.default;
    else fixtures.accountData.default = originalDefaultAccountData;
    const restoredShopLoad = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await page.selectOption("#automationShopSelect", "shop-ui-2");
    await restoredShopLoad;
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店（运营）");
    const restoredAutomationPanel = page.waitForResponse((response) => response.url().endsWith("/api/automation")
      && response.request().method() === "GET"
      && response.request().headers()["x-shop-account"] === "shop-ui-2");
    await openView(page, "auto-reply");
    await restoredAutomationPanel;
    assert.equal(await page.inputValue("#automationShopSelect"), "shop-ui-2");
    assert.equal(await page.inputValue("#automationFirstReply"), fixtures.automation.first_reply);

    await assertNoOverflow(page, "auto-reply desktop");
    // AI 客服设置面向普通店主，只使用自然语言内容，并保留五种模型连接格式。
    assert.equal(await page.evaluate(() => document.querySelector('[data-panel="auto-reply"] [data-member-only="true"]') === null), true, "auto-reply page has no membership-gated AI card");
    await openView(page, "ai-config");
    await page.waitForSelector('[data-panel="ai-config"]:not([hidden])');
    await page.waitForFunction(() => document.querySelector("#aiPersonaStatus")?.textContent === "未填写");
    assert.deepEqual(await page.locator('[data-panel="ai-config"] .sub-tab-btn').allTextContents(), ["会话工作台", "AI 客服设置", "规则客服"]);
    assert.equal(await page.locator("#aiPreviewTitle").textContent(), "连续对话沙盘");
    assert.equal(await page.locator("#aiProductSearch").getAttribute("placeholder"), "搜索商品标题");
    assert.deepEqual(
      await page.locator("#aiProvider option").evaluateAll((options) => options.map((option) => [option.value, option.textContent])),
      [["openai_chat_completions", "OpenAI / 兼容接口"], ["openai_responses", "OpenAI Responses"], ["anthropic_messages", "Anthropic Claude"], ["google_gemini", "Google Gemini"], ["ollama_chat", "Ollama 本地服务"]],
    );
    const aiPanelText = await page.locator('[data-panel="ai-config"]').innerText();
    for (const removed of ["aiPersonaJson", "aiKnowledgeJson", "批准发布", "结构化知识库", "配置文件", "知识命中", "智能生成配置"]) assert.equal(aiPanelText.includes(removed), false);
    const htmlSource = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
    const appSource = fs.readFileSync(path.join(staticRoot, "assets", "app.js"), "utf8");
    const cssSource = fs.readFileSync(path.join(staticRoot, "assets", "app.css"), "utf8");
    assert.equal(htmlSource.includes("aiPersonaJson"), false);
    assert.equal(htmlSource.includes("aiKnowledgeJson"), false);
    assert.equal(htmlSource.includes("introCurtain"), false);
    assert.equal(htmlSource.includes("enterWorkspaceButton"), false);
    assert.equal(htmlSource.includes("20260826-03"), false);
    assert.match(htmlSource, /app\.css\?v=20260831-01/);
    assert.match(htmlSource, /app\.js\?v=20260831-01/);
    assert.match(appSource, /ASSET_VERSION = "20260831-01"/);
    assert.equal(appSource.includes("dismissIntroCurtain"), false);
    assert.equal(cssSource.includes("intro-curtain"), false);
    assert.equal(cssSource.includes("spotlight-stage"), false);
    assert.equal(cssSource.includes("docs-github-card"), false);
    assert.equal(await page.inputValue("#aiStoreContent"), "");
    assert.equal(await page.inputValue("#aiKnowledgeContent"), "");

    const configWritesBeforeEmpty = fixtures.aiRequests.filter((request) => request.kind === "config").length;
    for (const invalidContent of ["  ...  ", "N/A", "暂无", "待补充", "请填写店铺与客服说明", "Please enter store details", "As an AI language model, I can help organize this.", "```json\n{\"content\":\"待补充\"}\n```", "{\"content\":\"待补充\"}"]) {
      await page.fill("#aiStoreContent", invalidContent);
      await page.click("#aiSavePersona");
      await page.waitForFunction(() => document.querySelector("#aiPersonaMessage")?.textContent.includes("空内容不会生效"));
      assert.equal(fixtures.aiRequests.filter((request) => request.kind === "config").length, configWritesBeforeEmpty);
    }

    await page.fill("#aiBaseUrl", "https://example.com/v1");
    await page.fill("#aiModel", "fixture-model");
    await page.fill("#aiApiKey", "fixture-ui-secret");
    const aiTestResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/connection/test") && response.request().method() === "POST");
    await page.click("#aiTestConnection");
    await aiTestResponse;
    const aiSaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/connection") && response.request().method() === "PUT");
    await page.click("#aiSaveConnection");
    await aiSaveResponse;
    await page.waitForFunction(() => document.querySelector("#aiKeyState")?.textContent === "已安全保存");
    assert.equal(fixtures.aiRequests.findLast((item) => item.kind === "connection").payload.provider, "openai_chat_completions");
    assert.equal(await page.locator("#aiApiKey").inputValue(), "");

    const testsBeforeProviderSwitch = fixtures.aiRequests.filter((item) => item.kind === "connection-test").length;
    await page.selectOption("#aiProvider", "anthropic_messages");
    await page.click("#aiTestConnection");
    await page.waitForFunction(() => document.querySelector("#aiConnectionMessage")?.textContent.includes("切换接口格式后请输入对应的 API Key"));
    assert.equal(fixtures.aiRequests.filter((item) => item.kind === "connection-test").length, testsBeforeProviderSwitch);
    await page.fill("#aiApiKey", "anthropic-ui-secret");
    const anthropicResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/connection/test") && response.request().method() === "POST");
    await page.click("#aiTestConnection");
    await anthropicResponse;
    assert.equal(fixtures.aiRequests.findLast((item) => item.kind === "connection-test").payload.provider, "anthropic_messages");
    await page.selectOption("#aiProvider", "ollama_chat");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    assert.equal(await page.locator("#aiApiKey").getAttribute("required"), null);
    await page.selectOption("#aiProvider", "openai_responses");
    assert.match(await page.locator("#aiBaseUrl").getAttribute("placeholder"), /api\.openai\.com\/v1/);
    await page.selectOption("#aiProvider", "google_gemini");
    assert.match(await page.locator("#aiBaseUrl").getAttribute("placeholder"), /generativelanguage\.googleapis\.com\/v1beta/);
    await page.selectOption("#aiProvider", "openai_chat_completions");
    const retestSavedProvider = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/connection/test") && response.request().method() === "POST");
    await page.click("#aiTestConnection");
    await retestSavedProvider;
    const resaveSavedProvider = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/connection") && response.request().method() === "PUT");
    await page.click("#aiSaveConnection");
    await resaveSavedProvider;

    await page.fill("#aiStoreContent", "本店主营数字学习资料，先回答当前商品问题；价格和状态以实时商品信息为准。营业时间为 9:00—23:00。");
    await page.fill("#aiPersonaName", "小鲸客服");
    await page.fill("#aiBuyerAddress", "亲");
    await page.fill("#aiForbiddenClaims", "不能编造价格、库存或付款状态\n不能承诺未经核验的发货结果");
    await page.fill("#aiHandoffRules", "退款、争议或投诉时转人工\n事实不足或冲突时转人工");
    const personaResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/config") && response.request().method() === "PUT");
    await page.click("#aiSavePersona");
    await personaResponse;
    await page.waitForFunction(() => document.querySelector("#aiPersonaStatus")?.textContent.includes("已保存"));
    const configWrite = fixtures.aiRequests.findLast((request) => request.kind === "config");
    assert.equal(configWrite.payload.store_content.includes("数字学习资料"), true);
    assert.equal(typeof configWrite.payload.forbidden_claims, "string");
    assert.equal(Object.prototype.hasOwnProperty.call(configWrite.payload, "config"), false);

    await page.click("#aiOpenTemplates");
    await page.waitForSelector("#aiTemplatesDialog[open]");
    await page.fill("#aiTemplateName", "售前客服模板");
    const templateSaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/ai/templates") && response.request().method() === "POST");
    await page.click("#aiSaveTemplate");
    await templateSaveResponse;
    await page.waitForFunction(() => document.querySelectorAll("#aiTemplateList [data-ai-template-row]").length === 1);
    assert.equal(fixtures.aiRequests.findLast((request) => request.kind === "template").payload.config.store_content.includes("数字学习资料"), true);
    await page.click('[data-close-dialog="aiTemplatesDialog"]');

    const knowledgeWritesBeforeEmpty = fixtures.aiRequests.filter((request) => request.kind === "knowledge").length;
    for (const invalidContent of [" ... ", "N/A", "暂无", "待补充", "请填写商品补充内容", "Please enter product details", "As an AI language model, I can help organize this.", "```json\n{\"content\":\"待补充\"}\n```", "{\"content\":\"待补充\"}"]) {
      await page.fill("#aiKnowledgeContent", invalidContent);
      await page.click("#aiSaveKnowledge");
      await page.waitForFunction(() => document.querySelector("#aiKnowledgeMessage")?.textContent.includes("空白内容不会生效"));
      assert.equal(fixtures.aiRequests.filter((request) => request.kind === "knowledge").length, knowledgeWritesBeforeEmpty);
    }

    const extractsBeforePlaceholders = fixtures.aiRequests.filter((request) => request.kind === "extract").length;
    for (const invalidContent of ["...", "N/A", "暂无", "待补充", "请粘贴商品说明", "```json\n{\"content\":\"待补充\"}\n```", "{\"content\":\"待补充\"}"]) {
      await page.fill("#aiExtractInput", invalidContent);
      await page.click("#aiExtractKnowledge");
      await page.waitForFunction(() => document.querySelector("#aiKnowledgeMessage")?.textContent.includes("有实际信息的商品说明"));
      assert.equal(fixtures.aiRequests.filter((request) => request.kind === "extract").length, extractsBeforePlaceholders);
    }

    await page.fill("#aiExtractInput", "付款后提供完整使用步骤，适合第一次使用的买家，争议问题转人工。");
    fixtures.aiExtractResponses.push({ content: "" }, { content: "```json\n{\"content\":\"错误\"}\n```" }, { content: "以下是我为你整理的内容" }, { content: "请填写商品说明" }, { content: "As an AI language model, I can help organize this." });
    for (const expected of ["没有返回可采用", "代码块或配置内容", "说明文字而不是商品内容", "没有返回可采用", "没有返回可采用"]) {
      const response = page.waitForResponse((item) => item.url().includes("/api/bot/ai/products/") && item.url().endsWith("/extract") && item.request().method() === "POST");
      await page.click("#aiExtractKnowledge");
      await response;
      await page.waitForFunction((message) => document.querySelector("#aiKnowledgeMessage")?.textContent.includes(message), expected);
      assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true);
      assert.equal(fixtures.aiRequests.filter((request) => request.kind === "knowledge").length, knowledgeWritesBeforeEmpty);
    }

    await page.fill("#aiKnowledgeContent", "店主原有的未保存内容");
    const knowledgeBeforeGeneration = await page.inputValue("#aiKnowledgeContent");
    const extractResponse = page.waitForResponse((response) => response.url().includes("/api/bot/ai/products/") && response.url().endsWith("/extract") && response.request().method() === "POST");
    await page.click("#aiExtractKnowledge");
    await extractResponse;
    await page.waitForSelector("#aiGeneratedKnowledgePreview:not([hidden])");
    assert.equal(await page.inputValue("#aiKnowledgeContent"), knowledgeBeforeGeneration);
    assert.equal(fixtures.aiRequests.filter((request) => request.kind === "knowledge").length, knowledgeWritesBeforeEmpty);
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "AI organized content preview desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "ai-generated-preview-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "AI organized content preview mobile");
    await captureScreenshot(page, { path: path.join(resultRoot, "ai-generated-preview-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.click("#aiApplyGeneratedKnowledge");
    await page.waitForSelector("#confirmDialog[open]");
    assert.equal(await page.locator("#confirmTitle").textContent(), "采用整理建议");
    assert.equal(await page.inputValue("#aiKnowledgeContent"), knowledgeBeforeGeneration);
    await page.click("#confirmAction");
    await page.waitForFunction(() => document.querySelector("#aiKnowledgeEditMode")?.textContent.includes("尚未保存"));
    assert.notEqual(await page.inputValue("#aiKnowledgeContent"), knowledgeBeforeGeneration);
    assert.equal(fixtures.aiRequests.filter((request) => request.kind === "knowledge").length, knowledgeWritesBeforeEmpty);
    const knowledgeSaveResponse = page.waitForResponse((response) => response.url().includes("/api/bot/ai/products/") && response.url().endsWith("/knowledge") && response.request().method() === "PUT");
    await page.click("#aiSaveKnowledge");
    await knowledgeSaveResponse;
    await page.waitForFunction(() => document.querySelector("#aiKnowledgeStatus")?.textContent.includes("已保存"));
    const knowledgeWrite = fixtures.aiRequests.findLast((request) => request.kind === "knowledge");
    assert.equal(typeof knowledgeWrite.payload.content, "string");
    assert.equal(Object.prototype.hasOwnProperty.call(knowledgeWrite.payload, "knowledge"), false);

    const questions = ["这个商品现在价格是多少？", "这个商品怎么使用？", "如果有售后问题怎么办？"];
    const replies = [];
    for (const question of questions) {
      await page.fill("#aiPreviewInput", question);
      const response = page.waitForResponse((item) => item.url().endsWith("/api/bot/ai/preview") && item.request().method() === "POST");
      await page.click("#aiRunPreview");
      await response;
      await page.waitForFunction(() => document.querySelector("#aiPreviewOutput")?.textContent.includes("实际回复"));
      replies.push(await page.locator("#aiPreviewOutput .ai-preview-answer div").textContent());
    }
    assert.equal(new Set(replies).size, 3);
    const previewRequests = fixtures.aiPreviewRequests.slice(-3);
    assert.deepEqual(previewRequests.map((request) => request.payload.buyer_message), questions);
    assert.equal(previewRequests.every((request) => !Object.prototype.hasOwnProperty.call(request.payload, "current_question")), true);
    assert.equal(previewRequests[1].payload.history.at(-1).content, replies[0]);
    assert.equal(previewRequests[2].payload.history.some((message) => message.content === questions[1]), true);
    const previewText = await page.locator("#aiPreviewOutput").innerText();
    for (const label of ["实时事实", "店铺内容", "商品补充", "会话", "内容状态", "安全状态"]) assert.equal(previewText.includes(label), true);
    assert.equal(previewText.includes("知识命中"), false);

    fixtures.aiPreviewResponseDelays.push(300);
    await page.fill("#aiPreviewInput", "迟到的价格问题");
    const latePreviewRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/ai/preview") && request.method() === "POST");
    await page.click("#aiRunPreview");
    await latePreviewRequest;
    const nextAiProduct = page.locator("#aiProductList [data-ai-product]:not(.is-active)").first();
    const nextAiProductTitle = await nextAiProduct.locator("strong").textContent();
    await nextAiProduct.click();
    await page.waitForFunction((title) => document.querySelector("#aiKnowledgeProductTitle")?.textContent === title, nextAiProductTitle);
    await page.waitForTimeout(350);
    assert.equal(await page.locator("#aiKnowledgeProductTitle").textContent(), nextAiProductTitle);
    assert.equal((await page.locator("#aiPreviewOutput").innerText()).includes("迟到的价格问题"), false);

    // A late extraction for the previous product cannot restore its input or
    // preview, and its finally block cannot unlock a newer product request.
    const staleExtractProductId = await page.locator("#aiProductList [data-ai-product].is-active").getAttribute("data-ai-product");
    fixtures.aiExtractResponseDelays.push(700, 1100);
    await page.fill("#aiExtractInput", "旧商品整理内容，不得进入新商品。");
    const staleExtractResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${staleExtractProductId}/extract`) && response.request().method() === "POST");
    await page.click("#aiExtractKnowledge");
    const extractTarget = page.locator("#aiProductList [data-ai-product]:not(.is-active)").first();
    const extractTargetId = await extractTarget.getAttribute("data-ai-product");
    const extractTargetTitle = await extractTarget.locator("strong").textContent();
    await extractTarget.click();
    await page.waitForFunction((title) => document.querySelector("#aiKnowledgeProductTitle")?.textContent === title, extractTargetTitle);
    assert.equal(await page.inputValue("#aiExtractInput"), "", "switching products clears the extraction input immediately");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true, "switching products clears the generated preview immediately");
    assert.equal(await page.locator("#aiKnowledgeMessage").textContent(), "", "switching products clears the previous product status");
    assert.equal(await page.locator("#confirmDialog").isVisible(), false, "switching products clears the pending confirmation context");
    await page.fill("#aiExtractInput", "Current product setup notes for first-time buyers.");
    const currentExtractResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${extractTargetId}/extract`) && response.request().method() === "POST");
    await page.click("#aiExtractKnowledge");
    await staleExtractResponse;
    assert.equal(await page.locator("#aiExtractKnowledge").isDisabled(), true, "a stale extraction finally must not unlock the current product request");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true, "a stale extraction response must not reveal the previous product preview");
    await currentExtractResponse;
    await page.waitForSelector("#aiGeneratedKnowledgePreview:not([hidden])");
    assert.match(await page.locator("#aiGeneratedKnowledgeRaw").textContent(), /Current product setup notes/);
    assert.doesNotMatch(await page.locator("#aiGeneratedKnowledgeRaw").textContent(), /旧商品整理内容/);
    assert.equal(await page.locator("#aiExtractKnowledge").isDisabled(), false);
    await page.click("#aiDiscardGeneratedKnowledge");

    // Saving uses the same account + product + product generation + request
    // generation snapshot, so an old product response cannot overwrite or
    // unlock a newer product save.
    fixtures.aiKnowledgeResponseDelays.push(700, 1100);
    await page.fill("#aiKnowledgeContent", "旧商品待保存内容，不得覆盖新商品。");
    const staleSaveResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${extractTargetId}/knowledge`) && response.request().method() === "PUT");
    await page.click("#aiSaveKnowledge");
    const saveTarget = page.locator("#aiProductList [data-ai-product]:not(.is-active)").first();
    const saveTargetId = await saveTarget.getAttribute("data-ai-product");
    const saveTargetTitle = await saveTarget.locator("strong").textContent();
    page.once("dialog", (dialog) => dialog.accept());
    await saveTarget.click();
    await page.waitForFunction((title) => document.querySelector("#aiKnowledgeProductTitle")?.textContent === title, saveTargetTitle);
    assert.equal(await page.inputValue("#aiExtractInput"), "");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true);
    assert.equal(await page.locator("#aiKnowledgeMessage").textContent(), "");
    await page.fill("#aiKnowledgeContent", "Suitable for first-time buyers. Follow the setup guide after payment.");
    const currentSaveResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${saveTargetId}/knowledge`) && response.request().method() === "PUT");
    await page.click("#aiSaveKnowledge");
    await staleSaveResponse;
    assert.equal(await page.locator("#aiSaveKnowledge").isDisabled(), true, "a stale save finally must not unlock the current product save");
    assert.equal(await page.inputValue("#aiKnowledgeContent"), "Suitable for first-time buyers. Follow the setup guide after payment.", "a stale save response must not overwrite the current editor");
    await currentSaveResponse;
    await page.waitForFunction(() => document.querySelector("#aiKnowledgeMessage")?.textContent.includes("已保存并用于回答"));
    assert.equal(await page.locator("#aiSaveKnowledge").isDisabled(), false);

    // Switching shops invalidates the old account and product scope at once.
    // The old shop response cannot restore UI or unlock a new-shop request.
    fixtures.aiExtractResponseDelays.push(1000, 1400);
    await page.fill("#aiExtractInput", "旧店铺商品内容，不得带到另一店铺。");
    const oldShopKey = await page.locator("#accountTabs .account-tab.is-active").getAttribute("data-account-switch");
    const newShopKey = oldShopKey === "default" ? "shop-ui-2" : "default";
    const newShopName = fixtures.shopAccounts.find((item) => item.key === newShopKey)?.name || newShopKey;
    const oldShopExtractResponse = page.waitForResponse((response) => response.url().includes("/api/bot/ai/products/") && response.url().endsWith("/extract") && response.request().headers()["x-shop-account"] === oldShopKey);
    await page.click("#aiExtractKnowledge");
    await page.click(`#accountTabs [data-account-switch="${newShopKey}"]`);
    assert.equal(await page.inputValue("#aiExtractInput"), "", "switching shops clears the extraction input immediately");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true, "switching shops clears the generated preview immediately");
    assert.equal(await page.locator("#aiKnowledgeMessage").textContent(), "", "switching shops clears the AI product status immediately");
    assert.equal(await page.locator("#confirmDialog").isVisible(), false, "switching shops clears the pending confirmation context");
    await page.waitForFunction((name) => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === name, newShopName);
    await openView(page, "ai-config");
    await page.waitForFunction(() => document.querySelector("#aiKnowledgeProductTitle")?.textContent !== "尚未选择商品");
    await page.fill("#aiExtractInput", "Current shop product notes for English-speaking buyers.");
    const newShopExtractResponse = page.waitForResponse((response) => response.url().includes("/api/bot/ai/products/") && response.url().endsWith("/extract") && response.request().headers()["x-shop-account"] === newShopKey);
    await page.click("#aiExtractKnowledge");
    await oldShopExtractResponse;
    assert.equal(await page.locator("#aiExtractKnowledge").isDisabled(), true, "an old-shop finally must not unlock the new-shop extraction");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true, "an old-shop response must not reveal stale content");
    await newShopExtractResponse;
    await page.waitForSelector("#aiGeneratedKnowledgePreview:not([hidden])");
    assert.match(await page.locator("#aiGeneratedKnowledgeRaw").textContent(), /Current shop product notes/);
    assert.doesNotMatch(await page.locator("#aiGeneratedKnowledgeRaw").textContent(), /旧店铺商品内容/);
    await page.click("#aiDiscardGeneratedKnowledge");

    await waitForPanelSettled(page);
    await assertNoOverflow(page, "AI customer-service settings desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "ai-config-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "AI customer-service settings mobile");
    await captureScreenshot(page, { path: path.join(resultRoot, "ai-config-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.click('#sideNav [data-view="chat"]');
    await page.waitForSelector('[data-panel="chat"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    if (await page.locator('#conversationItems [data-chat-id="chat-1"]').count()) await page.click('#conversationItems [data-chat-id="chat-1"]');
    assert.equal((await page.locator("#chatAiStatus").textContent()).includes("会员"), false, "AI controls remain independent of subscription language");

    // Switching accounts while a reply POST is pending invalidates the old
    // form context. Its late success may not repopulate or unlock the new one.
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#chatMessages .message-row").length >= 2);
    fixtures.manualReplyPostMode = "success";
    fixtures.manualReplyPollMode = "success";
    fixtures.manualReplyPostDelayMs = 300;
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
    await page.fill("#manualReplyInput", "切店铺时仍在发送");
    const accountSwitchReplyRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/messages/reply") && request.method() === "POST");
    await page.click('#manualReplyForm button[type="submit"]');
    await accountSwitchReplyRequest;
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    const currentAccountKey = await page.locator("#shopAccountsPanelList .shop-card.is-current").getAttribute("data-account-key");
    const nextAccountKey = currentAccountKey === "default" ? "shop-ui-2" : "default";
    const nextAccountName = fixtures.shopAccounts.find((item) => item.key === nextAccountKey)?.name || "海风数字店";
    fixtures.accountData["shop-ui-2"] = {
      products: [fixtures.products[1]],
      automation: { ...fixtures.automation, rules: fixtures.automation.rules.map((item) => ({ ...item })), deliveries: fixtures.automation.deliveries.map((item) => ({ ...item })) },
      conversations: [{ ...fixtures.conversations[0] }],
      quickReplies: [{ id: "shop-second", title: "备用店", content: "这是备用店快捷短语。" }],
      orders: [],
    };
    await page.click(`#shopAccountsPanelList [data-account-switch="${nextAccountKey}"]`);
    await page.waitForFunction((name) => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === name, nextAccountName);
    assert.equal(await page.inputValue("#manualReplyInput"), "", "switching shops must clear the previous reply body");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "switching shops must clear the previous reply status");
    await page.waitForTimeout(350);
    assert.equal(await page.inputValue("#manualReplyInput"), "", "a late response from the previous shop must be ignored");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "a late response from the previous shop must not update status");

    // Store-scoped loaders must follow the selected account, not just the
    // header label. The fixture deliberately gives the second shop a smaller
    // catalog so a stale response would be visible here.
    await page.click('#sideNav [data-view="goods"]');
    await page.waitForFunction((expected) => document.querySelectorAll("#productGrid .product-row").length === expected, nextAccountKey === "default" ? fixtures.products.length : 1);
    if (nextAccountKey === "default") {
      await openView(page, "shops");
      await page.waitForSelector('[data-panel="shops"]:not([hidden])');
      await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
      await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店（运营）");
      await page.click('#sideNav [data-view="goods"]');
      await page.waitForFunction(() => document.querySelectorAll("#productGrid .product-row").length === 1);
      assert.equal(await page.locator("#productGrid .product-title").textContent(), fixtures.products[1].title);
      await openView(page, "shops");
      await page.waitForSelector('[data-panel="shops"]:not([hidden])');
      await page.click('#shopAccountsPanelList [data-account-switch="default"]');
      await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");
    }

    // Deleting a non-default shop is a confirmed, scoped operation; the
    // default shop remains visible and its delete action stays disabled.
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    await page.click('[data-account-delete="shop-ui-2"]');
    await page.waitForSelector("#confirmDialog[open]");
    assert.equal(await page.locator("#confirmTitle").textContent(), "删除店铺");
    await page.click("#confirmAction");
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 1);
    assert.deepEqual(fixtures.shopAccountDeleteRequests, ["shop-ui-2"], "delete must call the scoped DELETE endpoint once");
    assert.equal(await page.locator('[data-account-delete="default"]').isDisabled(), true, "default shop deletion must remain protected after cleanup");

    // The same guarantee applies to logout, so reply content cannot remain for
    // the next person who opens the login screen in this browser.
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    await page.click('[data-chat-id="chat-1"]');
    await page.waitForFunction(() => document.querySelectorAll("#chatMessages .message-row").length >= 2 && document.querySelector("#chatMessages")?.textContent.includes("你好，这个商品怎么使用"));
    fixtures.manualReplyPostDelayMs = 300;
    await page.fill("#manualReplyInput", "退出时仍在发送");
    const logoutReplyRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/messages/reply") && request.method() === "POST");
    await page.click('#manualReplyForm button[type="submit"]');
    await logoutReplyRequest;
    await page.click("#logoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");
    assert.equal(await page.inputValue("#manualReplyInput"), "", "logout must clear the reply body");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "logout must clear reply status");
    await page.waitForTimeout(350);
    assert.equal(await page.inputValue("#manualReplyInput"), "", "a late reply response after logout must be ignored");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "a late reply response after logout must not restore status");

    // Platform administrators receive only account metadata, security events and
    // signed-update controls inside the existing project documentation domain.
    fixtures.me = {
      username: "admin-demo", expires_at: 0, active: false, plan: "free", plan_label: "免费",
      role: "admin", role_label: "管理员", is_admin: true, permissions: selfUsePermissions,
      platform_permissions: ["platform.audit.read", "platform.settings.manage", "platform.updates.manage", "platform.users.manage"],
    };
    await page.fill("#authUsername", "admin-demo");
    await page.fill("#authPassword", "Admin-Pass-123!");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await openView(page, "docs");
    await page.waitForFunction(() => Array.from(document.querySelectorAll("[data-docs-tab]")).filter((node) => !node.hidden).length === 4);
    assert.deepEqual(
      await page.locator('[data-docs-tab]:visible').allTextContents(),
      ["使用说明", "版本与更新", "账号与权限", "安全记录"],
      "administration must remain inside the existing project domain",
    );

    await page.click('[data-docs-tab="accounts"]');
    await page.waitForSelector('[data-docs-panel="accounts"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#adminUsersBody [data-admin-user-id]").length === 2);
    assert.match(await page.locator('[data-admin-user-id="2"]').textContent(), /owner-demo/);
    assert.doesNotMatch(await page.locator('[data-admin-user-id="2"]').textContent(), /Cookie|订单正文|库存正文/);
    await page.check("#registrationOpenToggle");
    await page.selectOption("#updateChannelSelect", "beta");
    await page.click('#platformSettingsForm button[type="submit"]');
    await page.waitForFunction(() => document.querySelector("#platformSettingsMessage")?.textContent.includes("已保存"));
    assert.deepEqual(fixtures.adminSettingRequests.at(-1), { registration_open: true, update_channel: "beta" });
    const ownerAdminRow = page.locator('[data-admin-user-id="2"]');
    const unlockResponse = page.waitForResponse((response) => response.url().endsWith("/api/admin/users/2/unlock") && response.request().method() === "POST");
    await ownerAdminRow.locator('[data-admin-user-action="unlock"]').click();
    assert.equal((await unlockResponse).status(), 200);
    await page.waitForFunction(() => {
      const row = document.querySelector('[data-admin-user-id="2"]');
      return row && !row.textContent.includes("登录锁定") && row.querySelector('[data-admin-user-action="unlock"]')?.disabled === true;
    });
    assert.ok(fixtures.adminUserRequests.some((item) => item.action === "unlock" && item.userId === 2));
    await ownerAdminRow.locator("[data-admin-user-role]").selectOption("admin");
    const roleChangeResponse = page.waitForResponse((response) => response.url().endsWith("/api/admin/users/2") && response.request().method() === "PATCH");
    await ownerAdminRow.locator('[data-admin-user-action="save"]').click();
    assert.equal((await roleChangeResponse).status(), 200);
    await page.waitForTimeout(100);
    assert.equal(fixtures.adminUsers.find((item) => item.id === 2)?.role, "admin");
    assert.equal(await page.locator('[data-admin-user-id="2"] [data-admin-user-role]').inputValue(), "admin");
    assert.ok(fixtures.adminUserRequests.some((item) => item.action === "patch" && item.userId === 2 && item.payload.role === "admin"));

    await page.click('[data-docs-tab="audit"]');
    await page.waitForSelector('[data-docs-panel="audit"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#auditEventList .audit-event").length === 1);
    assert.match(await page.locator("#auditEventList").textContent(), /登录成功/);
    assert.doesNotMatch(await page.locator("#auditEventList").textContent(), /Admin-Pass-123|bootstrap-ui-contract-token/);

    await page.click('[data-docs-tab="version"]');
    await page.waitForSelector('[data-docs-panel="version"]:not([hidden])');
    assert.equal(await page.locator("#adminUpdateControls").isVisible(), true);
    await page.click("#checkUpdateButton");
    await page.waitForFunction(() => document.querySelector("#updateActionMessage")?.textContent.includes("v0.2.0"));
    assert.equal(fixtures.updateRequests.at(-1).action, "check");
    assert.match(await page.locator("#versionReleaseNotes").textContent(), /<script>/, "network release notes must remain visible as text");
    assert.equal(await page.locator("#versionReleaseNotes script").count(), 0);
    assert.notEqual(await page.evaluate(() => window.__releaseNotesInjected), true);
    await page.click("#downloadUpdateButton");
    await page.waitForFunction(() => document.querySelector("#updateActionMessage")?.textContent.includes("已校验"));
    assert.deepEqual(fixtures.updateRequests.at(-1), { action: "download", payload: { version: "0.2.0" } });
    await page.fill("#updateAdminPassword", "Admin-Pass-123!");
    await page.click("#applyUpdateButton");
    await page.waitForFunction(() => document.querySelector("#updateActionMessage")?.textContent.includes("独立 updater"));
    assert.deepEqual(fixtures.adminConfirmRequests.at(-1), { password: "Admin-Pass-123!", action: "update.apply" });
    assert.deepEqual(fixtures.updateRequests.at(-1), {
      action: "apply", payload: { version: "0.2.0", confirmation_token: "ui-one-time-confirmation" },
    });
    await page.fill("#updateAdminPassword", "Admin-Pass-123!");
    await page.selectOption("#rollbackVersionSelect", "0.0.9");
    await page.click("#rollbackUpdateButton");
    await page.waitForFunction(() => document.querySelector("#updateActionMessage")?.textContent.includes("回滚请求已提交"));
    assert.deepEqual(fixtures.adminConfirmRequests.at(-1), { password: "Admin-Pass-123!", action: "update.rollback" });
    assert.deepEqual(fixtures.updateRequests.at(-1), {
      action: "rollback", payload: { version: "0.0.9", confirmation_token: "ui-one-time-confirmation" },
    });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.click('[data-docs-tab="accounts"]');
    await page.waitForSelector('[data-docs-panel="accounts"]:not([hidden])');
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "admin account controls mobile");
    assert.ok(await page.evaluate(() => {
      const wrapper = document.querySelector(".docs-table-wrap");
      return wrapper && wrapper.scrollWidth >= wrapper.clientWidth;
    }), "wide account controls must remain inside their bounded scroll wrapper");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.click("#logoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");

    assert.deepEqual(fixtures.authorizationHeaders, [], "browser must not send bearer authorization");
    assert.equal(expectedQrFailureResponses, fixtures.qrSyncFailures, "retryable QR sync failures must match observed responses");
    assert.equal(expectedQrFailureConsole, fixtures.qrSyncFailures, "retryable QR sync failures must match browser console observations");
    assert.equal(expectedQrStageFailureResponses, fixtures.qrStageFailures, "the staged QR failure must be observed exactly once");
    assert.equal(expectedQrStageFailureConsole, fixtures.qrStageFailures, "only the staged QR 502 may reach the browser console");
    assert.equal(expectedQrStageCancelResponses, fixtures.qrStageCancelNotFound, "closing a terminal QR failure may observe only its expected 404");
    assert.equal(expectedQrStageCancelConsole, fixtures.qrStageCancelNotFound, "only the terminal QR cancel 404 may reach the browser console");
    assert.equal(expectedManualReplyFailureResponses, fixtures.manualReplyPostFailures, "manual reply POST failures must be observed exactly once");
    assert.equal(expectedManualReplyFailureConsole, fixtures.manualReplyPostFailures, "manual reply POST failures must be the only expected 503 console entries");
    assert.equal(expectedManualReplyNotFoundResponses, fixtures.manualReplyPollNotFoundResponses, "manual reply status 404s must be observed exactly once");
    assert.equal(expectedManualReplyNotFoundConsole, fixtures.manualReplyPollNotFoundResponses, "manual reply status 404s must be the only expected 404 console entries");
    assert.deepEqual(errors, [], "browser should have no page or console errors");
    assert.deepEqual(failedResponses, [], "browser should have no failed responses");
    console.log(JSON.stringify({
      ok: true,
      selfUseNav: 6,
      productCards: fixtures.products.length,
      templates: fixtures.templates.length,
      cards: fixtures.cardRequests.length,
      screenshots: resultRoot,
    }));
  } finally {
    await browser.close();
    await close(server);
  }
}

run().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});

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
const versionSource = fs.readFileSync(path.join(repoRoot, "backend", "version.py"), "utf8");
const assetVersion = versionSource.match(/^ASSET_VERSION\s*=\s*["']([^"']+)["']\s*$/m)?.[1] || "";
assert.match(assetVersion, /^[0-9]{8}-[0-9]{2}$/);
const desktopSettingsOpsScope = ["settings", "ops", "dashboard", "goods", "resources", "popover", "home-alerts"].includes(process.env.SAAS_UI_SCOPE);
const mockOnlyScope = process.env.SAAS_UI_SCOPE === "mock";
// Public documentation images are opt-in, never a side effect of a test scope.
const docsCaptureScope = process.env.SAAS_UI_SCOPE === "docs-capture";
const screenshotsEnabled = !docsCaptureScope && !desktopSettingsOpsScope && !mockOnlyScope && process.env.SAAS_UI_SCREENSHOTS !== "0";
if (screenshotsEnabled) fs.mkdirSync(resultRoot, { recursive: true });
for (const staleName of screenshotsEnabled ? [
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
  "manual-reply-multi-desktop.png", "manual-reply-multi-mobile.png",
  "docs-manual-desktop.png", "docs-manual-mobile.png",
] : []) {
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
const dockerUpdateCapabilities = { deployment: "docker", check: true, download: false, apply: false, rollback: false, reason: "update_installation_unsupported", instruction: "使用原 Compose 文件和本地覆盖配置重新构建，保留数据卷。" };
const orderStatusOptions = [
  ["all", "全部"], ["processing", "处理中"], ["sent", "已发送"], ["manual", "待人工"],
  ["retry", "待重试"], ["ended", "已结束"], ["exception", "其他异常"],
].map(([value, label]) => ({ value, label }));
function orderFixturePage(rows, params, accountKey) {
  const safeRows = rows.map((order) => {
    const group = order.status_group || ({ delivered: "sent", manual_review: "manual", retry: "retry", failed: "exception", cancelled: "ended", expired: "ended" }[order.status] || "processing");
    return { platform_order_id: "", item_title: "", buyer_id: "", chat_id: "", conversation_available: false,
      reason_code: "", reason_label: "", delivery_type_label: "未获取", platform_status_label: "未获取", ...order,
      status_group: group, status_label: order.status_label || orderStatusOptions.find((opt) => opt.value === group)?.label || "未知状态" };
  });
  if (!params.has("page") && !params.has("page_size")) return { orders: safeRows.slice(0, Number(params.get("limit") || 50)) };
  const q = (params.get("q") || "").toLowerCase();
  const field = params.get("search_field") || "all";
  const searchKeys = { all: ["order_key", "platform_order_id", "item_id", "buyer_id"], order_id: ["order_key", "platform_order_id"], item_id: ["item_id"], buyer_id: ["buyer_id"] }[field] || [];
  const start = params.get("created_from") ? Date.parse(params.get("created_from")) : -Infinity;
  const end = params.get("created_to") ? Date.parse(params.get("created_to")) : Infinity;
  const matched = safeRows.filter((row) => (!q || searchKeys.some((key) => String(row[key] || "").toLowerCase().includes(q)))
    && (!(Number.isFinite(start) || Number.isFinite(end)) || (Date.parse(row.created_at) >= start && Date.parse(row.created_at) < end)));
  const counts = Object.fromEntries(orderStatusOptions.map((opt) => [opt.value, 0]));
  counts.all = matched.length;
  for (const row of matched) counts[row.status_group] += 1;
  const status = params.get("status") || "all";
  const selected = matched.filter((row) => status === "all" || row.status_group === status)
    .sort((a, b) => (Date.parse(b.created_at) - Date.parse(a.created_at)) || String(b.order_key).localeCompare(String(a.order_key)));
  const pageSize = Number(params.get("page_size") || 15);
  const pages = Math.ceil(selected.length / pageSize);
  const page = Math.min(Number(params.get("page") || 1), Math.max(pages, 1));
  return { account_key: accountKey, orders: selected.slice((page - 1) * pageSize, page * pageSize),
    page, page_size: pageSize, total: selected.length, total_pages: pages, status_counts: counts, status_options: orderStatusOptions };
}
const fixtures = {
  releaseCheckStatus: "available",
  orderQueries: [],
  orderQueryDelays: [],
  orderQueryError: false,
  authCapabilities: { registration_enabled: true, bootstrap_available: false, password_min_length: 12 },
  bootstrapRequests: [],
  passwordRequests: [],
  authLogoutRequests: 0,
  requestSequence: [],
  me: {
    username: "owner-demo", expires_at: 0, active: false, plan: "free", plan_label: "免费",
    role: "owner", role_label: "店主", is_admin: false, permissions: selfUsePermissions, platform_permissions: [],
  },
  version: {
    version: "0.1.0",
    commit: "ui-contract",
    build_time: "2026-08-31T00:00:00Z",
    asset_version: assetVersion,
    update_channel: "release",
    release_notes: "本地说明 <img src=x onerror=window.__releaseNotesInjected=true>",
    deployment: "docker", build_dirty: true, capabilities: { ...dockerUpdateCapabilities },
    update_check: { status: "unchecked", available: false, channel: "release", checked_at: null },
    latest_update: null,
  },
  adminSettings: {
    registration: { environment_allowed: true, database_open: false, users_exist: true, effective: false },
  },
  resourcePolicy: { max_shop_accounts: 20, max_running_workers: 3, worker_memory_mib: 400, revision: 0, source: "environment", updated_at: null },
  resourceRequests: [],
  resourceSaveRequests: [],
  resourceRowsByUser: {},
  resourceResponseDelayMs: 0,
  resourceError: false,
  resourceErrorOnce: false,
  resourceConflictOnce: false,
  deliveryStatusByAccount: {},
  deliveryStatusError: false,
  adminUsers: [
    { id: 1, username: "admin-demo", role: "admin", role_label: "管理员", enabled: true, locked: false, session_count: 1, created_at: 1788134400, password_changed_at: 1788134400 },
    { id: 2, username: "owner-demo", role: "owner", role_label: "店主", enabled: true, locked: true, session_count: 2, created_at: 1788134400, password_changed_at: 1788134400 },
  ],
  auditEvents: [
    { id: 1, event_type: "auth.login_succeeded", actor_user_id: 1, target_type: "user", target_id: "1", outcome: "success", source_hash: "source-hash", metadata: {}, created_at: 1788134400 },
  ],
  updateStatus: {
    current: { version: "0.1.0", commit: "ui-contract", build_time: "2026-08-31T00:00:00Z", asset_version: assetVersion, update_channel: "release" },
    latest_update: null,
    capabilities: { ...dockerUpdateCapabilities },
    update_check: { status: "unchecked", available: false, channel: "release", checked_at: null },
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
  analyticsByPeriod: null,
  analyticsRequests: [],
  analytics: {
    totals: { messages_total: 5, buyer_messages_total: 5, auto_replies_total: 3, fulfillment_success_total: 2, fulfillment_failed_total: 1, unread_conversations_total: 1 },
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
  // User settings and shop Agent sessions are strictly local fixtures. Agent
  // run receipts below simulate approved tool writes and never call a model.
  apiRequests: [],
  userConnections: new Map(),
  userConnectionTokens: new Map(),
  settingsRequests: [],
  settingsTestDelayMs: 0,
  opsRequests: [],
  opsReadResponses: [],
  opsWorkerTimers: new Set(),
  opsWorkerPaused: false,
  opsSiteSessionExpired: false,
  opsSessions: new Map(),
  opsCurrentSessions: new Map(),
  opsRuns: new Map(),
  opsChatsByRequest: new Map(),
  opsRetriesByRequest: new Map(),
  opsWrites: [],
  opsChatMode: "succeeded",
  opsChatDelayMs: 0,
  opsRunDelayMs: 0,
  opsRunWireOverrides: null,
  opsRunHold: false,
  opsMessagePageSize: 7,
  releaseCheckOverrides: null,
  aiPreviewRequests: [],
  aiPreviewResponseDelays: [],
  aiExtractResponses: [],
  aiExtractResponseDelays: [],
  aiKnowledgeResponseDelays: [],
  aiKnowledgeResponseGates: [],
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
  botStatusResponseGates: [],
  pendingBotStatusGates: new Set(),
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
  manualImageUploadDelayMs: 0,
  manualImageDeletes: [],
  manualImageDeleteMode: "success",
  manualImageDeleteModes: [],
  manualImageDeleteFailures: 0,
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

function holdNextBotStatus() {
  let release;
  const gate = {
    promise: new Promise((resolve) => { release = resolve; }),
    release() {
      release();
      fixtures.pendingBotStatusGates.delete(gate);
    },
  };
  fixtures.botStatusResponseGates.push(gate);
  fixtures.pendingBotStatusGates.add(gate);
  return gate;
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

function createManualReplyParts(content, media) {
  const parts = media.map((_item, index) => ({ index, kind: "image", status: index === 0 ? "queued" : "waiting" }));
  if (String(content || "").trim()) {
    parts.push({ index: parts.length, kind: "text", status: parts.length ? "waiting" : "queued" });
  }
  return parts;
}

function manualReplyStatusPayload(parent) {
  const current = parent.parts.find((part) => part.status !== "acknowledged");
  return {
    reply_id: parent.reply_id,
    outbox_id: parent.outbox_id,
    status: parent.status,
    attempts: parent.attempts,
    platform_acknowledged: parent.status === "acknowledged" && !current,
    current_part: current ? current.index : null,
    parts: parent.parts.map((part) => ({ ...part })),
  };
}

function manualReplyPendingMessage(parent) {
  const pending = parent.parts.filter((part) => part.status !== "acknowledged");
  if (!pending.length) return null;
  const pendingImageIndexes = pending.filter((part) => part.kind === "image").map((part) => part.index);
  const textPending = pending.some((part) => part.kind === "text");
  const media = pendingImageIndexes.map((partIndex) => {
    const mediaIndex = parent.parts.slice(0, partIndex + 1).filter((part) => part.kind === "image").length - 1;
    const source = parent.media[mediaIndex] || {};
    return { type: "image", url: "", alt: "图片", label: "图片", mime: source.mime || "image/jpeg" };
  });
  const content = textPending ? parent.content : "";
  return {
    role: "assistant_manual",
    content,
    content_type: media.length ? (content || media.length > 1 ? "rich" : "image") : "text",
    media,
    time: parent.time,
    chat_id: parent.chat_id,
    item_id: parent.item_id,
    reply_id: parent.reply_id,
    outbox_id: parent.outbox_id,
    delivery_status: parent.status,
    status: parent.status,
    attempts: parent.attempts,
    current_part: manualReplyStatusPayload(parent).current_part,
    parts: parent.parts.map((part) => ({ ...part })),
    account_key: parent.account_key,
  };
}

function manualReplyDisplayMessages(parent) {
  const messages = [];
  let imageNumber = 0;
  for (const part of parent.parts) {
    if (part.kind === "image") imageNumber += 1;
    if (part.status !== "acknowledged") continue;
    if (part.kind === "image") {
      messages.push({
        role: "assistant_manual",
        content: "",
        content_type: "image",
        media: [{ type: "image", url: `https://cdn.example/manual-${parent.outbox_id}-${imageNumber}.png`, alt: "图片", label: "图片" }],
        time: parent.time,
        chat_id: parent.chat_id,
        item_id: parent.item_id,
        delivery_status: "acknowledged",
        account_key: parent.account_key,
      });
    } else {
      messages.push({
        role: "assistant_manual",
        content: parent.content,
        content_type: "text",
        media: [],
        time: parent.time,
        chat_id: parent.chat_id,
        item_id: parent.item_id,
        delivery_status: "acknowledged",
        account_key: parent.account_key,
      });
    }
  }
  const pending = manualReplyPendingMessage(parent);
  if (pending) messages.push(pending);
  return messages;
}

function advanceManualReply(parent, mode, polls) {
  const terminal = mode === "dead_letter" ? "dead_letter" : mode === "manual_review" ? "manual_review" : "";
  if (terminal) {
    const current = parent.parts.find((part) => part.status !== "acknowledged");
    if (current) current.status = terminal;
    parent.status = terminal;
  } else if (mode === "pending") {
    const current = parent.parts.find((part) => part.status !== "acknowledged");
    if (current) current.status = "sending";
    parent.status = "sending";
  } else if (mode === "multipart_success" && parent.parts.length > 1 && polls <= 2) {
    parent.parts.forEach((part, index) => {
      part.status = index === 0 ? "acknowledged" : index === 1 ? (polls === 1 ? "sending" : "retry") : "waiting";
    });
    parent.status = polls === 1 ? "sending" : "retry";
  } else if (polls === 1) {
    const current = parent.parts.find((part) => part.status !== "acknowledged");
    if (current) current.status = "retry";
    parent.status = "retry";
  } else {
    parent.parts.forEach((part) => { part.status = "acknowledged"; });
    parent.status = "acknowledged";
  }
  parent.attempts = polls;
}

const mockConnectionProviders = [
  ["openai_chat_completions", "OpenAI / 兼容接口"], ["openai_responses", "OpenAI Responses"],
  ["anthropic_messages", "Anthropic Claude"], ["google_gemini", "Google Gemini"], ["ollama_chat", "Ollama 本地服务"],
].map(([code, label]) => ({ code, label, requires_api_key: code !== "ollama_chat" }));
function userConnectionFixture(username = fixtures.me.username) {
  if (!fixtures.userConnections.has(username)) {
    fixtures.userConnections.set(username, {
      scope: "user", initialized: false, provider: "openai_chat_completions", base_url: "", model: "",
      api_key_configured: false, connection_status: "unconfigured",
      revision: 0, key_revision: 0, last_error_code: "",
    });
  }
  return fixtures.userConnections.get(username);
}

function mockConnectionFingerprint(payload) {
  return JSON.stringify([payload.provider, payload.base_url, payload.model, payload.api_key || "", payload.expected_revision]);
}

function agentScopeKey(username, accountKey) { return `${username}:${accountKey}`; }
function createAgentSession(username, accountKey) {
  const session = { id: `mock-session-${fixtures.opsSessions.size + 1}`, shop_account_id: fixtures.shopAccounts.find((item) => item.key === accountKey)?.id || 1, account_key: accountKey, created_at: 1788825600, updated_at: 1788825600 };
  const stored = { username, accountKey, session, messages: [], latestRunId: "" };
  fixtures.opsSessions.set(session.id, stored);
  fixtures.opsCurrentSessions.set(agentScopeKey(username, accountKey), session.id);
  return stored;
}
function agentMessage(stored, role, content, extra = {}) {
  const seq = stored.messages.length + 1;
  stored.messages.push({ id: `${stored.session.id}-message-${seq}`, seq, role, content, ...extra });
}
function agentRunPayload(run, afterSeq = 0) {
  return { id: run.id, run_id: run.id, session_id: run.sessionId, status: run.status, request_id: run.requestId,
    created_at: 1788825600, updated_at: 1788825601, started_at: run.polls ? 1788825600 : null,
    finished_at: ["queued", "running", "cancel_requested"].includes(run.status) ? null : 1788825601,
    events: structuredClone(run.events.filter((event) => event.seq > afterSeq)),
    next_seq: run.events.at(-1)?.seq || 0, changed_count: run.changedCount, failed_count: run.failedCount,
    recoverable: run.recoverable === true, error: run.error ? structuredClone(run.error) : null };
}
function agentEvent(run, kind, detail) {
  const event = { seq: (run.events.at(-1)?.seq || 0) + 1, kind, created_at: 1788825600, ...detail };
  run.events.push(event);
  // Persisted visible events use assistant messages; kind, not role, identifies
  // a tool summary. A synthetic role=tool would hide real renderer regressions.
  agentMessage(fixtures.opsSessions.get(run.sessionId), "assistant", detail.content || detail.summary || "", { run_id: run.id, created_at: 1788825600, kind, summary: detail.summary || "", status: detail.status || run.status, targets: detail.targets || [], error: detail.error || null });
}
function recordAgentWrite(run, target = "100001") {
  if (!fixtures.opsWrites.some((item) => item.runId === run.id && item.target === target)) {
    fixtures.opsWrites.push({ runId: run.id, username: run.username, accountKey: run.accountKey, target });
    run.changedCount += 1;
  }
}
function advanceAgentRun(run) {
  if (["queued", "running"].includes(run.status) && run.polls++ === 0) {
    run.status = "running";
    agentEvent(run, "tool", { summary: "已查找当前店铺商品并读取配置", content: "", status: "succeeded", targets: ["DeepSeek 完整使用教程与常见问题处理"] });
    if (run.mode === "stop") {
      recordAgentWrite(run);
      agentEvent(run, "tool", { summary: "已保存第一项客服知识，配置已生效", content: "", status: "succeeded", targets: ["DeepSeek 完整使用教程与常见问题处理"] });
    }
    return;
  }
  if (run.status === "cancel_requested") {
    run.status = "cancelled";
    run.recoverable = false;
    agentEvent(run, "status", { status: "cancelled", summary: `已停止后续步骤；已成功 ${run.changedCount} 项仍然生效`, content: "" });
    return;
  }
  if (run.hold || !["queued", "running"].includes(run.status)) return;
  const summaries = {
    waiting_user: "找到两个同名商品，请提供商品 ID 后继续；尚未修改任何配置。",
    read_only: "只读检查完成，当前店铺资料已核对；没有修改配置。",
    succeeded: "已完成当前店铺知识与发货配置核对，结果均来自服务端回执。",
    partial_failed: "部分完成：已成功 1 项，失败 1 项；未回滚已生效配置。",
    partial_unrecoverable: "部分完成但不可直接重试：已成功 1 项，另 1 项需要人工复核。",
    needs_review: "配置版本冲突，结果需要复核；未重复执行已成功项目。",
    xss: '安全展示回执 <img src="/xianyu-saas/fixture-xss" onerror="window.__agentInjected=true"> <script>window.__agentInjected=true</script>',
  };
  const errorModes = {
    provider_error: { source: "provider", upstream_status: 401, code: "provider_error", upstream_code: "invalid_api_key", upstream_type: "authentication_error", message: "Upstream 401: provided API credential is invalid", upstream_request_id: "provider-request-fixture-401" },
    transport_error: { source: "transport", code: "upstream_timeout", message: "模拟传输超时，尚未执行任何写入" },
    application_error: { source: "application", code: "permission_denied", message: "本站权限已变更，不能保存当前配置" },
  };
  if (errorModes[run.mode] && !run.retrying) {
    run.status = "failed";
    run.recoverable = run.mode === "transport_error";
    run.error = structuredClone(errorModes[run.mode]);
    agentEvent(run, "error", { status: run.status, error: run.error, summary: run.error.message, content: "" });
    return;
  }
  run.status = ["read_only", "xss"].includes(run.mode) ? "succeeded" : run.mode === "partial_unrecoverable" ? "partial_failed" : run.mode;
  run.recoverable = run.mode === "partial_failed" && !run.retrying;
  if (["succeeded", "partial_failed", "partial_unrecoverable"].includes(run.mode)) recordAgentWrite(run);
  if (run.retrying) { recordAgentWrite(run, ["partial_failed", "partial_unrecoverable"].includes(run.mode) ? "100002" : "100001"); run.status = "succeeded"; run.recoverable = false; }
  run.failedCount = run.status === "partial_failed" ? 1 : 0;
  const summary = run.retrying ? "重试完成：累计成功 2 项，失败 0 项；第一项未重复执行。" : summaries[run.mode];
  if (run.status === "needs_review") run.error = { source: "application", code: "content_conflict", message: "配置版本冲突，请核对当前配置" };
  agentEvent(run, "assistant", { summary, content: summary, status: run.status, ...(run.error ? { error: run.error } : {}), targets: ["DeepSeek 完整使用教程与常见问题处理"] });
}

function scheduleAgentRun(run, delay = 180) {
  if (fixtures.opsWorkerPaused || run.workerTimer) return;
  const timer = setTimeout(() => {
    fixtures.opsWorkerTimers.delete(timer);
    run.workerTimer = null;
    advanceAgentRun(run);
    if (["queued", "running", "cancel_requested"].includes(run.status) && !run.hold) scheduleAgentRun(run, 1000);
  }, delay);
  run.workerTimer = timer;
  fixtures.opsWorkerTimers.add(timer);
  timer.unref();
}

function stopAgentMockWorkers() {
  for (const timer of fixtures.opsWorkerTimers) clearTimeout(timer);
  fixtures.opsWorkerTimers.clear();
  for (const run of fixtures.opsRuns.values()) run.workerTimer = null;
}

function handleSettingsOpsMock(req, res, apiPath, payload, rawBody = "") {
  if (!apiPath.startsWith("/api/settings/ai/") && !apiPath.startsWith("/api/ops/")) return false;
  const username = fixtures.me.username;
  const accountKey = String(req.headers["x-shop-account"] || "default");
  const reply = (value, status = 200) => { json(res, value, status); return true; };
  const fail = (code, message, status = 409) => reply({ detail: { code, message, ...(apiPath.startsWith("/api/ops/") ? { source: "application" } : {}) } }, status);
  const query = new URL(req.url, "http://127.0.0.1").searchParams;
  const request = { method: req.method, path: apiPath, username, accountKey, query: Object.fromEntries(query), payload: structuredClone(payload), rawBody, idempotencyKey: String(req.headers["idempotency-key"] || "") };
  if (apiPath.startsWith("/api/settings/ai/")) {
    const connection = userConnectionFixture(username);
    fixtures.settingsRequests.push(request);
    if (apiPath === "/api/settings/ai/connection" && req.method === "GET") return reply({ ...structuredClone(connection), providers: mockConnectionProviders });
    if (["POST", "PUT", "DELETE"].includes(req.method)) {
      const allowed = req.method === "DELETE" ? ["confirm", "expected_revision"] : ["provider", "base_url", "model", "api_key", "expected_revision", ...(req.method === "PUT" ? ["verification_token", "confirm"] : [])];
      if (Object.keys(payload).some((key) => !allowed.includes(key))) return fail("invalid_payload", "请求含已移除的迁移参数或未知字段", 422);
      if (payload.expected_revision !== connection.revision) return fail("revision_conflict", "连接已被其他页面修改，请刷新后重新测试");
      if (apiPath === "/api/settings/ai/connection/test" && req.method === "POST") {
        if (!mockConnectionProviders.some((item) => item.code === payload.provider)) return fail("invalid_provider", "不支持的接口格式", 400);
        if (!String(payload.base_url || "").startsWith("https://")) return fail("unsafe_url", "连接地址不安全", 400);
        if (!String(payload.model || "").trim()) return fail("model_not_found", "模型不存在", 404);
        if (payload.provider !== "ollama_chat" && !payload.api_key && !(connection.api_key_configured && connection.provider === payload.provider)) return fail("authentication_failed", "请输入当前接口的 API Key", 401);
        const token = `mock-user-verification-${fixtures.userConnectionTokens.size + 1}`;
        fixtures.userConnectionTokens.set(token, { username, fingerprint: mockConnectionFingerprint(payload) });
        const result = { ok: true, status: "verified", verification_token: token, expires_in: 180 };
        const delay = fixtures.settingsTestDelayMs;
        fixtures.settingsTestDelayMs = 0;
        if (delay) { setTimeout(() => json(res, result), delay); return true; }
        return reply(result);
      }
      if (apiPath === "/api/settings/ai/connection" && req.method === "PUT") {
        if (payload.confirm !== true) return fail("confirmation_required", "需要确认保存用户统一连接");
        const verification = fixtures.userConnectionTokens.get(payload.verification_token);
        if (!verification || verification.username !== username || verification.fingerprint !== mockConnectionFingerprint(payload)) return fail("verification_invalid", "测试凭证已失效，请重新测试连接");
        fixtures.userConnectionTokens.delete(payload.verification_token);
        const saved = { ...connection, initialized: true, provider: payload.provider, base_url: payload.base_url, model: payload.model,
          api_key_configured: Boolean(payload.api_key || (connection.provider === payload.provider && connection.api_key_configured)),
          connection_status: "verified", revision: connection.revision + 1,
          key_revision: connection.key_revision + 1, last_error_code: "" };
        fixtures.userConnections.set(username, saved);
        return reply({ ok: true, connection: structuredClone(saved) });
      }
      if (apiPath === "/api/settings/ai/connection" && req.method === "DELETE") {
        if (payload.confirm !== true) return fail("confirmation_required", "需要确认删除用户统一连接");
        const cleared = { ...connection, initialized: true, provider: "openai_chat_completions", base_url: "", model: "", api_key_configured: false, connection_status: "unconfigured",
          revision: connection.revision + 1, key_revision: connection.key_revision + 1 };
        fixtures.userConnections.set(username, cleared);
        return reply({ ok: true, connection: structuredClone(cleared) });
      }
    }
    return fail("mock_route_missing", `未实现的设置 mock: ${req.method} ${apiPath}`, 404);
  }

  fixtures.opsRequests.push(request);
  if (!fixtures.me.permissions?.includes("automation.ai")) return fail("permission_denied", "需要店铺 Agent 权限", 403);
  if (!fixtures.shopAccounts.some((item) => item.key === accountKey && item.enabled !== false)) return fail("account_not_found", "店铺不存在或已停用", 404);
  const scopedSession = (id) => {
    const session = fixtures.opsSessions.get(id);
    return session?.username === username && session?.accountKey === accountKey ? session : null;
  };
  const activeRun = (stored) => {
    const run = fixtures.opsRuns.get(stored?.latestRunId);
    if (!run || !["queued", "running", "cancel_requested"].includes(run.status)) return null;
    const { events, next_seq, ...summary } = agentRunPayload(run);
    return summary;
  };
  if (apiPath === "/api/ops/sessions/current" && req.method === "GET") {
    const stored = scopedSession(fixtures.opsCurrentSessions.get(agentScopeKey(username, accountKey)));
    return reply({ session: stored ? structuredClone(stored.session) : null, active_run: activeRun(stored) });
  }
  if (apiPath === "/api/ops/sessions" && req.method === "POST") {
    if (Object.keys(payload).length) return fail("invalid_payload", "新对话不接受业务上下文", 422);
    const stored = createAgentSession(username, accountKey);
    return reply({ session: structuredClone(stored.session), active_run: null }, 201);
  }
  const messagesMatch = apiPath.match(/^\/api\/ops\/sessions\/([^/]+)\/messages$/);
  if (messagesMatch && req.method === "GET") {
    const stored = scopedSession(decodeURIComponent(messagesMatch[1]));
    if (!stored) return fail("session_not_found", "会话不存在", 404);
    const cursor = Number(query.get("cursor") || 0);
    if (!Number.isSafeInteger(cursor) || cursor < 0) return fail("invalid_cursor", "消息游标无效", 422);
    const remaining = stored.messages.filter((item) => item.seq > cursor);
    const messages = remaining.slice(0, fixtures.opsMessagePageSize);
    const response = { session: structuredClone(stored.session), messages: structuredClone(messages),
      next_cursor: remaining.length > messages.length ? messages.at(-1).seq : null, active_run: activeRun(stored) };
    fixtures.opsReadResponses.push({ path: apiPath, accountKey, username, cursor, seqs: messages.map((item) => item.seq), nextCursor: response.next_cursor });
    return reply(response);
  }
  if (apiPath === "/api/ops/chat" && req.method === "POST") {
    if (Object.keys(payload).some((key) => !["session_id", "request_id", "message"].includes(key))) return fail("invalid_payload", "Agent 不接受浏览器历史、工具或手选目标", 422);
    if (typeof payload.request_id !== "string" || !payload.request_id.trim() || (request.idempotencyKey && request.idempotencyKey !== payload.request_id)) return fail("invalid_request_id", "需要一致的幂等请求标识", 400);
    if (typeof payload.message !== "string" || !payload.message.trim() || (payload.session_id !== undefined && typeof payload.session_id !== "string")) return fail("invalid_payload", "消息或会话格式无效", 422);
    const key = `${username}:${accountKey}:${payload.request_id}`;
    const repeated = fixtures.opsChatsByRequest.get(key);
    if (repeated) {
      if (repeated.message !== payload.message || repeated.originalSessionId !== payload.session_id) return fail("request_conflict", "请求标识已用于其他内容");
      return reply(repeated.response, 202);
    }
    let stored = payload.session_id ? scopedSession(payload.session_id) : null;
    if (payload.session_id && !stored) return fail("session_not_found", "会话不存在", 404);
    if (!stored) stored = createAgentSession(username, accountKey);
    if (activeRun(stored)) return fail("session_busy", "当前对话任务尚在执行");
    const id = `mock-run-${fixtures.opsRuns.size + 1}`;
    const run = { id, username, accountKey, sessionId: stored.session.id, requestId: payload.request_id, status: "queued", events: [], polls: 0,
      changedCount: 0, failedCount: 0, mode: fixtures.opsChatMode, hold: fixtures.opsRunHold, recoverable: false, error: null };
    fixtures.opsRuns.set(id, run);
    stored.latestRunId = id;
    agentMessage(stored, "user", payload.message, { run_id: id, kind: "message", status: "queued" });
    agentEvent(run, "status", { summary: "请求已排队，尚未完成配置", status: "queued", content: "" });
    const response = { run_id: id, session_id: stored.session.id, status: "queued" };
    fixtures.opsChatsByRequest.set(key, { message: payload.message, originalSessionId: payload.session_id, response });
    scheduleAgentRun(run);
    const delay = fixtures.opsChatDelayMs;
    fixtures.opsChatDelayMs = 0;
    if (delay) { setTimeout(() => json(res, response, 202), delay); return true; }
    return reply(response, 202);
  }
  const runMatch = apiPath.match(/^\/api\/ops\/runs\/([^/]+)(?:\/(cancel|retry))?$/);
  if (runMatch) {
    const run = fixtures.opsRuns.get(decodeURIComponent(runMatch[1]));
    if (!run || run.username !== username || run.accountKey !== accountKey) return fail("run_not_found", "任务不存在", 404);
    if (!runMatch[2] && req.method === "GET") {
      const afterSeq = Number(query.get("after_seq") || 0);
      if (!Number.isSafeInteger(afterSeq) || afterSeq < 0) return fail("invalid_cursor", "事件游标无效", 422);
      // Polling is read-only: only the independent mock worker advances tasks.
      const response = { ...agentRunPayload(run, afterSeq), ...(fixtures.opsRunWireOverrides || {}) };
      fixtures.opsReadResponses.push({ path: apiPath, accountKey, username, afterSeq, seqs: response.events.map((item) => item.seq), nextSeq: response.next_seq, status: response.status, changedCount: response.changed_count });
      const delay = fixtures.opsRunDelayMs;
      fixtures.opsRunDelayMs = 0;
      if (delay) { setTimeout(() => json(res, response), delay); return true; }
      return reply(response);
    }
    if (runMatch[2] === "cancel" && req.method === "POST") {
      if (rawBody.length) return fail("invalid_payload", "停止任务不接受请求体", 422);
      if (["queued", "running"].includes(run.status)) {
        run.status = "cancel_requested";
        agentEvent(run, "status", { summary: "已请求停止后续步骤，已成功配置不会回滚", status: "cancel_requested", content: "" });
        scheduleAgentRun(run);
      }
      return reply(agentRunPayload(run));
    }
    if (runMatch[2] === "retry" && req.method === "POST") {
      if (Object.keys(payload).some((key) => key !== "request_id") || typeof payload.request_id !== "string" || !payload.request_id.trim() || (request.idempotencyKey && payload.request_id !== request.idempotencyKey)) return fail("invalid_request_id", "重试需要一致的请求标识", 422);
      const key = `${username}:${accountKey}:${payload.request_id}`;
      const repeated = fixtures.opsRetriesByRequest.get(key);
      if (repeated) return repeated.runId === run.id ? reply(repeated.response, 202) : fail("request_conflict", "重试标识已用于其他任务");
      if (run.recoverable !== true) return fail("run_not_retryable", "当前任务必须先复核，不能直接重试");
      const stored = scopedSession(run.sessionId);
      if (activeRun(stored)) return fail("session_busy", "当前对话任务尚在执行");
      run.retrying = true;
      run.status = "queued";
      run.recoverable = false;
      run.hold = false;
      run.error = null;
      run.polls = 0;
      stored.latestRunId = run.id;
      agentEvent(run, "status", { summary: "已排队核对失败步骤，已成功配置不会重复执行", status: "queued", content: "" });
      const result = { run_id: run.id, session_id: run.sessionId, status: "queued" };
      fixtures.opsRetriesByRequest.set(key, { runId: run.id, response: result });
      scheduleAgentRun(run);
      return reply(result, 202);
    }
  }
  return fail("mock_route_missing", `已移除或未实现的 Agent mock: ${req.method} ${apiPath}`, 404);
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
    fixtures.apiRequests.push({ method: req.method, path: apiPath, username: loggedIn ? fixtures.me.username : "", accountKey: String(req.headers["x-shop-account"] || "default") });
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
        fixtures.authLogoutRequests += 1;
        fixtures.requestSequence.push("auth-logout");
        loggedIn = false;
        return json(res, { ok: true }, 200, { "set-cookie": "xianyu_saas_session=; Path=/xianyu-saas/; Max-Age=0; HttpOnly" });
      }
      if (apiPath === "/api/version/public" && req.method === "GET") return json(res, { version: fixtures.version.version, asset_version: fixtures.version.asset_version });
      if (!loggedIn) return json(res, { detail: "未登录" }, 401);
      if (apiPath.startsWith("/api/ops/") && fixtures.opsSiteSessionExpired) {
        fixtures.opsSiteSessionExpired = false;
        loggedIn = false;
        return json(res, { detail: { source: "application", code: "session_expired", message: "本站登录会话已失效" } }, 401);
      }
      if (req.headers["x-shop-account"]) fixtures.shopAccountHeaders.push(String(req.headers["x-shop-account"]));
      if (apiPath === "/api/me") return json(res, fixtures.me);
      if (apiPath === "/api/auth/password" && req.method === "POST") {
        fixtures.passwordRequests.push(payload);
        return json(res, { ok: true, other_sessions_revoked: true });
      }
      if (handleSettingsOpsMock(req, res, apiPath, payload, rawBody)) return;
      if (apiPath === "/api/version" && req.method === "GET") return json(res, fixtures.version);
      if (apiPath.startsWith("/api/admin/") && fixtures.me?.is_admin !== true) {
        return json(res, { detail: { code: "admin_required", message: "需要管理员权限" } }, 403);
      }
      if (apiPath === "/api/admin/resource-settings") {
        const settingsPayload = () => ({ settings: structuredClone(fixtures.resourcePolicy),
          bounds: { max_shop_accounts: { min: 1, max: 1000 }, max_running_workers: { min: 1, max: 1000 }, worker_memory_mib: { min: 128, max: 16384 } },
          usage: { running_workers: 1 }, memory_limit_kind: "address_space", applies_to: "new_starts", restart_performed: false });
        if (req.method === "GET") return json(res, settingsPayload());
        if (req.method === "PUT") {
          const fields = ["max_shop_accounts", "max_running_workers", "worker_memory_mib", "expected_revision"];
          if (Object.keys(payload).length !== fields.length || fields.some((key) => !Number.isInteger(payload[key]))
            || payload.expected_revision < 0 || payload.max_shop_accounts < 1 || payload.max_shop_accounts > 1000
            || payload.max_running_workers < 1 || payload.max_running_workers > 1000
            || payload.worker_memory_mib < 128 || payload.worker_memory_mib > 16384) return json(res, { detail: { message: "运行限制格式无效" } }, 422);
          if (fixtures.resourceConflictOnce) { fixtures.resourcePolicy.revision += 1; fixtures.resourceConflictOnce = false; }
          if (payload.expected_revision !== fixtures.resourcePolicy.revision) return json(res, { detail: { code: "resource_revision_conflict", message: "运行限制已被修改，请刷新后再保存" } }, 409);
          fixtures.resourceSaveRequests.push(structuredClone(payload));
          const { expected_revision, ...values } = payload;
          fixtures.resourcePolicy = { ...values, revision: expected_revision + 1, source: "saved", updated_at: Date.now() / 1000 };
          return json(res, settingsPayload());
        }
      }
      if (apiPath === "/api/bot/resources" && req.method === "GET") {
        const query = new URL(req.url, "http://127.0.0.1").searchParams;
        if ([...query.keys()].some((key) => !["cursor", "limit"].includes(key) || query.getAll(key).length !== 1)) return json(res, { detail: { code: "invalid_resource_query" } }, 400);
        const cursor = Number(query.get("cursor") || 0), limit = Number(query.get("limit") || 50);
        if (!Number.isSafeInteger(cursor) || cursor < 0 || !Number.isInteger(limit) || limit < 1 || limit > 100) return json(res, { detail: { message: "资源查询格式无效" } }, 422);
        fixtures.resourceRequests.push({ username: fixtures.me.username, cursor, limit });
        if (fixtures.resourceError || fixtures.resourceErrorOnce) {
          fixtures.resourceErrorOnce = false;
          return json(res, { detail: { code: "resources_unavailable", message: "店铺资源暂时无法读取" } }, 503);
        }
        const configured = fixtures.resourcePolicy.worker_memory_mib * 1024 * 1024;
        const all = fixtures.resourceRowsByUser[fixtures.me.username] || fixtures.shopAccounts.map((account) => ({
          account_id: account.id, key: account.key, name: account.name, enabled: account.enabled,
          worker_state: account.enabled === false ? "disabled" : "stopped", mode: "rules", metrics_state: "stopped",
          cpu_percent: 0, rss_bytes: 0, vms_bytes: 0, uptime_seconds: 0, memory_limit_bytes: null,
          configured_memory_limit_bytes: configured, pending_restart: false, sampled_at: Date.now() / 1000, message: "未运行",
        }));
        const eligible = all.filter((row) => row.account_id > cursor).sort((a, b) => a.account_id - b.account_id);
        const page = eligible.slice(0, limit).map((row) => ({ ...row, configured_memory_limit_bytes: configured,
          pending_restart: row.metrics_state !== "stopped" && row.memory_limit_bytes != null && row.memory_limit_bytes !== configured }));
        const response = structuredClone({ scope: "own_shops", accounts: page,
          next_cursor: eligible.length > limit ? page.at(-1).account_id : null, total: all.length,
          limits: fixtures.resourcePolicy, usage: { shop_accounts: all.length, running_workers: all.filter((row) => ["running", "starting", "stopping"].includes(row.worker_state)).length },
          sample_interval_seconds: 5, stale_after_seconds: 15, sampled_at: Date.now() / 1000, cpu_basis: "one_core", memory_limit_kind: "address_space" });
        const delay = fixtures.resourceResponseDelayMs;
        fixtures.resourceResponseDelayMs = 0;
        if (delay > 0) { setTimeout(() => json(res, response), delay); return; }
        return json(res, response);
      }
      if (apiPath === "/api/admin/settings" && req.method === "GET") return json(res, fixtures.adminSettings);
      if (apiPath === "/api/admin/settings" && req.method === "PUT") {
        fixtures.adminSettingRequests.push(payload);
        if (Object.keys(payload).some((key) => key !== "registration_open")) return json(res, { detail: { code: "invalid_payload", message: "发布源已统一，不接受通道设置" } }, 422);
        if (typeof payload.registration_open === "boolean") {
          fixtures.adminSettings.registration.database_open = payload.registration_open;
          fixtures.adminSettings.registration.effective = fixtures.adminSettings.registration.environment_allowed && payload.registration_open && fixtures.adminSettings.registration.users_exist;
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
        fixtures.updateStatus.update_check = {
          version: ["no_release", "error", "unchecked"].includes(fixtures.releaseCheckStatus) ? "" : fixtures.releaseCheckStatus === "current" ? fixtures.version.version : "0.2.0",
          channel: "release", status: fixtures.releaseCheckStatus,
          available: ["available", "incomplete"].includes(fixtures.releaseCheckStatus), current_version: fixtures.version.version,
          release_notes: "Release 0.2.0 <script>window.__releaseNotesInjected=true</script>",
          error_code: fixtures.releaseCheckStatus === "error" ? "update_source_failed" : "", checked_at: 1788134400,
          ...(fixtures.releaseCheckOverrides || {}),
        };
        fixtures.version.update_check = structuredClone(fixtures.updateStatus.update_check);
        return json(res, fixtures.updateStatus.update_check);
      }
      if (apiPath === "/api/admin/updates/download" && req.method === "POST") {
        fixtures.updateRequests.push({ action: "download", payload });
        fixtures.updateStatus.latest_update = {
          ...fixtures.updateStatus.update_check,
          version: String(payload.version || "0.2.0"), status: "staged", updated_at: 1788134401,
        };
        return json(res, {
          version: fixtures.updateStatus.latest_update.version,
          channel: "release",
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
          version: String(payload.version || ""), channel: "release",
          status: action === "apply" ? "apply_requested" : "rollback_requested", error_code: "", updated_at: 1788134402,
        };
        return json(res, { queued: true, action, version: String(payload.version || "") }, 202);
      }
      if (apiPath === "/api/bot/accounts" && req.method === "GET") return json(res, { accounts: fixtures.shopAccounts });
      if (apiPath === "/api/bot/accounts" && req.method === "POST") {
        if (fixtures.shopAccounts.length >= fixtures.resourcePolicy.max_shop_accounts) return json(res, { detail: { code: "shop_limit_reached", message: "已达到可添加店铺上限" } }, 409);
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
        fixtures.requestSequence.push(`account-delete:${key}`);
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
        const gate = fixtures.botStatusResponseGates.shift();
        if (gate) {
          void gate.promise.then(() => { if (!res.destroyed) json(res, response, 200, headers); });
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
        const days = Number(new URL(req.url, "http://127.0.0.1").searchParams.get("period") || 1);
        fixtures.analyticsRequests.push(days);
        return json(res, fixtures.analyticsByPeriod?.[days] || fixtures.analytics);
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
      if (apiPath === "/api/bot/ai/status" && req.method === "GET") {
        const scoped = scopedAi(req);
        return json(res, structuredClone(scoped.value.status));
      }
      if (apiPath === "/api/bot/ai/connection" && req.method === "GET") {
        const scoped = scopedAi(req);
        const userConnection = fixtures.userConnections.get(fixtures.me.username);
        const response = structuredClone(userConnection?.initialized ? userConnection : scoped.value.connection);
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
        const gate = fixtures.aiKnowledgeResponseGates.shift();
        if (gate) { gate.then(() => json(res, response)); return; }
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
        if (!current || payload.confirm !== true) return json(res, { detail: "请先保存商品补充内容并确认" }, 409);
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
      if (apiPath === "/api/bot/products/delivery-status" && req.method === "GET") {
        if (fixtures.deliveryStatusError) return json(res, { detail: { message: "发货状态暂时无法读取" } }, 503);
        const { accountKey, value: products } = scopedFixture(req, "products", fixtures.products);
        if (fixtures.deliveryStatusByAccount[accountKey]) return json(res, structuredClone(fixtures.deliveryStatusByAccount[accountKey]));
        const automation = scopedFixture(req, "automation", fixtures.automation).value;
        const ids = new Set(products.map((p) => String(p.id)));
        const items = (automation.deliveries || []).filter((item) => ids.has(String(item.item_id))).map((item) => ({
          item_id: String(item.item_id), delivery: "material", configured: Boolean(String(item.material || "").trim()), enabled: item.enabled !== false, template_id: null,
        }));
        for (const template of fixtures.templates) for (const id of template.item_ids || []) {
          if (ids.has(String(id)) && !items.some((item) => item.item_id === String(id))) items.push({ item_id: String(id), delivery: template.delivery, configured: true, enabled: template.enabled !== false, template_id: template.id });
        }
        return json(res, { available: true, items });
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
        const manualMessages = fixtures.manualReplies
          .filter((item) => item.account_key === accountKey && (!selected || item.chat_id === selected))
          .flatMap(manualReplyDisplayMessages);
        const allMessages = [...messages, ...manualMessages];
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
        const uploadNumber = fixtures.manualImageRequests.length;
        const response = {
          ok: true,
          media: {
            type: "image",
            url: "",
            path: `manual_reply_test_${uploadNumber}.jpg`,
            alt: "图片",
            label: "图片",
            name: "",
            mime: contentType.split(";", 1)[0] || "image/jpeg",
          },
        };
        const delay = Number(fixtures.manualImageUploadDelayMs || 0);
        fixtures.manualImageUploadDelayMs = 0;
        if (delay > 0) {
          setTimeout(() => json(res, response), delay);
          return;
        }
        return json(res, response);
      }
      if (apiPath === "/api/bot/messages/image" && req.method === "DELETE") {
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const path = String(payload.path || "");
        const account = fixtures.shopAccounts.find((item) => item.key === accountKey);
        fixtures.requestSequence.push(`image-delete:${accountKey}`);
        if (!account || account.enabled === false) return json(res, { detail: "店铺不存在" }, 404);
        const deleteMode = fixtures.manualImageDeleteModes.shift() || fixtures.manualImageDeleteMode;
        if (deleteMode === "failure") {
          fixtures.manualImageDeleteFailures += 1;
          return json(res, { detail: { code: "image_delete_unavailable", message: "图片暂时无法删除，请稍后重试" } }, 503);
        }
        if (deleteMode === "active") {
          return json(res, { detail: { code: "image_in_use", message: "图片已进入发送队列，不能删除" } }, 409);
        }
        fixtures.manualImageDeletes.push({ accountKey, path });
        return json(res, { ok: true, deleted: true });
      }
      if (apiPath === "/api/bot/messages/reply" && req.method === "POST") {
        const selected = payload.chat_id || "chat-2";
        const accountKey = String(req.headers["x-shop-account"] || "default");
        const replyId = String(req.headers["idempotency-key"] || "");
        const media = Array.isArray(payload.media) ? payload.media.map((item) => ({ ...item })) : [];
        fixtures.manualReplyRequests.push({ chatId: selected, content: payload.content, media, replyId, accountKey });
        const delay = Number(fixtures.manualReplyPostDelayMs || 0);
        fixtures.manualReplyPostDelayMs = 0;
        if (fixtures.manualReplyPostMode === "failure") {
          fixtures.manualReplyPostFailures += 1;
          const reject = () => json(res, { detail: { code: "reply_unavailable", message: "回复暂时无法提交，请稍后重试" } }, 503);
          if (delay > 0) setTimeout(reject, delay);
          else reject();
          return;
        }
        const parent = {
          content: String(payload.content || ""),
          media,
          time: "2026-08-15 15:20",
          chat_id: selected,
          item_id: selected === "chat-1" ? "100001" : "100002",
          reply_id: replyId,
          outbox_id: fixtures.manualReplies.length + 1,
          status: "queued",
          attempts: 0,
          parts: createManualReplyParts(payload.content, media),
          account_key: accountKey,
        };
        fixtures.manualReplies.push(parent);
        fixtures.manualReplyPollModes.set(replyId, fixtures.manualReplyPollMode);
        const reply = manualReplyStatusPayload(parent);
        const message = manualReplyPendingMessage(parent);
        const accept = () => json(res, { ok: true, accepted: true, saved: true, delivered: false, platform_acknowledged: false, reply, message });
        if (delay > 0) setTimeout(accept, delay);
        else accept();
        return;
      }
      const manualReplyStatusMatch = apiPath.match(/^\/api\/bot\/messages\/reply\/([^/]+)$/);
      if (manualReplyStatusMatch && req.method === "GET") {
        const replyId = decodeURIComponent(manualReplyStatusMatch[1]);
        const parent = fixtures.manualReplies.find((item) => item.reply_id === replyId);
        if (!parent) return json(res, { detail: "not found" }, 404);
        const mode = fixtures.manualReplyPollModes.get(replyId) || "success";
        if (mode === "not_found") {
          fixtures.manualReplyPollNotFoundResponses += 1;
          return json(res, { detail: "not found" }, 404);
        }
        fixtures.manualReplyPolls += 1;
        const replyPolls = Number(fixtures.manualReplyPollsById.get(replyId) || 0) + 1;
        fixtures.manualReplyPollsById.set(replyId, replyPolls);
        advanceManualReply(parent, mode, replyPolls);
        return json(res, { reply: manualReplyStatusPayload(parent) });
      }
      if (apiPath === "/api/bot/orders" && req.method === "GET") {
        const scoped = scopedFixture(req, "orders", fixtures.orders);
        const paged = url.searchParams.has("page");
        fixtures.orderQueries.push({ account: scoped.accountKey, ...Object.fromEntries(url.searchParams) });
        if (paged && fixtures.orderQueryError) return json(res, { detail: { code: "orders_unavailable", message: "订单记录暂时无法读取，请稍后重试" } }, 503);
        const response = orderFixturePage(scoped.value, url.searchParams, scoped.accountKey);
        const delay = paged && fixtures.orderQueryDelays.length ? fixtures.orderQueryDelays.shift() : takeLoaderDelay("orders", scoped.accountKey);
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
  if (!screenshotsEnabled) return;
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

async function dispatchManualReplyImageEvent(page, type, file) {
  await page.locator(".chat-window").evaluate((node, detail) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(detail.bytes)], detail.name, {
      type: detail.mimeType,
      lastModified: detail.lastModified,
    }));
    const event = new Event(detail.type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, detail.type === "paste" ? "clipboardData" : "dataTransfer", { value: transfer });
    node.dispatchEvent(event);
  }, { type, ...file });
}

async function checkOrderManagement(browser, baseUrl) {
  const saved = { orders: fixtures.orders, accounts: fixtures.shopAccounts, accountData: fixtures.accountData };
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const errors = [];
  const externalRequests = [];
  await routeOfflineMock(page, baseUrl, externalRequests);
  page.on("pageerror", (error) => errors.push(error.message));
  const groups = orderStatusOptions.filter((item) => item.value !== "all");
  const makeOrder = (index, account = "default") => {
    const group = groups[index % groups.length];
    return { order_key: account + "-same-prefix-long-record-" + String(index).padStart(5, "0"),
      platform_order_id: String(429175119103700000n + BigInt(index)), item_id: "100001",
      item_title: "数字商品资料 " + index, item_image_url: "", buyer_id: "buyer-" + index,
      chat_id: index === 61 ? "chat-1" : "", conversation_available: index === 61,
      quantity: 1, paid_amount: index === 61 ? "0.00" : index === 60 ? null : "6.50",
      status: { processing: "verified", sent: "delivered", manual: "manual_review", retry: "retry", ended: "expired", exception: "unknown" }[group.value],
      status_group: group.value, status_label: group.label,
      reason_code: group.value === "manual" ? "inventory_empty" : "",
      reason_label: group.value === "manual" ? "库存不足，请核对后处理" : "",
      created_at: new Date(Date.UTC(2026, 8, 5, 10, 0, index)).toISOString(), updated_at: "",
      verified_at: "2026-09-05T10:00:00Z", delivered_at: "", platform_shipped_at: "",
      delivery_type_label: "卡密", platform_status_label: "核验时待发货" };
  };
  fixtures.orders = Array.from({ length: 62 }, (_, index) => makeOrder(index));
  fixtures.shopAccounts = [...saved.accounts, { ...saved.accounts[0], id: 2, key: "order-second", name: "第二订单店铺" }];
  fixtures.accountData = { ...saved.accountData, "order-second": { orders: [makeOrder(900, "order-second")] } };
  const orderRows = () => page.locator("#orderList [data-order-key]");
  const settled = async () => page.waitForFunction(() => !document.querySelector("#refreshOrders")?.disabled);
  const refresh = async () => {
    const response = page.waitForResponse((item) => item.url().includes("/api/bot/orders?") && new URL(item.url()).searchParams.has("page"));
    await page.click("#refreshOrders");
    await response;
    await settled();
  };
  const query = async (value) => {
    await page.fill("#ordersSearchInput", value);
    await page.locator("#ordersSearchInput").press("Enter");
    await settled();
  };
  try {
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.fill("#authUsername", "owner-demo");
    await page.fill("#authPassword", "password-123");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await openView(page, "orders");
    await page.waitForFunction(() => document.querySelectorAll("#orderList [data-order-key]").length === 15);
    await settled();
    assert.match(await page.locator("#ordersTotal").textContent(), /62/);
    assert.equal(await page.inputValue("#ordersShopSelect"), "default");
    assert.match(await page.locator("#orderList").textContent(), /0\.00/);
    const firstKey = await orderRows().first().getAttribute("data-order-key");
    await page.locator("#orderList [data-order-toggle]").first().click();
    assert.equal(await page.locator("#orderList [data-order-toggle]").first().getAttribute("aria-expanded"), "true");
    await page.evaluate(() => { window.__orderCopies = []; Object.defineProperty(navigator.clipboard, "writeText", { configurable: true, value: async (text) => window.__orderCopies.push(text) }); });
    await page.locator("#orderList [data-order-copy]").first().click();
    assert.deepEqual(await page.evaluate(() => window.__orderCopies), ["429175119103700061"]);
    await page.locator("#orderList [data-order-toggle]").first().click();
    await page.click("#ordersNext");
    await settled();
    assert.notEqual(await orderRows().first().getAttribute("data-order-key"), firstKey);
    for (const size of ["30", "50", "15"]) {
      await page.selectOption("#ordersPageSize", size);
      await page.waitForFunction((n) => document.querySelectorAll("#orderList [data-order-key]").length === n, Number(size));
    }
    await page.fill("#ordersPageInput", "5");
    await page.click("#ordersPageJump");
    await page.waitForFunction(() => document.querySelectorAll("#orderList [data-order-key]").length === 2);
    await page.click("#ordersReset");
    await page.waitForFunction(() => document.querySelectorAll("#orderList [data-order-key]").length === 15);
    await page.locator("#ordersStatusTabs button").filter({ hasText: "待人工" }).click();
    await settled();
    assert.equal(await orderRows().count(), 10);
    assert.match(await page.locator("#orderList").textContent(), /库存不足/);
    await page.click("#ordersReset");
    await settled();
    await page.selectOption("#ordersSearchField", "order_id");
    await query("429175119103700061");
    assert.equal(await orderRows().count(), 1);
    await page.locator("#orderList [data-order-chat]:not(:disabled)").first().click();
    await page.waitForSelector('[data-panel="chat"]:not([hidden])');
    await page.waitForFunction(() => document.querySelector("#chatMessages")?.textContent.includes("付款后会发送完整说明"));
    await openView(page, "orders");
    await query("no-such-order");
    assert.equal(await orderRows().count(), 0);
    assert.equal(await page.locator("#ordersEmpty").isVisible(), false, "filtered-empty results must not use the no-orders landing state");
    assert.match(await page.locator("#orderList .orders-empty-filter").innerText(), /匹配|筛选|查询/);
    await page.click("#ordersReset");
    await settled();
    fixtures.orderQueryError = true;
    await refresh();
    assert.match(await page.locator("#ordersMessage").textContent(), /暂时无法读取|读取失败/);
    fixtures.orderQueryError = false;
    await refresh();
    await page.waitForFunction(() => document.querySelectorAll("#orderList [data-order-key]").length === 15);
    await settled();
    assert.doesNotMatch(await page.locator("#ordersMessage").textContent(), /暂时无法读取|读取失败/);
    await assertNoOverflow(page, "orders desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "orders-compact-desktop.png"), fullPage: true });
    fixtures.orderQueryDelays = [500];
    const stalePageRequest = page.waitForRequest((item) => item.url().includes("/api/bot/orders?") && new URL(item.url()).searchParams.has("page") && item.headers()["x-shop-account"] === "default");
    await page.click("#refreshOrders");
    await stalePageRequest;
    await page.selectOption("#ordersShopSelect", "order-second");
    await page.waitForFunction(() => document.querySelector("#orderList")?.textContent.includes("429175119103700900"));
    await page.waitForTimeout(650);
    assert.equal(await page.locator('[data-panel="orders"]').isVisible(), true);
    assert.equal(await page.inputValue("#ordersShopSelect"), "order-second");
    assert.equal(await orderRows().count(), 1);
    assert.doesNotMatch(await page.locator("#orderList").textContent(), /429175119103700061/);
    fixtures.accountData["order-second"].orders = [];
    await refresh();
    assert.equal(await orderRows().count(), 0);
    assert.match(await page.locator("#ordersEmpty").textContent(), /记录|订单/);
    assert.deepEqual(errors, []);
    assert.ok(fixtures.orderQueries.some((entry) => entry.page === "5"));
    assert.ok(fixtures.orderQueries.some((entry) => entry.account === "order-second"));
    assert.deepEqual(externalRequests, [], "orders regression must remain isolated from external services");
    console.log(JSON.stringify({ ok: true, scope: "orders", rows: 62, desktop: 1440, screenshots: screenshotsEnabled ? resultRoot : 0 }));
  } finally {
    fixtures.orders = saved.orders;
    fixtures.shopAccounts = saved.accounts;
    fixtures.accountData = saved.accountData;
    fixtures.orderQueryError = false;
    fixtures.orderQueryDelays = [];
    await page.close();
  }
}

async function routeOfflineMock(page, baseUrl, externalRequests) {
  const origin = new URL(baseUrl).origin;
  // Context routing also covers an unexpected popup's first request. Docs do
  // not use the regression-only CDN pixel, and WebSockets never connect out.
  const router = docsCaptureScope ? page.context() : page;
  if (docsCaptureScope) {
    assert.equal(new URL(baseUrl).hostname, "127.0.0.1");
    await router.routeWebSocket("**/*", (socket) => {
      externalRequests.push(socket.url());
      socket.close();
    });
  }
  await router.route("**/*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (docsCaptureScope) fixtures.docsCaptureRequests.push({ method: request.method(), url: url.href });
    if (url.origin === origin || ["data:", "blob:"].includes(url.protocol)) return route.continue();
    if (!docsCaptureScope && url.hostname === "cdn.example") return route.fulfill({ status: 200, contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64") });
    externalRequests.push(url.href);
    return route.abort();
  });
}

async function assertDesktopAssetVersion(page) {
  assert.equal(assetVersion, "20260909-01", "the release must use the approved unified asset version");
  const assets = await page.locator('script[src*="assets/app.js"], link[href*="assets/app.css"]').evaluateAll((nodes) => nodes.map((node) => new URL(node.src || node.href).searchParams.get("v")));
  assert.deepEqual(assets, [assetVersion, assetVersion], "HTML, stylesheet and application script versions must agree");
}

async function assertNoBusinessStorage(page, pattern) {
  const snapshot = await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage }, writes: window.__uiStorageWrites || [] }));
  assert.doesNotMatch(JSON.stringify(snapshot), pattern, "business messages and API keys must never enter browser storage, including transient writes");
}

async function desktopContractPage(browser, baseUrl, contextOptions = {}) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, serviceWorkers: "block", ...contextOptions });
  page.setDefaultTimeout(8000);
  const evidence = { pageErrors: [], failedResponses: [], externalRequests: [], allowedFailures: [], confirmDialogs: [] };
  fixtures.desktopEvidence = evidence;
  page.on("dialog", async (dialog) => {
    if (dialog.type() === "confirm" && /统一|连接|密钥|方案|变更|执行/.test(dialog.message())) {
      evidence.confirmDialogs.push(dialog.message());
      await dialog.accept();
    } else {
      evidence.pageErrors.push(`unexpected ${dialog.type()} dialog: ${dialog.message()}`);
      await dialog.dismiss();
    }
  });
  await routeOfflineMock(page, baseUrl, evidence.externalRequests);
  await page.addInitScript(() => {
    window.__uiStorageWrites = [];
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      window.__uiStorageWrites.push({ storage: this === localStorage ? "local" : "session", key: String(key), value: String(value) });
      return original.call(this, key, value);
    };
  });
  page.on("pageerror", (error) => evidence.pageErrors.push(error.stack || error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && !/^Failed to load resource:.*status of \d+/.test(message.text())) evidence.pageErrors.push(message.text());
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const url = new URL(response.url());
    if (url.pathname.endsWith("/api/me") && response.status() === 401) return;
    evidence.failedResponses.push({ path: url.pathname.replace("/xianyu-saas", ""), status: response.status() });
  });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  assert.deepEqual(evidence.pageErrors, [], "desktop initialization must bind before login");
  return { page, evidence };
}

async function desktopLogin(page, username) {
  await page.fill("#authUsername", username);
  await page.fill("#authPassword", "Mock-only-Pass-123!");
  await page.click("#authSubmit");
  await page.waitForSelector("#workspace:not([hidden])");
}

async function desktopApiClick(page, selector, apiPath, method = "POST", expectedStatus = 200) {
  const [result] = await Promise.all([
    page.waitForResponse((item) => new URL(item.url()).pathname === `/xianyu-saas${apiPath}` && item.request().method() === method),
    page.locator(selector).click(),
  ]);
  assert.equal(result.status(), expectedStatus, `${method} ${apiPath}: ${await result.text()}`);
  return result.json();
}

async function waitForMockGate(promise, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} did not arrive within 8000ms`)), 8000);
    })]);
  } finally { clearTimeout(timer); }
}

async function assertUnifiedVersionLink(page) {
  const details = page.locator("#versionBadgeDetails");
  assert.equal(await details.evaluate((node) => node.tagName), "A", "version details is a direct external link, not a settings action");
  assert.equal(await details.getAttribute("href"), "https://github.com/tswawa/xianyu-saas/releases");
  assert.equal(await details.getAttribute("target"), "_blank");
  assert.equal(await details.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await page.locator('#versionBadgePopover a[href="https://github.com/tswawa/xianyu-saas/releases"]').count(), 1, "merge duplicate release/details buttons into one link");
}

async function assertVersionPopoverBounds(page, label) {
  const result = await page.evaluate(() => {
    const popover = document.querySelector('#versionBadgePopover');
    const bounds = popover.getBoundingClientRect();
    const selector = '.version-popover-actions > .button, .version-popover-val, .version-popover-status';
    return { left: bounds.left, right: bounds.right, viewport: innerWidth,
      clientWidth: popover.clientWidth, scrollWidth: popover.scrollWidth,
      children: [...popover.querySelectorAll(selector)].filter((node) => !node.hidden && node.getClientRects().length).map((node) => {
        const rect = node.getBoundingClientRect();
        return { id: node.id, left: rect.left, right: rect.right, clientWidth: node.clientWidth, scrollWidth: node.scrollWidth };
      }) };
  });
  assert.ok(result.left >= 7 && result.right <= result.viewport - 7, `${label}: popover stays inside the viewport: ${JSON.stringify(result)}`);
  assert.ok(result.scrollWidth <= result.clientWidth + 1, `${label}: popover must not scroll horizontally`);
  for (const node of result.children) {
    assert.ok(node.left >= result.left && node.right <= result.right + 1, `${label}: ${node.id} extends outside its popover: ${JSON.stringify(node)}`);
    assert.ok(node.scrollWidth <= node.clientWidth + 1, `${label}: ${node.id} must wrap rather than clip text`);
  }
}

async function checkVersionPopover(browser, baseUrl) {
  fixtures.me = { ...fixtures.me, username: 'popover-admin', role: 'admin', is_admin: true,
    platform_permissions: ['platform.settings.manage', 'platform.users.manage', 'platform.audit.read', 'platform.updates.manage'] };
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  const cases = [];
  try {
    await desktopLogin(page, fixtures.me.username);
    for (const width of [1440, 1280, 768, 640, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      for (const scale of [1, 1.5, 2]) {
        if (!await page.locator('#versionBadgePopover').isVisible()) await page.click('#versionBadgeButton');
        await page.evaluate((factor) => {
          const popover = document.querySelector('#versionBadgePopover');
          const nodes = [...popover.querySelectorAll('.version-popover-header, .version-popover-row, .version-popover-label, .version-popover-val, .version-popover-status, .version-popover-actions > .button')];
          nodes.forEach((node) => { node.style.fontSize = ''; });
          const sizes = nodes.map((node) => parseFloat(getComputedStyle(node).fontSize));
          nodes.forEach((node, index) => { node.style.fontSize = `${sizes[index] * factor}px`; });
        }, scale);
        await assertVersionPopoverBounds(page, `${width}px/${scale}x text`);
        await page.evaluate(() => {
          document.querySelector('#versionBadgeCurrent').textContent = 'v0.1.0-beta.1234567890+build.abcdefghijklmnopqrstuvwxyz';
          document.querySelector('#versionBadgeStatus').textContent = '检查失败：暂时无法连接发布服务，请稍后重试';
        });
        await assertVersionPopoverBounds(page, `${width}px/${scale}x long version`);
        await page.evaluate(() => {
          document.querySelector('#versionBadgeCurrent').textContent = 'v0.1.0';
          document.querySelector('#versionBadgeStatus').textContent = '尚未检查';
        });
        await page.click('#versionBadgeClose');
        assert.equal(await page.locator('#versionBadgePopover').isHidden(), true);
        assert.equal(await page.locator('#versionBadgeButton').getAttribute('aria-expanded'), 'false');
        cases.push(`${width}/${scale}`);
      }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => document.querySelectorAll('#versionBadgePopover [style]').forEach((node) => { node.style.fontSize = ''; }));
    await assertUnifiedVersionLink(page);
    await checkUpdateFromBadge(page);
    assert.equal(await page.locator('#versionBadgeButton').getAttribute('aria-expanded'), 'false');
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: 'popover', cases, longText: true, checksAndExternalLink: true }));
  } catch (error) {
    await reportDesktopFailure(page, 'popover', error, evidence);
    throw error;
  } finally { await page.close(); }
}

async function checkUpdateFromBadge(page) {
  if (!(await page.locator("#versionBadgePopover").isVisible())) await page.click("#versionBadgeButton");
  await assertVersionPopoverBounds(page, 'version update actions');
  const result = await desktopApiClick(page, "#versionBadgeRefresh", "/api/admin/updates/check");
  await page.waitForFunction(() => document.querySelector("#versionBadgeRefresh")?.disabled === false);
  await page.click("#versionBadgeClose");
  return result;
}

function assertDesktopEvidence(evidence) {
  assert.deepEqual(evidence.pageErrors, [], "desktop must not raise page/console errors");
  assert.deepEqual(evidence.failedResponses, evidence.allowedFailures, "only explicitly exercised conflict/error responses are allowed");
  assert.deepEqual(evidence.externalRequests, [], "desktop scopes may only use the local mock server and fixture images");
  assert.deepEqual(fixtures.authorizationHeaders, [], "browser must not send bearer authorization");
}

async function reportDesktopFailure(page, scope, error, evidence) {
  const state = await page.evaluate(() => ({
    view: document.querySelector('[data-panel]:not([hidden])')?.dataset.panel,
    account: document.querySelector('#accountTabs .account-tab.is-active')?.dataset.accountSwitch,
    connectionMessage: document.querySelector('#aiConnectionMessage')?.textContent,
    versionStatus: document.querySelector('#versionBadgeStatus')?.textContent,
    opsHistory: document.querySelector('#opsChatHistory')?.textContent?.slice(-1500),
    sendDisabled: document.querySelector('#opsSendBtn')?.disabled,
    stopDisabled: document.querySelector('#opsStopBtn')?.disabled,
  })).catch(() => null);
  console.error(JSON.stringify({ ok: false, scope, error: error.message, state, evidence,
    versionCheck: fixtures.version.update_check, releaseCheckOverrides: fixtures.releaseCheckOverrides,
    recentRequests: fixtures.apiRequests.slice(-16), recentOpsReads: fixtures.opsReadResponses.slice(-6) }));
}

async function checkSettingsOpsMock(baseUrl) {
  const owner = structuredClone(fixtures.me);
  const accounts = fixtures.shopAccounts;
  fixtures.shopAccounts = [...accounts, { ...accounts[0], id: 2, key: "mock-second", name: "离线隔离店" }];
  fixtures.opsWorkerPaused = true;
  const call = async (apiPath, { method = "GET", body, accountKey = "default", status = 200 } = {}) => {
    const headers = { "x-shop-account": accountKey, ...(body === undefined ? {} : { "content-type": "application/json" }) };
    const response = await fetch(`${baseUrl}${apiPath}`, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const result = await response.json();
    assert.equal(response.status, status, `${method} ${apiPath}: ${JSON.stringify(result)}`);
    return result;
  };
  const chat = (body, status = 202) => call("/api/ops/chat", { method: "POST", body, status });
  try {
    await call("/api/auth/login", { method: "POST", body: { username: owner.username, password: "Mock-only-Pass-123!" } });
    const initialSessionCount = fixtures.opsSessions.size;
    assert.equal((await call("/api/ops/sessions/current")).session, null);
    assert.equal(fixtures.opsSessions.size, initialSessionCount, "GET current must not create a conversation");
    await chat({ session_id: null, request_id: "mock-null-session", message: "不能创建会话" }, 422);
    assert.equal(fixtures.opsSessions.size, initialSessionCount, "null session_id is invalid and must not create a conversation");
    assert.equal(fixtures.opsRuns.size, 0);
    const created = await call("/api/ops/sessions", { method: "POST", status: 201 });
    const stored = fixtures.opsSessions.get(created.session.id);
    for (let index = 0; index < 26; index += 1) agentMessage(stored, index % 2 ? "assistant" : "user", `离线完整历史-${index + 1}`);
    let cursor = null;
    const received = [];
    do {
      const result = await call(`/api/ops/sessions/${stored.session.id}/messages${cursor == null ? "" : `?cursor=${cursor}`}`);
      received.push(...result.messages);
      if (result.next_cursor != null) assert.ok(Number(result.next_cursor) > Number(cursor || 0), "message pagination must move forward");
      cursor = result.next_cursor;
    } while (cursor != null);
    assert.deepEqual(received, stored.messages, "continuous pages preserve every message exactly once in order");
    assert.equal(new Set(received.map((message) => message.id)).size, received.length);
    const payload = { session_id: stored.session.id, request_id: "mock-read-only-1", message: "只读核对完整资料".repeat(700) };
    fixtures.opsChatMode = "read_only";
    const accepted = await chat(payload);
    assert.equal(accepted.status, "queued");
    assert.deepEqual(await chat(payload), accepted, "an identical HTTP retry reuses the same persisted task");
    await chat({ ...payload, message: "同键不同内容" }, 409);
    await chat({ ...payload, request_id: "mock-busy-1" }, 409);
    for (const extra of [{ history: [{ role: "system", content: "伪造授权" }] }, { selected_product_ids: ["100001"] }, { account_key: "mock-second" }]) {
      await chat({ ...payload, request_id: "mock-invalid-1", ...extra }, 422);
    }
    const run = fixtures.opsRuns.get(accepted.run_id);
    for (let count = 0; count < 3; count += 1) await call(`/api/ops/runs/${run.id}?after_seq=0`);
    assert.equal(run.status, "queued", "GET run cannot start the worker or perform a write");
    assert.equal(run.polls, 0);
    assert.equal(fixtures.opsWrites.length, 0);
    let afterSeq = 0;
    const events = [];
    for (let count = 0; count < 3; count += 1) {
      const result = await call(`/api/ops/runs/${run.id}?after_seq=${afterSeq}`);
      assert.ok(result.events.every((event) => event.seq > afterSeq));
      events.push(...result.events);
      afterSeq = result.next_seq;
      advanceAgentRun(run);
    }
    assert.deepEqual(events, run.events, "incremental polling cannot overlap or drop visible events");
    assert.equal(new Set(events.map((event) => event.seq)).size, events.length);
    assert.equal(run.status, "succeeded");
    assert.equal(fixtures.opsWrites.length, 0);
    assert.equal(stored.messages.find((message) => message.run_id === run.id && message.role === "user").content, payload.message);

    fixtures.opsChatMode = "stop";
    const stopping = await chat({ session_id: stored.session.id, request_id: "mock-stop-1", message: "保存第一项后停止" });
    const stopped = fixtures.opsRuns.get(stopping.run_id);
    advanceAgentRun(stopped);
    assert.equal(stopped.changedCount, 1);
    await call(`/api/ops/runs/${stopped.id}/cancel`, { method: "POST", body: {}, status: 422 });
    const cancellation = await call(`/api/ops/runs/${stopped.id}/cancel`, { method: "POST" });
    assert.equal(cancellation.status, "cancel_requested");
    assert.equal(cancellation.changed_count, 1);
    advanceAgentRun(stopped);
    const stoppedWrites = fixtures.opsWrites.length;
    advanceAgentRun(stopped);
    assert.equal((await call(`/api/ops/runs/${stopped.id}`)).status, "cancelled");
    assert.equal(fixtures.opsWrites.length, stoppedWrites, "cancelled runs never revive on later reads or worker ticks");
    await call(`/api/ops/runs/${stopped.id}/retry`, { method: "POST", body: { request_id: "mock-cancelled-retry" }, status: 409 });

    fixtures.opsChatMode = "partial_failed";
    const partial = await chat({ session_id: stored.session.id, request_id: "mock-partial-1", message: "两项配置第二项可恢复" });
    const partialRun = fixtures.opsRuns.get(partial.run_id);
    advanceAgentRun(partialRun);
    advanceAgentRun(partialRun);
    assert.equal((await call(`/api/ops/runs/${partialRun.id}`)).recoverable, true);
    const retryBody = { request_id: "mock-retry-1" };
    const retried = await call(`/api/ops/runs/${partialRun.id}/retry`, { method: "POST", body: retryBody, status: 202 });
    assert.deepEqual(await call(`/api/ops/runs/${partialRun.id}/retry`, { method: "POST", body: retryBody, status: 202 }), retried);
    advanceAgentRun(partialRun);
    advanceAgentRun(partialRun);
    assert.equal(partialRun.status, "succeeded");
    assert.equal(partialRun.changedCount, 2);
    assert.equal(fixtures.opsWrites.filter((write) => write.runId === partialRun.id && write.target === "100001").length, 1);
    partialRun.status = "partial_failed";
    for (const recoverable of [false, undefined, "true"]) {
      partialRun.recoverable = recoverable;
      assert.equal((await call(`/api/ops/runs/${partialRun.id}`)).recoverable, false);
      await call(`/api/ops/runs/${partialRun.id}/retry`, { method: "POST", body: { request_id: `mock-invalid-retry-${recoverable}` }, status: 409 });
    }
    const oldMessages = structuredClone(stored.messages);
    const next = await call("/api/ops/sessions", { method: "POST", status: 201 });
    assert.notEqual(next.session.id, stored.session.id);
    assert.deepEqual(stored.messages, oldMessages, "new conversation does not erase old history or receipts");
    for (const options of [{ accountKey: "mock-second" }, {}]) {
      if (!options.accountKey) fixtures.me = { ...owner, username: "mock-other-user" };
      assert.equal((await call("/api/ops/sessions/current", options)).session, null);
      for (const target of [`/api/ops/sessions/${stored.session.id}/messages`, `/api/ops/runs/${run.id}`]) await call(target, { ...options, status: 404 });
      await call(`/api/ops/runs/${partialRun.id}/retry`, { ...options, method: "POST", body: retryBody, status: 404 });
      await call(`/api/ops/runs/${stopped.id}/cancel`, { ...options, method: "POST", status: 404 });
    }
    console.log(JSON.stringify({ ok: true, scope: "mock", browser: false, cases: ["read-only-get", "complete-pagination", "incremental-events", "chat-idempotency", "strict-chat-body", "scoped-user-shop", "stop-without-body", "stop-preserves-writes", "recoverable-boolean-only", "retry-no-replay", "new-session-keeps-history"] }));
  } finally {
    stopAgentMockWorkers();
    fixtures.opsWorkerPaused = false;
    fixtures.me = owner;
    fixtures.shopAccounts = accounts;
  }
}

function resourceRow(account, values = {}) {
  return { account_id: account.id, key: account.key, name: account.name, enabled: true,
    worker_state: "running", mode: "rules", metrics_state: "ready", cpu_percent: 12.5,
    rss_bytes: 64 * 1024 * 1024, vms_bytes: 120 * 1024 * 1024, uptime_seconds: 3661,
    memory_limit_bytes: 400 * 1024 * 1024, configured_memory_limit_bytes: 400 * 1024 * 1024,
    pending_restart: false, sampled_at: Date.now() / 1000, message: "", ...values };
}

async function checkHomeAlerts(browser, baseUrl) {
  const baseBot = structuredClone(fixtures.bot);
  const missingPrice = { ...productFixtures[0], title: "测试用商品超长标题ABCDEFGHIJKLMNOPQRSTUVWXYZ不应挤压价格", price_display: "", image_url: "https://cdn.example/home-product.png" };
  fixtures.products = [missingPrice];
  fixtures.bot = { ...baseBot, product_count: 1 };
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  const cases = [];
  const reloadHome = async () => {
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("#workspace:not([hidden])");
    await openView(page, "home");
    await page.waitForFunction((count) => document.querySelectorAll("#homeProductGrid .home-product-card").length === count, Math.min(fixtures.products.length, 6));
  };
  const checkLayout = async (count) => {
    for (const width of [1440, 1280, 980, 768, 640, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const scale of [1, 1.5, 2]) {
        await page.evaluate((factor) => {
          const nodes = [...document.querySelectorAll("#homeProductGrid .home-product-card, #homeProductGrid .home-product-name, #homeProductGrid .home-product-price, #homeProductGrid .badge")];
          nodes.forEach((node) => { node.style.fontSize = ""; });
          const sizes = nodes.map((node) => parseFloat(getComputedStyle(node).fontSize));
          nodes.forEach((node, index) => { node.style.fontSize = `${sizes[index] * factor}px`; });
        }, scale);
        const result = await page.locator("#homeProductGrid").evaluate((grid) => {
          const bounds = grid.getBoundingClientRect();
          return { width: bounds.width, left: bounds.left, right: bounds.right, clientWidth: grid.clientWidth, scrollWidth: grid.scrollWidth,
            cards: [...grid.querySelectorAll(".home-product-card")].map((card) => {
              const rect = card.getBoundingClientRect();
              const price = card.querySelector(".home-product-price");
              const title = card.querySelector(".home-product-name");
              const thumb = card.querySelector(".home-product-thumb").getBoundingClientRect();
              const style = getComputedStyle(price);
              return { width: rect.width, left: rect.left, right: rect.right, price: price.textContent,
                priceWidth: price.clientWidth, priceScrollWidth: price.scrollWidth, priceHeight: price.getBoundingClientRect().height,
                priceLineHeight: parseFloat(style.lineHeight), priceWeight: Number(style.fontWeight), priceColor: style.color,
                pending: price.classList.contains("is-pending"), titleHeight: title.getBoundingClientRect().height,
                titleLineHeight: parseFloat(getComputedStyle(title).lineHeight), ratio: thumb.width / thumb.height };
            }) };
        });
        const label = `${count} products / ${width}px / ${scale}x`;
        assert.equal(result.cards.length, count, label);
        assert.ok(result.scrollWidth <= result.clientWidth + 1, `${label}: grid cannot overflow`);
        for (const card of result.cards) {
          assert.ok(card.width >= Math.min(160, result.width) - 1, `${label}: card is too narrow: ${JSON.stringify(card)}`);
          assert.ok(card.left >= result.left - 1 && card.right <= result.right + 1, `${label}: card must fit its panel`);
          assert.ok(card.priceScrollWidth <= card.priceWidth + 1, `${label}: price cannot be clipped: ${JSON.stringify(card)}`);
          assert.ok(card.priceHeight <= card.priceLineHeight + 1, `${label}: price stays on one line`);
          assert.ok(card.titleHeight <= card.titleLineHeight * 2 + 1, `${label}: long title stays within two lines`);
          assert.ok(Math.abs(card.ratio - 1.6) < 0.03, `${label}: thumbnail retains 16:10 ratio`);
          if (card.pending) {
            assert.equal(card.price, "价格待同步");
            assert.ok(card.priceWeight <= 500, `${label}: missing price is neutral copy, not an amount`);
          }
        }
        assert.equal(result.cards[0].pending, true, `${label}: missing price has a distinct style`);
        if (count > 1) {
          assert.equal(result.cards[1].price, "¥0", `${label}: actual zero price must not become missing`);
          assert.equal(result.cards[1].pending, false);
          assert.notEqual(result.cards[0].priceColor, result.cards[1].priceColor);
        } else if (result.width >= 340) {
          assert.ok(result.cards[0].width < result.width * 0.7, `${label}: a single preview must not stretch across the whole panel`);
        }
        await assertNoOverflow(page, label);
        cases.push(label);
      }
    }
  };
  const setAlert = (code, { legacy = false, reauth = false, kind = "shop_account", errorCode = "" } = {}) => {
    fixtures.bot = { ...baseBot, product_count: 1, connected: false,
      sync_status: reauth ? "verified" : code, auth_code: reauth ? code : "ok", reauthorization_required: reauth,
      connection_state: legacy || code === "verification_required" ? "security_check" : "degraded", catalog_state: "stale",
      capabilities: { view_products: true, sync_products: code !== "risk_cooldown", publish_products: false },
      cookie_status: { code, label: legacy ? "需要安全验证" : "待确认", message: "旧消息：闲鱼要求安全验证，请先在闲鱼 App 完成安全验证" } };
    fixtures.attention = [{ id: "att_aaaaaaaaaaaaaaaaaaaaaaaa", kind, code: errorCode ? "degraded" : code, error_code: errorCode,
      title: legacy ? "需要安全验证" : ({ platform_busy: "闲鱼请求繁忙", account_restricted: "账号受限", session_expired: "登录会话已失效" }[code] || "旧告警"),
      message: legacy ? "闲鱼要求安全验证，请先在闲鱼 App 或浏览器完成安全验证" : "接口返回的状态需要确认。",
      action_label: "查看店铺", action_view: "shops", severity: "warning", resolved: false }];
  };
  try {
    await desktopLogin(page, fixtures.me.username);
    await assertDesktopAssetVersion(page);
    await page.waitForSelector("#homeProductGrid .home-product-card");
    await page.locator("#homeProductGrid img").scrollIntoViewIfNeeded();
    await page.waitForFunction(() => { const img = document.querySelector("#homeProductGrid img"); return img?.complete && img.naturalWidth > 0; });
    await checkLayout(1);
    fixtures.products = [missingPrice, { ...productFixtures[1], price_display: "¥0" }, ...productFixtures.slice(2, 6)];
    fixtures.bot.product_count = 6;
    await reloadHome();
    await checkLayout(6);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator("#homeProductGrid .home-product-card").first().click();
    await page.waitForSelector('[data-panel="goods"]:not([hidden])');
    assert.match(await page.locator("#productGrid").innerText(), /测试用商品/);
    fixtures.products = [missingPrice];

    // Legacy titles, worker auth state and saved messages are not evidence of
    // a current App verification prompt. Normalize copy without losing IDs.
    setAlert("risk_control", { legacy: true, reauth: true, errorCode: "risk_control" });
    await reloadHome();
    assert.equal(await page.locator("#attentionList strong").innerText(), "接口请求受限");
    assert.match(await page.locator("#attentionList p").innerText(), /尚不能确认/);
    assert.doesNotMatch(await page.locator("#attentionList").innerText(), /闲鱼要求安全验证|请先在闲鱼 App/);
    await page.locator('[data-attention-toggle="att_aaaaaaaaaaaaaaaaaaaaaaaa"]').click();
    await page.waitForFunction(() => document.querySelector("#attentionCount")?.textContent === "0");
    await reloadHome();
    assert.equal(await page.locator('[data-attention-toggle]').getAttribute("aria-pressed"), "true", "processed status survives copy normalization and reload");
    await page.locator('[data-attention-toggle]').click();
    await page.waitForFunction(() => document.querySelector("#attentionCount")?.textContent === "1");
    await page.locator('#attentionList [data-view="shops"]').click();
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    assert.equal(await page.locator("#cookieStatusTitle").innerText(), "接口请求受限");
    assert.equal(await page.locator("#shopConnectionTitle").innerText(), "店铺连接需要确认");
    assert.match(await page.locator("#shopAccountsPanelList").innerText(), /接口请求受限/);
    assert.equal(await page.locator("#checkCookieButton").isEnabled(), true);

    for (const [code, label, title] of [
      ["verification_required", "接口要求验证", "自动连接接口要求验证"],
      ["risk_cooldown", "请求保护冷却中", "店铺连接需要确认"],
      ["platform_busy", "闲鱼请求繁忙", "店铺连接需要确认"],
    ]) {
      setAlert(code, { legacy: code === "risk_cooldown", reauth: code === "verification_required" });
      await reloadHome();
      assert.equal(await page.locator("#attentionList strong").innerText(), label);
      if (code === "verification_required") assert.match(await page.locator("#attentionList p").innerText(), /明确的验证要求.*App 不一定弹窗/);
      if (code === "risk_cooldown") assert.match(await page.locator("#attentionList p").innerText(), /系统因之前的受限请求/);
      for (const width of [1280, 640, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await assertNoOverflow(page, `${code} attention ${width}px`);
      }
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.locator('#attentionList [data-view="shops"]').click();
      await page.waitForSelector('[data-panel="shops"]:not([hidden])');
      assert.equal(await page.locator("#shopConnectionTitle").innerText(), title);
      assert.equal(await page.locator("#checkCookieButton").isDisabled(), code === "risk_cooldown");
      assert.equal(await page.locator("#homeProductGrid .home-product-card").count(), 1, "blocked connection retains cached products");
    }
    for (const [code, label] of [["account_restricted", "账号受限"], ["session_expired", "登录会话已失效"]]) {
      setAlert(code, { reauth: code === "session_expired" });
      await reloadHome();
      assert.equal(await page.locator("#attentionList strong").innerText(), label, "unrelated errors retain their original meaning");
    }
    fixtures.bot = { ...baseBot, product_count: 1 };
    fixtures.attention = [];
    await reloadHome();
    assert.match(await page.locator("#attentionList").innerText(), /当前没有需要处理的事项/);
    assert.equal(await page.locator("#attentionCount").innerText(), "0");
    assert.equal(fixtures.shopActionRequests.length, 0, "view and acknowledgement actions cannot trigger shop probes");
    assert.equal(fixtures.cookieSaves + fixtures.botStartModes.length + fixtures.qrStarts + fixtures.qrConnects, 0, "display fixes must not reauthorize or restart workers");
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: "home-alerts", layoutCases: cases.length, alertCases: ["legacy-worker-risk", "resolved-history", "explicit-verification", "local-cooldown", "platform-busy", "account-restricted", "session-expired", "recovered"], noPlatformRequests: true }));
  } catch (error) {
    await reportDesktopFailure(page, "home-alerts", error, evidence);
    throw error;
  } finally { await page.close(); }
}

async function checkDashboardDesktop(browser, baseUrl) {
  const today = { buyer_messages_total: 11, messages_total: 18, auto_replies_total: 7,
    fulfillment_success_total: 5, fulfillment_failed_total: 0, unread_conversations_total: 3 };
  fixtures.analyticsByPeriod = Object.fromEntries([1, 7, 30].map((days) => [days, {
    totals: { ...today, buyer_messages_total: 11 * days, auto_replies_total: 7 * days },
    buckets: Array.from({ length: days }, (_, index) => ({ date: `2026-08-${String(index % 28 + 1).padStart(2, "0")}`,
      buyer_messages_total: index + 1, messages_total: index + 3, auto_replies_total: index })),
  }]));
  fixtures.shopAccounts.push({ ...fixtures.shopAccounts[0], id: 2, key: "goods-second", name: "商品隔离二店" });
  fixtures.accountData["goods-second"] = { products: [{ id: "200001", title: "仅二店商品", price_display: "¥2" }],
    automation: { ...fixtures.automation, rules: [], deliveries: [] }, bot: { ...fixtures.bot, shop_name: "商品隔离二店" } };
  fixtures.deliveryStatusByAccount.default = { available: true, items: [
    { item_id: "100001", delivery: "material", configured: true, enabled: true, template_id: null },
    { item_id: "100002", delivery: "pan", configured: true, enabled: true, template_id: "tpl-1" },
    { item_id: "100003", delivery: "redeem", configured: true, enabled: false, template_id: "tpl-2" },
    { item_id: "100004", delivery: "conflict", configured: false, enabled: false, template_id: null },
  ] };
  fixtures.resourceRowsByUser[fixtures.me.username] = [resourceRow(fixtures.shopAccounts[0]),
    resourceRow(fixtures.shopAccounts[1], { metrics_state: "sampling", cpu_percent: null })];
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  try {
    await desktopLogin(page, fixtures.me.username);
    await assertDesktopAssetVersion(page);
    if (process.env.SAAS_UI_SCOPE !== 'goods') {
      await page.waitForFunction(() => document.querySelector('#homeStatCards')?.textContent.includes('11'));
      assert.match(await page.locator('[data-panel="home"] h1').innerText(), /店铺概览/);
      const stats = await page.locator('#homeStatCards').innerText();
      assert.match(stats, /自动回复/);
      assert.match(stats, /未读会话/);
      assert.equal(stats.includes('%'), false, 'message counts must not be mislabeled as reply rate');
      for (const value of [11, 7, 5, 3]) assert.match(stats, new RegExp(`(^|\\D)${value}(\\D|$)`));
      await page.waitForFunction(() => document.querySelector('#analyticsChart')?.children.length > 0);
      assert.ok(fixtures.analyticsRequests.includes(1) && fixtures.analyticsRequests.includes(7), 'today totals and seven-day trend have separate sources');
      assert.equal(fixtures.apiRequests.some((item) => item.path.startsWith('/api/admin/')), false, 'owner must not fetch platform-private settings');
      await assertNoOverflow(page, 'new overview desktop');
    }

    await openView(page, 'goods');
    await page.waitForSelector('#productViewCards');
    await page.selectOption('#productPageSize', '12');
    await page.waitForFunction(() => document.querySelectorAll('#productGrid [data-product-id]').length === 12);
    const writesBefore = fixtures.batchCommits.length + fixtures.templateRequests.length;
    await page.click('#productNextPage');
    const pageLabel = await page.locator('#productPageLabel').innerText();
    await page.click('#productViewList');
    assert.equal(await page.locator('#productPageLabel').innerText(), pageLabel, 'layout switch preserves page');
    assert.equal(await page.locator('#productGrid [data-product-id]').count(), 10);
    await page.click('#productViewCards');
    assert.equal(await page.locator('#productGrid [data-product-id]').count(), 10);
    await page.selectOption('#productPageSize', '24');
    await page.fill('#productSearch', '100002');
    await page.waitForFunction(() => document.querySelectorAll('#productGrid [data-product-id]').length === 1);
    assert.match(await page.locator('#productGrid').innerText(), /网盘/);
    await page.click('#productViewList');
    assert.equal(await page.inputValue('#productSearch'), '100002');
    await page.fill('#productSearch', '');
    await page.selectOption('#productStatusFilter', 'paused');
    assert.equal(await page.locator('#productGrid [data-product-id]').count(), 1);
    assert.match(await page.locator('#productGrid').innerText(), /暂停/);
    await page.selectOption('#productStatusFilter', 'all');
    await page.fill('#productSearch', '100002');
    const advanced = page.locator('#productGrid [data-product-id="100002"]');
    assert.equal(await advanced.locator('[data-edit-delivery]').count(), 0, 'advanced templates must not open material overwrite flow');
    assert.equal(await advanced.locator('[data-delivery-toggle]').count(), 0, 'advanced templates must not use material pause commands');
    await advanced.getByRole('button', { name: /模板/ }).click();
    await page.waitForSelector('[data-panel="templates"]:not([hidden])');
    assert.equal(fixtures.batchCommits.length + fixtures.templateRequests.length, writesBefore);
    await page.evaluate(() => document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close()));

    await openView(page, 'shops');
    await page.click('#shopAccountsPanelList [data-account-switch="goods-second"]');
    await openView(page, 'goods');
    await page.waitForFunction(() => document.querySelector('#productGrid')?.textContent.includes('仅二店商品'));
    assert.equal((await page.locator('#productGrid').innerText()).includes('100002'), false);
    for (const width of [1280, 768, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.click('#productViewCards');
      await assertNoOverflow(page, `goods cards ${width}`);
      await page.click('#productViewList');
      await assertNoOverflow(page, `goods list ${width}`);
    }
    const stored = await page.evaluate(() => window.__uiStorageWrites);
    assert.equal(JSON.stringify(stored).includes('仅二店商品'), false, 'preferences must not persist product bodies');
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: process.env.SAAS_UI_SCOPE, cases: [...(process.env.SAAS_UI_SCOPE === 'goods' ? [] : ['honest-today-metrics', 'separate-trend']), 'cards-list-pagination', 'search-and-status', 'advanced-binding-no-overwrite', 'shop-isolation', '1280-768-390-layout', 'preferences-only'] }));
  } catch (error) {
    await reportDesktopFailure(page, process.env.SAAS_UI_SCOPE, error, evidence);
    throw error;
  } finally { await page.close(); }
}

async function checkResourcesDesktop(browser, baseUrl) {
  fixtures.me = { ...fixtures.me, username: 'resource-admin', role: 'admin', role_label: '管理员', is_admin: true,
    platform_permissions: ['platform.settings.manage', 'platform.users.manage', 'platform.audit.read', 'platform.updates.manage'] };
  fixtures.shopAccounts[0].name = '管理员测试店';
  const firstAccount = fixtures.shopAccounts[0];
  fixtures.shopAccounts = Array.from({ length: 53 }, (_, index) => index === 0 ? firstAccount : {
    ...firstAccount, id: index + 1, key: `resource-shop-${index + 1}`, name: `资源分页店${index + 1}`,
  });
  const originalAccountCount = fixtures.shopAccounts.length;
  fixtures.resourceRowsByUser['resource-admin'] = fixtures.shopAccounts.map((account, index) => resourceRow(account, index === 0 ? {} : {
    worker_state: 'stopped', metrics_state: 'stopped', cpu_percent: 0, rss_bytes: 0, vms_bytes: 0, memory_limit_bytes: null, uptime_seconds: 0,
  }));
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  try {
    await desktopLogin(page, fixtures.me.username);
    await openView(page, 'settings');
    await page.click('[data-settings-tab="resources"]');
    await page.waitForFunction(() => document.querySelector('#resourceMemoryMiB')?.value === '400');
    assert.equal(await page.inputValue('#resourceMaxWorkers'), '3', 'use effective deployment value, not code fallback 15');
    const beforeActions = fixtures.shopActionRequests.length;
    await page.fill('#resourceMaxShops', '1');
    await page.fill('#resourceMaxWorkers', '1');
    await page.fill('#resourceMemoryMiB', '768');
    await desktopApiClick(page, '#saveResourceSettings', '/api/admin/resource-settings', 'PUT');
    assert.deepEqual(fixtures.resourceSaveRequests.at(-1), { expected_revision: 0, max_shop_accounts: 1, max_running_workers: 1, worker_memory_mib: 768 });
    assert.equal(fixtures.shopActionRequests.length, beforeActions, 'saving must not stop/restart Workers');
    assert.match(await page.locator('#resourceSettingsMessage').innerText(), /下次|启动|重启/);
    assert.equal(fixtures.resourceRowsByUser['resource-admin'][0].memory_limit_bytes, 400 * 1024 * 1024);
    assert.equal(await page.locator('[data-action-resource-start], [data-action-resource-stop]').count(), 0, 'read-only monitoring must not add direct Worker controls');
    assert.equal(fixtures.shopAccounts.length, originalAccountCount, 'lowering quota must preserve all existing shops');
    fixtures.resourceConflictOnce = true;
    await page.fill('#resourceMemoryMiB', '1024');
    evidence.allowedFailures.push({ path: '/api/admin/resource-settings', status: 409 });
    await desktopApiClick(page, '#saveResourceSettings', '/api/admin/resource-settings', 'PUT', 409);
    assert.equal(fixtures.resourcePolicy.worker_memory_mib, 768, 'stale form cannot overwrite the newer revision');
    assert.match(await page.locator('#resourceSettingsMessage').innerText(), /刷新|修改|更新/);
    assert.equal(fixtures.resourceSaveRequests.length, 1);
    await openView(page, 'home');
    await page.waitForFunction(() => document.querySelector('[data-panel="home"]')?.textContent.includes('768'));
    const home = await page.locator('[data-panel="home"]').innerText();
    assert.match(home, /64/);
    assert.match(home, /400/);
    assert.match(home, /768/);
    await assertNoOverflow(page, 'resources home desktop');
    assert.ok(await page.locator('#homeResourceBody tr').count() <= 5, 'overview shows a compact resource summary');
    await openView(page, 'shops');
    await page.waitForFunction(() => document.querySelector('#shopResourcesBody')?.textContent.includes('资源分页店53'));
    assert.ok(fixtures.resourceRequests.some((request) => request.cursor > 0), 'the monitor must read beyond the first fifty shops');
    await page.waitForLoadState('networkidle');
    fixtures.resourceErrorOnce = true;
    evidence.allowedFailures.push({ path: '/api/bot/resources', status: 503 });
    await desktopApiClick(page, '#refreshShopResources', '/api/bot/resources', 'GET', 503);
    await page.waitForFunction(() => /失败|无法|上次|未更新/.test(document.querySelector('#shopResourcesMessage')?.textContent || ''));
    assert.ok((await page.locator('#shopResourcesBody').innerText()).includes('资源分页店53'), 'marked stale data may remain visible after a failed read');
    const slowReadBase = fixtures.resourceRequests.length;
    fixtures.resourceResponseDelayMs = 6300;
    await Promise.all([
      page.waitForRequest((request) => new URL(request.url()).pathname.endsWith('/api/bot/resources')),
      page.click('#refreshShopResources'),
    ]);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
      delete document.hidden;
      delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(5200);
    assert.equal(fixtures.resourceRequests.length, slowReadBase + 1, 'visibility changes must not start overlapping resource requests');
    await page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname.endsWith('/api/bot/resources') && url.searchParams.get('cursor') === '50';
    });
    await page.waitForLoadState('networkidle');
    fixtures.resourceResponseDelayMs = 800;
    await Promise.all([
      page.waitForRequest((request) => new URL(request.url()).pathname.endsWith('/api/bot/resources')),
      page.click('#refreshShopResources'),
    ]);
    await page.click('#logoutButton');
    await page.waitForSelector('#authUsername:visible');
    fixtures.me = { ...fixtures.me, username: 'resource-owner', role: 'owner', role_label: '店主', is_admin: false, platform_permissions: [] };
    fixtures.resourceRowsByUser['resource-owner'] = [resourceRow({ id: 8, key: 'default', name: '普通账号独有店' },
      { worker_state: 'unknown', metrics_state: 'unavailable', cpu_percent: null, rss_bytes: null, vms_bytes: null, memory_limit_bytes: null, message: '暂时无法采样' })];
    const requestsBefore = fixtures.apiRequests.length;
    await desktopLogin(page, fixtures.me.username);
    await openView(page, 'settings');
    await page.click('[data-settings-tab="resources"]');
    await page.waitForSelector('[data-settings-panel="resources"]:not([hidden])');
    for (const id of ['resourceMaxShops', 'resourceMaxWorkers', 'resourceMemoryMiB']) {
      const input = page.locator(`#${id}`);
      if (await input.count()) assert.equal(await input.isDisabled() || await input.getAttribute('readonly') !== null, true, 'owner resource policy is read only');
    }
    assert.equal(fixtures.apiRequests.slice(requestsBefore).some((item) => item.path.startsWith('/api/admin/')), false);
    assert.equal((await page.locator('[data-settings-panel="resources"]').innerText()).includes('1024'), false, 'unsaved administrator draft must not cross users');
    await openView(page, 'home');
    await page.waitForFunction(() => document.querySelector('#homeResourceBody')?.textContent.includes('普通账号独有店'));
    await page.waitForTimeout(900);
    const ownedResources = await page.locator('#homeResourceBody').innerText();
    assert.equal(ownedResources.includes('管理员测试店'), false, 'late previous-user resource responses must be discarded');
    assert.equal(ownedResources.includes('已停止'), false, 'unknown process identity must not be presented as confirmed stopped');
    assert.doesNotMatch(ownedResources, /\b0(?:\.0)?\s*(?:MiB|MB|%)/, 'unavailable sampling must not invent zero usage');
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const hiddenReads = fixtures.resourceRequests.length;
    await page.waitForTimeout(5500);
    assert.equal(fixtures.resourceRequests.length, hiddenReads, 'hidden page must stop resource polling');
    await page.evaluate(() => {
      delete document.hidden;
      delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await openView(page, 'goods');
    await page.waitForTimeout(200);
    const offViewReads = fixtures.resourceRequests.length;
    await page.waitForTimeout(5500);
    assert.equal(fixtures.resourceRequests.length, offViewReads, 'unrelated views must stop resource polling');
    await openView(page, 'settings');
    await page.click('[data-settings-tab="resources"]');
    for (const width of [768, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await assertNoOverflow(page, `resource settings ${width}`);
    }
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: 'resources', cases: ['effective-defaults', 'admin-only-cas', 'no-stop-on-save', 'applied-vs-next-limit', 'conflict-keeps-policy', 'owner-read-only', 'cross-user-draft-clear', 'mobile-layout'] }));
  } catch (error) {
    await reportDesktopFailure(page, 'resources', error, evidence);
    throw error;
  } finally { await page.close(); }
}

async function checkSettingsDesktop(browser, baseUrl) {
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  const owner = structuredClone(fixtures.me);
  const accounts = fixtures.shopAccounts;
  fixtures.shopAccounts = [...accounts, { ...accounts[0], id: 2, key: "settings-second", name: "设置保留测试店" }];
  try {
    await desktopLogin(page, owner.username);
    await assertDesktopAssetVersion(page);
    await openView(page, "settings");
    await page.waitForSelector('[data-settings-panel="ai"]:not([hidden])');
    assert.equal(await page.locator('[data-panel="settings"] #aiModel').count(), 1, "the existing model form must move into settings");
    assert.equal(await page.locator('[data-panel="ai-config"] #aiModel').count(), 0);
    assert.equal(await page.locator("#checkUpdateButton").count(), 0, "the duplicate version check button must be removed");
    for (const tab of ["ai", "security"]) assert.equal(await page.locator(`[data-settings-tab="${tab}"]`).isVisible(), true);
    for (const tab of ["accounts", "audit"]) assert.equal(await page.locator(`[data-settings-tab="${tab}"]`).isVisible(), false);
    assert.equal(await page.locator('#settingsAiLegacySelect, .settings-ai-legacy-box, [data-settings-tab="version"], [data-settings-panel="version"], #adminUpdateControls, #updateChannelSelect').count(), 0, "removed migration/version controls must not remain hidden in DOM");
    assert.equal(fixtures.settingsRequests.some((item) => item.path.includes("legacy-sources")), false);
    await page.fill("#aiBaseUrl", "https://first.example.invalid/v1");
    await page.fill("#aiModel", "first-user-model");
    await page.fill("#aiApiKey", "mock-first-user-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    const initialTest = fixtures.settingsRequests.findLast((item) => item.path.endsWith("/test")).payload;
    assert.equal(initialTest.expected_revision, 0);
    assert.deepEqual(Object.keys(initialTest).sort(), ["api_key", "base_url", "expected_revision", "model", "provider"]);
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    const initialSave = fixtures.settingsRequests.findLast((item) => item.method === "PUT").payload;
    assert.equal(initialSave.confirm, true);
    assert.ok(initialSave.verification_token);
    assert.equal(userConnectionFixture().revision, 1);
    await page.waitForFunction(() => document.querySelector("#aiApiKey")?.value === "");

    // A draft belongs to the signed-in user, not the selected shop. Switching
    // stores must preserve both visible inputs and the draft's test context.
    await page.fill("#aiBaseUrl", "https://draft.example.invalid/v1");
    await page.fill("#aiModel", "owner-unsaved-model");
    await page.fill("#aiApiKey", "mock-owner-unsaved-secret");
    await openView(page, "shops");
    await page.click('#shopAccountsPanelList [data-account-switch="settings-second"]');
    await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active")?.dataset.accountSwitch === "settings-second");
    await openView(page, "settings");
    assert.equal(await page.inputValue("#aiModel"), "owner-unsaved-model");
    assert.equal(await page.inputValue("#aiBaseUrl"), "https://draft.example.invalid/v1");
    assert.equal(await page.inputValue("#aiApiKey"), "mock-owner-unsaved-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    await page.fill("#aiModel", "owner-edited-after-test");
    assert.equal(await page.locator("#aiSaveConnection").isDisabled(), true, "editing after testing must invalidate the verification token");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    await page.waitForFunction(() => document.querySelector("#aiApiKey")?.value === "");
    assert.equal(userConnectionFixture().model, "owner-edited-after-test");
    assert.equal(userConnectionFixture().revision, 2);
    assert.equal(fixtures.aiRequests.filter((item) => ["connection", "connection-test", "key"].includes(item.kind)).length, 0, "settings must not fall back to legacy shop write endpoints");
    assert.doesNotMatch(await page.locator("body").innerText(), /mock-owner-unsaved-secret/);
    assert.equal(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }).includes("mock-owner-unsaved-secret")), false, "secrets must not enter browser storage");
    await assertNoBusinessStorage(page, /mock-(?:first-user|owner-unsaved)-secret/);

    // A stale settings revision is a visible conflict, not a silent overwrite.
    await page.fill("#aiModel", "conflicting-model");
    userConnectionFixture().revision += 1;
    evidence.allowedFailures.push({ path: "/api/settings/ai/connection/test", status: 409 });
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test", "POST", 409);
    await page.waitForFunction(() => /变化|修改|冲突|刷新/.test(document.querySelector("#aiConnectionMessage")?.textContent || ""));
    assert.equal(userConnectionFixture().model, "owner-edited-after-test");
    assert.equal(await page.locator("#aiSaveConnection").isDisabled(), true);

    // Help is a reopenable dialog, not an extra navigation domain.
    for (let cycle = 0; cycle < 2; cycle += 1) {
      await page.click("#settingsDocsBtn");
      await page.waitForSelector("#docsHelpModal[open]");
      assert.equal(await page.locator("#docsHelpModal .docs-manual").count(), 1);
      assert.equal(await page.locator("#docsHelpModal details.docs-faq-item").count(), 4);
      await page.locator("#docsHelpModal").getByRole("button", { name: /关闭/ }).click();
      await page.waitForSelector("#docsHelpModal:not([open])", { state: "attached" });
    }
    await page.click("#versionBadgeButton");
    await page.waitForSelector("#versionBadgePopover:not([hidden])");
    assert.match(await page.locator("#versionBadgeValue").innerText(), /0\.1\.0/);
    assert.doesNotMatch(await page.locator("#versionBadgeStatus").innerText(), /已是最新|当前最新/);
    await assertUnifiedVersionLink(page);
    assert.equal(await page.locator("#versionBadgeRefresh").isVisible(), false, "owners must not receive the administrator's external check action");
    assert.equal(await page.locator("#versionBadgeButton").evaluate((node) => node.classList.contains("has-update")), false);
    await assertNoOverflow(page, "settings version popover desktop");
    await page.click("#versionBadgeClose");
    assert.equal(fixtures.updateRequests.length, 0, "opening version metadata must never check a remote release");
    assert.deepEqual(fixtures.apiRequests.filter((item) => item.username === owner.username && item.path.startsWith("/api/admin/")), [], "ordinary users must not invoke any administrator endpoint");

    await page.click('[data-settings-tab="ai"]');
    await page.fill("#aiModel", "must-clear-at-logout");
    await page.fill("#aiApiKey", "mock-clear-at-logout-secret");
    await page.click("#logoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    fixtures.me = { ...owner, username: "owner-second", id: 22 };
    await desktopLogin(page, "owner-second");
    await openView(page, "settings");
    assert.notEqual(await page.inputValue("#aiModel"), "must-clear-at-logout", "a different user must not inherit the previous draft");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    assert.equal(userConnectionFixture().initialized, false);

    // Delete requires its own explicit confirmation and keeps the user's
    // initialized tombstone revision; subsequent testing must not fall back to 0.
    await page.fill("#aiBaseUrl", "https://second.example.invalid/v1");
    await page.fill("#aiModel", "second-user-model");
    await page.fill("#aiApiKey", "mock-second-user-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    await page.click("#aiDeleteKey");
    await page.waitForSelector("#confirmDialog[open]");
    const deletesBeforeCancel = fixtures.settingsRequests.filter((item) => item.method === "DELETE").length;
    await page.click("#confirmCancel");
    assert.equal(fixtures.settingsRequests.filter((item) => item.method === "DELETE").length, deletesBeforeCancel);
    await page.click("#aiDeleteKey");
    await page.waitForSelector("#confirmDialog[open]");
    await desktopApiClick(page, "#confirmAction", "/api/settings/ai/connection", "DELETE");
    await page.waitForSelector("#confirmDialog:not([open])", { state: "attached" });
    assert.deepEqual(fixtures.settingsRequests.findLast((item) => item.method === "DELETE").payload, { confirm: true, expected_revision: 1 });
    assert.equal(userConnectionFixture().initialized, true);
    assert.equal(userConnectionFixture().revision, 2);
    assert.equal(userConnectionFixture().api_key_configured, false);
    assert.equal(await page.inputValue("#aiModel"), "");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    await page.fill("#aiBaseUrl", "https://second.example.invalid/v1");
    await page.fill("#aiModel", "second-user-reconfigured");
    await page.fill("#aiApiKey", "mock-second-new-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    assert.equal(fixtures.settingsRequests.findLast((item) => item.path.endsWith("/test")).payload.expected_revision, 2);
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    assert.equal(userConnectionFixture().revision, 3);
    await page.click("#logoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");

    fixtures.me = { ...owner, username: "admin-demo", id: 1, role: "admin", is_admin: true, role_label: "管理员",
      platform_permissions: ["platform.audit.read", "platform.settings.manage", "platform.updates.manage", "platform.users.manage"] };
    await desktopLogin(page, "admin-demo");
    await openView(page, "settings");
    for (const tab of ["ai", "security", "accounts", "audit"]) assert.equal(await page.locator(`[data-settings-tab="${tab}"]`).isVisible(), true);
    await page.click('[data-settings-tab="accounts"]');
    await page.waitForFunction(() => document.querySelectorAll("#adminUsersBody [data-admin-user-id]").length === 2);
    await page.click('[data-settings-tab="audit"]');
    await page.waitForFunction(() => document.querySelectorAll("#auditEventList .audit-event").length === 1);
    await page.click("#versionBadgeButton");
    await page.waitForSelector("#versionBadgePopover:not([hidden])");
    assert.equal(fixtures.updateRequests.length, 0, "administrator startup still reads cached status only");
    for (const [status, text] of [["no_release", /无发布|尚无|暂无/], ["current", /未发现更高|无需更新|当前版本/], ["error", /无法|失败|错误/], ["available", /0\.2\.0|可更新|更高版本/]]) {
      fixtures.releaseCheckStatus = status;
      const count = fixtures.updateRequests.length;
      await desktopApiClick(page, "#versionBadgeRefresh", "/api/admin/updates/check");
      await page.waitForFunction(() => !document.querySelector("#versionBadgeRefresh")?.disabled);
      assert.equal(fixtures.updateRequests.length, count + 1, "one badge click must trigger one external-check mock request");
      assert.match(await page.locator("#versionBadgeStatus").innerText(), text);
      if (status !== "current") assert.doesNotMatch(await page.locator("#versionBadgeStatus").innerText(), /已是最新|当前最新/);
      assert.equal(await page.locator("#versionBadgeButton").evaluate((node) => node.classList.contains("has-update")), status === "available", "only a confirmed higher release receives a yellow highlight");
      await assertUnifiedVersionLink(page);
    }
    for (const overrides of [{ version: "0.0.9" }, { version: "0.1.0" }, { available: false }, { version: "not-a-version" },
      { status: "error", available: true, version: "9.0.0" }, { status: "unchecked", available: true, version: "9.0.0" },
      { status: "incomplete", available: true, version: "9.0.0", release_url: "javascript:window.__releaseNotesInjected=true" }]) {
      fixtures.releaseCheckOverrides = overrides;
      await desktopApiClick(page, "#versionBadgeRefresh", "/api/admin/updates/check");
      await page.waitForFunction(() => !document.querySelector("#versionBadgeRefresh")?.disabled);
      assert.equal(await page.locator("#versionBadgeButton").evaluate((node) => node.classList.contains("has-update")), false, "stale or invalid cache data must not invent an upgrade");
      await assertUnifiedVersionLink(page);
    }
    fixtures.releaseCheckOverrides = null;
    await page.click("#versionBadgeClose");
    assert.equal(fixtures.settingsRequests.some((item) => item.path.includes("legacy-sources") || Object.keys(item.payload).some((key) => key.startsWith("source_"))), false);
    assert.deepEqual(fixtures.updateRequests.filter((item) => item.action !== "check"), [], "the simplified UI must not call removed download/apply/rollback actions");
    assert.notEqual(await page.evaluate(() => window.__releaseNotesInjected), true);
    await assertNoOverflow(page, "settings administrator desktop");
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: "settings", desktop: 1440, screenshots: 0, connectionWrites: fixtures.settingsRequests.filter((item) => item.method === "PUT").length,
      cases: ["no-migration", "cross-shop-draft", "test-invalidation", "revision-conflict", "cross-user-clear", "delete-confirm-tombstone", "help-reopen", "owner-no-admin", "single-release-link", "confirmed-higher-version"] }));
  } catch (error) {
    await reportDesktopFailure(page, "settings", error, evidence);
    throw error;
  } finally {
    fixtures.me = owner;
    fixtures.shopAccounts = accounts;
    await page.close();
  }
}

async function checkOpsDesktop(browser, baseUrl) {
  Object.assign(userConnectionFixture(), { initialized: true, base_url: "https://mock.example.invalid/v1", model: "mock-agent-model", connection_status: "verified", api_key_configured: true, revision: 1, key_revision: 1 });
  const owner = structuredClone(fixtures.me);
  const accounts = fixtures.shopAccounts;
  const accountData = fixtures.accountData;
  fixtures.shopAccounts = [...accounts, { ...accounts[0], id: 2, key: "ops-second", name: "运维隔离测试店" }];
  fixtures.accountData = { ...accountData, "ops-second": { products: [{ ...fixtures.products[0], id: "200001", title: "第二店铺专属商品" }] } };
  const seed = createAgentSession(fixtures.me.username, "default");
  for (let index = 0; index < 26; index += 1) agentMessage(seed, index % 2 ? "assistant" : "user", `完整历史第${index + 1}条：${index === 0 ? "首条约定不能丢失" : "按原顺序保存"}`, { kind: "message", created_at: 1788825600 + index });
  const { page, evidence } = await desktopContractPage(browser, baseUrl);
  const history = page.locator("#opsChatHistory");
  const panel = page.locator('[data-panel="ops"]');
  let releaseStalePoll = () => {};
  let releaseUserAck = () => {};
  const submit = async (message, mode) => {
    fixtures.opsChatMode = mode;
    await page.fill("#opsPromptInput", message);
    return desktopApiClick(page, "#opsSendBtn", "/api/ops/chat", "POST", 202);
  };
  const waitText = async (text) => page.waitForFunction((value) => document.querySelector("#opsChatHistory")?.textContent.includes(value), text, { timeout: 12000 });
  const switchShop = async (key) => {
    await page.locator(`#accountTabs [data-account-switch="${key}"]`).click();
    await page.waitForFunction((value) => document.querySelector("#accountTabs .account-tab.is-active")?.dataset.accountSwitch === value, key);
    await page.waitForSelector('[data-panel="ops"]:not([hidden])');
  };
  try {
    await desktopLogin(page, fixtures.me.username);
    await assertDesktopAssetVersion(page);
    // The existing account selector loads the complete shop catalog from its
    // management view. Establish both shops through that real UI read first.
    await openView(page, "shops");
    await page.waitForSelector('#accountTabs [data-account-switch="ops-second"]');
    await openView(page, "ops");
    assert.equal(await page.locator("#opsShopSelect, #opsProductSelect, #opsRuleSelect, #opsConnectionBadge, .ops-context-bar, #opsPlanContainer, #opsPlanCard, #opsPlanConfirmBtn, #opsDiffModal").count(), 0, "the Agent page must remove target selection and approval UI, not hide it");
    assert.equal(await page.locator("#opsPromptInput").getAttribute("maxlength"), null, "Agent input has no 2000/4000-character product quota");
    await waitText("完整历史第26条");
    const restoredHistory = await history.innerText();
    let previousMessagePosition = -1;
    for (let index = 1; index <= 26; index += 1) {
      const marker = `完整历史第${index}条`;
      assert.equal(restoredHistory.split(marker).length - 1, 1, "paginated history must not drop or duplicate a message");
      const position = restoredHistory.indexOf(marker);
      assert.ok(position > previousMessagePosition, "history must preserve server message order");
      previousMessagePosition = position;
    }
    assert.ok(fixtures.opsRequests.filter((item) => item.path.endsWith("/messages")).length >= 4, "all mock history pages must be fetched");
    assert.equal(fixtures.opsRequests.filter((item) => item.path === "/api/ops/sessions" && item.method === "POST").length, 0, "opening the current session is read-only");

    const longMessage = " \n\t只读核对这份完整资料，不修改任何配置。\n" + "保留上下文与完整输入。".repeat(450) + "\n末尾唯一标记-不得截断\n\t ";
    assert.ok(longMessage.length > 4000);
    fixtures.opsChatMode = "read_only";
    fixtures.opsChatDelayMs = 250;
    await page.fill("#opsPromptInput", longMessage);
    const chatCount = fixtures.opsRequests.filter((item) => item.path === "/api/ops/chat").length;
    const accepted = page.waitForResponse((item) => item.url().endsWith("/api/ops/chat") && item.status() === 202);
    await page.locator("#opsSendBtn").evaluate((button) => { button.click(); button.click(); });
    const first = await (await accepted).json();
    await waitText("只读检查完成");
    assert.equal(fixtures.opsRequests.filter((item) => item.path === "/api/ops/chat").length, chatCount + 1, "double clicking send must create only one task");
    const sent = fixtures.opsRequests.findLast((item) => item.path === "/api/ops/chat");
    assert.deepEqual(Object.keys(sent.payload).sort(), ["message", "request_id", "session_id"]);
    assert.equal(sent.payload.message, longMessage);
    assert.equal(sent.payload.session_id, seed.session.id);
    assert.equal(typeof sent.payload.request_id, "string");
    assert.ok(sent.payload.request_id.length > 0);
    if (sent.idempotencyKey) assert.equal(sent.idempotencyKey, sent.payload.request_id);
    assert.equal(fixtures.opsWrites.length, 0);
    assert.ok(fixtures.opsRequests.some((item) => item.path.endsWith(first.run_id) && Number(item.query.after_seq) > 0), "run polling must request incremental events");
    const firstRunReads = fixtures.opsReadResponses.filter((item) => item.path === `/api/ops/runs/${first.run_id}`);
    const receivedEventSeqs = firstRunReads.flatMap((item) => item.seqs);
    assert.equal(new Set(receivedEventSeqs).size, receivedEventSeqs.length, "run polling must not request overlapping event pages");
    for (let index = 1; index < firstRunReads.length; index += 1) assert.equal(firstRunReads[index].afterSeq, firstRunReads[index - 1].nextSeq, "each poll must continue from the previous next_seq");
    assert.deepEqual(receivedEventSeqs, fixtures.opsRuns.get(first.run_id).events.map((event) => event.seq), "incremental polling must consume the complete visible event stream");
    assert.match(await history.innerText(), /完整历史第1条/);
    assert.match(await history.innerText(), /末尾唯一标记-不得截断/);
    const details = history.locator("details").first();
    assert.ok(await details.count(), "tool progress belongs in collapsible message summaries");
    if (await details.getAttribute("open") !== null) await details.locator("summary").click();
    await details.locator("summary").click();
    assert.equal(await details.getAttribute("open"), "");
    await details.locator("summary").click();
    assert.equal(await details.getAttribute("open"), null, "tool summaries must support collapsing again");
    await assertNoOverflow(page, "Agent complete history and long input desktop");

    const clarification = await submit("给同名教程配置发货，但先确认是哪个商品", "waiting_user");
    await waitText("找到两个同名商品");
    assert.equal(fixtures.opsRuns.get(clarification.run_id).status, "waiting_user");
    assert.equal(fixtures.opsWrites.length, 0, "clarification is not permission to write");
    const completed = await submit("商品 ID 100001，使用现有入门课程资源", "succeeded");
    await waitText("结果均来自服务端回执");
    assert.equal(completed.session_id, clarification.session_id, "clarification continues the same session");
    assert.equal(fixtures.opsWrites.filter((item) => item.runId === completed.run_id).length, 1);
    assert.deepEqual(evidence.confirmDialogs, [], "unambiguous Agent requests never require a second confirmation");

    const providerFailure = await submit("模拟上游密钥认证失败的真实错误", "provider_error");
    await waitText("provided API credential is invalid");
    assert.equal(fixtures.opsRuns.get(providerFailure.run_id).error.upstream_status, 401);
    assert.match(await history.innerText(), /401/);
    assert.equal(await page.locator("#workspace").isVisible(), true, "provider 401 is not site session expiry");
    assert.equal(fixtures.authLogoutRequests, 0);
    assert.match(await history.textContent(), /上游|provider|模型服务/i, "provider errors must show their origin");
    assert.match(await history.textContent(), /invalid_api_key/);
    assert.match(await history.textContent(), /authentication_error/);
    assert.match(await history.textContent(), /provider-request-fixture-401/);
    assert.equal(await panel.locator(`[data-retry-run="${providerFailure.run_id}"]`).count(), 0, "an unrecoverable provider failure is not automatically retryable");
    const transportFailure = await submit("模拟真实传输超时，而不是上游拒绝", "transport_error");
    await waitText("模拟传输超时");
    assert.match(await history.textContent(), /传输|transport/i);
    assert.equal(fixtures.opsRuns.get(transportFailure.run_id).recoverable, true);
    assert.equal(await panel.locator(`[data-retry-run="${transportFailure.run_id}"]`).isVisible(), true);
    const applicationFailure = await submit("模拟本站权限变更错误", "application_error");
    await waitText("本站权限已变更");
    assert.match(await history.textContent(), /本站|application/i);
    assert.equal(await panel.locator(`[data-retry-run="${applicationFailure.run_id}"]`).count(), 0);
    assert.equal(fixtures.opsWrites.filter((item) => [providerFailure.run_id, transportFailure.run_id, applicationFailure.run_id].includes(item.runId)).length, 0);
    // Deliberately malformed wire metadata must never authorize a retry. This
    // is fault injection, not a claim that the real API returns string booleans.
    fixtures.opsRunWireOverrides = { recoverable: "false" };
    const malformedRetry = await submit("拒绝将字符串 false 当成恢复授权", "application_error");
    await page.waitForSelector("#opsSendBtn:not(:disabled)");
    assert.equal(fixtures.opsRuns.get(malformedRetry.run_id).status, "failed");
    assert.equal(await panel.locator(`[data-retry-run="${malformedRetry.run_id}"]`).count(), 0, "retry requires recoverable === true, not a truthy string");
    fixtures.opsRunWireOverrides = null;
    const partial = await submit("配置两项，第二项模拟可恢复传输失败", "partial_failed");
    await waitText("部分完成：已成功 1 项，失败 1 项");
    assert.equal(fixtures.opsWrites.filter((item) => item.runId === partial.run_id).length, 1);
    const retryResponse = page.waitForResponse((item) => item.url().endsWith(`/api/ops/runs/${partial.run_id}/retry`) && item.status() === 202);
    await panel.getByRole("button", { name: /重试/ }).last().click();
    await retryResponse;
    await waitText("第一项未重复执行");
    assert.equal(fixtures.opsWrites.filter((item) => item.runId === partial.run_id).length, 2);
    assert.equal(fixtures.opsWrites.filter((item) => item.runId === partial.run_id && item.target === "100001").length, 1);
    const retry = fixtures.opsRequests.findLast((item) => item.path.endsWith("/retry"));
    assert.deepEqual(Object.keys(retry.payload), ["request_id"]);
    assert.equal(typeof retry.payload.request_id, "string");
    assert.ok(retry.payload.request_id.length > 0);
    if (retry.idempotencyKey) assert.equal(retry.idempotencyKey, retry.payload.request_id);
    const notRecoverable = await submit("部分成功但第二项结果不明，不能直接重试", "partial_unrecoverable");
    await waitText("部分完成但不可直接重试");
    assert.equal(fixtures.opsRuns.get(notRecoverable.run_id).status, "partial_failed");
    assert.equal(fixtures.opsRuns.get(notRecoverable.run_id).recoverable, false);
    assert.equal(await panel.locator(`[data-retry-run="${notRecoverable.run_id}"]`).count(), 0, "partial_failed alone must not authorize retry");
    const review = await submit("结果不明时只提示复核，不盲目重放", "needs_review");
    await waitText("结果需要复核");
    assert.match(await history.innerText(), /配置版本冲突|content_conflict/);
    assert.equal(await panel.locator(`[data-retry-run="${review.run_id}"]`).count(), 0);
    await submit('仅回显安全文本 <img src="/xianyu-saas/user-xss" onerror="window.__agentInjected=true">', "xss");
    await waitText("安全展示回执");
    assert.equal(await history.locator("img, script, iframe").count(), 0, "user text and server summaries must be escaped, not executed as markup");
    assert.notEqual(await page.evaluate(() => window.__agentInjected), true);

    // Stop is scoped to its original task and must retain successful receipts.
    fixtures.opsRunHold = true;
    const stopped = await submit("先保存第一项，再等待停止剩余工作", "stop");
    await waitText("已保存第一项客服知识");
    const writesBeforeStop = fixtures.opsWrites.length;
    const cancelResult = await desktopApiClick(page, "#opsStopBtn", `/api/ops/runs/${stopped.run_id}/cancel`);
    assert.equal(cancelResult.changed_count, 1, "stop acknowledgement retains the committed receipt count");
    await waitText("已停止后续步骤");
    assert.equal(fixtures.opsWrites.length, writesBeforeStop);
    assert.equal(fixtures.opsRuns.get(stopped.run_id).changedCount, 1);
    assert.deepEqual(fixtures.opsRequests.findLast((item) => item.path.endsWith("/cancel")).payload, {});
    assert.equal(fixtures.opsRequests.findLast((item) => item.path.endsWith("/cancel")).rawBody, "", "cancel must not send even an empty JSON body");
    advanceAgentRun(fixtures.opsRuns.get(stopped.run_id));
    assert.equal(fixtures.opsWrites.length, writesBeforeStop, "a later worker tick must not revive a cancelled task");
    assert.match(await history.innerText(), /仍然生效|不会回滚|已生效/);

    // Hold a real old-shop poll until the new shop has loaded. A global switch
    // changes only the view, never task ownership or persisted receipts.
    const gate = new Promise((resolve) => { releaseStalePoll = resolve; });
    let markStaleCaptured;
    let markStaleDelivered;
    const staleCaptured = new Promise((resolve) => { markStaleCaptured = resolve; });
    const staleDelivered = new Promise((resolve) => { markStaleDelivered = resolve; });
    await page.route("**/api/ops/runs/**", async (route) => {
      if (new URL(route.request().url()).origin !== new URL(baseUrl).origin) return route.fallback();
      const request = route.request();
      const candidate = fixtures.opsRuns.get(new URL(request.url()).pathname.split("/").at(-1));
      if (request.method() !== "GET" || !candidate || candidate.sessionId !== seed.session.id || candidate.mode !== "succeeded" || !candidate.hold || request.headers()["x-shop-account"] !== "default") return route.fallback();
      try {
        const response = await route.fetch();
        markStaleCaptured();
        await gate;
        await route.fulfill({ response });
      } catch (error) {
        if (!page.isClosed() && !/aborted|already handled/i.test(error.message)) evidence.pageErrors.push(error.message);
      } finally { markStaleDelivered(); }
    });
    const stale = await submit("旧店铺迟到回执不得覆盖第二店铺", "succeeded");
    await waitForMockGate(staleCaptured, "old-shop poll");
    const oldPollCount = fixtures.opsRequests.filter((item) => item.path === `/api/ops/runs/${stale.run_id}`).length;
    await switchShop("ops-second");
    await page.waitForSelector("#opsSendBtn:not(:disabled)");
    assert.doesNotMatch(await history.innerText(), /旧店铺迟到|完整历史第1条|第一项客服知识/);
    assert.equal(await page.inputValue("#opsPromptInput"), "");
    assert.equal(fixtures.opsRequests.filter((item) => item.path === "/api/ops/sessions" && item.method === "POST").length, 0, "switching to a shop without a conversation must not create one via GET");
    fixtures.opsRunHold = false;
    const second = await submit("只读分析第二店铺，保留独立会话", "read_only");
    await waitText("只读检查完成");
    assert.notEqual(second.session_id, stale.session_id);
    assert.equal(fixtures.opsRuns.get(second.run_id).accountKey, "ops-second");
    assert.equal(fixtures.opsRequests.filter((item) => item.path === `/api/ops/runs/${stale.run_id}`).length, oldPollCount, "switching shops stops old-view polling without cancelling the old task");
    const oldRun = fixtures.opsRuns.get(stale.run_id);
    oldRun.hold = false;
    advanceAgentRun(oldRun);
    if (oldRun.status === "running") advanceAgentRun(oldRun);
    releaseStalePoll();
    await waitForMockGate(staleDelivered, "released old-shop poll");
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.locator("#accountTabs .account-tab.is-active").getAttribute("data-account-switch"), "ops-second");
    assert.doesNotMatch(await history.innerText(), /旧店铺迟到|第一项客服知识/);
    await switchShop("default");
    await waitText("旧店铺迟到回执不得覆盖第二店铺");
    await waitText("已停止后续步骤");
    assert.match(await history.innerText(), /完整历史第1条/);
    assert.equal(fixtures.opsRuns.get(stale.run_id).accountKey, "default");
    assert.equal(fixtures.opsWrites.filter((item) => item.runId === stale.run_id).length, 1);

    const sessionsBeforeNew = fixtures.opsSessions.size;
    await desktopApiClick(page, "#opsNewSessionBtn", "/api/ops/sessions", "POST", 201);
    await page.waitForFunction(() => !document.querySelector("#opsChatHistory")?.textContent.includes("完整历史第1条"));
    assert.equal(fixtures.opsSessions.size, sessionsBeforeNew + 1);
    assert.ok(fixtures.opsSessions.get(seed.session.id).messages.some((item) => item.content.includes("首条约定不能丢失")), "new conversation preserves old server history");
    assert.equal(await page.evaluate(() => /完整历史第|末尾唯一标记|旧店铺迟到/.test(JSON.stringify({ ...localStorage }))), false, "business conversations must never enter localStorage");
    await assertNoBusinessStorage(page, /完整历史第|末尾唯一标记|旧店铺迟到|mock-agent-model/);

    // A delayed 202 acknowledgement belongs to the original signed-in user,
    // even when another user has already opened the same global shop key.
    const userGate = new Promise((resolve) => { releaseUserAck = resolve; });
    let markUserAckCaptured;
    let markUserAckDelivered;
    const userAckCaptured = new Promise((resolve) => { markUserAckCaptured = resolve; });
    const userAckDelivered = new Promise((resolve) => { markUserAckDelivered = resolve; });
    let delayedAck;
    await page.route("**/api/ops/chat", async (route) => {
      if (new URL(route.request().url()).origin !== new URL(baseUrl).origin) return route.fallback();
      try {
        const response = await route.fetch();
        delayedAck = await response.json();
        markUserAckCaptured();
        await userGate;
        await route.fulfill({ response });
      } catch (error) {
        if (!page.isClosed() && !/aborted|already handled/i.test(error.message)) evidence.pageErrors.push(error.message);
      } finally { markUserAckDelivered(); }
    });
    fixtures.opsRunHold = true;
    fixtures.opsChatMode = "read_only";
    await page.fill("#opsPromptInput", "原用户迟到确认，不能进入另一个用户的会话");
    await page.click("#opsSendBtn");
    await waitForMockGate(userAckCaptured, "old-user 202 acknowledgement");
    await page.click("#logoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");
    fixtures.me = { ...owner, username: "ops-other-user", id: 23 };
    await desktopLogin(page, fixtures.me.username);
    await openView(page, "ops");
    await page.waitForSelector("#opsSendBtn:not(:disabled)");
    releaseUserAck();
    await waitForMockGate(userAckDelivered, "released old-user acknowledgement");
    await waitForPanelSettled(page);
    assert.equal(await page.inputValue("#opsPromptInput"), "");
    assert.doesNotMatch(await history.innerText(), /原用户迟到确认|完整历史第|旧店铺迟到/);
    assert.equal(await page.locator("#opsSendBtn").isEnabled(), true, "old-user callbacks must not re-lock the new user's composer");
    assert.equal(fixtures.opsSessions.get(delayedAck.session_id).username, owner.username);
    assert.deepEqual(fixtures.opsRequests.filter((item) => item.username === "ops-other-user" && (item.path.includes(delayedAck.run_id) || item.path.includes(delayedAck.session_id))), [], "old-user callback identifiers must not be fetched under the next user");
    await assertNoBusinessStorage(page, /原用户迟到确认|完整历史第|末尾唯一标记|旧店铺迟到/);

    // Provider 401 is not a login failure, but a real application HTTP 401 is.
    fixtures.opsSiteSessionExpired = true;
    evidence.allowedFailures.push({ path: "/api/ops/sessions", status: 401 });
    await desktopApiClick(page, "#opsNewSessionBtn", "/api/ops/sessions", "POST", 401);
    await page.waitForSelector("#authScreen:not([hidden])");
    assert.equal(await page.locator("#workspace").isVisible(), false, "site session expiry must not be suppressed by the Agent API wrapper");
    assert.deepEqual(evidence.confirmDialogs, []);
    assert.deepEqual(fixtures.apiRequests.filter((item) => item.path.startsWith("/api/admin/")), []);
    assert.deepEqual(fixtures.opsRequests.filter((item) => item.path.includes("/plans/") || item.path.endsWith("/context")), [], "removed preview and context APIs must not be called");
    assert.deepEqual(fixtures.apiRequests.filter((item) => item.path.startsWith("/api/bot/") && ["POST", "PUT", "PATCH", "DELETE"].includes(item.method)), [], "Agent does not call manual mutation or real fulfillment endpoints");
    await assertNoOverflow(page, "Agent shop recovery desktop");
    assertDesktopEvidence(evidence);
    console.log(JSON.stringify({ ok: true, scope: "ops", desktop: 1440, screenshots: 0, cases: ["single-column-no-approval", "26-message-pagination", "long-input-verbatim", "incremental-events-no-overlap", "double-send", "read-only", "clarification", "direct-result", "provider-401-no-logout", "error-origins", "recoverable-boolean-only", "partial-retry-no-replay", "needs-review", "escaped-messages", "stop-preserves-writes", "global-shop-isolation", "restore-original-session", "new-session-keeps-history", "cross-user-late-202", "site-401-expires"], mockWrites: fixtures.opsWrites.length }));
  } catch (error) {
    await reportDesktopFailure(page, "ops", error, evidence);
    throw error;
  } finally {
    releaseStalePoll();
    releaseUserAck();
    stopAgentMockWorkers();
    fixtures.opsRunWireOverrides = null;
    fixtures.opsSiteSessionExpired = false;
    fixtures.me = owner;
    fixtures.shopAccounts = accounts;
    fixtures.accountData = accountData;
    await page.close();
  }
}

function seedDocsCaptureFixtures() {
  assert.ok(docsCaptureScope, "documentation fixtures require the explicit capture scope");
  fixtures.docsCaptureRequests = [];
  fixtures.me = { ...fixtures.me, username: "docs-owner" };
  fixtures.version = { ...fixtures.version, commit: "demo", build_dirty: false, release_notes: "演示工作台" };
  const updatedAt = "2026-09-10T10:00:00+08:00";
  fixtures.products = [
    { ...productFixtures[0], title: "数字工具使用教程", description: "手机与电脑阅读说明，包含常见问题解答。", updated_at: updatedAt },
    { ...productFixtures[1], title: "聊天表情素材包", description: "日常聊天素材，按商品配置交付。", price_display: "¥3.00", updated_at: updatedAt },
    { ...productFixtures[2], title: "店铺运营指南", description: "店铺日常管理与商品整理参考资料。", updated_at: updatedAt },
  ];
  fixtures.shopAccounts = [
    { ...fixtures.shopAccounts[0], name: "海风演示店", product_count: 3, last_verified_at: updatedAt, last_sync_at: updatedAt },
    { ...fixtures.shopAccounts[0], id: 2, key: "docs-second", name: "资料演示店", product_count: 1, last_verified_at: updatedAt, last_sync_at: updatedAt },
  ];
  fixtures.bot = { ...fixtures.bot, shop_name: "海风演示店", running: true, desired_running: true,
    running_total: 1, automation_mode: "rules_ai", product_count: 3, last_sync_at: updatedAt,
    auth_code: "ok", auth_phase: "WS_REGISTERED", catalog_state: "ready" };
  fixtures.config = { ...fixtures.config, bot_running: true };
  fixtures.automation = { ...fixtures.automation, rules: [{ ...fixtures.automation.rules[0], reply: "资料支持手机和电脑阅读，下载问题请在会话留言。" }],
    deliveries: [{ item_id: "100001", enabled: true, delivery: "material", material: "请按随商品发送的使用说明阅读资料。" }] };
  fixtures.templates = [
    { ...fixtures.templates[0], name: "表情素材卡密发货", delivery: "redeem", item_ids: ["100002"], item_count: 1 },
    { ...fixtures.templates[1], name: "运营指南网盘发货", item_ids: ["100003"], item_count: 1 },
  ];
  fixtures.deliveryStatusByAccount.default = { available: true, items: [
    { item_id: "100001", delivery: "material", configured: true, enabled: true, template_id: null },
    { item_id: "100002", delivery: "redeem", configured: true, enabled: true, template_id: "tpl-1" },
    { item_id: "100003", delivery: "pan", configured: true, enabled: true, template_id: "tpl-2" },
  ] };
  fixtures.accountData["docs-second"] = {
    bot: { ...fixtures.bot, shop_name: "资料演示店", running: false, desired_running: false, product_count: 1 },
    products: [fixtures.products[2]], automation: { ...fixtures.automation, rules: [], deliveries: [] }, conversations: [], orders: [],
  };
  fixtures.resourceRowsByUser[fixtures.me.username] = [
    resourceRow(fixtures.shopAccounts[0], { mode: "rules_ai", cpu_percent: 2.8, rss_bytes: 86 * 1024 * 1024, vms_bytes: 168 * 1024 * 1024, uptime_seconds: 9360 }),
    resourceRow(fixtures.shopAccounts[1], { worker_state: "stopped", metrics_state: "stopped", cpu_percent: 0, rss_bytes: 0, vms_bytes: 0, memory_limit_bytes: null, uptime_seconds: 0, message: "未运行" }),
  ];
  const totals = { buyer_messages_total: 12, messages_total: 20, auto_replies_total: 8, fulfillment_success_total: 3, fulfillment_failed_total: 0, unread_conversations_total: 1 };
  const buckets = [7, 10, 8, 15, 11, 16, 12].map((count, index) => ({ date: `2026-09-${String(index + 4).padStart(2, "0")}`,
    buyer_messages_total: count, auto_replies_total: [5, 7, 6, 11, 8, 12, 8][index] }));
  fixtures.analyticsByPeriod = { 1: { totals, buckets: buckets.slice(-1) }, 7: { totals, buckets } };
  fixtures.orders = ["delivered", "manual_review", "processing", "delivered", "delivered"].map((status, index) => {
    const product = fixtures.products[index % 3];
    const createdAt = `2026-09-10T${String(15 - index).padStart(2, "0")}:20:00+08:00`;
    return { order_key: `DEMO-0910-${String(index + 1).padStart(3, "0")}`, platform_order_id: "", item_id: product.id,
      item_title: product.title, buyer_id: `demo-buyer-${String(index + 1).padStart(2, "0")}`, buyer_nick: `演示买家 ${index + 1}`,
      chat_id: index === 0 ? "chat-1" : "", conversation_available: index === 0,
      quantity: 1, paid_amount: product.price_display.replace("¥", ""), status, created_at: createdAt,
      verified_at: createdAt, delivered_at: status === "delivered" ? createdAt : "",
      delivery_type_label: ["资料", "卡密", "网盘"][index % 3], platform_status_label: status === "delivered" ? "已发货" : "待发货",
      reason_code: status === "manual_review" ? "manual_confirmation" : "", reason_label: status === "manual_review" ? "规格需人工确认" : "" };
  });
  fixtures.attention = [{ id: "att_aaaaaaaaaaaaaaaaaaaaaaaa", kind: "order", code: "manual_review", title: "1 笔订单待人工确认",
    message: "买家需要确认素材规格，请查看订单后回复。", action_label: "查看订单", action_view: "orders", severity: "warning", resolved: false }];
  fixtures.summary = { messages_total: 20, orders_total: 5, delivered_total: 3, attention_total: 1, last_activity: "09-10 15:20" };
  fixtures.messages = [
    { role: "user", content: "教程支持手机阅读吗？", time: "2026-09-10 15:17", chat_id: "chat-1", item_id: "100001" },
    { role: "assistant", content: "支持手机和电脑阅读。付款后按商品配置发送使用说明。", time: "2026-09-10 15:18", chat_id: "chat-1", item_id: "100001" },
    { role: "user", content: "好的，谢谢。", time: "2026-09-10 15:19", chat_id: "chat-1", item_id: "100001" },
    { role: "user", content: "这套素材包含哪些规格？", time: "2026-09-10 15:20", chat_id: "chat-2", item_id: "100002" },
  ];
  fixtures.conversations = [
    { chat_id: "chat-2", item_id: "100002", buyer_label: "演示买家 2", preview: "这套素材包含哪些规格？", time: "2026-09-10 15:20", message_count: 1, unread: true, manual_mode: false },
    { chat_id: "chat-1", item_id: "100001", buyer_label: "演示买家 1", preview: "好的，谢谢。", time: "2026-09-10 15:19", message_count: 3, unread: false, manual_mode: false },
  ];
  const storeConfig = { ...defaultAiStoreConfig, store_content: "本店提供数字学习资料与聊天素材。先回答商品使用问题；交付方式以当前商品配置为准。",
    persona_name: "海风客服", buyer_address: "你好", emoji_level: "none",
    forbidden_claims: "不编造库存、付款或发货状态；不承诺未说明的功能。", handoff_rules: "退款、规格争议及付款状态不确定时转人工。" };
  // Only connection metadata is seeded. No API key, verification token, shop
  // cookie, redeemable code, real buyer profile or external image is supplied.
  const connection = { scope: "user", initialized: true, provider: "openai_chat_completions", base_url: "https://api.example.com/v1",
    model: "example-chat-model", api_key_configured: true, connection_status: "verified", status: "verified", revision: 1, key_revision: 1, last_error_code: "" };
  fixtures.userConnections.set(fixtures.me.username, connection);
  fixtures.ai = { ...fixtures.ai, status: { enabled: true, running: true, connection_verified: true, error_code: "" }, connection,
    config: { draft: storeConfig, published: { revision: 1, config: storeConfig }, status: "saved", revision: 1 },
    products: fixtures.products.map((item) => ({ item_id: item.id, knowledge_status: "saved", snapshot_fingerprint: `demo-${item.id}`,
      facts: { item_id: item.id, title: item.title, description: item.description, price: item.price_display, stock: "", status: "在售", skus: [] } })),
    knowledge: Object.fromEntries(fixtures.products.map((item, index) => [item.id, { item_id: item.id, status: "saved", knowledge_status: "saved", revision: 1,
      content: ["资料支持手机和电脑阅读。\n付款后按商品配置发送使用说明。\n下载与阅读问题请在会话留言。", "说明素材格式与使用方法，规格不清楚时请转人工确认。", "包含商品整理与日常管理参考，付款后按绑定模板交付。"][index] }])) };
  const pool = { id: "pool-demo-1", name: "表情素材兑换池", note: "素材包交付批次 A", total: 120, available: 85, reserved: 3, used: 32, enabled: true };
  fixtures.cards = { pool, pools: [pool, { id: "pool-demo-2", name: "备用素材兑换池", note: "素材包交付批次 B", total: 40, available: 25, reserved: 0, used: 15, enabled: true }],
    stats: { pools: 2, total: 160, available: 110, reserved: 3, used: 47 } };
  const session = createAgentSession(fixtures.me.username, "default");
  agentMessage(session, "user", "只读检查当前店铺的商品与发货配置。", { kind: "message" });
  agentMessage(session, "assistant", "资料、卡密、网盘各 1 项；卡密可用库存 110。没有修改配置。", { kind: "tool", summary: "已读取商品、发货配置与库存概要", status: "succeeded", targets: ["当前店铺的 3 个商品"] });
  agentMessage(session, "assistant", "3 个商品均已配置发货，表情素材可用库存为 110。", { kind: "message" });
  agentMessage(session, "user", "只更新数字工具使用教程的客服说明：支持手机和电脑阅读，付款后按商品配置发送使用说明。", { kind: "message" });
  agentMessage(session, "assistant", "已保存 1 项，其他商品和发货配置未变。", { kind: "tool", summary: "已保存商品客服补充内容", status: "succeeded", targets: [fixtures.products[0].title] });
  agentMessage(session, "assistant", "教程客服说明已生效。下载与阅读问题仍可在会话中咨询。", { kind: "message" });
}

async function captureDocs(browser, baseUrl) {
  seedDocsCaptureFixtures();
  const docsRoot = path.join(repoRoot, "docs", "assets", "readme");
  assert.ok(fs.statSync(docsRoot).isDirectory(), "the public image directory must already exist");
  const { page, evidence } = await desktopContractPage(browser, baseUrl, {
    viewport: { width: 1440, height: 1000 }, locale: "zh-CN", timezoneId: "Asia/Shanghai", reducedMotion: "reduce",
  });
  const captures = [];
  const desktopViewport = { width: 1440, height: 1000 };
  const mobileViewport = { width: 390, height: 844 };
  const visit = async (view, viewport = desktopViewport) => {
    await page.setViewportSize(viewport);
    await openView(page, view);
    await page.evaluate(() => window.scrollTo(0, 0));
  };
  const capture = async (name, requiredInFrame = []) => {
    await page.waitForLoadState("networkidle");
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(Array.from(document.images).filter((image) => image.getClientRects().length).map((image) => image.decode()));
    });
    await page.mouse.move(1, 1);
    await page.locator("#toastRegion .toast").last().waitFor({ state: "detached", timeout: 6000 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, `docs ${name}`);
    for (const selector of requiredInFrame) {
      const bounds = await page.locator(selector).boundingBox();
      const viewport = page.viewportSize();
      assert.ok(bounds && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1,
        `${name}: ${selector} must fit in the image: ${JSON.stringify({ bounds, viewport })}`);
    }
    assert.equal(await page.locator('dialog[open], [aria-busy="true"]:visible, .spin:visible').count(), 0, `${name}: no dialog or busy indicator`);
    assert.doesNotMatch(await page.locator('[data-panel]:not([hidden])').innerText(), /正在加载|正在读取|加载中|mock-user-verification|bootstrap-ui-contract|sk-[A-Za-z0-9]/);
    assert.equal(await page.locator('input[type="password"]:visible').evaluateAll((inputs) => inputs.every((input) => input.value === "")), true, `${name}: sensitive fields must be empty`);
    assertDesktopEvidence(evidence);
    const buffer = await page.screenshot({ animations: "disabled", caret: "hide", fullPage: false });
    const width = buffer.readUInt32BE(16), height = buffer.readUInt32BE(20);
    assert.deepEqual({ width, height }, page.viewportSize());
    captures.push({ name, width, height, bytes: buffer.length, buffer });
  };
  const readyHome = async () => page.waitForFunction(() => document.querySelectorAll("#homeProductGrid .home-product-card").length === 3
    && document.querySelectorAll("#homeOrderList tr").length === 5 && document.querySelectorAll("#analyticsChart .chart-bar").length === 7
    && document.querySelector("#homeResourceBody")?.textContent.includes("海风演示店") && document.querySelector("#homeStatCards")?.textContent.includes("12"));
  const readyChat = async () => {
    await page.waitForSelector('#conversationItems [data-chat-id="chat-1"]');
    await page.click('#conversationItems [data-chat-id="chat-1"]');
    await page.waitForFunction(() => document.querySelectorAll("#chatMessages .message-row").length === 3 && document.querySelector("#chatPinnedProductTitle")?.textContent.includes("数字工具使用教程"));
  };
  try {
    await desktopLogin(page, fixtures.me.username);
    await assertDesktopAssetVersion(page);
    await visit("shops");
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 2 && document.querySelector("#shopResourcesBody")?.textContent.includes("86"));
    await visit("home", { width: 1600, height: 1050 });
    await readyHome();
    await capture("overview.png", ["#homeStatCards", "#homeProductGrid", ".home-resources-card"]);

    await visit("shops");
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 2 && document.querySelector("#shopResourcesBody")?.textContent.includes("86"));
    await capture("shops.png", ["#shopAccountsPanelList", ".shop-resources-card"]);

    await visit("chat");
    await readyChat();
    await desktopApiClick(page, "#toggleChatTakeover", "/api/bot/conversations/chat-1/takeover");
    await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    assert.match(await page.locator("#chatAiStatus").innerText(), /AI 已开启/);
    await capture("customer-service.png", [".chat-layout"]);

    await visit("ai-config", { width: 1600, height: 1280 });
    await page.waitForSelector('#aiProductList [data-ai-product="100001"]');
    await page.click('#aiProductList [data-ai-product="100001"]');
    await page.waitForFunction(() => document.querySelector("#aiKnowledgeContent")?.value.includes("资料支持手机和电脑阅读") && document.querySelector("#aiStoreContent")?.value.includes("数字学习资料"));
    assert.match(await page.locator("#aiOverallStatus").innerText(), /AI 运行中/);
    await capture("ai-config.png", [".ai-persona-card", ".ai-knowledge-card", ".ai-test-console"]);

    await visit("goods", { width: 1440, height: 800 });
    await page.waitForFunction(() => document.querySelectorAll("#productGrid [data-product-id]").length === 3 && document.querySelector("#productGrid")?.textContent.includes("网盘"));
    assert.match(await page.locator('#productGrid [data-product-id="100002"]').innerText(), /卡密自动发货/);
    await capture("goods.png", ["#productGrid", "#productPagination"]);

    await visit("cards", { width: 1440, height: 800 });
    await page.waitForFunction(() => document.querySelectorAll("#cardsList .cards-row").length === 2 && document.querySelector("#cardsStats")?.textContent.includes("160"));
    await capture("cards.png", ["#cardsStats", "#cardsList", "#cardsCreateForm"]);

    await visit("orders", { width: 1440, height: 900 });
    await page.waitForFunction(() => document.querySelectorAll("#orderList [data-order-key]").length === 5 && !document.querySelector("#refreshOrders")?.disabled);
    await capture("orders.png", [".orders-workbench"]);

    await visit("settings", { width: 1440, height: 900 });
    await page.click('[data-settings-tab="ai"]');
    await page.waitForFunction(() => document.querySelector("#aiModel")?.value === "example-chat-model");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    await capture("settings.png", ["#settingsAiPanel"]);

    await visit("ops");
    await page.waitForFunction(() => document.querySelectorAll("#opsChatHistory .ops-receipt-card").length === 2 && !document.querySelector("#opsSendBtn")?.disabled);
    const history = await page.locator("#opsChatHistory").innerText();
    // Reload the actual page to prove the image is backed by persisted session
    // fixtures, not by injected DOM, browser history or an obsolete proposal UI.
    await page.reload({ waitUntil: "networkidle" });
    await visit("ops");
    await page.waitForFunction(() => document.querySelectorAll("#opsChatHistory .ops-receipt-card").length === 2 && !document.querySelector("#opsSendBtn")?.disabled);
    assert.equal(await page.locator("#opsChatHistory").innerText(), history);
    assert.doesNotMatch(history, /审批|提案|正在|排队/);
    await capture("operations.png", [".ops-container", ".ops-input-area"]);

    await visit("home", mobileViewport);
    await readyHome();
    await capture("overview-mobile.png", ["#homeStatCards"]);

    await visit("chat", mobileViewport);
    await readyChat();
    await waitForPanelSettled(page);
    await page.locator(".chat-window").evaluate((node) => window.scrollTo(0, window.scrollY + node.getBoundingClientRect().top - 8));
    await capture("customer-service-mobile.png", [".chat-head", "#manualReplyForm"]);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.click("#headerLogoutButton");
    await page.waitForSelector("#authScreen:not([hidden])");
    const ownerName = fixtures.me.username;
    fixtures.me = { ...fixtures.me, username: "docs-admin", role: "admin", role_label: "管理员", is_admin: true,
      platform_permissions: ["platform.settings.manage", "platform.users.manage", "platform.audit.read", "platform.updates.manage"] };
    fixtures.resourceRowsByUser[fixtures.me.username] = structuredClone(fixtures.resourceRowsByUser[ownerName]);
    fixtures.userConnections.set(fixtures.me.username, structuredClone(fixtures.userConnections.get(ownerName)));
    await desktopLogin(page, fixtures.me.username);
    await visit("settings", { width: 1440, height: 900 });
    await page.click('[data-settings-tab="resources"]');
    await page.waitForFunction(() => document.querySelector("#resourceMemoryMiB")?.value === "400" && !document.querySelector("#saveResourceSettings")?.disabled);
    assert.equal(await page.inputValue("#resourceMaxShops"), "20");
    assert.equal(await page.inputValue("#resourceMaxWorkers"), "3");
    await capture("resources.png", ["#settingsResourcesPanel"]);

    assert.equal(captures.length, 12);
    assert.equal(new Set(captures.map((item) => item.name)).size, 12);
    const origin = new URL(baseUrl).origin;
    assert.equal(fixtures.docsCaptureRequests.every((request) => new URL(request.url).origin === origin), true, "every captured HTTP request must use the loopback mock server");
    assert.equal(fixtures.apiRequests.filter((request) => request.method !== "GET").every((request) => ["/api/auth/login", "/api/auth/logout", "/api/bot/conversations/chat-1/read", "/api/bot/conversations/chat-2/read", "/api/bot/conversations/chat-1/takeover"].includes(request.path)), true, "capture must not invoke model tests, platform probes or configuration writes");
    assert.equal(fixtures.settingsRequests.some((request) => request.method !== "GET"), false);
    assert.equal(fixtures.opsRequests.some((request) => request.method !== "GET"), false);
    assert.equal(fixtures.cookieSaves + fixtures.qrStarts + fixtures.qrConnects + fixtures.shopActionRequests.length + fixtures.resourceSaveRequests.length + fixtures.opsWrites.length, 0);
    assertDesktopEvidence(evidence);
    // Publish only after every view and safety assertion has passed. Ordinary
    // full/docs/settings/ops scopes never reach this public-image write path.
    for (const { name, buffer } of captures) fs.writeFileSync(path.join(docsRoot, name), buffer);
    console.log(JSON.stringify({ ok: true, scope: "docs-capture", screenshots: captures.map(({ buffer, ...item }) => ({ ...item, path: `docs/assets/readme/${item.name}` })),
      safety: { localRequests: fixtures.docsCaptureRequests.length, apiRequests: fixtures.apiRequests.length, externalRequests: evidence.externalRequests.length,
        pageErrors: evidence.pageErrors.length, unexpectedHttpErrors: evidence.failedResponses.length, authorizationHeaders: fixtures.authorizationHeaders.length,
        modelTests: 0, platformActions: 0, configurationWrites: 0, persistedAgentReceipts: 2 } }));
  } catch (error) {
    await reportDesktopFailure(page, "docs-capture", error, evidence);
    throw error;
  } finally { await page.close(); }
}

async function run() {
  const server = createServer();
  const port = await listen(server);
  if (mockOnlyScope) {
    try { await checkSettingsOpsMock(`http://127.0.0.1:${port}/xianyu-saas`); }
    finally { await close(server); }
    return;
  }
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  const failedResponses = [];
  const externalRequests = [];
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
  let expectedManualImageDeleteFailureConsole = 0;
  let expectedManualImageDeleteFailureResponses = 0;
  let expectedManualReplyNotFoundConsole = 0;
  let expectedManualReplyNotFoundResponses = 0;
  try {
    if (docsCaptureScope) {
      await captureDocs(browser, `http://127.0.0.1:${port}/xianyu-saas/`);
      return;
    }
    if (desktopSettingsOpsScope) {
      const check = { settings: checkSettingsDesktop, ops: checkOpsDesktop, dashboard: checkDashboardDesktop, goods: checkDashboardDesktop, resources: checkResourcesDesktop, popover: checkVersionPopover, "home-alerts": checkHomeAlerts }[process.env.SAAS_UI_SCOPE];
      await check(browser, `http://127.0.0.1:${port}/xianyu-saas/`);
      return;
    }
    if (process.env.SAAS_UI_SCOPE === "orders") {
      await checkOrderManagement(browser, `http://127.0.0.1:${port}/xianyu-saas/`);
      return;
    }
    const bootstrapToken = "bootstrap-ui-contract-token-0123456789abcdef";
    fixtures.authCapabilities = { registration_enabled: false, bootstrap_available: true, password_min_length: 12 };
    const bootstrapPage = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, serviceWorkers: "block" });
    await routeOfflineMock(bootstrapPage, `http://127.0.0.1:${port}/xianyu-saas/`, externalRequests);
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

    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, serviceWorkers: "block" });
    await routeOfflineMock(page, `http://127.0.0.1:${port}/xianyu-saas/`, externalRequests);
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
      const expectedManualImageDeleteFailure = message.type() === "error"
        && message.text().includes("status of 503")
        && !expectedManualReplyFailure
        && expectedManualImageDeleteFailureConsole < fixtures.manualImageDeleteFailures;
      const expectedManualReplyNotFound = message.type() === "error"
        && message.text().includes("status of 404")
        && expectedManualReplyNotFoundConsole < fixtures.manualReplyPollNotFoundResponses;
      if (expectedCookieProbe) expectedCookieProbeConsole += 1;
      if (expectedQrFailure) expectedQrFailureConsole += 1;
      if (expectedQrStageFailure) expectedQrStageFailureConsole += 1;
      if (expectedQrStageCancel) expectedQrStageCancelConsole += 1;
      if (expectedManualReplyFailure) expectedManualReplyFailureConsole += 1;
      if (expectedManualImageDeleteFailure) expectedManualImageDeleteFailureConsole += 1;
      if (expectedManualReplyNotFound) expectedManualReplyNotFoundConsole += 1;
      if (message.type() === "error" && !expectedAnonymousProbe && !expectedCookieProbe && !expectedQrFailure && !expectedQrStageFailure && !expectedQrStageCancel && !expectedManualReplyFailure && !expectedManualImageDeleteFailure && !expectedManualReplyNotFound) errors.push(`console: ${message.text()}`);
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
      const expectedManualImageDeleteFailure = response.status() === 503 && response.url().endsWith("/api/bot/messages/image");
      const expectedManualReplyNotFound = response.status() === 404 && response.url().includes("/api/bot/messages/reply/");
      if (expectedCookieProbe) expectedCookieProbeResponses += 1;
      if (expectedQrFailure) expectedQrFailureResponses += 1;
      if (expectedQrStageFailure) expectedQrStageFailureResponses += 1;
      if (expectedQrStageCancel) expectedQrStageCancelResponses += 1;
      if (expectedManualReplyFailure) expectedManualReplyFailureResponses += 1;
      if (expectedManualImageDeleteFailure) expectedManualImageDeleteFailureResponses += 1;
      if (expectedManualReplyNotFound) expectedManualReplyNotFoundResponses += 1;
      if (response.status() >= 400 && !expectedAnonymousProbe && !expectedCookieProbe && !expectedQrFailure && !expectedQrStageFailure && !expectedQrStageCancel && !expectedManualReplyFailure && !expectedManualImageDeleteFailure && !expectedManualReplyNotFound) failedResponses.push(`${response.status()} ${response.url()}`);
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
    assert.deepEqual(await page.locator("#sideNav .side-nav-item").allTextContents(), ["店铺概览", "智能客服", "商品与发货", "订单管理", "智能运维"], "primary navigation is grouped into business domains");
    assert.equal(await page.locator('#sideNav [data-view="chat"], #sideNav [data-view="goods"], #sideNav [data-view="orders"]').count(), 3, "chat, fulfillment and orders remain primary owner tools");
    assert.equal(await page.locator('[data-panel="chat"]:not([hidden]), [data-panel="goods"]:not([hidden]), [data-panel="orders"]:not([hidden])').count(), 0, "inactive panels stay hidden while the dashboard is active");
    assert.deepEqual(await page.locator(".sidebar-bottom [data-view] .side-nav-tooltip").allTextContents(), ["店铺管理", "系统设置"], "shop management and unified settings stay in the sidebar footer");
    assert.equal(await page.locator("#logoutButton").count(), 1, "sidebar footer keeps only the logout action");
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".side-nav")).overflowY), "visible", "the left navigation must never scroll");
    assert.ok(await page.evaluate(() => {
      const nav = document.querySelector(".side-nav");
      return nav && nav.scrollHeight <= nav.clientHeight + 1;
    }), "the left navigation must fit without a scrollable overflow");
    const statLabels = await page.locator("#homeStatCards .stat-card-label").allTextContents();
    const statValues = await page.locator("#homeStatCards .stat-card-value").allTextContents();
    assert.deepEqual(statLabels, ["今日买家消息", "今日自动回复", "今日发货成功", "当前未读会话"]);
    assert.deepEqual(statValues, ["5", "3", "2", "1"], "overview uses API counts without invented reply rates or duplicate attention totals");
    assert.equal(await page.locator("#homeStatCards .stat-card-sub").count(), 0, "period context stays in metric labels without redundant decoration");
    assert.equal(await page.locator(".overview-grid-2col > .card-section").count(), 4, "overview keeps trends, attention, recent orders and product previews grouped");
    assert.equal(await page.locator("#homeOrderList, #homeResourceBody").count(), 2, "overview includes actionable orders and per-shop resource summary");
    assert.equal(await page.locator("#analyticsChart .chart-bar").count(), fixtures.analytics.buckets.length, "overview renders the analytics trend");
    assert.deepEqual(await page.locator("#homeProductGrid .home-product-name").allTextContents(), fixtures.products.slice(0, 6).map((item) => item.title), "home shows up to six featured products");
    const visibleText = await page.locator("body").innerText();
    for (const removed of ["会员服务", "选择套餐", "会员权益", "立即开通", "开通 AI 客服", "续费", "升级", "模板管理", "兑换码", "卡券管理", "账号、连接状态和店铺操作集中在一处", "已识别的商品会自动整理成列表", "商品信息会自动整理，不需要填写复杂配置"]) {
      assert.equal(visibleText.includes(removed), false, `${removed} must be removed from the self-use workspace`);
    }
    assert.equal(await page.evaluate(() => localStorage.getItem("whale_token")), null, "access token must not use localStorage");
    assert.equal(await page.locator('[data-panel="home"] .page-head-copy p').count(), 0, "page headers keep a clean title without annotation microcopy");
    assert.equal(await page.locator('[data-panel="home"] .section-title p').count(), 0, "section titles keep a clean heading without annotation microcopy");

    // The former docs page is now a help dialog within unified settings.
    await openView(page, "settings");
    await page.click("#settingsDocsBtn");
    await page.waitForSelector("#docsHelpModal[open]");
    await waitForPanelSettled(page);
    assert.equal(await page.locator(".docs-manual").count(), 1);
    assert.equal(await page.locator(".docs-step-item").count(), 4);
    assert.equal(await page.locator("details.docs-faq-item").count(), 4);
    assert.doesNotMatch(await page.locator(".docs-manual").textContent(), /绝不会|全部测试通过|除此以外不向外部/);
    for (const view of ["shops", "goods", "chat", "orders", "home"]) {
      assert.ok(await page.locator('[data-view="' + view + '"]').count(), "guide destinations remain accessible from the workspace");
      if (process.env.SAAS_UI_SCOPE === "docs") {
        await page.locator('[data-close-dialog="docsHelpModal"]').first().click();
        await openView(page, view);
        await openView(page, "settings");
        await page.click("#settingsDocsBtn");
        await page.waitForSelector("#docsHelpModal[open]");
      }
    }
    await page.locator(".docs-faq-summary").first().click();
    assert.equal(await page.locator("details.docs-faq-item").first().getAttribute("open"), "");
    assert.match(await page.locator(".docs-faq-content").first().textContent(), /关机|休眠/);
    assert.equal(await page.locator("#docsHelpModal > .docs-help-footer").evaluate((footer) => footer === footer.parentElement.lastElementChild), true, "author links must stay in the final help dialog footer");
    for (const [selector, href] of [["#docsAuthorGithub", "https://github.com/tswawa"], ["#docsProjectGithub", "https://github.com/tswawa/xianyu-saas"]]) {
      const link = page.locator(selector);
      assert.equal(await link.getAttribute("href"), href, `${selector} must point to the expected GitHub destination`);
      assert.equal(await link.getAttribute("target"), "_blank", `${selector} must open in a new tab`);
      assert.equal(await link.getAttribute("rel"), "noopener noreferrer", `${selector} must use a safe external-link policy`);
    }
    assert.equal(await page.locator(".docs-help-footer").textContent().then((value) => value.includes("作者 GitHub") && value.includes("项目仓库")), true, "manual footer must label author and project links");
    assert.equal(await page.locator('#docsHelpModal .dialog-head a[href*="github.com"]').count(), 0, "author links must not return to the top banner");
    assert.ok(await page.evaluate(() => {
      const manual = document.querySelector("#docsHelpModal .docs-manual");
      const footer = document.querySelector("#docsHelpModal .docs-help-footer");
      return Boolean(manual.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING);
    }), "the signature block must follow the manual in the dialog");
    await assertNoOverflow(page, "project manual desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "docs-manual-desktop.png"), fullPage: true });
    await page.locator('[data-close-dialog="docsHelpModal"]').first().click();
    await page.waitForSelector("#docsHelpModal:not([open])", { state: "attached" });

    // All signed-in owners can inspect version metadata and change their own password,
    // while platform account, audit and update actions remain hidden.
    assert.equal(await page.locator('[data-settings-tab="accounts"]').isVisible(), false, "owners must not see platform account administration");
    assert.equal(await page.locator('[data-settings-tab="audit"]').isVisible(), false, "owners must not see platform audit records");
    assert.equal(await page.locator('[data-settings-tab="version"], [data-settings-panel="version"], #adminUpdateControls, #checkUpdateButton').count(), 0);
    await page.click("#versionBadgeButton");
    await page.waitForSelector("#versionBadgePopover:not([hidden])");
    assert.match(await page.locator("#versionBadgeValue").innerText(), /0\.1\.0/);
    await assertUnifiedVersionLink(page);
    assert.equal(await page.locator("#versionBadgeRefresh").isVisible(), false);
    assert.notEqual(await page.evaluate(() => window.__releaseNotesInjected), true);
    await assertNoOverflow(page, "owner version desktop");
    await page.click("#versionBadgeClose");
    await page.click('[data-settings-tab="security"]');
    assert.equal(await page.locator("#securityUsernameValue").textContent(), "owner-demo");
    await page.fill("#currentPasswordInput", "password-123");
    await page.fill("#newPasswordInput", "Owner-New-Pass-123!");
    const passwordChangeResponse = page.waitForResponse((response) => response.url().endsWith("/api/auth/password") && response.request().method() === "POST");
    await page.click('#passwordChangeForm button[type="submit"]');
    assert.equal((await passwordChangeResponse).status(), 200, "password change must reach the authenticated API");
    await page.waitForFunction(() => document.querySelector("#passwordChangeMessage")?.textContent.includes("其他会话已撤销"));
    assert.deepEqual(fixtures.passwordRequests.at(-1), { current_password: "password-123", new_password: "Owner-New-Pass-123!" });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "owner version and password desktop");

    await openView(page, "home");
    await page.waitForSelector('[data-panel="home"]:not([hidden])');
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "dashboard after project manual");

    if (process.env.SAAS_UI_SCOPE !== "docs") {
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
    assert.match(await page.locator('[data-panel="templates"] .page-head-copy h1').textContent(), /商品与发货/);
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
    const savedTemplateResponse = await editTemplateResponse;
    assert.equal(savedTemplateResponse.status(), 200, await savedTemplateResponse.text());
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
    await page.waitForFunction((count) => document.querySelectorAll("#productGrid [data-product-id]").length === Math.min(count, Number(document.querySelector("#productPageSize")?.value || 12)), fixtures.products.length);

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
    await page.waitForFunction(() => document.querySelector("#productGrid :is(.product-title, .product-card-title)")?.textContent === "同布尔新 token 商品");
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
    await page.waitForFunction(() => document.querySelector("#productGrid :is(.product-title, .product-card-title)")?.textContent === "最终 token 商品");
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
    // Gate the old response explicitly: fixed delays do not guarantee reverse
    // ordering when the browser or test runner is under load.
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
    const reverseStatusGate = holdNextBotStatus();
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
    reverseStatusGate.release();
    await (await reverseOldStatusResponse).finished();
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
    const equalReverseStatusGate = holdNextBotStatus();
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
    equalReverseStatusGate.release();
    await (await equalReverseOldResponse).finished();
    await page.waitForTimeout(60);
    assert.equal(fixtures.productGetRequests.length, equalReverseProductBase + 1, "a late same-boolean status must not create another catalog token or products request");
    await page.waitForFunction(() => document.querySelector("#productGrid :is(.product-title, .product-card-title)")?.textContent === "同布尔反序新刷新商品");
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
    await page.waitForFunction((count) => document.querySelectorAll("#productGrid [data-product-id]").length === Math.min(count, Number(document.querySelector("#productPageSize")?.value || 12)), fixtures.products.length);

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
    assert.equal(await page.locator(".page-head-copy h1").evaluateAll((headings) => headings.length >= 10 && headings.every((heading) => heading.querySelectorAll(".page-head-icon").length === 1)), true, "every workspace panel title keeps exactly one icon");
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
    assert.ok(htmlSource.includes(`app.css?v=${assetVersion}`));
    assert.ok(htmlSource.includes(`app.js?v=${assetVersion}`));
    assert.ok(appSource.includes(`ASSET_VERSION = "${assetVersion}"`));
    assert.match(htmlSource, /id="manualReplyFile"[^>]*multiple/);
    assert.match(htmlSource, /最多 8 张；图片会按顺序逐张发送，文字最后单独发送。/);
    assert.match(appSource, /MANUAL_IMAGE_MAX_COUNT = 8/);
    assert.match(appSource, /method: "DELETE"/);
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

    // Discard the deliberately invalid local test input before leaving the
    // editor; the normal unsaved-configuration guard must remain enabled.
    await page.fill("#aiStoreContent", "");
    // The connection form retains its IDs but now lives in user settings.
    await openView(page, "settings");
    await page.click('[data-settings-tab="ai"]');
    await page.fill("#aiBaseUrl", "https://example.com/v1");
    await page.fill("#aiModel", "fixture-model");
    await page.fill("#aiApiKey", "fixture-ui-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    await page.waitForFunction(() => document.querySelector("#aiApiKey")?.value === "");
    assert.equal(fixtures.settingsRequests.findLast((item) => item.method === "PUT").payload.provider, "openai_chat_completions");
    assert.equal(fixtures.settingsRequests.findLast((item) => item.method === "PUT").payload.confirm, true);

    const testsBeforeProviderSwitch = fixtures.settingsRequests.filter((item) => item.path.endsWith("/test")).length;
    await page.selectOption("#aiProvider", "anthropic_messages");
    await page.click("#aiTestConnection");
    await page.waitForFunction(() => /请输入.*API Key/.test(document.querySelector("#aiConnectionMessage")?.textContent || ""));
    assert.equal(fixtures.settingsRequests.filter((item) => item.path.endsWith("/test")).length, testsBeforeProviderSwitch);
    await page.fill("#aiApiKey", "anthropic-ui-secret");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    assert.equal(fixtures.settingsRequests.findLast((item) => item.path.endsWith("/test")).payload.provider, "anthropic_messages");
    await page.selectOption("#aiProvider", "ollama_chat");
    assert.equal(await page.inputValue("#aiApiKey"), "");
    assert.equal(await page.locator("#aiApiKey").getAttribute("required"), null);
    await page.selectOption("#aiProvider", "openai_responses");
    assert.match(await page.locator("#aiBaseUrl").getAttribute("placeholder"), /api\.openai\.com\/v1/);
    await page.selectOption("#aiProvider", "google_gemini");
    assert.match(await page.locator("#aiBaseUrl").getAttribute("placeholder"), /generativelanguage\.googleapis\.com\/v1beta/);
    await page.selectOption("#aiProvider", "openai_chat_completions");
    await desktopApiClick(page, "#aiTestConnection", "/api/settings/ai/connection/test");
    await desktopApiClick(page, "#aiSaveConnection", "/api/settings/ai/connection", "PUT");
    await openView(page, "ai-config");

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
    let releaseStaleSave, releaseCurrentSave;
    fixtures.aiKnowledgeResponseGates.push(new Promise((resolve) => { releaseStaleSave = resolve; }), new Promise((resolve) => { releaseCurrentSave = resolve; }));
    await page.fill("#aiKnowledgeContent", "旧商品待保存内容，不得覆盖新商品。");
    const staleSaveResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${extractTargetId}/knowledge`) && response.request().method() === "PUT");
    // Cleanup after another assertion failure must not mask the original error.
    void staleSaveResponse.catch(() => undefined);
    await Promise.all([
      page.waitForRequest((request) => request.url().includes(`/api/bot/ai/products/${extractTargetId}/knowledge`) && request.method() === "PUT"),
      page.click("#aiSaveKnowledge"),
    ]);
    const saveTarget = page.locator("#aiProductList [data-ai-product]:not(.is-active)").first();
    const saveTargetId = await saveTarget.getAttribute("data-ai-product");
    const saveTargetTitle = await saveTarget.locator("strong").textContent();
    const selectedProductLoads = Promise.all([
      page.waitForResponse((response) => response.url().endsWith(`/api/bot/ai/products/${saveTargetId}/knowledge`) && response.request().method() === 'GET'),
      page.waitForResponse((response) => response.url().endsWith(`/api/bot/ai/products/${saveTargetId}/versions`) && response.request().method() === 'GET'),
    ]);
    void selectedProductLoads.catch(() => undefined);
    page.once("dialog", (dialog) => dialog.accept());
    await saveTarget.click();
    const loadedProductResponses = await selectedProductLoads;
    await Promise.all(loadedProductResponses.map((response) => response.finished()));
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.waitForFunction((title) => document.querySelector("#aiKnowledgeProductTitle")?.textContent === title, saveTargetTitle);
    assert.equal(await page.inputValue("#aiExtractInput"), "");
    assert.equal(await page.locator("#aiGeneratedKnowledgePreview").isHidden(), true);
    assert.equal(await page.locator("#aiKnowledgeMessage").textContent(), "");
    await page.fill("#aiKnowledgeContent", "Suitable for first-time buyers. Follow the setup guide after payment.");
    const currentSaveResponse = page.waitForResponse((response) => response.url().includes(`/api/bot/ai/products/${saveTargetId}/knowledge`) && response.request().method() === "PUT");
    void currentSaveResponse.catch(() => undefined);
    await Promise.all([
      page.waitForRequest((request) => request.url().includes(`/api/bot/ai/products/${saveTargetId}/knowledge`) && request.method() === "PUT"),
      page.click("#aiSaveKnowledge"),
    ]);
    releaseStaleSave();
    await staleSaveResponse;
    assert.equal(await page.locator("#aiSaveKnowledge").isDisabled(), true, "a stale save finally must not unlock the current product save");
    assert.equal(await page.inputValue("#aiKnowledgeContent"), "Suitable for first-time buyers. Follow the setup guide after payment.", "a stale save response must not overwrite the current editor");
    releaseCurrentSave();
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
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }

    // Manual replies accept ordered appends from the picker, clipboard and drop
    // surface without uploading until the parent submit is clicked.
    assert.equal(await page.locator("#manualReplyFile").getAttribute("multiple"), "");
    const imageUploadsBeforeQueue = fixtures.manualImageRequests.length;
    await page.locator("#manualReplyFile").setInputFiles([
      { name: "01-select.png", mimeType: "image/jpeg", buffer: Buffer.from([1, 2, 3, 4]) },
      { name: "02-select.png", mimeType: "image/png", buffer: Buffer.from([5, 6, 7, 8]) },
    ]);
    await page.waitForFunction(() => document.querySelectorAll("#manualReplyPreview .reply-image-card").length === 2);
    await dispatchManualReplyImageEvent(page, "paste", { name: "03-paste.png", mimeType: "image/png", bytes: [9, 10, 11], lastModified: 3 });
    await page.waitForFunction(() => document.querySelectorAll("#manualReplyPreview .reply-image-card").length === 3);
    const droppedFile = { name: "04-drop.png", mimeType: "image/webp", bytes: [12, 13, 14], lastModified: 4 };
    await dispatchManualReplyImageEvent(page, "dragenter", droppedFile);
    assert.equal(await page.locator("#manualReplyDropzone").evaluate((node) => node.classList.contains("is-drag-active")), true);
    await dispatchManualReplyImageEvent(page, "dragover", droppedFile);
    await dispatchManualReplyImageEvent(page, "drop", droppedFile);
    await page.waitForFunction(() => document.querySelectorAll("#manualReplyPreview .reply-image-card").length === 4);
    assert.equal(await page.locator("#manualReplyDropzone").evaluate((node) => node.classList.contains("is-drag-active")), false);
    await page.locator("#manualReplyFile").setInputFiles([
      { name: "05-extra.png", mimeType: "image/jpeg", buffer: Buffer.from([15]) },
      { name: "06-extra.png", mimeType: "image/jpeg", buffer: Buffer.from([16]) },
      { name: "07-extra.png", mimeType: "image/jpeg", buffer: Buffer.from([17]) },
      { name: "08-extra.png", mimeType: "image/jpeg", buffer: Buffer.from([18]) },
    ]);
    await page.waitForFunction(() => document.querySelectorAll("#manualReplyPreview .reply-image-card").length === 8);
    assert.deepEqual(
      await page.locator("#manualReplyPreview .reply-image-card strong").allTextContents(),
      ["01-select.png", "02-select.png", "03-paste.png", "04-drop.png", "05-extra.png", "06-extra.png", "07-extra.png", "08-extra.png"],
      "picker, paste and drop appends must keep the user's order",
    );
    await page.locator("#manualReplyFile").setInputFiles({ name: "09-rejected.png", mimeType: "image/jpeg", buffer: Buffer.from([19]) });
    await page.waitForFunction(() => document.querySelector("#replyMessage")?.textContent.includes("最多可添加 8 张"));
    assert.equal(await page.locator("#manualReplyPreview .reply-image-card").count(), 8, "the ninth image must reject the whole append");
    assert.equal(fixtures.manualImageRequests.length, imageUploadsBeforeQueue, "selecting, pasting and dropping must not upload before submit");
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "manual reply multi-image desktop");
    await captureScreenshot(page, { path: path.join(resultRoot, "manual-reply-multi-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "manual reply multi-image mobile");
    assert.equal(await page.locator("#manualReplyPreview [data-remove-manual-attachment]").evaluateAll((buttons) => buttons.every((button) => {
      const box = button.getBoundingClientRect();
      return box.width >= 44 && box.height >= 44;
    })), true, "every mobile attachment remove button must remain touchable");
    await captureScreenshot(page, { path: path.join(resultRoot, "manual-reply-multi-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    for (const name of ["02-select.png", "05-extra.png", "06-extra.png", "07-extra.png", "08-extra.png"]) {
      await page.locator("#manualReplyPreview .reply-image-card", { hasText: name }).locator("button").click();
    }
    assert.deepEqual(
      await page.locator("#manualReplyPreview .reply-image-card strong").allTextContents(),
      ["01-select.png", "03-paste.png", "04-drop.png"],
      "removing an attachment must preserve the relative order of the rest",
    );

    fixtures.manualReplyPostMode = "success";
    fixtures.manualReplyPollMode = "multipart_success";
    const multipartUploadStart = fixtures.manualImageRequests.length;
    const multipartReplyStart = fixtures.manualReplyRequests.length;
    const multipartContent = "三张图片按顺序发送，文字最后";
    await page.fill("#manualReplyInput", multipartContent);
    const multipartResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 200);
    await page.click('#manualReplyForm button[type="submit"]');
    await multipartResponse;
    assert.deepEqual(
      fixtures.manualImageRequests.slice(multipartUploadStart).map((request) => request.fileName),
      ["01-select.png", "03-paste.png", "04-drop.png"],
      "uploads must follow the visible attachment order",
    );
    assert.equal(fixtures.manualReplyRequests.length, multipartReplyStart + 1, "one click must create one parent reply request");
    const multipartRequest = fixtures.manualReplyRequests.at(-1);
    assert.equal(multipartRequest.content, multipartContent);
    assert.deepEqual(
      multipartRequest.media.map((item) => item.path),
      Array.from({ length: 3 }, (_item, index) => `manual_reply_test_${multipartUploadStart + index + 1}.jpg`),
      "the parent media array must preserve upload order",
    );
    assert.ok(multipartRequest.replyId.length >= 8, "the parent request must use one client idempotency key");
    await page.waitForSelector('#chatMessages .message-delivery[data-parent-status="queued"]');
    assert.deepEqual(
      await page.locator('#chatMessages .message-delivery[data-parent-status="queued"] .message-part-status').evaluateAll((parts) => parts.map((part) => [part.dataset.partKind, part.dataset.partStatus])),
      [["image", "queued"], ["image", "waiting"], ["image", "waiting"], ["text", "waiting"]],
      "the queued parent must expose every ordered segment",
    );
    assert.equal((await page.locator("body").innerText()).includes("manual_reply_test_"), false, "private upload paths must never be rendered");
    await page.waitForFunction(() => {
      const parent = document.querySelector('#chatMessages .message-delivery[data-parent-status="sending"]');
      return parent && parent.querySelector('[data-part-status="acknowledged"]') && parent.querySelector('[data-part-status="sending"]');
    });
    await page.waitForFunction(() => {
      const parent = document.querySelector('#chatMessages .message-delivery[data-parent-status="retry"]');
      return parent && parent.querySelector('[data-part-status="acknowledged"]') && parent.querySelector('[data-part-status="retry"]');
    });
    await page.waitForFunction((expectedContent) => {
      const rows = Array.from(document.querySelectorAll("#chatMessages .message-row")).filter((row) => row.querySelector(".message-role")?.textContent === "人工回复");
      const latest = rows.slice(-4);
      return latest.length === 4
        && latest.slice(0, 3).every((row) => row.querySelector(".message-media-image img"))
        && latest[3].querySelector(".message-text")?.textContent === expectedContent
        && !document.querySelector('#chatMessages .message-delivery[data-parent-status="acknowledged"]');
    }, multipartContent, { timeout: 8000 });
    const multipartRendered = await page.evaluate((expectedContent) => {
      const rows = Array.from(document.querySelectorAll("#chatMessages .message-row")).filter((row) => row.querySelector(".message-role")?.textContent === "人工回复").slice(-4);
      return rows.map((row) => ({
        content: row.querySelector(".message-text")?.textContent || "",
        image: row.querySelector(".message-media-image img")?.getAttribute("src") || "",
        status: row.querySelector(".message-status")?.textContent || "",
      })).concat([{ expectedContent }]);
    }, multipartContent);
    assert.deepEqual(multipartRendered.slice(0, 3).map((item) => item.image), [
      "https://cdn.example/manual-1-1.png",
      "https://cdn.example/manual-1-2.png",
      "https://cdn.example/manual-1-3.png",
    ]);
    assert.equal(multipartRendered[3].content, multipartContent);
    assert.equal(multipartRendered.slice(0, 4).every((item) => item.status === "闲鱼已接收"), true);

    // A failed parent submit keeps uploaded media for retry, reuses the same
    // idempotency key and does not upload the image again.
    await page.locator("#manualReplyFile").setInputFiles({ name: "retry-one.jpg", mimeType: "image/jpeg", buffer: Buffer.from([20, 21]) });
    await page.fill("#manualReplyInput", "单图幂等重试");
    fixtures.manualReplyPostMode = "failure";
    fixtures.manualReplyPollMode = "dead_letter";
    const retryUploadStart = fixtures.manualImageRequests.length;
    const retryReplyStart = fixtures.manualReplyRequests.length;
    const failedParentResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 503);
    await page.click('#manualReplyForm button[type="submit"]');
    await failedParentResponse;
    await page.waitForFunction(() => document.querySelector("#replyMessage")?.textContent.includes("暂时无法提交"));
    assert.equal(fixtures.manualImageRequests.length, retryUploadStart + 1);
    assert.match(await page.locator("#manualReplyPreview").innerText(), /已上传，等待提交/);
    fixtures.manualReplyPostMode = "success";
    const retriedParentResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 200);
    await page.click('#manualReplyForm button[type="submit"]');
    await retriedParentResponse;
    assert.equal(fixtures.manualImageRequests.length, retryUploadStart + 1, "retrying the same parent must not upload its image twice");
    const retriedRequests = fixtures.manualReplyRequests.slice(retryReplyStart);
    assert.equal(retriedRequests.length, 2);
    assert.equal(retriedRequests[0].replyId, retriedRequests[1].replyId, "a failed parent retry must reuse its client request id");
    assert.deepEqual(retriedRequests[0].media.map((item) => item.path), retriedRequests[1].media.map((item) => item.path));
    await page.waitForFunction(() => {
      const parent = document.querySelector('#chatMessages .message-delivery[data-parent-status="dead_letter"]');
      return parent && parent.querySelector('[data-part-status="dead_letter"]') && parent.querySelector('[data-part-status="waiting"]');
    }, null, { timeout: 5000 });
    assert.match(await page.locator('#chatMessages .message-delivery[data-parent-status="dead_letter"]').innerText(), /父任务|需处理/);

    // Removing an uploaded-but-not-queued image calls the scoped deletion API
    // as best-effort cleanup and removes it from the preview immediately.
    await page.locator("#manualReplyFile").setInputFiles({ name: "remove-uploaded.png", mimeType: "image/png", buffer: Buffer.from([22, 23]) });
    await page.fill("#manualReplyInput", "上传后取消的图片");
    fixtures.manualReplyPostMode = "failure";
    const cleanupFailure = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 503);
    await page.click('#manualReplyForm button[type="submit"]');
    await cleanupFailure;
    await page.waitForFunction(() => document.querySelector("#replyMessage")?.textContent.includes("暂时无法提交"));
    const cleanupPath = fixtures.manualReplyRequests.at(-1).media[0].path;
    const cleanupDelete = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE");
    await page.click("#manualReplyPreview [data-remove-manual-attachment]");
    await cleanupDelete;
    assert.equal(await page.locator("#manualReplyPreview .reply-image-card").count(), 0);
    assert.deepEqual(fixtures.manualImageDeletes.at(-1), { accountKey: "default", path: cleanupPath });

    // Pure text and the legacy single-image shape continue to use the same
    // parent/parts flow without introducing extra media or text segments.
    fixtures.manualReplyPostMode = "success";
    fixtures.manualReplyPollMode = "success";
    await page.fill("#manualReplyInput", "纯文字兼容回复");
    const pureTextUploadCount = fixtures.manualImageRequests.length;
    const pureTextResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 200);
    await page.click('#manualReplyForm button[type="submit"]');
    await pureTextResponse;
    assert.equal(fixtures.manualImageRequests.length, pureTextUploadCount);
    assert.deepEqual(fixtures.manualReplyRequests.at(-1).media, []);
    await page.waitForFunction(() => Array.from(document.querySelectorAll("#chatMessages .message-row")).some((row) => row.querySelector(".message-text")?.textContent === "纯文字兼容回复" && row.querySelector(".message-status")?.textContent === "闲鱼已接收"), null, { timeout: 5000 });

    await page.locator("#manualReplyFile").setInputFiles({ name: "legacy-single.gif", mimeType: "image/gif", buffer: Buffer.from([24, 25]) });
    assert.equal(await page.inputValue("#manualReplyInput"), "");
    const singleImageResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 200);
    await page.click('#manualReplyForm button[type="submit"]');
    await singleImageResponse;
    assert.equal(fixtures.manualReplyRequests.at(-1).content, "");
    assert.equal(fixtures.manualReplyRequests.at(-1).media.length, 1);
    await page.waitForFunction(() => {
      const rows = Array.from(document.querySelectorAll("#chatMessages .message-row"));
      return rows.some((row) => row.querySelector('.message-media-image img[src*="manual-4-1.png"]') && row.querySelector(".message-status")?.textContent === "闲鱼已接收");
    }, null, { timeout: 5000 });

    // A pending reply operation owns its chat and account context. Conversation
    // and account switches are blocked until it settles, so no later operation
    // can overwrite the one destructive cleanup must await.
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#chatMessages .message-row").length >= 2);
    fixtures.manualReplyPostMode = "success";
    fixtures.manualReplyPollMode = "success";
    fixtures.manualReplyPostDelayMs = 1500;
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
    await page.fill("#manualReplyInput", "切店铺时仍在发送");
    const accountSwitchReplyRequest = page.waitForRequest((request) => request.url().endsWith("/api/bot/messages/reply") && request.method() === "POST");
    const accountSwitchReplyResponse = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 200);
    const originalChatId = await page.locator("#conversationItems .conversation-item.is-active").getAttribute("data-chat-id");
    const blockedChatId = await page.locator(`#conversationItems [data-chat-id]:not([data-chat-id="${originalChatId}"])`).first().getAttribute("data-chat-id");
    await page.click('#manualReplyForm button[type="submit"]');
    await accountSwitchReplyRequest;
    await page.click(`#conversationItems [data-chat-id="${blockedChatId}"]`);
    assert.equal(await page.locator("#conversationItems .conversation-item.is-active").getAttribute("data-chat-id"), originalChatId, "pending reply must block conversation switching");
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
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card.is-current").getAttribute("data-account-key"), currentAccountKey, "pending reply must block account switching");
    await accountSwitchReplyResponse;
    await page.waitForTimeout(20);
    await page.click(`#shopAccountsPanelList [data-account-switch="${nextAccountKey}"]`);
    await page.waitForFunction((name) => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === name, nextAccountName);
    assert.equal(await page.inputValue("#manualReplyInput"), "", "switching shops after completion must clear the previous reply body");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "switching shops after completion must clear the previous reply status");

    // Store-scoped loaders must follow the selected account, not just the
    // header label. The fixture deliberately gives the second shop a smaller
    // catalog so a stale response would be visible here.
    await page.click('#sideNav [data-view="goods"]');
    await page.waitForFunction((expected) => document.querySelectorAll("#productGrid [data-product-id]").length === Math.min(expected, Number(document.querySelector("#productPageSize")?.value || 12)), nextAccountKey === "default" ? fixtures.products.length : 1);
    if (nextAccountKey === "default") {
      await openView(page, "shops");
      await page.waitForSelector('[data-panel="shops"]:not([hidden])');
      await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
      await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "备用店（运营）");
      await page.click('#sideNav [data-view="goods"]');
      await page.waitForFunction(() => document.querySelectorAll("#productGrid [data-product-id]").length === 1);
      assert.equal(await page.locator("#productGrid :is(.product-title, .product-card-title)").textContent(), fixtures.products[1].title);
      await openView(page, "shops");
      await page.waitForSelector('[data-panel="shops"]:not([hidden])');
      await page.click('#shopAccountsPanelList [data-account-switch="default"]');
      await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active .account-tab-name")?.textContent === "海风数字店");
    }

    // Deleting the current non-default shop must clean uploaded-but-not-queued
    // media with the old shop scope before the account is disabled.
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    if (await page.locator('#shopAccountsPanelList .shop-card.is-current').getAttribute("data-account-key") !== "shop-ui-2") {
      await page.click('#shopAccountsPanelList [data-account-switch="shop-ui-2"]');
      await page.waitForFunction(() => document.querySelector("#accountTabs .account-tab.is-active")?.dataset.accountSwitch === "shop-ui-2");
    }
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    await page.locator("#conversationItems [data-chat-id]").first().click();
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
    fixtures.manualReplyPostMode = "failure";
    fixtures.manualReplyPostDelayMs = 0;
    await page.locator("#manualReplyFile").setInputFiles([
      { name: "delete-shop-first.png", mimeType: "image/png", buffer: Buffer.from([26, 27]) },
      { name: "delete-shop-second.png", mimeType: "image/png", buffer: Buffer.from([28, 29]) },
    ]);
    await page.fill("#manualReplyInput", "删除店铺前清理未入队图片");
    const failedDeleteShopParent = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 503);
    await page.click('#manualReplyForm button[type="submit"]');
    await failedDeleteShopParent;
    await page.waitForFunction(() => document.querySelector("#replyMessage")?.textContent.includes("暂时无法提交"));
    const deleteShopCleanupPaths = fixtures.manualReplyRequests.at(-1).media.map((item) => item.path);
    const imageDeleteStart = fixtures.manualImageDeletes.length;

    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    fixtures.manualImageDeleteMode = "success";
    fixtures.manualImageDeleteModes = ["success", "failure"];
    await page.click('[data-account-delete="shop-ui-2"]');
    await page.waitForSelector("#confirmDialog[open]");
    assert.equal(await page.locator("#confirmTitle").textContent(), "删除店铺");
    const failedScopedCleanup = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE" && response.request().headers()["x-shop-account"] === "shop-ui-2" && response.status() === 503);
    await page.click("#confirmAction");
    assert.equal((await failedScopedCleanup).status(), 503);
    await page.waitForTimeout(50);
    assert.deepEqual(fixtures.shopAccountDeleteRequests, [], "failed media cleanup must block account deletion");
    assert.equal(await page.locator("#shopAccountsPanelList .shop-card").count(), 2);
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForSelector('[data-panel="chat"]:not([hidden])');
    assert.equal(await page.locator("#manualReplyPreview .reply-image-card").count(), 2, "failed cleanup must retain both source files for retry");
    assert.deepEqual(
      await page.locator("#manualReplyPreview .reply-image-card").evaluateAll((cards) => cards.map((card) => ({
        name: card.querySelector("strong")?.textContent || "",
        status: card.querySelector("small")?.textContent || "",
      }))),
      [
        { name: "delete-shop-first.png", status: "待上传 · 2 B" },
        { name: "delete-shop-second.png", status: "已上传，等待提交 · 2 B" },
      ],
      "successfully deleted media must be cleared while failed media remains reusable",
    );

    fixtures.manualImageDeleteMode = "success";
    const sequenceStart = fixtures.requestSequence.length;
    await openView(page, "shops");
    await page.waitForSelector('[data-panel="shops"]:not([hidden])');
    await page.click('[data-account-delete="shop-ui-2"]');
    await page.waitForSelector("#confirmDialog[open]");
    const scopedImageCleanup = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE" && response.request().headers()["x-shop-account"] === "shop-ui-2");
    const scopedAccountDelete = page.waitForResponse((response) => response.url().endsWith("/api/bot/accounts/shop-ui-2") && response.request().method() === "DELETE");
    await page.click("#confirmAction");
    assert.equal((await scopedImageCleanup).status(), 200, "old-shop media cleanup must finish before disabling the account");
    assert.equal((await scopedAccountDelete).status(), 200);
    await page.waitForFunction(() => document.querySelectorAll("#shopAccountsPanelList .shop-card").length === 1);
    assert.deepEqual(fixtures.requestSequence.slice(sequenceStart, sequenceStart + 2), ["image-delete:shop-ui-2", "account-delete:shop-ui-2"]);
    assert.deepEqual(fixtures.manualImageDeletes.slice(imageDeleteStart), [
      { accountKey: "shop-ui-2", path: deleteShopCleanupPaths[0] },
      { accountKey: "shop-ui-2", path: deleteShopCleanupPaths[1] },
    ]);
    assert.deepEqual(fixtures.shopAccountDeleteRequests, ["shop-ui-2"], "delete must call the scoped DELETE endpoint once");
    assert.equal(await page.locator('[data-account-delete="default"]').isDisabled(), true, "default shop deletion must remain protected after cleanup");
    fixtures.manualReplyPostMode = "success";

    // Logout must use the still-authenticated old account scope for uploaded
    // media. A cleanup failure blocks logout and retains the attachment.
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    await page.click('[data-chat-id="chat-1"]');
    await page.waitForFunction(() => document.querySelectorAll("#chatMessages .message-row").length >= 2 && document.querySelector("#chatMessages")?.textContent.includes("你好，这个商品怎么使用"));
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
    fixtures.manualReplyPostMode = "failure";
    await page.locator("#manualReplyFile").setInputFiles({ name: "logout-unqueued.png", mimeType: "image/png", buffer: Buffer.from([28, 29]) });
    await page.fill("#manualReplyInput", "退出前清理未入队图片");
    const failedLogoutParent = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/reply") && response.request().method() === "POST" && response.status() === 503);
    await page.click('#manualReplyForm button[type="submit"]');
    await failedLogoutParent;
    await page.waitForFunction(() => document.querySelector("#replyMessage")?.textContent.includes("暂时无法提交"));
    const logoutCleanupPath = fixtures.manualReplyRequests.at(-1).media[0].path;
    const logoutCountBeforeFailure = fixtures.authLogoutRequests;
    fixtures.manualImageDeleteMode = "failure";
    const failedLogoutCleanup = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE" && response.request().headers()["x-shop-account"] === "default");
    await page.click("#logoutButton");
    assert.equal((await failedLogoutCleanup).status(), 503);
    await page.waitForTimeout(50);
    assert.equal(fixtures.authLogoutRequests, logoutCountBeforeFailure, "failed image cleanup must block session logout");
    assert.equal(await page.locator("#workspace").isVisible(), true);
    assert.equal(await page.locator("#manualReplyPreview .reply-image-card").count(), 1, "failed logout cleanup must retain the attachment");

    fixtures.manualImageDeleteMode = "success";
    const logoutSequenceStart = fixtures.requestSequence.length;
    const successfulLogoutCleanup = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE" && response.request().headers()["x-shop-account"] === "default" && response.status() === 200);
    const firstLogoutResponse = page.waitForResponse((response) => response.url().endsWith("/api/auth/logout") && response.request().method() === "POST");
    await page.click("#logoutButton");
    await successfulLogoutCleanup;
    assert.equal((await firstLogoutResponse).status(), 200);
    await page.waitForSelector("#authScreen:not([hidden])");
    assert.deepEqual(fixtures.requestSequence.slice(logoutSequenceStart, logoutSequenceStart + 2), ["image-delete:default", "auth-logout"]);
    assert.deepEqual(fixtures.manualImageDeletes.at(-1), { accountKey: "default", path: logoutCleanupPath });
    assert.equal(fixtures.authLogoutRequests, logoutCountBeforeFailure + 1);
    assert.equal(await page.inputValue("#manualReplyInput"), "", "logout must clear the reply body");
    assert.equal(await page.locator("#replyMessage").textContent(), "", "logout must clear reply status");

    // Preserve the existing late-response fence after signing in again: a text
    // parent response arriving after logout may not restore the old form state.
    fixtures.manualReplyPostMode = "success";
    await page.fill("#authUsername", "owner-demo");
    await page.fill("#authPassword", "password-123");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    await page.click('[data-chat-id="chat-1"]');
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
    fixtures.manualReplyPostMode = "success";
    fixtures.manualImageUploadDelayMs = 300;
    await page.locator("#manualReplyFile").setInputFiles({ name: "logout-during-upload.png", mimeType: "image/png", buffer: Buffer.from([30, 31]) });
    await page.fill("#manualReplyInput", "上传中退出不应创建父任务");
    const delayedUploadParentCount = fixtures.manualReplyRequests.length;
    const delayedUploadLogoutCount = fixtures.authLogoutRequests;
    const delayedUploadSequenceStart = fixtures.requestSequence.length;
    const delayedUploadRequest = page.waitForRequest((request) => request.url().includes("/api/bot/messages/image?chat_id=") && request.method() === "POST");
    const delayedUploadResponse = page.waitForResponse((response) => response.url().includes("/api/bot/messages/image?chat_id=") && response.request().method() === "POST" && response.status() === 200);
    const delayedUploadCleanup = page.waitForResponse((response) => response.url().endsWith("/api/bot/messages/image") && response.request().method() === "DELETE" && response.request().headers()["x-shop-account"] === "default" && response.status() === 200);
    const delayedUploadLogout = page.waitForResponse((response) => response.url().endsWith("/api/auth/logout") && response.request().method() === "POST");
    await page.click('#manualReplyForm button[type="submit"]');
    await delayedUploadRequest;
    await page.click("#logoutButton");
    const delayedUploadMedia = (await (await delayedUploadResponse).json()).media;
    await delayedUploadCleanup;
    assert.equal((await delayedUploadLogout).status(), 200);
    await page.waitForSelector("#authScreen:not([hidden])");
    assert.equal(fixtures.manualReplyRequests.length, delayedUploadParentCount, "logout during upload must cancel before parent enqueue");
    assert.equal(fixtures.authLogoutRequests, delayedUploadLogoutCount + 1);
    assert.deepEqual(fixtures.requestSequence.slice(delayedUploadSequenceStart, delayedUploadSequenceStart + 2), ["image-delete:default", "auth-logout"]);
    assert.deepEqual(fixtures.manualImageDeletes.at(-1), { accountKey: "default", path: delayedUploadMedia.path });

    await page.fill("#authUsername", "owner-demo");
    await page.fill("#authPassword", "password-123");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await page.click('#sideNav [data-view="chat"]');
    await page.waitForFunction(() => document.querySelectorAll("#conversationItems [data-chat-id]").length >= 1);
    await page.click('[data-chat-id="chat-1"]');
    if (await page.locator('#manualReplyForm button[type="submit"]').isDisabled()) {
      const takeoverResponse = page.waitForResponse((response) => response.url().includes("/api/bot/conversations/") && response.url().endsWith("/takeover") && response.request().method() === "POST");
      await page.click("#toggleChatTakeover");
      await takeoverResponse;
      await page.waitForFunction(() => document.querySelector('#manualReplyForm button[type="submit"]')?.disabled === false);
    }
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

    } else {
      await page.click("#logoutButton");
      await page.waitForSelector("#authScreen:not([hidden])");
    }

    // Platform administrators receive only account metadata, security events and
    // one release-check action in the compact version popover.
    fixtures.me = {
      username: "admin-demo", expires_at: 0, active: false, plan: "free", plan_label: "免费",
      role: "admin", role_label: "管理员", is_admin: true, permissions: selfUsePermissions,
      platform_permissions: ["platform.audit.read", "platform.settings.manage", "platform.updates.manage", "platform.users.manage"],
    };
    await page.fill("#authUsername", "admin-demo");
    await page.fill("#authPassword", "Admin-Pass-123!");
    await page.click("#authSubmit");
    await page.waitForSelector("#workspace:not([hidden])");
    await openView(page, "settings");
    await page.waitForFunction(() => Array.from(document.querySelectorAll("[data-settings-tab]")).filter((node) => !node.hidden).length === 5);
    assert.deepEqual(
      await page.locator('[data-settings-tab]:visible').allTextContents(),
      ["模型连接", "运行与资源", "账号安全", "账号与权限", "安全记录"],
      "administration and model connection must remain inside unified settings",
    );

    await page.click('[data-settings-tab="accounts"]');
    await page.waitForSelector('[data-settings-panel="accounts"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#adminUsersBody [data-admin-user-id]").length === 2);
    assert.match(await page.locator('[data-admin-user-id="2"]').textContent(), /owner-demo/);
    assert.doesNotMatch(await page.locator('[data-admin-user-id="2"]').textContent(), /Cookie|订单正文|库存正文/);
    await page.check("#registrationOpenToggle");
    assert.equal(await page.locator("#updateChannelSelect").count(), 0, "release source selection has been removed");
    await page.click('#platformSettingsForm button[type="submit"]');
    await page.waitForFunction(() => document.querySelector("#platformSettingsMessage")?.textContent.includes("已保存"));
    assert.deepEqual(fixtures.adminSettingRequests.at(-1), { registration_open: true });
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

    await page.click('[data-settings-tab="audit"]');
    await page.waitForSelector('[data-settings-panel="audit"]:not([hidden])');
    await page.waitForFunction(() => document.querySelectorAll("#auditEventList .audit-event").length === 1);
    assert.match(await page.locator("#auditEventList").textContent(), /登录成功/);
    assert.doesNotMatch(await page.locator("#auditEventList").textContent(), /Admin-Pass-123|bootstrap-ui-contract-token/);

    assert.equal(await page.locator('#adminUpdateControls, #downloadUpdateButton, #applyUpdateButton, #rollbackUpdateButton, [data-settings-panel="version"]').count(), 0, "installation and version-detail forms must be removed");
    await page.click("#versionBadgeButton");
    await page.waitForSelector("#versionBadgePopover:not([hidden])");
    await assertUnifiedVersionLink(page);
    for (const [status, expected] of [["no_release", /尚无|暂无|无发布/], ["current", /未发现更高|当前版本|无需更新/], ["error", /无法|失败|错误/], ["available", /0\.2\.0/]]) {
      fixtures.releaseCheckStatus = status;
      await desktopApiClick(page, "#versionBadgeRefresh", "/api/admin/updates/check");
      await page.waitForFunction(() => !document.querySelector("#versionBadgeRefresh")?.disabled);
      assert.match(await page.locator("#versionBadgeStatus").innerText(), expected);
      assert.equal(await page.locator("#versionBadgeButton").evaluate((node) => node.classList.contains("has-update")), status === "available");
    }
    assert.deepEqual(fixtures.updateRequests.filter((item) => item.action !== "check"), []);
    await page.click("#versionBadgeClose");

    await page.click('[data-settings-tab="accounts"]');
    await page.waitForSelector('[data-settings-panel="accounts"]:not([hidden])');
    await waitForPanelSettled(page);
    await assertNoOverflow(page, "admin account controls desktop");
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
    assert.equal(expectedManualReplyFailureConsole, fixtures.manualReplyPostFailures, "manual reply POST failures must be observed in the browser console");
    assert.equal(expectedManualImageDeleteFailureResponses, fixtures.manualImageDeleteFailures, "manual image cleanup failures must be observed exactly once");
    assert.equal(expectedManualImageDeleteFailureConsole, fixtures.manualImageDeleteFailures, "manual image cleanup failures must be observed in the browser console");
    assert.equal(expectedManualReplyNotFoundResponses, fixtures.manualReplyPollNotFoundResponses, "manual reply status 404s must be observed exactly once");
    assert.equal(expectedManualReplyNotFoundConsole, fixtures.manualReplyPollNotFoundResponses, "manual reply status 404s must be the only expected 404 console entries");
    assert.deepEqual(errors, [], "browser should have no page or console errors");
    assert.deepEqual(failedResponses, [], "browser should have no failed responses");
    assert.deepEqual(externalRequests, [], "full regression must not contact external services");
    console.log(JSON.stringify({
      ok: true,
      scope: process.env.SAAS_UI_SCOPE === "docs" ? "docs" : "full",
      selfUseNav: 7,
      productCards: fixtures.products.length,
      templates: fixtures.templates.length,
      cards: fixtures.cardRequests.length,
      screenshots: screenshotsEnabled ? resultRoot : 0,
    }));
  } catch (error) {
    if (process.env.SAAS_UI_SCOPE === "docs") {
      const activePage = browser.contexts().flatMap((context) => context.pages()).at(-1);
      const state = activePage ? await activePage.evaluate(() => ({
        view: document.querySelector('[data-panel]:not([hidden])')?.dataset.panel,
        updateMessage: document.querySelector("#updateActionMessage")?.textContent,
        updateStatus: document.querySelector("#updateInstallStatusValue")?.textContent,
        applyDisabled: document.querySelector("#applyUpdateButton")?.disabled,
      })) : null;
      console.error(JSON.stringify({ scope: "docs", state, updateActions: fixtures.updateRequests.map((item) => item.action), adminConfirmCount: fixtures.adminConfirmRequests.length }));
    }
    throw error;
  } finally {
    for (const gate of fixtures.pendingBotStatusGates) gate.release();
    await browser.close();
    await close(server);
  }
}

run().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});

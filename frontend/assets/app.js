/* global fetch, URL */
(() => {
  "use strict";

  const API_PREFIX = "/xianyu-saas";
  const QR_LOGIN_POLL_MS = 1500;
  const ASSET_VERSION = "20260913-02";
  const AI_TEXT_PLACEHOLDERS = new Set(["无", "暂无", "没有", "未填写", "待填写", "待补充", "占位", "n/a", "na", "none", "null", "todo", "tbd"]);
  const ICONS = API_PREFIX + "/assets/icons.svg?v=" + ASSET_VERSION + "#";
  // 旧版视图 key → 新版视图 key（历史会话/书签兜底）。
  const VIEW_ALIASES = {
    overview: "home",
    "shop-accounts": "shops",
    products: "goods",
    automation: "auto-reply",
    analytics: "home",
    membership: "home",
    vip: "home",
    chats: "chat",
    ai: "ai-config",
    docs: "settings",
  };
  const ACTIVE_ACCOUNT_STORAGE_PREFIX = "xianyu-saas.active-account:";
  const INBOX_STORAGE_PREFIX = "xianyu-saas.inbox:";
  const MANUAL_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
  const MANUAL_IMAGE_MAX_COUNT = 8;
  const MANUAL_REPLY_UPLOAD_TIMEOUT_MS = 30_000;
  const MANUAL_REPLY_POST_TIMEOUT_MS = 30_000;
  const MANUAL_REPLY_DELETE_TIMEOUT_MS = 10_000;
  const MANUAL_REPLY_OPERATION_WAIT_TIMEOUT_MS = 35_000;
  const MANUAL_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
  const state = {
    authMode: "login",
    registrationAllowed: false,
    firstRegistrationAvailable: false,
    bootstrapAvailable: false,
    passwordMinLength: 12,
    view: "home",
    me: null,
    accounts: [],
    activeAccountKey: "",
    shopAccountsPage: 1,
    shopAccountsPageSize: 5,
    accountEpoch: 0,
    refreshGeneration: 0,
    refreshOwner: null,
    config: null,
    bot: null,
    version: null,
    versionUpdate: null,
    platformUpdate: null,
    versionLoadedPublic: false,
    settingsAi: {
      connection: null,
      verificationToken: "",
      testedFingerprint: "",
      draft: null,
    },
    ops: newOpsState(),
    automation: { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true },
    automationEditor: { type: "", index: -1 },
    ai: {
      status: null,
      connection: null,
      config: null,
      templates: [],
      products: [],
      selectedItemId: "",
      knowledge: null,
      versions: [],
      productSearch: "",
      verificationToken: "",
      testedFingerprint: "",
      loadGeneration: 0,
      productGeneration: 0,
      knowledgeGeneration: 0,
      knowledgeRequestGeneration: 0,
      extractionGeneration: 0,
      previewGeneration: 0,
      previewBusy: false,
      generatedKnowledge: null,
      previewHistory: [],
      dirty: { connection: false, config: false, knowledge: false },
      baseline: { connection: "", config: "", knowledge: "" },
    },
    attention: [],
    summary: null,
    analytics: null,
    todayAnalytics: null,
    trendAnalytics: null,
    trendPeriod: 7,
    analyticsPeriod: 7,
    analyticsPage: null,
    resources: null,
    resourcesError: null,
    resourcesLoading: false,
    resourcesInflightPromise: null,
    resourcesPendingReload: false,
    resourcesPollTimer: null,
    resourcesGeneration: 0,
    resourceSettings: null,
    resourceRevision: 0,
    resourceDraft: null,
    resourceDraftDirty: false,
    products: [],
    productsAccountKey: "",
    productsLoad: null,
    productsTruncated: null,
    productsTruncatedAccountKey: "",
    goodsViewMode: "cards",
    goodsSearch: "",
    goodsStatusFilter: "all",
    goodsPage: 1,
    goodsPageSize: 12,
    deliveryStatus: { available: false, items: new Map(), loaded: false, error: null },
    deliveryStatusAccountKey: "",
    deliveryStatusEpoch: 0,
    catalogStatusGeneration: 0,
    catalogStatus: null,
    productsRequestGeneration: 0,
    batchDelivery: { enabled: true, previewToken: "", preview: null, generation: 0 },
    automationMutations: { rules: false, deliveries: false, settings: false, runtime: false },
    automationMutationGeneration: 0,
    automationMutationOwner: null,
    automationLoadGeneration: 0,
    conversations: [],
    messages: [],
    messageSearch: "",
    messageMatchCount: 0,
    messageSearchTimer: 0,
    inboxSearchTimer: 0,
    quickReplies: [],
    quickRepliesGeneration: 0,
    selectedChatId: "",
    messageLoadGeneration: 0,
    messageSelectionInFlight: false,
    manualReply: {
      request: null,
      attachments: [],
      dragging: false,
      submitting: false,
      uploading: false,
      uploadingIndex: -1,
      cleaning: false,
      operation: null,
      destructiveOperation: null,
      polling: new Set(),
      generation: 0,
    },
    conversationCommands: {
      generation: 0,
      read: new Map(),
      takeover: new Map(),
    },
    merchantPollTimer: 0,
    merchantPollInFlight: false,
    inbox: {
      search: "",
      filter: "all",
      readAt: {},
      takeover: {},
    },
    orders: [],
    ordersPage: createInitialOrdersPageState(),
    templates: [],
    cards: null,
    cardsAccountKey: "",
    cardsLoad: null,
    templateEditorOpenGeneration: 0,
    templateEditor: { editingId: "", productIds: [] },
    cardsEditor: { editingId: "", mode: "import" },
    confirmAction: null,
    docs: newDocsState(),
    qrLogin: {
      loginId: "",
      accountKey: "",
      status: "idle",
      message: "",
      pollTimer: 0,
      objectUrl: "",
      generation: 0,
      failures: 0,
      pollAttempts: 0,
      retryTimer: 0,
      retryAt: 0,
      expiresAt: 0,
      retryAction: "start",
      operation: "",
      polling: false,
    },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[char]));

  class ApiError extends Error {
    constructor(message, status, code = "", detail = null) {
      super(message);
      this.status = status;
      this.code = code;
      this.detail = detail;
    }
  }

  function text(selector, value) {
    const node = typeof selector === "string" ? $(selector) : selector;
    if (node) node.textContent = value == null ? "" : String(value);
  }

  function formatDate(value) {
    if (value === null || value === undefined || value === "") return "--";
    const number = Number(value);
    const date = Number.isFinite(number) && number > 1000000000
      ? new Date(number * 1000)
      : new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function normalizeView(view) {
    const key = String(view || "").trim();
    return VIEW_ALIASES[key] || key;
  }

  function domainView(view) {
    const normalized = normalizeView(view);
    if (["chat", "ai-config", "auto-reply"].includes(normalized)) return "chat";
    if (["goods", "templates", "cards"].includes(normalized)) return "goods";
    if (normalized === "settings" || normalized === "docs") return "settings";
    if (normalized === "ops") return "ops";
    return normalized;
  }

  function newClientRequestId() {
    if (typeof window.crypto?.randomUUID === "function") return window.crypto.randomUUID();
    if (typeof window.crypto?.getRandomValues === "function") {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
      return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
    }
    return "request-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 14);
  }

  function statCard(label, value, icon, tone, sub = "") {
    return '<div class="stat-card"><div class="stat-card-copy"><span class="stat-card-label">' + esc(label) + '</span><strong class="stat-card-value">' + esc(value) + "</strong>" +
      (sub ? '<span class="stat-card-sub">' + esc(sub) + "</span>" : "") + "</div>" +
      '<span class="stat-card-icon ' + tone + '"><svg class="icon"><use href="' + ICONS + icon + '"></use></svg></span></div>';
  }

  function productImageUrl(product) {
    if (!product || typeof product !== "object") return "";
    const candidates = [product.image_url, product.image, product.main_image];
    if (Array.isArray(product.images)) candidates.unshift(product.images[0]);
    const value = candidates.find((item) => typeof item === "string" && /^https?:\/\//i.test(item.trim()));
    return value ? value.trim() : "";
  }

  function productThumb(product, kind) {
    const title = String(product?.title || "").trim();
    const image = productImageUrl(product);
    const thumbClass = kind === "home" ? "home-product-thumb" : "product-thumb";
    const monogramClass = kind === "home" ? "home-product-monogram" : "product-monogram";
    if (image) return '<span class="' + thumbClass + '"><img src="' + esc(image) + '" alt="" loading="lazy" referrerpolicy="no-referrer"></span>';
    if (title) return '<span class="' + thumbClass + '"><span class="' + monogramClass + '" aria-hidden="true">' + esc(title.slice(0, 1)) + '</span></span>';
    return '<span class="' + thumbClass + '"><svg class="icon"><use href="' + ICONS + 'box"></use></svg></span>';
  }

  function loadGoodsPreferences() {
    try {
      const raw = JSON.parse(window.localStorage.getItem("xianyu-saas.goods-prefs") || "{}");
      if (raw.viewMode === "cards" || raw.viewMode === "list") state.goodsViewMode = raw.viewMode;
      if (raw.pageSize === 12 || raw.pageSize === 24) state.goodsPageSize = raw.pageSize;
    } catch (error) {}
  }

  function persistGoodsPreferences() {
    try {
      window.localStorage.setItem("xianyu-saas.goods-prefs", JSON.stringify({
        viewMode: state.goodsViewMode,
        pageSize: state.goodsPageSize,
      }));
    } catch (error) {}
  }

  function setGoodsViewMode(mode) {
    if (mode !== "cards" && mode !== "list") return;
    state.goodsViewMode = mode;
    persistGoodsPreferences();
    renderProducts();
  }

  function setGoodsPageSize(size) {
    const parsed = Number(size);
    state.goodsPageSize = parsed === 24 ? 24 : 12;
    state.goodsPage = 1;
    persistGoodsPreferences();
    renderProducts();
  }

  function accountStorageKey() {
    const username = String(state.me?.username || "").trim();
    return username ? ACTIVE_ACCOUNT_STORAGE_PREFIX + username : "";
  }

  function readStoredAccountKey() {
    const key = accountStorageKey();
    if (!key) return "";
    try {
      return String(window.localStorage.getItem(key) || "").trim();
    } catch (error) {
      return "";
    }
  }

  function persistAccountKey(key) {
    const storageKey = accountStorageKey();
    if (!storageKey || !key) return;
    try {
      window.localStorage.setItem(storageKey, key);
    } catch (error) {
      // Private browsing or a blocked storage area should not stop the app.
    }
  }

  function inboxStorageKey() {
    const username = String(state.me?.username || "").trim();
    const account = String(state.activeAccountKey || "default").trim();
    return username && account ? INBOX_STORAGE_PREFIX + encodeURIComponent(username) + ":" + encodeURIComponent(account) : "";
  }

  function loadInboxPreferences() {
    const defaults = { search: "", filter: "all", readAt: {}, takeover: {} };
    const key = inboxStorageKey();
    if (!key) {
      state.inbox = defaults;
      return;
    }
    try {
      const raw = JSON.parse(window.localStorage.getItem(key) || "{}");
      state.inbox = {
        search: typeof raw.search === "string" ? raw.search.slice(0, 120) : "",
        filter: ["unread", "takeover"].includes(raw.filter) ? raw.filter : "all",
        readAt: raw.readAt && typeof raw.readAt === "object" ? raw.readAt : {},
        takeover: raw.takeover && typeof raw.takeover === "object" ? raw.takeover : {},
      };
    } catch (error) {
      state.inbox = defaults;
    }
  }

  function persistInboxPreferences() {
    const key = inboxStorageKey();
    if (!key) return;
    try {
      // Only conversation IDs and control timestamps are kept locally.  Message
      // bodies and any platform credentials never enter browser storage.
      window.localStorage.setItem(key, JSON.stringify({
        search: String(state.inbox.search || "").slice(0, 120),
        filter: ["unread", "takeover"].includes(state.inbox.filter) ? state.inbox.filter : "all",
        readAt: state.inbox.readAt || {},
        takeover: state.inbox.takeover || {},
      }));
    } catch (error) {
      // Private browsing or a blocked storage area should not stop the inbox.
    }
  }

  function resetAccountInboxState({ restorePreferences = false } = {}) {
    state.conversations = [];
    state.messages = [];
    state.messageSearch = "";
    state.messageMatchCount = 0;
    if (state.messageSearchTimer) window.clearTimeout(state.messageSearchTimer);
    if (state.inboxSearchTimer) window.clearTimeout(state.inboxSearchTimer);
    state.messageSearchTimer = 0;
    state.inboxSearchTimer = 0;
    state.quickReplies = [];
    state.quickRepliesGeneration += 1;
    state.selectedChatId = "";
    state.inbox = { search: "", filter: "all", readAt: {}, takeover: {} };
    if (restorePreferences) loadInboxPreferences();
  }

  function accountLabel(account) {
    if (!account) return "未连接店铺";
    const name = String(account.name || "").trim();
    if (name) return name;
    return account.key === "default" ? "默认店铺" : "店铺账号";
  }

  function accountHealthCode(account) {
    const errorCode = String(account?.last_error_code || "").toLowerCase();
    return errorCode || String(account?.status || "unconfigured").toLowerCase();
  }

  function accountStatusLabel(account) {
    const labels = {
      ready: "已连接",
      restricted: "部分能力受限",
      account_restricted: "部分能力受限",
      risk_control: "接口请求受限",
      risk_cooldown: "请求保护冷却中",
      verification_required: "接口要求验证",
      session_expired: "已断开 · 登录失效",
      cookie_expired: "已断开 · 登录失效",
      cookie_invalid: "已断开 · 登录无效",
      cookie_incomplete: "已断开 · 登录不完整",
      expired: "已断开 · 需重新登录",
      sync_cooldown: "检测冷却中",
      sync_busy: "正在检测",
      network_error: "检测失败 · 网络异常",
      platform_busy: "检测失败 · 平台繁忙",
      platform_error: "检测失败 · 平台异常",
      profile_missing: "检测失败 · 店铺未识别",
      sync_error: "检测失败",
      degraded: "连接待确认",
      waiting_login: "等待登录",
      unconfigured: "未连接",
    };
    return labels[accountHealthCode(account)] || "连接待确认";
  }

  function accountStatusClass(account) {
    const code = accountHealthCode(account);
    if (code === "ready") return "is-ready";
    if (["restricted", "account_restricted", "verification_required", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "expired"].includes(code)) return "is-error";
    if (["risk_control", "risk_cooldown", "degraded", "sync_cooldown", "sync_busy", "network_error", "platform_busy", "platform_error", "profile_missing", "sync_error"].includes(code)) return "is-warning";
    return "is-muted";
  }

  function accountSyncLabel(account) {
    return account?.last_sync_at ? "最近同步 " + formatDate(account.last_sync_at) : "等待首次连接";
  }

  function currentAccount() {
    return state.accounts.find((item) => item.key === state.activeAccountKey) || null;
  }

  function accountFromBot(bot) {
    const account = bot?.account;
    if (!account || typeof account !== "object" || !account.key) return null;
    return {
      id: account.id,
      key: String(account.key),
      name: String(account.name || ""),
      status: String(account.status || "unconfigured"),
      enabled: account.enabled !== false,
      last_error_code: String(account.last_error_code || ""),
      last_verified_at: account.last_verified_at || null,
      last_sync_at: account.last_sync_at || null,
      product_count: Number(account.product_count || account.products_count || 0),
    };
  }

  function ensureCurrentAccount(bot = state.bot) {
    const fromBot = accountFromBot(bot);
    const key = String(state.activeAccountKey || fromBot?.key || "default");
    state.activeAccountKey = key;
    const existing = state.accounts.find((item) => item.key === key);
    if (existing && fromBot) Object.assign(existing, fromBot);
    else if (fromBot) state.accounts.push(fromBot);
    if (!state.accounts.some((item) => item.key === key)) {
      state.accounts.unshift({ key, name: key === "default" ? "默认店铺" : "店铺账号", status: "unconfigured", enabled: true });
    }
    persistAccountKey(key);
    renderAccountSwitcher();
  }

  function renderAccountSwitcher() {
    const current = currentAccount();
    const enabledAccounts = state.accounts.filter((account) => account.enabled !== false);
    const pageSize = Math.max(1, Number(state.shopAccountsPageSize || 5));
    const pageCount = Math.max(1, Math.ceil(enabledAccounts.length / pageSize));
    state.shopAccountsPage = Math.min(Math.max(1, Number(state.shopAccountsPage || 1)), pageCount);
    const start = (state.shopAccountsPage - 1) * pageSize;
    const visibleAccounts = enabledAccounts.slice(start, start + pageSize);
    const markup = visibleAccounts.length ? visibleAccounts.map((account) => {
      const active = account.key === state.activeAccountKey;
      const liveAuthCode = active && ["risk_control", "verification_required", "session_expired"].includes(String(state.bot?.auth_code || ""))
        ? String(state.bot.auth_code)
        : "";
      const liveSyncCode = active && COOKIE_BLOCKING_CODES.has(String(state.bot?.sync_status || ""))
        ? String(state.bot.sync_status)
        : "";
      const effectiveAccount = liveAuthCode || liveSyncCode
        ? Object.assign({}, account, { last_error_code: liveAuthCode || liveSyncCode })
        : account;
      const status = accountStatusLabel(effectiveAccount);
      const count = active && state.products.length ? state.products.length : Number(account.product_count || account.products_count || 0);
      const sync = account.last_sync_at ? formatDate(account.last_sync_at) : "--";
      const label = accountLabel(account);
      const deleteLabel = account.key === "default" ? "默认店铺不可删除" : "断开" + label;
      const switchLabel = active ? "当前店铺" : "切换到" + label;
      const healthCode = accountHealthCode(effectiveAccount);
      const isError = ["expired", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "restricted", "account_restricted", "verification_required"].includes(healthCode);
      const isWarning = ["risk_control", "risk_cooldown", "degraded", "sync_cooldown", "sync_busy", "network_error", "platform_busy", "platform_error", "profile_missing", "sync_error"].includes(healthCode);
      const statusBadge = isError ? "badge-red" : healthCode === "ready" ? "badge-green" : isWarning ? "badge-amber" : "badge-muted";
      const toneClass = isError ? " is-expired" : healthCode === "ready" ? " is-ready" : " is-unconfigured";
      const needsReconnect = ["expired", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "restricted", "account_restricted", "verification_required", "degraded", "waiting_login"].includes(healthCode);
      return '<article class="shop-card' + (active ? " is-current" : "") + toneClass + '" data-account-key="' + esc(account.key) + '">' +
        '<button class="shop-card-main" type="button" data-account-switch="' + esc(account.key) + '" aria-label="' + esc(switchLabel) + '" title="' + esc(switchLabel) + '"' + (active ? ' aria-current="true"' : "") + '>' +
        '<span class="shop-card-avatar">' + esc(label.slice(0, 1)) + '</span><span class="shop-card-copy"><strong>' + esc(label) + '</strong><small>' + esc(account.key === "default" ? "默认账号" : "已绑定账号") + '</small></span></button>' +
        '<span class="badge ' + statusBadge + '">' + esc(active ? "当前 · " + status : status) + '</span>' +
        '<div class="shop-card-meta"><span><svg class="icon"><use href="' + ICONS + 'box"></use></svg>' + esc(count ? count + " 个商品" : "暂无商品") + '</span><span><svg class="icon"><use href="' + ICONS + 'clock"></use></svg>' + esc(sync === "--" ? "等待首次同步" : "最近同步 " + sync) + '</span></div>' +
        '<div class="shop-card-actions">' +
        '<button class="button button-secondary button-compact" type="button" data-shop-action="check" data-shop-key="' + esc(account.key) + '" aria-label="检测' + esc(label) + '" title="重新检测"><span>检测</span></button>' +
        '<button class="button ' + (needsReconnect ? "button-primary" : "button-secondary") + ' button-compact" type="button" data-shop-action="reconnect" data-shop-key="' + esc(account.key) + '" aria-label="重连' + esc(label) + '" title="重新连接"><span>重新连接</span></button>' +
        '<button class="button button-secondary button-compact" type="button" data-account-rename="' + esc(account.key) + '" aria-label="修改' + esc(label) + '名称" title="修改名称"><span>改名</span></button>' +
        '<button class="button button-secondary button-danger-soft button-compact" type="button" data-account-delete="' + esc(account.key) + '" aria-label="' + esc(deleteLabel) + '" title="' + esc(deleteLabel) + '"' + (account.key === "default" ? " disabled" : "") + '><span>断开</span></button>' +
        '</div></article>';
    }).join("") : '<div class="automation-empty">还没有店铺账号，先添加一个店铺。</div>';
    const list = $("#shopAccountsPanelList");
    if (list) list.innerHTML = markup;
    const count = $("#shopAccountsCount");
    if (count) {
      count.textContent = enabledAccounts.length + " 个";
      count.className = "badge " + (enabledAccounts.length ? "badge-muted" : "badge-amber");
    }
    const pagination = $("#shopAccountsPagination");
    if (pagination) {
      pagination.hidden = pageCount <= 1;
      text("#shopAccountsPageLabel", "第 " + state.shopAccountsPage + " / " + pageCount + " 页");
      const previous = pagination.querySelector('[data-shop-page="prev"]');
      const next = pagination.querySelector('[data-shop-page="next"]');
      if (previous) previous.disabled = state.shopAccountsPage <= 1;
      if (next) next.disabled = state.shopAccountsPage >= pageCount;
      const size = $("#shopAccountsPageSize");
      if (size) size.value = String(pageSize);
    }
    renderAccountTabs();
    renderShopResources();
  }

  function renderAccountTabs() {
    const host = $("#accountTabs");
    if (!host) return;
    const accounts = state.accounts.filter((account) => account.enabled !== false);
    host.setAttribute("aria-label", "当前店铺：" + accountLabel(currentAccount()));
    const tabs = accounts.map((account) => {
      const active = account.key === state.activeAccountKey;
      const label = accountLabel(account);
      return '<button class="account-tab' + (active ? " is-active" : "") + '" type="button" role="tab" aria-selected="' + String(active) + '" title="' + esc(accountStatusLabel(account)) + '" aria-label="' + esc((active ? "当前店铺：" : "切换到：") + label) + '" data-account-switch="' + esc(account.key) + '">' +
        '<span class="account-tab-avatar">' + esc(label.slice(0, 1)) + '</span>' +
        '<span class="account-tab-name">' + esc(label) + '</span>' +
        '<i class="account-tab-dot ' + accountStatusClass(account) + '" aria-hidden="true"></i></button>';
    }).join("");
    host.innerHTML = tabs + '<button class="account-tab account-tab-add" type="button" data-view="shops" data-open-shop-add aria-label="添加店铺" title="添加店铺"><svg class="icon"><use href="' + ICONS + 'plus"></use></svg></button>';
  }

  function showToast(message, type = "success") {
    const region = $("#toastRegion");
    if (!region) return;
    region.querySelectorAll(".toast").forEach((toast) => toast.remove());
    const item = document.createElement("div");
    item.className = "toast" + (type === "error" ? " is-error" : type === "warning" ? " is-warning" : "");
    item.innerHTML = '<svg class="icon"><use href="' + ICONS + '' + (type === "success" ? "circle-check" : "circle-alert") + '"></use></svg><span>' + esc(message) + "</span>";
    region.appendChild(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function formMessage(selector, message, success = false) {
    const node = typeof selector === "string" ? $(selector) : selector;
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("is-success", success);
  }

  function captureAccountContext(epoch = state.accountEpoch) {
    return { epoch, accountKey: state.activeAccountKey };
  }

  function accountContextMatches(context) {
    return context?.epoch === state.accountEpoch && context?.accountKey === state.activeAccountKey;
  }

  function captureAiProductScope(itemId = state.ai?.selectedItemId) {
    return {
      account: captureAccountContext(),
      itemId: String(itemId || ""),
      productGeneration: Number(state.ai?.productGeneration || 0),
    };
  }

  function aiProductScopeMatches(scope) {
    return Boolean(scope)
      && accountContextMatches(scope.account)
      && scope.itemId === String(state.ai?.selectedItemId || "")
      && scope.productGeneration === Number(state.ai?.productGeneration || 0);
  }

  function beginRefreshContext() {
    const context = Object.assign(captureAccountContext(), {
      generation: ++state.refreshGeneration,
    });
    state.refreshOwner = context;
    return context;
  }

  function refreshContextMatches(context) {
    return accountContextMatches(context)
      && state.refreshOwner?.generation === context?.generation
      && state.refreshOwner?.epoch === context?.epoch
      && state.refreshOwner?.accountKey === context?.accountKey;
  }

  function registerCatalogStatus(bot) {
    const context = captureAccountContext();
    const status = {
      token: ++state.catalogStatusGeneration,
      epoch: context.epoch,
      accountKey: context.accountKey,
      truncated: bot?.products_truncated === true,
    };
    state.catalogStatus = status;
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    return status;
  }

  function catalogStatusMatches(status, context = captureAccountContext()) {
    return Boolean(status)
      && status.epoch === context.epoch
      && status.accountKey === context.accountKey;
  }

  function accountScopedApi(context, path, options = {}) {
    const headers = Object.assign({}, options.headers || {}, {
      "X-Shop-Account": context?.accountKey || "default",
    });
    return api(path, Object.assign({}, options, { headers }));
  }

  const AI_PROVIDER_COPY = {
    openai_chat_completions: { label: "OpenAI / 兼容接口", basePlaceholder: "例如：https://api.example.com/v1", keyLabel: "API Key", keyPlaceholder: "首次配置请输入；已保存时留空表示保留", modelPlaceholder: "例如：gpt-4o-mini、deepseek-chat" },
    openai_responses: { label: "OpenAI Responses", basePlaceholder: "例如：https://api.openai.com/v1", keyLabel: "API Key", keyPlaceholder: "填写 OpenAI API Key；已保存时留空表示保留", modelPlaceholder: "例如：gpt-5-mini" },
    anthropic_messages: { label: "Anthropic Claude", basePlaceholder: "例如：https://api.anthropic.com/v1", keyLabel: "API Key", keyPlaceholder: "填写 Anthropic API Key；已保存时留空表示保留", modelPlaceholder: "例如：claude-sonnet-4-20250514" },
    google_gemini: { label: "Google Gemini", basePlaceholder: "例如：https://generativelanguage.googleapis.com/v1beta", keyLabel: "API Key", keyPlaceholder: "填写 Google AI API Key；已保存时留空表示保留", modelPlaceholder: "例如：gemini-2.5-flash" },
    ollama_chat: { label: "Ollama 本地服务", basePlaceholder: "例如：http://127.0.0.1:11434/api", keyLabel: "访问密钥（可选）", keyPlaceholder: "本机 Ollama 通常可留空", modelPlaceholder: "例如：qwen2.5:7b" },
  };

  const AI_CONNECTION_STATUS_COPY = {
    unconfigured: ["未配置", "badge-muted", "请填写连接信息并先测试连接"],
    pending: ["待测试", "badge-amber", "连接信息变化后需要重新测试"],
    unverified: ["待测试", "badge-amber", "请先完成连接测试"],
    verified: ["已验证", "badge-green", "连接已验证，可保存并启用 AI 客服"],
    success: ["测试成功", "badge-green", "连接测试成功，可以保存"],
    authentication_failed: ["鉴权失败", "badge-red", "API Key 无效或没有调用权限"],
    model_not_found: ["模型不存在", "badge-red", "模型名不存在或当前 Key 无权使用"],
    rate_limited: ["请求限流", "badge-amber", "模型服务正在限流，请稍后重试"],
    timeout: ["连接超时", "badge-amber", "模型服务响应超时，请检查地址或稍后重试"],
    unsafe_url: ["地址不安全", "badge-red", "Base URL 未通过服务端安全校验"],
    address_unsafe: ["地址不安全", "badge-red", "Base URL 未通过服务端安全校验"],
    credential_store_unavailable: ["凭据不可用", "badge-red", "服务端密钥存储暂不可用，AI 已安全关闭"],
    credential_unavailable: ["凭据不可用", "badge-red", "当前店铺的加密凭据不可用，AI 已安全关闭"],
    unavailable: ["服务不可用", "badge-red", "模型服务暂不可用，请稍后重试"],
    service_unavailable: ["服务不可用", "badge-red", "模型服务暂不可用，请稍后重试"],
    invalid_response: ["响应无效", "badge-red", "模型服务返回了无法识别的响应"],
    response_invalid: ["响应无效", "badge-red", "模型服务返回了无法识别的响应"],
  };

  function emptyAiState() {
    return {
      status: null,
      connection: null,
      config: null,
      templates: [],
      products: [],
      selectedItemId: "",
      knowledge: null,
      versions: [],
      productSearch: "",
      verificationToken: "",
      testedFingerprint: "",
      loadGeneration: 0,
      productGeneration: 0,
      knowledgeGeneration: 0,
      knowledgeRequestGeneration: 0,
      extractionGeneration: 0,
      previewGeneration: 0,
      previewBusy: false,
      generatedKnowledge: null,
      previewHistory: [],
      dirty: { connection: false, config: false, knowledge: false },
      baseline: { connection: "", config: "", knowledge: "" },
    };
  }

  function clearAiProductTransientUi() {
    state.confirmAction = null;
    closeDialog("confirmDialog");
    ["#aiExtractInput", "#aiKnowledgeContent", "#aiPreviewInput"].forEach((selector) => {
      const input = $(selector);
      if (input) input.value = "";
    });
    formMessage("#aiKnowledgeMessage", "");
    text("#aiKnowledgeEditMode", "");
    const preview = $("#aiGeneratedKnowledgePreview");
    if (preview) preview.hidden = true;
    text("#aiGeneratedKnowledgeRaw", "");
    clearAiPreview();
    setBusy($("#aiExtractKnowledge"), false);
    setBusy($("#aiSaveKnowledge"), false);
  }

  function resetAiState({ preserveSearch = false } = {}) {
    const previousSearch = preserveSearch ? String(state.ai?.productSearch || "") : "";
    const next = emptyAiState();
    next.productSearch = previousSearch;
    next.loadGeneration = Number(state.ai?.loadGeneration || 0) + 1;
    next.productGeneration = Number(state.ai?.productGeneration || 0) + 1;
    next.knowledgeGeneration = Number(state.ai?.knowledgeGeneration || 0) + 1;
    next.knowledgeRequestGeneration = Number(state.ai?.knowledgeRequestGeneration || 0) + 1;
    next.extractionGeneration = Number(state.ai?.extractionGeneration || 0) + 1;
    next.previewGeneration = Number(state.ai?.previewGeneration || 0) + 1;
    state.ai = next;
    clearAiProductTransientUi();
  }

  function hasMeaningfulText(value) {
    return /[A-Za-z0-9\u3400-\u9FFF]/.test(String(value || ""));
  }

  function hasMeaningfulAIText(value) {
    const candidate = String(value || "").trim();
    if (!candidate) return false;
    if (/^```[\s\S]*```$/.test(candidate)) return false;
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return false;
    } catch (error) {
      // 普通说明不是 JSON，继续按自然语言检查。
    }
    const compact = candidate.normalize("NFKC").replace(/\s+/g, "").toLocaleLowerCase("en-US");
    if (AI_TEXT_PLACEHOLDERS.has(compact)) return false;
    const withoutPunctuation = candidate.replace(/[\s\p{P}\p{S}]/gu, "");
    if (!/[\p{L}\p{N}]/u.test(withoutPunctuation)) return false;
    if (/^(?:示例|例如|比如|example|placeholder|default)(?:[：:]|\s|$)/i.test(candidate)) return false;
    if (/^(?:请(?:先)?(?:填写|输入|粘贴|补充)|在此(?:填写|输入))[^。！？!?，,；;\n]{0,40}$/.test(candidate)) return false;
    if (/^(?:please\s+(?:enter|fill|paste|provide)|enter\s+here)\b[^.!?\n]{0,80}$/i.test(candidate)) return false;
    if (/^(?:未(?:配置|填写)|暂无(?:内容)?|内容待补充)(?:[：:]|\s|$)/.test(candidate)) return false;
    if (/^(?:以下是|下面是|作为(?:一个)?AI|我是(?:一个)?AI|我(?:可以|将|已经)为你|根据你的(?:要求|输入))/.test(candidate)) return false;
    if (/^(?:as an ai|i(?:'m| am) an ai|here is|below is|based on your (?:request|input))\b/i.test(candidate)) return false;
    return true;
  }

  function naturalLanguageValue(value) {
    if (Array.isArray(value)) return value.map((item) => String(item || "").trim()).filter(Boolean).join("\n");
    return String(value || "").trim();
  }

  function legacyKnowledgeToContent(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return "";
    const sections = [];
    const add = (label, content) => {
      const textValue = naturalLanguageValue(content);
      if (hasMeaningfulAIText(textValue)) sections.push(label + "：\n" + textValue);
    };
    add("商品说明", value.summary);
    add("主要特点", value.selling_points);
    add("规格信息", value.specifications);
    add("价格说明", value.price_policy);
    add("交付说明", value.delivery_notes);
    add("使用方式", value.usage_notes);
    add("售后说明", value.after_sales);
    if (Array.isArray(value.faqs)) {
      const faqs = value.faqs.map((faq) => {
        const question = String(faq?.question || "").trim();
        const answer = String(faq?.answer || "").trim();
        return question && answer ? "问：" + question + "\n答：" + answer : "";
      }).filter(Boolean).join("\n\n");
      add("常见问答", faqs);
    }
    add("不能回答", value.forbidden_answers);
    add("转人工情况", value.handoff_rules);
    add("其他补充", value.custom_notes);
    return sections.join("\n\n");
  }

  function generatedContentFromResult(result) {
    const direct = result?.content ?? result?.draft?.content ?? result?.knowledge?.content ?? result?.knowledge?.draft?.content;
    let candidate = String(direct || "").trim();
    if (!candidate) {
      const legacy = result?.draft || result?.knowledge?.draft || result?.knowledge;
      candidate = legacyKnowledgeToContent(legacy);
    }
    const raw = String(result?.raw_output || "").trim();
    const inspect = raw || candidate;
    if (/```|^\s*[\[{][\s\S]*[\]}]\s*$/.test(inspect)) throw new ApiError("AI 返回了代码块或配置内容，未采用，请重新整理");
    if (/^(以下是|下面是|作为(?:一个)?AI|我(?:已经|将|可以)为你|根据你的要求)/.test(candidate)) throw new ApiError("AI 返回了说明文字而不是商品内容，未采用，请重新整理");
    if (!hasMeaningfulAIText(candidate)) throw new ApiError("AI 没有返回可采用的商品内容，请补充资料后重试");
    return candidate.slice(0, 12000);
  }

  function aiStoreFormValue() {
    return {
      store_content: String($("#aiStoreContent")?.value || "").trim(),
      persona_preset: String($("#aiPersonaPreset")?.value || "friendly"),
      persona_name: String($("#aiPersonaName")?.value || "").trim(),
      tone: String($("#aiTone")?.value || "friendly"),
      buyer_address: String($("#aiBuyerAddress")?.value || "").trim(),
      reply_length: String($("#aiReplyLength")?.value || "short"),
      emoji_level: String($("#aiEmojiLevel")?.value || "low"),
      forbidden_claims: String($("#aiForbiddenClaims")?.value || "").trim(),
      handoff_rules: String($("#aiHandoffRules")?.value || "").trim(),
    };
  }

  function aiStoreBaselineValue(config = aiStoreFormValue()) {
    return JSON.stringify(config);
  }

  function writeAiStoreForm(config, { setBaseline = true } = {}) {
    const clean = config && typeof config === "object" ? config : {};
    const values = {
      store_content: String(clean.store_content ?? clean.common_knowledge ?? ""),
      persona_preset: String(clean.persona_preset || "friendly"),
      persona_name: String(clean.persona_name || ""),
      tone: String(clean.tone || "friendly"),
      buyer_address: String(clean.buyer_address || ""),
      reply_length: String(clean.reply_length || "short"),
      emoji_level: String(clean.emoji_level || "low"),
      forbidden_claims: naturalLanguageValue(clean.forbidden_claims),
      handoff_rules: naturalLanguageValue(clean.handoff_rules),
    };
    if ($("#aiStoreContent")) $("#aiStoreContent").value = values.store_content;
    if ($("#aiPersonaPreset")) $("#aiPersonaPreset").value = values.persona_preset;
    if ($("#aiPersonaName")) $("#aiPersonaName").value = values.persona_name;
    if ($("#aiTone")) $("#aiTone").value = values.tone;
    if ($("#aiBuyerAddress")) $("#aiBuyerAddress").value = values.buyer_address;
    if ($("#aiReplyLength")) $("#aiReplyLength").value = values.reply_length;
    if ($("#aiEmojiLevel")) $("#aiEmojiLevel").value = values.emoji_level;
    if ($("#aiForbiddenClaims")) $("#aiForbiddenClaims").value = values.forbidden_claims;
    if ($("#aiHandoffRules")) $("#aiHandoffRules").value = values.handoff_rules;
    if (setBaseline) {
      state.ai.baseline.config = aiStoreBaselineValue(values);
      state.ai.dirty.config = false;
    }
  }

  function aiProviderCode(value = $("#aiProvider")?.value) {
    const provider = String(value || "openai_chat_completions").trim();
    return AI_PROVIDER_COPY[provider] ? provider : "openai_chat_completions";
  }

  function aiProviderRequiresKey(provider = aiProviderCode()) {
    return provider !== "ollama_chat";
  }

  function aiProviderHasReusableKey(provider = aiProviderCode()) {
    const connection = state.ai?.connection || {};
    return connection.api_key_configured === true && aiProviderCode(connection.provider) === provider;
  }

  function renderAiProviderFields() {
    const provider = aiProviderCode();
    const copy = AI_PROVIDER_COPY[provider] || AI_PROVIDER_COPY.openai_chat_completions;
    text("#aiBaseUrlLabel", "服务地址");
    text("#aiKeyLabel", copy.keyLabel);
    const baseInput = $("#aiBaseUrl");
    const keyInput = $("#aiApiKey");
    const modelInput = $("#aiModel");
    if (baseInput) baseInput.placeholder = copy.basePlaceholder;
    if (keyInput) {
      keyInput.placeholder = copy.keyPlaceholder;
      keyInput.required = aiProviderRequiresKey(provider) && !aiProviderHasReusableKey(provider);
    }
    if (modelInput) modelInput.placeholder = copy.modelPlaceholder;
  }

  function aiConnectionCandidate() {
    return {
      provider: aiProviderCode(),
      base_url: String($("#aiBaseUrl")?.value || "").trim(),
      model: String($("#aiModel")?.value || "").trim(),
      api_key: String($("#aiApiKey")?.value || "").trim(),
    };
  }

  function aiConnectionFingerprint(candidate = aiConnectionCandidate()) {
    return JSON.stringify({
      provider: candidate.provider,
      base_url: candidate.base_url,
      model: candidate.model,
      key_mode: candidate.api_key ? "candidate" : "saved",
      key_revision: Number(state.ai?.connection?.key_revision || 0),
    });
  }

  function syncAiDirtyFlags() {
    if (!state.ai) return false;
    state.ai.dirty.connection = false;
    if ($("#aiStoreForm")) state.ai.dirty.config = aiStoreBaselineValue() !== state.ai.baseline.config;
    const knowledge = $("#aiKnowledgeContent");
    if (knowledge && state.ai.selectedItemId) state.ai.dirty.knowledge = knowledge.value !== state.ai.baseline.knowledge;
    return Boolean(state.ai.dirty.config || state.ai.dirty.knowledge);
  }

  function confirmDiscardAiChanges(reason = "切换") {
    if (!syncAiDirtyFlags()) return true;
    const accepted = window.confirm("当前店铺 AI 客服配置有未保存修改。确认" + reason + "并丢弃这些修改吗？");
    if (accepted) state.ai.dirty = { connection: false, config: false, knowledge: false };
    return accepted;
  }

  function connectionStatusCode(connection = state.ai?.connection, status = state.ai?.status) {
    if (connection?.status) return String(connection.status);
    if (connection?.connection_status) return String(connection.connection_status);
    if (connection?.test_status) return String(connection.test_status);
    if (connection?.verified === true || status?.connection_verified === true) return "verified";
    if (connection?.last_error_code || connection?.error_code || status?.current_error_code || status?.error_code) {
      return String(connection?.last_error_code || connection?.error_code || status?.current_error_code || status?.error_code);
    }
    if (connection?.base_url || connection?.model || connection?.api_key_configured) return "pending";
    return "unconfigured";
  }

  function connectionStatusInfo(code = connectionStatusCode()) {
    return AI_CONNECTION_STATUS_COPY[code] || ["需要处理", "badge-red", "连接状态异常，请重新测试"];
  }

  function aiConnectionVerified() {
    return connectionStatusCode() === "verified";
  }

  function aiKnowledgeStatus(product, knowledge = null) {
    return String(knowledge?.knowledge_status || knowledge?.status || product?.knowledge_status || product?.content_status || product?.status || "unconfigured");
  }

  function aiKnowledgeStatusInfo(status) {
    const map = {
      unconfigured: ["未补充", "badge-muted"],
      empty: ["未补充", "badge-muted"],
      draft: ["已保存", "badge-green"],
      saved: ["已保存生效", "badge-green"],
      active: ["已保存生效", "badge-green"],
      effective: ["已保存生效", "badge-green"],
      published: ["已保存生效", "badge-green"],
      stale: ["需确认", "badge-amber"],
      needs_confirmation: ["需确认", "badge-amber"],
      disabled: ["已停用", "badge-red"],
      archived: ["已停用", "badge-muted"],
    };
    return map[status] || ["未补充", "badge-muted"];
  }

  function aiConfigDraft(data) {
    if (!data || typeof data !== "object") return {};
    return data.draft || data.content || data.config?.draft || data.config || data.settings || data;
  }

  function aiStoreHasContent(data = state.ai?.config) {
    return hasMeaningfulAIText(aiConfigDraft(data)?.store_content ?? aiConfigDraft(data)?.common_knowledge);
  }

  function aiKnowledgeContent(data) {
    const root = data?.knowledge || data || {};
    const direct = root.content ?? root.draft?.content ?? root.published?.content ?? root.published?.knowledge?.content;
    if (hasMeaningfulAIText(direct)) return String(direct).trim();
    const legacy = root.draft || root.published?.knowledge || root.published || null;
    return legacyKnowledgeToContent(legacy);
  }

  function setAiInlineStatus(message, type = "warning") {
    const host = $("#aiConnectionMessage");
    if (!host) return;
    host.hidden = !message;
    host.className = "inline-status ai-form-status" + (type === "error" ? " is-error" : type === "success" ? " is-success" : "");
    text($("span", host), message || "");
  }

  function aiProductFacts(product) {
    return product?.facts && typeof product.facts === "object" ? product.facts : product || {};
  }

  function aiProductItemId(product) {
    const facts = aiProductFacts(product);
    return String(product?.item_id || product?.id || facts.item_id || facts.id || "");
  }

  function aiProductTitle(product) {
    const facts = aiProductFacts(product);
    return String(product?.title || facts.title || facts.name || "未命名商品");
  }

  function renderAiProducts() {
    const host = $("#aiProductList");
    if (!host) return;
    const search = String(state.ai?.productSearch || "").trim().toLowerCase();
    const products = (state.ai?.products || []).filter((product) => {
      const itemId = aiProductItemId(product);
      return !search || [aiProductTitle(product), itemId].filter(Boolean).join(" ").toLowerCase().includes(search);
    });
    if (!products.length) {
      host.innerHTML = '<div class="automation-empty">' + (search ? "没有匹配的真实商品" : "当前店铺还没有可补充客服内容的商品") + "</div>";
      return;
    }
    host.innerHTML = products.map((product) => {
      const itemId = aiProductItemId(product);
      const status = aiKnowledgeStatus(product, itemId === state.ai.selectedItemId ? state.ai.knowledge : null);
      const statusInfo = aiKnowledgeStatusInfo(status);
      return '<button type="button" class="knowledge-product-btn' + (itemId === state.ai.selectedItemId ? " is-active" : "") + '" data-ai-product="' + esc(itemId) + '">' +
        '<strong>' + esc(aiProductTitle(product)) + '</strong><span class="badge knowledge-product-status ' + statusInfo[1] + '">' + statusInfo[0] + "</span></button>";
    }).join("");
  }

  function renderAiVersions() {
    const host = $("#aiKnowledgeVersions");
    if (!host) return;
    const versions = Array.isArray(state.ai?.versions) ? state.ai.versions : [];
    host.innerHTML = versions.length ? versions.slice(0, 8).map((version) => {
      const label = version.label || version.status || (version.revision != null ? "版本 " + version.revision : "历史版本");
      return '<span class="knowledge-version-item">' + esc(label) + (version.updated_at ? " · " + esc(formatDate(version.updated_at)) : "") + "</span>";
    }).join("") : "";
  }

  function renderAiTemplates() {
    const host = $("#aiTemplateList");
    if (!host) return;
    const templates = Array.isArray(state.ai?.templates) ? state.ai.templates : [];
    if (!templates.length) {
      host.innerHTML = '<div class="ai-template-empty">还没有客服模板，可以把当前配置保存到这里。</div>';
      return;
    }
    host.innerHTML = templates.map((template) => '<div class="ai-template-row" data-ai-template-row="' + esc(template.id) + '">' +
      '<div class="ai-template-copy"><strong>' + esc(template.name || "未命名模板") + '</strong><small>' + esc(template.updated_at ? "更新于 " + formatDate(template.updated_at) : "当前店铺模板") + '</small></div>' +
      '<div class="ai-template-actions"><button class="button button-secondary button-compact" type="button" data-ai-template-load="' + esc(template.id) + '">使用</button><button class="button button-danger button-compact" type="button" data-ai-template-delete="' + esc(template.id) + '">删除</button></div></div>').join("");
  }

  function openAiTemplates() {
    renderAiTemplates();
    formMessage("#aiTemplateMessage", "");
    const dialog = $("#aiTemplatesDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
    $("#aiTemplateName")?.focus();
  }

  async function saveAiTemplate(event) {
    event.preventDefault();
    const name = String($("#aiTemplateName")?.value || "").trim();
    if (!name) {
      formMessage("#aiTemplateMessage", "请输入模板名称");
      return;
    }
    const config = aiStoreFormValue();
    if (!hasMeaningfulAIText(config.store_content)) {
      formMessage("#aiTemplateMessage", "请先填写店铺与客服说明");
      return;
    }
    const context = captureAccountContext();
    const button = event.submitter || $("#aiSaveTemplate");
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/ai/templates", {
        method: "POST",
        body: JSON.stringify({ name, config }),
      });
      if (!accountContextMatches(context)) return;
      const saved = result?.template;
      state.ai.templates = [saved].concat((state.ai.templates || []).filter((item) => item.id !== saved?.id));
      if ($("#aiTemplateName")) $("#aiTemplateName").value = "";
      renderAiTemplates();
      formMessage("#aiTemplateMessage", "客服模板已保存", true);
      showToast("客服模板已保存");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#aiTemplateMessage", error.message || "客服模板保存失败");
    } finally {
      if (accountContextMatches(context)) setBusy(button, false);
    }
  }

  function loadAiTemplate(templateId) {
    const template = (state.ai.templates || []).find((item) => String(item.id) === String(templateId));
    const config = template?.config || template?.content || template?.settings;
    if (!template || !config) return;
    writeAiStoreForm(config, { setBaseline: false });
    state.ai.dirty.config = aiStoreBaselineValue() !== state.ai.baseline.config;
    formMessage("#aiPersonaMessage", "客服模板已加载，点击保存并生效后使用", true);
    closeDialog("aiTemplatesDialog");
    showToast("已加载客服模板");
  }

  function confirmDeleteAiTemplate(templateId) {
    const template = (state.ai.templates || []).find((item) => String(item.id) === String(templateId));
    if (!template) return;
    const context = captureAccountContext();
    text("#confirmTitle", "删除客服模板");
    text("#confirmMessage", "确认删除“" + template.name + "”吗？已保存的店铺配置不会受到影响。");
    text("#confirmAction", "确认删除");
    state.confirmAction = async () => {
      await accountScopedApi(context, "/api/bot/ai/templates/" + encodeURIComponent(template.id), { method: "DELETE" });
      if (!accountContextMatches(context)) return;
      state.ai.templates = (state.ai.templates || []).filter((item) => item.id !== template.id);
      renderAiTemplates();
      formMessage("#aiTemplateMessage", "客服模板已删除", true);
      showToast("客服模板已删除");
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function clearAiGeneratedKnowledge({ message = "" } = {}) {
    state.ai.generatedKnowledge = null;
    state.ai.extractionGeneration += 1;
    renderAiKnowledgeEditor({ preserveText: true });
    if (message) formMessage("#aiKnowledgeMessage", message, true);
  }

  function renderAiGeneratedKnowledge() {
    const host = $("#aiGeneratedKnowledgePreview");
    const raw = $("#aiGeneratedKnowledgeRaw");
    const pending = state.ai?.generatedKnowledge;
    const visible = Boolean(pending && pending.itemId === String(state.ai.selectedItemId || "") && pending.productGeneration === Number(state.ai.productGeneration || 0));
    if (host) host.hidden = !visible;
    if (raw) raw.textContent = visible ? String(pending.content || "") : "";
  }

  function confirmApplyAiGeneratedKnowledge() {
    const pending = state.ai?.generatedKnowledge;
    const itemId = String(state.ai.selectedItemId || "");
    if (!pending || pending.itemId !== itemId || pending.productGeneration !== Number(state.ai.productGeneration || 0) || !hasMeaningfulAIText(pending.content)) return;
    const scope = captureAiProductScope(itemId);
    text("#confirmTitle", "采用整理建议");
    text("#confirmMessage", "确认将这份建议放入商品补充内容编辑区吗？现有未保存内容会被覆盖，但不会自动保存或用于回答。");
    text("#confirmAction", "采用到编辑区");
    state.confirmAction = async () => {
      if (!aiProductScopeMatches(scope) || state.ai.generatedKnowledge !== pending || !hasMeaningfulAIText(pending.content)) return;
      const value = String(pending.content || "").trim();
      $("#aiKnowledgeContent").value = value;
      state.ai.knowledge = Object.assign({}, state.ai.knowledge || {}, { preview_generated: true });
      state.ai.dirty.knowledge = value !== state.ai.baseline.knowledge;
      state.ai.generatedKnowledge = null;
      renderAiKnowledgeEditor({ preserveText: true });
      formMessage("#aiKnowledgeMessage", "整理建议已放入编辑区，请检查后再保存", true);
      showToast("整理建议已采用，尚未保存");
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function renderAiProductFacts(product) {
    const facts = product ? aiProductFacts(product) : {};
    const skus = Array.isArray(facts.skus) ? facts.skus.map((sku) => [sku?.name, sku?.price, sku?.stock != null ? "库存 " + sku.stock : ""].filter(Boolean).join(" · ")).filter(Boolean).join("；") : "";
    text("#aiFactTitle", product ? aiProductTitle(product) : "--");
    text("#aiFactPrice", facts.price_display ?? facts.price ?? product?.price_display ?? "--");
    text("#aiFactStock", facts.stock === 0 ? "0" : facts.stock || "未提供");
    text("#aiFactStatus", facts.status || product?.status || "未提供");
    text("#aiFactDescription", facts.description || product?.description || "未提供");
    text("#aiFactSkus", skus || "未提供");
  }

  function renderAiKnowledgeEditor({ preserveText = false } = {}) {
    const product = (state.ai?.products || []).find((item) => aiProductItemId(item) === String(state.ai.selectedItemId || ""));
    const textarea = $("#aiKnowledgeContent");
    const disabled = !product;
    if (textarea) textarea.disabled = disabled;
    ["#aiSaveKnowledge", "#aiDisableKnowledge", "#aiExtractKnowledge"].forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = disabled;
    });
    if ($("#aiRunPreview")) $("#aiRunPreview").disabled = !state.me || Boolean(state.ai.previewBusy);
    text("#aiKnowledgeProductTitle", product ? aiProductTitle(product) : "尚未选择商品");
    renderAiProductFacts(product);
    const status = aiKnowledgeStatus(product, state.ai.knowledge);
    const statusInfo = aiKnowledgeStatusInfo(status);
    const badge = $("#aiKnowledgeStatus");
    if (badge) {
      badge.textContent = product ? statusInfo[0] : "请选择商品";
      badge.className = "badge " + (product ? statusInfo[1] : "badge-muted");
    }
    if (textarea && product && !preserveText) {
      const value = aiKnowledgeContent(state.ai.knowledge);
      textarea.value = value;
      state.ai.baseline.knowledge = value;
      state.ai.dirty.knowledge = false;
    } else if (textarea && !product) {
      textarea.value = "";
      state.ai.baseline.knowledge = "";
    }
    const generatedPending = state.ai.generatedKnowledge?.itemId === String(state.ai.selectedItemId || "")
      && state.ai.generatedKnowledge?.productGeneration === Number(state.ai.productGeneration || 0);
    text("#aiKnowledgeEditMode", generatedPending ? "整理建议待确认" : state.ai.knowledge?.preview_generated ? "已采用，尚未保存" : statusInfo[0]);
    renderAiGeneratedKnowledge();
    renderAiProducts();
  }

  function renderAiConfig({ preserveEditors = false } = {}) {
    if (!$("[data-panel=\"ai-config\"]")) return;
    const account = currentAccount();
    text("#aiConfigShopName", accountLabel(account));
    const connection = state.ai?.connection || {};
    const statusCode = connectionStatusCode(connection, state.ai?.status);
    const statusInfo = connectionStatusInfo(statusCode);
    const overall = $("#aiOverallStatus");
    const aiRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules_ai");
    if (overall) {
      overall.textContent = aiRunning ? "AI 运行中" : statusCode === "verified" ? "AI 已暂停" : statusInfo[0];
      overall.className = "badge " + (aiRunning ? "badge-green" : statusCode === "verified" ? "badge-amber" : statusInfo[1]);
    }
    renderAiConfigSummary();
    if (!preserveEditors) {
      writeAiStoreForm(aiConfigDraft(state.ai.config));
    }
    const configStatus = String(state.ai?.config?.content_status || state.ai?.config?.status || (aiStoreHasContent() ? "saved" : "unconfigured"));
    const personaInfo = aiStoreHasContent() ? aiKnowledgeStatusInfo(configStatus) : ["未填写", "badge-muted"];
    const personaBadge = $("#aiPersonaStatus");
    if (personaBadge) {
      personaBadge.textContent = personaInfo[0];
      personaBadge.className = "badge " + personaInfo[1];
    }
    const search = $("#aiProductSearch");
    if (search && search.value !== state.ai.productSearch) search.value = state.ai.productSearch;
    renderAiTemplates();
    renderAiKnowledgeEditor({ preserveText: preserveEditors });
    renderAiStatus();
  }

  function renderAiConfigSummary() {
    const globalConn = state.settingsAi?.connection;
    const shopConn = state.ai?.connection;
    const badge = $("#aiConnectionBadge");
    const title = $("#aiConnectionSummaryTitle");
    const desc = $("#aiConnectionSummaryText");

    if (globalConn?.verified || globalConn?.provider) {
      if (badge) {
        badge.textContent = "全局已生效";
        badge.className = "badge badge-green";
      }
      if (title) {
        title.textContent = "统一模型: " + (globalConn.model || "") + " (" + (AI_PROVIDER_COPY[globalConn.provider]?.label || globalConn.provider || "") + ")";
      }
      if (desc) {
        let txt = "当前账号已统一配置大模型，供智能客服与智能运维等功能调用。";
        if (shopConn?.model) {
          txt += "（检测到该店铺曾有独立配置，当前已优先使用账号全局统一连接）";
        }
        desc.textContent = txt;
      }
    } else if (shopConn?.verified || shopConn?.provider) {
      if (badge) {
        badge.textContent = "店铺独立生效";
        badge.className = "badge badge-amber";
      }
      if (title) {
        title.textContent = "店铺独立模型: " + (shopConn.model || "") + " (" + (AI_PROVIDER_COPY[shopConn.provider]?.label || shopConn.provider || "") + ")";
      }
      if (desc) {
        desc.textContent = "当前正在使用该店铺的历史独立配置。建议前往系统设置将其升级保存为全局统一连接。";
      }
    } else {
      if (badge) {
        badge.textContent = "未配置";
        badge.className = "badge badge-muted";
      }
      if (title) {
        title.textContent = "尚未配置模型连接";
      }
      if (desc) {
        desc.textContent = "当前账号尚未统一配置 AI 模型连接，智能客服与智能运维暂不可用。点击右侧按钮前往配置。";
      }
    }
  }

  async function loadAiKnowledge(itemId, context = captureAccountContext()) {
    if (!state.me || !accountContextMatches(context)) return;
    const selected = String(itemId || "").trim();
    if (!selected) return;
    const productGeneration = Number(state.ai.productGeneration || 0);
    const generation = ++state.ai.knowledgeGeneration;
    const basePath = "/api/bot/ai/products/" + encodeURIComponent(selected);
    const [knowledge, versions] = await Promise.all([
      accountScopedApi(context, basePath + "/knowledge"),
      accountScopedApi(context, basePath + "/versions").catch(() => ({ versions: [] })),
    ]);
    if (!state.me || !accountContextMatches(context) || generation !== state.ai.knowledgeGeneration || productGeneration !== Number(state.ai.productGeneration || 0) || selected !== state.ai.selectedItemId) return;
    state.ai.knowledge = knowledge?.knowledge || knowledge || null;
    state.ai.versions = Array.isArray(versions?.versions) ? versions.versions : [];
    state.ai.generatedKnowledge = null;
    state.ai.extractionGeneration += 1;
    renderAiKnowledgeEditor();
  }

  async function loadAiConfig({ preserveSelection = true } = {}) {
    if (!state.me) return false;
    const context = captureAccountContext();
    const generation = ++state.ai.loadGeneration;
    const [status, connection, config, templates, products] = await Promise.all([
      accountScopedApi(context, "/api/bot/ai/status"),
      accountScopedApi(context, "/api/bot/ai/connection"),
      accountScopedApi(context, "/api/bot/ai/config"),
      accountScopedApi(context, "/api/bot/ai/templates"),
      accountScopedApi(context, "/api/bot/ai/products"),
    ]);
    if (!state.me || !accountContextMatches(context) || generation !== state.ai.loadGeneration) return false;
    state.ai.status = status || null;
    state.ai.connection = connection?.connection || connection || null;
    state.ai.config = config?.config || config || null;
    state.ai.templates = Array.isArray(templates?.templates) ? templates.templates : [];
    state.ai.products = Array.isArray(products?.products) ? products.products : [];
    state.ai.productGeneration += 1;
    const availableIds = new Set(state.ai.products.map((item) => aiProductItemId(item)));
    if (!preserveSelection || !availableIds.has(String(state.ai.selectedItemId || ""))) {
      state.ai.selectedItemId = aiProductItemId(state.ai.products[0]);
    }
    state.ai.knowledge = null;
    state.ai.versions = [];
    state.ai.generatedKnowledge = null;
    state.ai.extractionGeneration += 1;
    state.ai.verificationToken = "";
    state.ai.testedFingerprint = "";
    renderAiConfig();
    if (state.ai.selectedItemId && accountContextMatches(context) && state.view === "ai-config") {
      await loadAiKnowledge(state.ai.selectedItemId, context);
    }
    return accountContextMatches(context) && generation === state.ai.loadGeneration;
  }

  function settingsAiConnectionFingerprint(candidate) {
    return JSON.stringify([
      candidate.provider,
      candidate.base_url,
      candidate.model,
      candidate.api_key || "",
      Number(state.settingsAi?.connection?.revision || 0),
    ]);
  }

  function invalidateAiConnectionTest() {
    state.ai.verificationToken = "";
    state.ai.testedFingerprint = "";
    state.ai.dirty.connection = false;
    if (state.settingsAi) {
      state.settingsAi.verificationToken = "";
      state.settingsAi.testedFingerprint = "";
      state.settingsAi.draft = {
        provider: $("#aiProvider")?.value || "",
        base_url: $("#aiBaseUrl")?.value || "",
        model: $("#aiModel")?.value || "",
        api_key: $("#aiApiKey")?.value || "",
      };
    }
    if ($("#aiSaveConnection")) $("#aiSaveConnection").disabled = true;
    const connection = state.ai.connection || {};
    state.ai.connection = Object.assign({}, connection, { status: "pending", verified: false });
    renderAiConfig({ preserveEditors: true });
  }

  function validateAiConnectionCandidate(candidate, { requireKey = false } = {}) {
    if (!candidate.provider) throw new ApiError("请选择支持的接口格式");
    if (!candidate.base_url) throw new ApiError("请填写服务地址");
    if (!candidate.model) throw new ApiError("请填写模型名");
    if (requireKey && candidate.provider !== "ollama" && candidate.provider !== "ollama_chat" && !candidate.api_key) {
      throw new ApiError("请输入当前接口的 API Key");
    }
  }

  async function testUnifiedAiConnection() {
    const settings = state.settingsAi;
    if (!state.me || settings.operation) return;
    const candidate = {
      provider: $("#aiProvider")?.value || "",
      base_url: String($("#aiBaseUrl")?.value || "").trim(),
      model: String($("#aiModel")?.value || "").trim(),
      api_key: String($("#aiApiKey")?.value || "").trim(),
    };
    try {
      const hasConfiguredKey = Boolean(
        candidate.api_key ||
        (state.settingsAi?.connection?.api_key_configured && state.settingsAi?.connection?.provider === candidate.provider)
      );
      validateAiConnectionCandidate(candidate, { requireKey: !hasConfiguredKey });
    } catch (error) {
      formMessage("#aiConnectionMessage", error.message);
      return;
    }
    const button = $("#aiTestConnection");
    const fingerprint = settingsAiConnectionFingerprint(candidate);
    settings.operation = "test";
    setBusy(button, true);
    formMessage("#aiConnectionMessage", "正在测试统一模型连接…");
    try {
      const payload = {
        provider: candidate.provider,
        base_url: candidate.base_url,
        model: candidate.model,
        expected_revision: Number(state.settingsAi?.connection?.revision || 0),
      };
      if (candidate.api_key) payload.api_key = candidate.api_key;
      const result = await api("/api/settings/ai/connection/test", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (state.settingsAi !== settings || !state.me || fingerprint !== settingsAiConnectionFingerprint(aiConnectionCandidate())) return;
      if (result?.ok !== true || result?.status !== "verified" || !result.verification_token) throw new ApiError("连接测试未返回有效凭证");
      settings.verificationToken = result.verification_token;
      settings.testedFingerprint = fingerprint;
      if ($("#aiSaveConnection")) $("#aiSaveConnection").disabled = false;
      renderSettingsAiPanel();
      renderAiConfigSummary();
      updateOpsConnectionBadge();
      formMessage("#aiConnectionMessage", "连接测试成功，请点击“保存统一连接”以生效", true);
      showToast("模型连接测试成功");
    } catch (error) {
      if (state.settingsAi !== settings || !state.me) return;
      settings.verificationToken = "";
      settings.testedFingerprint = "";
      if ($("#aiSaveConnection")) $("#aiSaveConnection").disabled = true;
      formMessage("#aiConnectionMessage", error.message || "模型连接测试失败，请核对配置");
    } finally {
      if (state.settingsAi === settings) { settings.operation = ""; setBusy(button, false); renderSettingsAiPanel(); }
    }
  }

  async function saveUnifiedAiConnection(event) {
    if (event) event.preventDefault();
    const settings = state.settingsAi;
    if (!state.me || settings.operation) return;
    const candidate = {
      provider: $("#aiProvider")?.value || "",
      base_url: String($("#aiBaseUrl")?.value || "").trim(),
      model: String($("#aiModel")?.value || "").trim(),
      api_key: String($("#aiApiKey")?.value || "").trim(),
    };
    try {
      const hasConfiguredKey = Boolean(
        candidate.api_key ||
        (state.settingsAi?.connection?.api_key_configured && state.settingsAi?.connection?.provider === candidate.provider)
      );
      validateAiConnectionCandidate(candidate, { requireKey: !hasConfiguredKey });
      if (!state.settingsAi.verificationToken || state.settingsAi.testedFingerprint !== settingsAiConnectionFingerprint(candidate)) {
        throw new ApiError("请先点击“测试连接”并测试通过后再保存");
      }
    } catch (error) {
      formMessage("#aiConnectionMessage", error.message);
      return;
    }
    const button = $("#aiSaveConnection") || event?.submitter;
    settings.operation = "save";
    const submittedFingerprint = settingsAiConnectionFingerprint(candidate);
    setBusy(button, true);
    try {
      const payload = {
        provider: candidate.provider,
        base_url: candidate.base_url,
        model: candidate.model,
        verification_token: state.settingsAi.verificationToken,
        expected_revision: Number(state.settingsAi?.connection?.revision || 0),
        confirm: true,
      };
      if (candidate.api_key) payload.api_key = candidate.api_key;
      const result = await api("/api/settings/ai/connection", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      if (state.settingsAi !== settings || !state.me) return;
      if (!result?.connection || result.ok !== true) throw new ApiError("连接保存结果无效，请刷新确认");
      const unchanged = submittedFingerprint === settingsAiConnectionFingerprint(aiConnectionCandidate());
      settings.connection = result.connection;
      settings.verificationToken = "";
      settings.testedFingerprint = "";
      if (unchanged) {
        settings.draft = null;
        if ($("#aiApiKey")) $("#aiApiKey").value = "";
      }
      renderSettingsAiPanel();
      renderAiConfigSummary();
      updateOpsConnectionBadge();
      formMessage("#aiConnectionMessage", unchanged ? "统一模型连接已保存，供本人各功能使用" : "已保存提交的连接；编辑区仍有未保存修改", true);
    } catch (error) {
      if (state.settingsAi === settings && state.me) {
        settings.verificationToken = "";
        settings.testedFingerprint = "";
        formMessage("#aiConnectionMessage", error.message || "连接保存失败");
      }
    } finally {
      if (state.settingsAi === settings) {
        settings.operation = "";
        setBusy(button, false);
        renderSettingsAiPanel();
        button.disabled = !settings.verificationToken;
      }
    }
  }

  async function deleteUnifiedAiConnection() {
    const button = $("#aiDeleteKey");
    const settings = state.settingsAi;
    if (!state.me || settings.operation || !button || button.disabled) return;
    settings.operation = "delete";
    setBusy(button, true);
    try {
      const result = await api("/api/settings/ai/connection", {
        method: "DELETE",
        body: JSON.stringify({ confirm: true, expected_revision: Number(settings.connection?.revision || 0) }),
      });
      if (state.settingsAi !== settings || !state.me) return;
      if (result?.ok !== true || !result.connection) throw new ApiError("删除结果未确认，请刷新设置");
      settings.connection = result.connection;
      settings.verificationToken = "";
      settings.testedFingerprint = "";
      settings.draft = null;
      $("#aiApiKey").value = "";
      $("#aiSaveConnection").disabled = true;
      renderSettingsAiPanel();
      renderAiConfigSummary();
      updateOpsConnectionBadge();
      formMessage("#aiConnectionMessage", "统一模型密钥与连接已删除", true);
    } catch (error) {
      if (state.settingsAi === settings && state.me) formMessage("#aiConnectionMessage", error.message || "删除失败，请重试");
    } finally {
      if (state.settingsAi === settings) { settings.operation = ""; setBusy(button, false); renderSettingsAiPanel(); }
    }
  }

  function confirmDeleteAiKey() {
    text("#confirmTitle", "删除全局统一大模型 API Key");
    text("#confirmMessage", "删除后当前账号的统一 AI 连接将被清除，智能客服与智能运维将无法调用大模型。是否继续？");
    text("#confirmAction", "确认删除 Key");
    state.confirmAction = async () => {
      await deleteUnifiedAiConnection();
      closeDialog("confirmDialog");
    };
    openDialog("confirmDialog");
  }

  const testAiConnection = testUnifiedAiConnection;
  const saveAiConnection = saveUnifiedAiConnection;

  async function saveAiPersona() {
    const config = aiStoreFormValue();
    if (!hasMeaningfulAIText(config.store_content)) {
      formMessage("#aiPersonaMessage", "请填写有实际信息的店铺与客服说明，空内容不会生效");
      return;
    }
    const button = $("#aiSavePersona");
    const context = captureAccountContext();
    setBusy(button, true);
    try {
      const expectedRevision = Number(state.ai.config?.revision || 0);
      const result = await accountScopedApi(context, "/api/bot/ai/config", {
        method: "PUT",
        body: JSON.stringify({ ...config, expected_revision: expectedRevision }),
      });
      if (!accountContextMatches(context)) return;
      state.ai.config = result?.config || result || { draft: config, status: "saved" };
      writeAiStoreForm(aiConfigDraft(state.ai.config));
      renderAiConfig({ preserveEditors: true });
      formMessage("#aiPersonaMessage", "店铺客服内容已保存并生效", true);
      showToast("店铺客服内容已保存并生效");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#aiPersonaMessage", error.message || "店铺客服内容保存失败");
    } finally {
      if (accountContextMatches(context)) setBusy(button, false);
    }
  }

  async function selectAiProduct(itemId) {
    const selected = String(itemId || "");
    if (!selected || selected === state.ai.selectedItemId) return;
    if (state.ai.dirty.knowledge && !confirmDiscardAiChanges("商品")) return;
    state.ai.selectedItemId = selected;
    state.ai.productGeneration += 1;
    state.ai.knowledgeGeneration += 1;
    state.ai.knowledgeRequestGeneration += 1;
    state.ai.extractionGeneration += 1;
    state.ai.knowledge = null;
    state.ai.versions = [];
    state.ai.generatedKnowledge = null;
    state.ai.baseline.knowledge = "";
    state.ai.dirty.knowledge = false;
    clearAiProductTransientUi();
    renderAiKnowledgeEditor();
    try {
      await loadAiKnowledge(selected);
    } catch (error) {
      if (String(state.ai.selectedItemId) === selected) {
        formMessage("#aiKnowledgeMessage", error.message || "商品客服内容读取失败");
        showToast(error.message || "商品客服内容读取失败", "error");
      }
    }
  }

  async function extractAiKnowledge() {
    const itemId = String(state.ai.selectedItemId || "");
    const source = String($("#aiExtractInput")?.value || $("#aiKnowledgeContent")?.value || "").trim();
    if (!itemId) return;
    if (!hasMeaningfulAIText(source)) {
      formMessage("#aiKnowledgeMessage", "请先输入有实际信息的商品说明");
      return;
    }
    const button = $("#aiExtractKnowledge");
    const scope = captureAiProductScope(itemId);
    const generation = ++state.ai.extractionGeneration;
    state.ai.generatedKnowledge = null;
    renderAiGeneratedKnowledge();
    setBusy(button, true);
    try {
      const result = await accountScopedApi(scope.account, "/api/bot/ai/products/" + encodeURIComponent(itemId) + "/extract", {
        method: "POST",
        body: JSON.stringify({ content: source }),
      });
      if (!aiProductScopeMatches(scope) || generation !== state.ai.extractionGeneration) return;
      const content = generatedContentFromResult(result);
      state.ai.generatedKnowledge = { itemId, productGeneration: scope.productGeneration, content };
      renderAiKnowledgeEditor({ preserveText: true });
      formMessage("#aiKnowledgeMessage", "整理建议已返回，请预览后决定是否采用；编辑区和已保存内容均未修改", true);
      showToast("整理建议已生成，等待采用");
    } catch (error) {
      if (aiProductScopeMatches(scope) && generation === state.ai.extractionGeneration) formMessage("#aiKnowledgeMessage", error.message || "AI 整理失败");
    } finally {
      if (aiProductScopeMatches(scope) && generation === state.ai.extractionGeneration) setBusy(button, false);
    }
  }

  async function saveAiKnowledge() {
    const itemId = String(state.ai.selectedItemId || "");
    const content = String($("#aiKnowledgeContent")?.value || "").trim();
    if (!hasMeaningfulAIText(content)) {
      formMessage("#aiKnowledgeMessage", "请填写有实际信息的商品补充内容；空白内容不会生效");
      return false;
    }
    const button = $("#aiSaveKnowledge");
    const scope = captureAiProductScope(itemId);
    const requestGeneration = ++state.ai.knowledgeRequestGeneration;
    setBusy(button, true);
    try {
      const expectedRevision = Number(state.ai.knowledge?.revision || 0);
      const result = await accountScopedApi(scope.account, "/api/bot/ai/products/" + encodeURIComponent(itemId) + "/knowledge", {
        method: "PUT",
        body: JSON.stringify({ content, expected_revision: expectedRevision }),
      });
      if (!aiProductScopeMatches(scope) || requestGeneration !== state.ai.knowledgeRequestGeneration) return false;
      state.ai.knowledge = result?.knowledge || result || { content, status: "saved" };
      $("#aiKnowledgeContent").value = aiKnowledgeContent(state.ai.knowledge) || content;
      state.ai.baseline.knowledge = $("#aiKnowledgeContent").value;
      state.ai.dirty.knowledge = false;
      state.ai.generatedKnowledge = null;
      renderAiKnowledgeEditor({ preserveText: true });
      formMessage("#aiKnowledgeMessage", "商品补充内容已保存并用于回答", true);
      showToast("商品补充内容已保存并用于回答");
      return true;
    } catch (error) {
      if (aiProductScopeMatches(scope) && requestGeneration === state.ai.knowledgeRequestGeneration) formMessage("#aiKnowledgeMessage", error.message || "商品补充内容保存失败");
      return false;
    } finally {
      if (aiProductScopeMatches(scope) && requestGeneration === state.ai.knowledgeRequestGeneration) setBusy(button, false);
    }
  }

  function confirmDisableAiKnowledge() {
    const itemId = String(state.ai.selectedItemId || "");
    if (!itemId) return;
    const context = captureAccountContext();
    text("#confirmTitle", "停用商品补充内容");
    text("#confirmMessage", "停用后 AI 不再使用这份商品补充内容，但仍可依据实时商品事实和店铺内容回答。是否继续？");
    text("#confirmAction", "确认停用");
    state.confirmAction = async () => {
      const result = await accountScopedApi(context, "/api/bot/ai/products/" + encodeURIComponent(itemId) + "/disable", {
        method: "POST",
        body: JSON.stringify({
          confirm: true,
          expected_revision: Number(state.ai.knowledge?.revision || 0),
        }),
      });
      if (!accountContextMatches(context) || itemId !== state.ai.selectedItemId) return;
      state.ai.knowledge = result?.knowledge || result || Object.assign({}, state.ai.knowledge, { status: "disabled" });
      renderAiKnowledgeEditor({ preserveText: true });
      showToast("商品补充内容已停用");
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function previewSources(result) {
    const raw = result?.sources || result?.used_sources || result?.context_sources || [];
    const values = Array.isArray(raw) ? raw : Object.entries(raw || {}).filter(([, used]) => Boolean(used)).map(([key]) => key);
    const labels = {
      realtime_facts: "实时事实", product_facts: "实时事实", facts: "实时事实",
      store_content: "店铺内容", store: "店铺内容",
      product_content: "商品补充", product_knowledge: "商品补充", knowledge: "商品补充",
      conversation: "会话", history: "会话", session: "会话",
    };
    return Array.from(new Set(values.map((value) => labels[String(value)] || String(value)).filter(Boolean)));
  }

  function renderAiPreviewHistory() {
    const host = $("#aiPreviewHistory");
    if (!host) return;
    const history = Array.isArray(state.ai?.previewHistory) ? state.ai.previewHistory.slice(-6) : [];
    host.innerHTML = history.length ? history.map((message) => '<div class="ai-preview-turn is-' + (message.role === "assistant" ? "assistant" : "user") + '"><strong>' + (message.role === "assistant" ? "客服" : "买家") + '</strong><span>' + esc(message.content) + "</span></div>").join("") : "<span>还没有模拟对话。</span>";
  }

  function clearAiPreview() {
    state.ai.previewGeneration += 1;
    state.ai.previewHistory = [];
    state.ai.previewBusy = false;
    setBusy($("#aiRunPreview"), false);
    if ($("#aiPreviewInput")) $("#aiPreviewInput").value = "";
    if ($("#aiPreviewOutput")) $("#aiPreviewOutput").innerHTML = "<span>回复后会显示实际回复、使用资料、内容状态与安全状态。</span>";
    renderAiPreviewHistory();
  }

  async function runAiPreview() {
    if (!state.me || state.ai.previewBusy) return;
    const itemId = String(state.ai.selectedItemId || "");
    const question = String($("#aiPreviewInput")?.value || "").trim();
    if (!hasMeaningfulText(question)) {
      showToast("请输入当前买家问题", "warning");
      return;
    }
    const button = $("#aiRunPreview");
    const output = $("#aiPreviewOutput");
    const scope = captureAiProductScope(itemId);
    const generation = ++state.ai.previewGeneration;
    const history = (state.ai.previewHistory || []).slice(-6).map((message) => ({ role: message.role, content: message.content }));
    const storeConfig = aiStoreFormValue();
    const payload = { buyer_message: question, store_config: storeConfig, history };
    if (itemId) {
      payload.item_id = itemId;
      payload.knowledge = { content: String($("#aiKnowledgeContent")?.value || "").trim() };
    }
    state.ai.previewBusy = true;
    setBusy(button, true);
    output.innerHTML = "<span>正在使用当前草稿模拟回复，不会保存配置或发送闲鱼消息…</span>";
    try {
      const result = await accountScopedApi(scope.account, "/api/bot/ai/preview", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!aiProductScopeMatches(scope) || generation !== state.ai.previewGeneration) return;
      if (result?.sent !== false) throw new ApiError("沙盘响应无效，请稍后重试");
      const reply = String(result?.reply?.content || result?.reply || result?.answer || "").trim();
      if (!reply && result?.decision === "no_reply") {
        const reason = String(result.reason_code || "");
        const message = {
          connection_unconfigured: "请先在设置中测试并保存统一模型连接。",
          connection_unverified: "模型连接尚未验证，请在设置中重新测试并保存。",
          dns_fake_ip: "模型域名被解析为 Fake-IP，请先修正 DNS。",
          revision_conflict: "模型连接已变更，请重新测试。",
        }[reason] || AI_CONNECTION_STATUS_COPY[reason]?.[2];
        if (message) throw new ApiError(message, 0, reason);
      }
      const sources = previewSources(result);
      const knowledgeStatus = String(result?.knowledge_status || result?.content_status || aiKnowledgeStatus(null, state.ai.knowledge));
      const safety = String(result?.safety_status || result?.safety?.status || result?.safety || "已通过安全检查");
      state.ai.previewHistory = history.concat([{ role: "user", content: question }, ...(reply ? [{ role: "assistant", content: reply }] : [])]).slice(-6);
      if (String($("#aiPreviewInput").value).trim() === question) $("#aiPreviewInput").value = "";
      renderAiPreviewHistory();
      output.innerHTML = '<div class="ai-preview-answer"><strong>实际回复</strong><div>' + esc(reply || "本次未生成可发送回复，请转人工处理") + '</div></div><div class="ai-preview-details"><div><strong>使用资料</strong><span>' + esc(sources.length ? sources.join("、") : "未标明") + '</span></div><div><strong>内容状态</strong><span>' + esc(aiKnowledgeStatusInfo(knowledgeStatus)[0]) + '</span></div><div><strong>安全状态</strong><span>' + esc(safety) + "</span></div></div>";
    } catch (error) {
      if (aiProductScopeMatches(scope) && generation === state.ai.previewGeneration) output.innerHTML = '<span>沙盘测试失败：' + esc(error.message || "请稍后重试") + "</span>";
    } finally {
      if (aiProductScopeMatches(scope) && generation === state.ai.previewGeneration) {
        state.ai.previewBusy = false;
        setBusy(button, false);
      }
    }
  }

  const AUTOMATION_MUTATION_SELECTORS = [
    "#saveReplyRuleButton",
    "#cancelReplyRuleEdit",
    "#replyRuleList [data-edit-rule]",
    "#replyRuleList [data-remove-rule]",
    "[data-open-batch-delivery]",
    "[data-edit-delivery]",
    "[data-delivery-toggle]",
    "#batchDeliveryCheck",
    "#batchDeliveryCommit",
    "#saveAutomationButton",
    "#chatAiStart",
    "#chatAiStop",
  ];

  function setAutomationMutationBusy(kind, busy) {
    if (!Object.prototype.hasOwnProperty.call(state.automationMutations, kind)) return;
    state.automationMutations[kind] = Boolean(busy);
    const anyBusy = Object.values(state.automationMutations).some(Boolean);
    AUTOMATION_MUTATION_SELECTORS.forEach((selector) => {
      $$(selector).forEach((node) => { node.disabled = anyBusy; });
    });
    if (!anyBusy) {
      const commit = $("#batchDeliveryCommit");
      if (commit) commit.disabled = !state.batchDelivery.previewToken;
      $$('[data-open-batch-delivery]').forEach((button) => { button.disabled = !state.products.length; });
    }
  }

  function beginAutomationMutation(kind, messageSelector = "#automationMessage") {
    const activeKind = Object.keys(state.automationMutations).find((candidate) => state.automationMutations[candidate]);
    if (activeKind) {
      formMessage(
        messageSelector,
        activeKind === kind ? "同类设置正在保存，请稍后再试" : "另一项自动化设置正在保存，请稍后再试",
      );
      return null;
    }
    const owner = {
      kind,
      generation: ++state.automationMutationGeneration,
      context: captureAccountContext(),
    };
    state.automationMutationOwner = owner;
    state.automationLoadGeneration += 1;
    setAutomationMutationBusy(kind, true);
    return owner;
  }

  function endAutomationMutation(owner) {
    if (!owner || state.automationMutationOwner !== owner) return;
    state.automationMutationOwner = null;
    state.automationLoadGeneration += 1;
    setAutomationMutationBusy(owner.kind, false);
  }

  function resetAutomationMutations() {
    state.automationMutationGeneration += 1;
    state.automationMutationOwner = null;
    state.automationLoadGeneration += 1;
    Object.keys(state.automationMutations).forEach((kind) => {
      state.automationMutations[kind] = false;
    });
    setAutomationMutationBusy("rules", false);
  }

  function resetConversationCommands() {
    state.conversationCommands.generation += 1;
    state.conversationCommands.read.clear();
    state.conversationCommands.takeover.clear();
  }

  function beginConversationCommand(kind, chatId) {
    const generation = ++state.conversationCommands.generation;
    const context = Object.assign(captureAccountContext(), {
      kind,
      chatId: String(chatId || ""),
      generation,
    });
    state.conversationCommands[kind].set(context.chatId, generation);
    return context;
  }

  function conversationCommandMatches(context) {
    return accountContextMatches(context)
      && state.conversationCommands[context.kind]?.get(context.chatId) === context.generation;
  }

  function revokeManualReplyAttachmentPreview(attachment) {
    const previewUrl = String(attachment?.previewUrl || "");
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    if (attachment) attachment.previewUrl = "";
  }

  async function deleteManualReplyUploadedMedia(media, accountKey = state.activeAccountKey, { strict = false } = {}) {
    const path = String(media?.path || "").trim();
    if (!path) return false;
    try {
      await api("/api/bot/messages/image", {
        method: "DELETE",
        headers: accountKey ? { "X-Shop-Account": accountKey } : {},
        body: JSON.stringify({ path }),
        timeoutMs: MANUAL_REPLY_DELETE_TIMEOUT_MS,
        suppressSessionReset: true,
      });
      return true;
    } catch (error) {
      if (strict) throw error;
      return false;
    }
  }

  async function cleanupManualReplyUploadedMedia(accountKey = state.activeAccountKey) {
    const uploaded = state.manualReply.attachments.filter((attachment) => attachment?.media?.path);
    if (!uploaded.length) return true;
    state.manualReply.cleaning = true;
    renderChat();
    let firstError = null;
    try {
      for (const attachment of uploaded) {
        try {
          await deleteManualReplyUploadedMedia(attachment.media, accountKey, { strict: true });
          attachment.media = null;
        } catch (error) {
          if (!firstError) firstError = error;
        }
      }
    } finally {
      state.manualReply.cleaning = false;
      renderChat();
    }
    if (firstError) throw firstError;
    return true;
  }

  function manualReplyOperationInFlight() {
    return Boolean(state.manualReply.operation || state.manualReply.destructiveOperation);
  }

  function waitForManualReplyOperation(operation) {
    if (!operation?.promise) return Promise.resolve();
    let timer = 0;
    const timeout = new Promise((_, reject) => {
      timer = window.setTimeout(() => {
        reject(new ApiError("当前回复请求超时，未执行操作，请刷新后重试", 408, "manual_reply_operation_timeout"));
      }, MANUAL_REPLY_OPERATION_WAIT_TIMEOUT_MS);
    });
    return Promise.race([operation.promise, timeout]).finally(() => {
      if (timer) window.clearTimeout(timer);
    });
  }

  async function prepareManualReplyForDestructiveAction(accountKey = state.activeAccountKey) {
    const operation = state.manualReply.operation;
    if (operation) {
      operation.cancelled = true;
      await waitForManualReplyOperation(operation);
    }
    return cleanupManualReplyUploadedMedia(accountKey);
  }

  function runManualReplyDestructiveAction(task) {
    if (state.manualReply.destructiveOperation) return state.manualReply.destructiveOperation;
    const operation = Promise.resolve().then(task);
    state.manualReply.destructiveOperation = operation;
    operation.then(
      () => { if (state.manualReply.destructiveOperation === operation) state.manualReply.destructiveOperation = null; },
      () => { if (state.manualReply.destructiveOperation === operation) state.manualReply.destructiveOperation = null; },
    );
    return operation;
  }

  function releaseManualReplyAttachments({ cleanupUploaded = false, accountKey = state.activeAccountKey } = {}) {
    const attachments = state.manualReply.attachments.splice(0);
    const cleanupTasks = [];
    attachments.forEach((attachment) => {
      revokeManualReplyAttachmentPreview(attachment);
      if (cleanupUploaded && attachment?.media?.path) {
        cleanupTasks.push(deleteManualReplyUploadedMedia(attachment.media, accountKey));
      }
    });
    return cleanupTasks;
  }

  function setManualReplyDragActive(active) {
    state.manualReply.dragging = Boolean(active);
    $("#manualReplyForm")?.classList.toggle("is-drag-active", state.manualReply.dragging);
    $("#manualReplyDropzone")?.classList.toggle("is-drag-active", state.manualReply.dragging);
    $(".chat-window")?.classList.toggle("is-drag-active", state.manualReply.dragging);
  }

  function resetManualReplyContext({ clearInput = true, cleanupUploaded = true, accountKey = state.activeAccountKey, force = false } = {}) {
    const activeOperation = state.manualReply.operation;
    if (activeOperation && !force) {
      activeOperation.cancelled = true;
      return waitForManualReplyOperation(activeOperation)
        .then(() => resetManualReplyContext({ clearInput, cleanupUploaded, accountKey, force: true }))
        .catch((error) => {
          if (error?.code === "manual_reply_operation_timeout") {
            showToast("当前回复仍在处理中，未执行清理操作", "error");
          }
          return false;
        });
    }
    const shouldCleanupUploaded = cleanupUploaded && (!state.manualReply.submitting || state.manualReply.uploading);
    state.manualReply.generation += 1;
    state.manualReply.request = null;
    const cleanupTasks = releaseManualReplyAttachments({ cleanupUploaded: shouldCleanupUploaded, accountKey });
    setManualReplyDragActive(false);
    state.manualReply.submitting = false;
    state.manualReply.uploading = false;
    state.manualReply.uploadingIndex = -1;
    state.manualReply.polling.clear();
    const input = $("#manualReplyInput");
    const file = $("#manualReplyFile");
    if (clearInput && input) input.value = "";
    if (file) file.value = "";
    formMessage("#replyMessage", "");
    setBusy($("#manualReplyForm button[type=submit]"), false);
    renderManualReplyAttachment();
    return Promise.allSettled(cleanupTasks);
  }

  function manualReplyContextMatches(chatId, epoch, accountKey, generation) {
    return epoch === state.accountEpoch
      && accountKey === state.activeAccountKey
      && String(chatId || "") === String(state.selectedChatId || "")
      && generation === state.manualReply.generation;
  }

  async function readResponse(response) {
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json")
      ? await response.json().catch(() => ({}))
      : await response.text().catch(() => "");
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail : data;
      const message = detail && typeof detail === "object" ? detail.message : detail;
      const code = detail && typeof detail === "object" ? detail.code || "" : "";
      throw new ApiError(message || "请求失败（" + response.status + "）", response.status, code, detail);
    }
    return data;
  }

  const COOKIE_ERROR_COPY = {
    risk_control: "最近一次自动连接请求未通过接口校验或被限制，尚不能确认是否需要安全验证。",
    verification_required: "自动连接接口返回了明确的验证要求，请在对应的闲鱼官方页面按提示处理；App 不一定弹窗。",
    risk_cooldown: "系统因之前的受限请求暂停了检测，请等待冷却结束后再试。",
    cookie_expired: "登录会话已失效，请使用闲鱼 App 重新扫码授权。",
    cookie_invalid: "登录信息无效，请重新登录闲鱼并连接。",
    cookie_incomplete: "登录信息不完整，请重新登录闲鱼并连接。",
    qr_query_failed: "二维码状态确认失败，请刷新二维码重试。",
    login_confirm_failed: "扫码确认成功，但闲鱼登录确认失败，请刷新二维码重试。",
    mtop_context_failed: "扫码确认成功，但登录上下文初始化失败，请刷新二维码重试。",
    qr_cookie_incomplete: "扫码确认成功，但登录信息不完整，请刷新二维码重试。",
    unconfigured: "还没有连接店铺，请先登录闲鱼。",
    sync_cooldown: "检测过于频繁，已进入冷却，请稍后再试。",
    sync_busy: "已有店铺检测正在进行，请等待本次检测完成。",
    network_error: "暂时无法连接闲鱼：请稍后重新检测。",
    platform_busy: "闲鱼当前请求繁忙，系统会降低频率后再试。",
    platform_error: "闲鱼暂时无法识别账号：请稍后重新检测。",
    sync_error: "暂时无法确认登录状态：请稍后重新检测。",
    account_restricted: "闲鱼限制了当前账号的部分操作，暂时不能发布商品。",
  };

  const COOKIE_STATUS_LABELS = {
    unconfigured: "未连接",
    pending: "待检测",
    verified: "已验证",
    risk_control: "接口请求受限",
    verification_required: "接口要求验证",
    risk_cooldown: "请求保护冷却中",
    cookie_expired: "登录已失效",
    cookie_invalid: "需要重新登录",
    cookie_incomplete: "需要重新登录",
    account_restricted: "部分能力受限",
  };

  const COOKIE_STATUS_ACTIONS = {
    risk_control: "稍后重新检测；仅在闲鱼明确提示时处理验证",
    verification_required: "在对应的闲鱼官方页面按提示验证后重新检测",
    risk_cooldown: "等待本地请求保护冷却结束后重新检测",
    cookie_expired: "重新扫码授权后自动恢复服务",
    cookie_invalid: "重新登录闲鱼后自动连接",
    cookie_incomplete: "重新登录闲鱼后自动连接",
    account_restricted: "请在闲鱼官方页面查看处理通知",
  };

  const COOKIE_BLOCKING_CODES = new Set([
    "risk_control", "risk_cooldown", "verification_required", "cookie_expired", "cookie_invalid", "cookie_incomplete", "account_restricted",
  ]);

  function cookieErrorMessage(error) {
    return COOKIE_ERROR_COPY[error?.code] || error?.message || "登录状态检测失败，请稍后重试。";
  }

  function cookieStatusInfo(bot) {
    const authCode = ["risk_control", "verification_required"].includes(bot?.auth_code)
      ? bot.auth_code
      : bot?.auth_code === "session_expired"
        ? "cookie_expired"
        : "";
    const fallbackCode = bot?.reauthorization_required
      ? (authCode || "cookie_expired")
      : bot?.sync_status || (bot?.cookies_set ? "pending" : "unconfigured");
    const status = bot?.cookie_status && typeof bot.cookie_status === "object" ? bot.cookie_status : {};
    const code = bot?.reauthorization_required ? fallbackCode : status.code || fallbackCode;
    const fallback = COOKIE_ERROR_COPY[code] || status.message || "暂时无法确认登录状态，请稍后重新检测。";
    return {
      code,
      label: COOKIE_STATUS_LABELS[code] || status.label || "需要处理",
      message: COOKIE_ERROR_COPY[code] || status.message || fallback,
      action: COOKIE_STATUS_ACTIONS[code] || status.action || (COOKIE_BLOCKING_CODES.has(code) ? "处理后重新检测" : ""),
      checked_at: status.checked_at || "",
    };
  }

  function shopStateView(bot = {}) {
    const cookie = cookieStatusInfo(bot);
    const code = cookie.code;
    const blocking = COOKIE_BLOCKING_CODES.has(code);
    // Legacy risk states did not distinguish an interface rejection from a
    // real verification challenge. Keep them blocked without asserting one.
    const connection = ["risk_control", "risk_cooldown"].includes(code) ? "degraded" : bot.connection_state || (
      !bot.cookies_set ? "unconfigured" :
        code === "account_restricted" ? "connected" :
          code === "verified" && bot.connected !== false ? "connected" :
            code === "pending" ? "checking" :
              code === "verification_required" ? "security_check" :
                code === "cookie_expired" || code === "cookie_invalid" || code === "cookie_incomplete" ? "reauth_required" :
                  bot.connected ? "connected" : "degraded"
    );
    const productCount = Number(bot.product_count || 0);
    const catalog = bot.catalog_state || (
      code === "account_restricted" || code === "risk_control" || code === "risk_cooldown" || code === "verification_required" ? (productCount ? "stale" : "blocked") :
        code === "pending" ? "syncing" :
          code === "verified" ? (productCount ? "ready" : "empty") :
            !bot.cookies_set ? "not_started" : "unavailable"
    );
    const restricted = code === "account_restricted" || bot.publish_state === "blocked";
    const copy = {
      unconfigured: {
        action: "连接店铺",
        title: "连接闲鱼店铺",
        description: "在闲鱼官方页面完成登录，店铺和商品会自动识别。",
        button: "连接闲鱼店铺",
        hint: "登录成功后自动识别店铺和商品。",
      },
      checking: {
        action: "查看检测进度",
        title: "正在识别店铺",
        description: "登录状态已提交，系统正在整理店铺和商品。",
        button: "检测进行中",
        hint: "请稍候，完成后会自动更新状态。",
      },
      connected: {
        action: "管理店铺",
        title: restricted ? "账号已连接，但发布受限" : "店铺已连接",
        description: restricted ? "登录仍然有效；闲鱼当前限制了部分操作。" : "店铺已连接，商品和自动规则可以继续管理。",
        button: restricted ? "重新检测状态" : "重新连接店铺",
        hint: restricted ? "请先在闲鱼官方页面处理账号通知，再重新检测。" : "需要更换账号时，可重新连接店铺。",
      },
      security_check: {
        action: "按接口提示验证",
        title: "自动连接接口要求验证",
        description: "接口返回了明确的验证要求，请在对应的闲鱼官方页面处理；App 不一定弹窗。",
        button: "重新检测店铺",
        hint: "按官方页面提示处理后重新检测，系统不会绕过平台限制。",
      },
      reauth_required: {
        action: "重新扫码授权",
        title: "店铺需要重新授权",
        description: "当前登录会话已失效，请使用闲鱼 App 重新扫码授权。授权成功后，自动回复会自动恢复。",
        button: "重新扫码授权",
        hint: "扫码和确认只在闲鱼官方页面完成。",
      },
      degraded: {
        action: "重新检测店铺",
        title: "店铺连接需要确认",
        description: "暂时无法完成最新检测，已有商品不会被清空。",
        button: "重新检测店铺",
        hint: "可稍后重新检测，系统会保留上次成功结果。",
      },
    };
    const selected = copy[connection] || copy.degraded;
    return {
      code,
      cookie,
      connection,
      catalog,
      restricted,
      productCount,
      selected,
      canSync: bot.capabilities?.sync_products !== false && !["checking", "security_check"].includes(connection),
      canPublish: bot.capabilities?.publish_products === true && !restricted,
      hasSnapshot: Boolean(bot.products_set || productCount || catalog === "empty" || catalog === "stale" || catalog === "ready"),
      blocking,
    };
  }

  async function api(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const headers = Object.assign(
      options.body ? { "Content-Type": "application/json" } : {},
      method === "GET" || method === "HEAD" || method === "OPTIONS"
        ? {}
        : { "X-SaaS-Browser-Intent": "browser-write" },
      options.headers || {},
    );
    // The backend validates this scope against the signed-in user.  Keeping
    // it in one request helper prevents a page action from accidentally
    // reading or writing the previous shop after an account switch.
    if (state.activeAccountKey && path.startsWith("/api/") && !headers["X-Shop-Account"]) {
      headers["X-Shop-Account"] = state.activeAccountKey;
    }
    let timeoutMs = Number(options.timeoutMs || 0);
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0) timeoutMs = 0;
    const fetchOptions = Object.assign({}, options, {
      credentials: "same-origin",
      headers,
    });
    delete fetchOptions.timeoutMs;
    const suppressSessionReset = Boolean(fetchOptions.suppressSessionReset);
    delete fetchOptions.suppressSessionReset;
    let controller = null;
    let detachAbort = null;
    if (timeoutMs > 0 && typeof AbortController === "function") {
      controller = new AbortController();
      const originalSignal = options.signal;
      if (originalSignal) {
        if (originalSignal.aborted) controller.abort();
        else {
          const onAbort = () => controller.abort();
          originalSignal.addEventListener("abort", onAbort, { once: true });
          detachAbort = () => originalSignal.removeEventListener("abort", onAbort);
        }
      }
      fetchOptions.signal = controller.signal;
    }
    const clearRequestTimer = () => {
      if (detachAbort) detachAbort();
      detachAbort = null;
    };
    let timeoutTimer = 0;
    let timedOutByPromise = false;
    const requestPromise = fetch(API_PREFIX + path, fetchOptions).then(async (response) => {
      if (response.status === 401 && !path.startsWith("/api/auth/") && !suppressSessionReset) {
        clearSession(false);
        throw new ApiError("登录已过期，请重新登录", 401);
      }
      return readResponse(response);
    });
    // A timed-out fetch may reject after the race settles; attach a handler so
    // cancellation never becomes an unhandled browser rejection.
    requestPromise.catch(() => {});
    try {
      if (timeoutMs > 0) {
        const timeoutPromise = new Promise((_, reject) => {
          timeoutTimer = window.setTimeout(() => {
            timedOutByPromise = true;
            if (controller) controller.abort();
            reject(new ApiError("请求超时，请稍后重试", 408, "request_timeout"));
          }, timeoutMs);
        });
        return await Promise.race([requestPromise, timeoutPromise]);
      }
      return await requestPromise;
    } catch (error) {
      if (timedOutByPromise) {
        throw new ApiError("请求超时，请稍后重试", 408, "request_timeout");
      }
      if (error instanceof ApiError) throw error;
      if (error?.name === "AbortError") throw new ApiError("请求已取消，请稍后重试", 499, "request_cancelled");
      throw new ApiError("网络连接失败，请稍后重试");
    } finally {
      if (timeoutTimer) window.clearTimeout(timeoutTimer);
      timeoutTimer = 0;
      clearRequestTimer();
    }
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle("is-loading", busy);
  }

  function clearQrLoginPoll() {
    if (state.qrLogin.pollTimer) window.clearTimeout(state.qrLogin.pollTimer);
    state.qrLogin.pollTimer = 0;
  }

  function clearQrLoginImage() {
    const image = $("#xianyuQrImage");
    if (image) {
      image.hidden = true;
      image.removeAttribute("src");
    }
    if (state.qrLogin.objectUrl) {
      URL.revokeObjectURL(state.qrLogin.objectUrl);
      state.qrLogin.objectUrl = "";
    }
  }

  function qrLoginRequestOptions(accountKey, options = {}) {
    return Object.assign({}, options, {
      headers: Object.assign({}, options.headers || {}, accountKey ? { "X-Shop-Account": accountKey } : {}),
    });
  }

  async function loadQrLoginImage(generation) {
    const loginId = state.qrLogin.loginId;
    const accountKey = state.qrLogin.accountKey;
    if (!loginId || !accountKey || state.qrLogin.generation !== generation) return;
    clearQrLoginImage();
    let response;
    try {
      response = await fetch(API_PREFIX + "/api/bot/login/" + encodeURIComponent(loginId) + "/qr.svg", qrLoginRequestOptions(accountKey, {
        credentials: "same-origin",
      }));
    } catch (error) {
      throw new ApiError("二维码加载失败，请稍后重试");
    }
    if (state.qrLogin.generation !== generation) return;
    if (!response.ok) await readResponse(response);
    const contentType = String(response.headers.get("content-type") || "").toLowerCase();
    if (!contentType.startsWith("image/svg+xml")) throw new ApiError("二维码响应无效，请刷新后重试");
    const blob = await response.blob();
    if (!blob.size || blob.size > 512 * 1024) throw new ApiError("二维码响应无效，请刷新后重试");
    if (state.qrLogin.generation !== generation) return;
    const objectUrl = URL.createObjectURL(blob);
    if (state.qrLogin.generation !== generation) {
      URL.revokeObjectURL(objectUrl);
      return;
    }
    state.qrLogin.objectUrl = objectUrl;
    const image = $("#xianyuQrImage");
    if (image) image.src = objectUrl;
  }

  function qrRemaining(deadline) {
    return Math.max(0, Math.ceil((Number(deadline || 0) - Date.now()) / 1000));
  }

  function clearQrLoginClock() {
    if (state.qrLogin.retryTimer) window.clearTimeout(state.qrLogin.retryTimer);
    state.qrLogin.retryTimer = 0;
  }

  function recordQrExpiry(seconds) {
    if (typeof seconds === "number" && Number.isFinite(seconds)) {
      state.qrLogin.expiresAt = Date.now() + Math.max(0, Math.min(seconds, 600)) * 1000;
    }
  }

  function applyQrLoginError(error, action) {
    const login = state.qrLogin;
    const detail = error?.detail && typeof error.detail === "object" ? error.detail : {};
    clearQrLoginPoll();
    recordQrExpiry(detail.login_expires_in);
    const delay = typeof detail.retry_after === "number" && Number.isFinite(detail.retry_after)
      ? Math.max(0, Math.min(Math.ceil(detail.retry_after), 86400)) : 0;
    const remaining = qrRemaining(login.expiresAt);
    const reusable = Boolean(login.loginId && remaining > delay && (
      action === "poll" || (action === "complete" && detail.can_retry_login === true)
    ));
    login.retryAction = reusable ? action : "start";
    login.message = cookieErrorMessage(error);
    login.retryAt = delay > 0 ? Date.now() + delay * 1000 : 0;
    if (delay > 0) login.status = "cooldown";
    else if (error?.status === 404 || error?.status === 410 || error?.code === "login_expired") login.status = "expired";
    else login.status = action === "complete" ? "sync_error" : "error";
    if (action !== "poll" || !reusable) clearQrLoginImage();
    reflectCookieError(error);
  }

  function renderQrLogin() {
    const login = state.qrLogin;
    clearQrLoginClock();
    const wait = qrRemaining(login.retryAt);
    const remaining = qrRemaining(login.expiresAt);
    if (login.loginId && login.expiresAt && !remaining && !["starting", "syncing", "connected"].includes(login.status)) {
      login.retryAction = "start";
      clearQrLoginPoll();
      clearQrLoginImage();
      if (!wait) {
        login.status = "expired";
        login.message = "本次扫码会话已过期，请重新生成二维码。";
      }
    }
    if (login.status === "cooldown" && !wait) {
      login.status = login.retryAction === "complete" ? "sync_error" : login.retryAction === "poll" ? "paused" : "ready";
      login.message = "";
    }
    const connected = shopStateView(state.bot || {}).connection === "connected";
    const statusCopy = {
      idle: [connected ? "重新连接店铺" : "连接闲鱼店铺", "请使用闲鱼 App 扫码"],
      starting: ["正在生成二维码", "请稍候"],
      waiting: ["请使用闲鱼 App 扫码", "打开闲鱼 App，扫描上方二维码"],
      scanned: ["已扫码", "请在手机上确认登录"],
      syncing: ["登录成功", "正在识别店铺和商品"],
      sync_error: ["店铺识别未完成", login.retryAction === "complete" ? "冷却已结束，可以重试本次店铺识别。" : "请重新扫码后再连接店铺。"],
      cooldown: ["连接暂时等待", login.retryAction === "start" ? `请等待 ${wait} 秒后再生成二维码。` : `请等待 ${wait} 秒后再继续，本次会话剩余 ${remaining} 秒。`],
      ready: ["可以开始连接", "等待已结束，请生成二维码后扫码。"],
      paused: ["可以继续检查", "等待已结束，可以继续检查扫码状态。"],
      connected: ["店铺连接成功", "商品已经自动整理"],
      expired: ["扫码会话已过期", "请重新生成二维码"],
      error: ["暂时无法登录", "请重新生成二维码后再试"],
    };
    const copy = statusCopy[login.status] || statusCopy.error;
    text("#xianyuLoginTitle", connected ? "重新连接店铺" : "连接闲鱼店铺");
    text("#xianyuLoginStatus", copy[0]);
    text("#xianyuLoginMessage", login.status === "cooldown" ? copy[1] : login.message || copy[1]);
    const refresh = $("#refreshXianyuLogin");
    if (refresh) {
      refresh.hidden = !["expired", "error", "sync_error", "cooldown", "ready", "paused"].includes(login.status);
      refresh.disabled = wait > 0 || Boolean(login.operation);
      refresh.classList.toggle("is-loading", Boolean(login.operation));
      text(refresh.querySelector("span"), wait ? `等待 ${wait} 秒` : login.retryAction === "complete" ? "重试连接" : login.retryAction === "poll" ? "继续检查" : "生成二维码");
    }
    const image = $("#xianyuQrImage");
    const showQr = ["waiting", "scanned", "paused"].includes(login.status) && Boolean(login.objectUrl);
    if (image) image.hidden = !showQr;
    const placeholder = $("#qrLoginPlaceholder");
    if (placeholder) {
      placeholder.hidden = showQr;
      placeholder.classList.toggle("is-working", ["starting", "syncing"].includes(login.status));
      const icon = placeholder.querySelector("use");
      if (icon) icon.setAttribute("href", ICONS + (wait ? "clock" : "refresh-cw"));
    }
    if (wait || (remaining && ["waiting", "scanned", "sync_error", "cooldown", "paused"].includes(login.status))) {
      const generation = login.generation;
      login.retryTimer = window.setTimeout(() => {
        if (state.qrLogin.generation === generation) renderQrLogin();
      }, 1000);
    }
  }

  function resetQrLogin() {
    clearQrLoginPoll();
    clearQrLoginClock();
    clearQrLoginImage();
    const placeholder = $("#qrLoginPlaceholder");
    if (placeholder) placeholder.hidden = false;
    state.qrLogin = {
      loginId: "",
      accountKey: "",
      status: "idle",
      message: "",
      pollTimer: 0,
      objectUrl: "",
      generation: state.qrLogin.generation + 1,
      failures: 0,
      pollAttempts: 0,
      retryTimer: 0,
      retryAt: 0,
      expiresAt: 0,
      retryAction: "start",
      operation: "",
      polling: false,
    };
    renderQrLogin();
  }

  function openQrLoginDialog() {
    const dialog = $("#xianyuLoginDialog");
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function loginResponseIsSafe(payload) {
    if (!payload || typeof payload !== "object") return true;
    const forbidden = new Set(["cookie", "cookies", "token", "access_token", "login_token", "lgtoken", "unb", "account_ref"]);
    return !Object.entries(payload).some(([key, value]) => (
      forbidden.has(key.toLowerCase()) || (value && typeof value === "object" && !loginResponseIsSafe(value))
    ));
  }

  async function cancelQrLoginId(loginId, accountKey) {
    if (!loginId || !accountKey) return;
    try {
      await api("/api/bot/login/" + encodeURIComponent(loginId) + "/cancel", qrLoginRequestOptions(accountKey, { method: "POST" }));
    } catch (error) {
      // Login sessions are short-lived and may already be completing or expired.
    }
  }

  async function cancelQrLogin(remote = true, close = true) {
    const loginId = state.qrLogin.loginId;
    const accountKey = state.qrLogin.accountKey;
    resetQrLogin();
    if (close) closeDialog("xianyuLoginDialog");
    if (!remote || !loginId) return;
    await cancelQrLoginId(loginId, accountKey);
  }

  function scheduleQrLoginPoll(generation, delay = QR_LOGIN_POLL_MS) {
    clearQrLoginPoll();
    state.qrLogin.pollTimer = window.setTimeout(() => {
      void pollQrLogin(generation);
    }, delay);
  }

  async function finishQrLogin(generation, completed = null) {
    if (state.qrLogin.generation !== generation) return;
    state.qrLogin.status = "connected";
    state.qrLogin.message = "";
    renderQrLogin();
    try {
      if (completed?.account?.key && completed.account.key !== state.activeAccountKey) {
        state.activeAccountKey = String(completed.account.key);
        persistAccountKey(state.activeAccountKey);
      }
      await refreshState();
      await loadAccounts().catch(() => {});
      const account = currentAccount();
      const displayName = $("#shopDisplayNameInput");
      if (displayName) displayName.value = accountLabel(account);
      $("#renameShopAccountForm")?.removeAttribute("hidden");
    } catch (error) {
      showToast("店铺已连接，数据刷新稍后再试", "warning");
    }
    if (state.qrLogin.generation !== generation) return;
    resetQrLogin();
    closeDialog("xianyuLoginDialog");
    showToast("店铺连接成功");
      showView("shops");
  }

  async function completeQrLogin(generation) {
    const login = state.qrLogin;
    const loginId = login.loginId;
    const accountKey = login.accountKey;
    if (!loginId || !accountKey || login.generation !== generation || login.operation || qrRemaining(login.retryAt)) return;
    if (login.expiresAt && !qrRemaining(login.expiresAt)) { renderQrLogin(); return; }
    login.operation = "complete";
    login.status = "syncing";
    login.message = "";
    clearQrLoginPoll();
    clearQrLoginImage();
    renderQrLogin();
    try {
      const result = await api("/api/bot/login/complete", qrLoginRequestOptions(accountKey, {
        method: "POST",
        body: JSON.stringify({ login_id: loginId }),
      }));
      if (state.qrLogin.generation !== generation) return;
      if (!loginResponseIsSafe(result)) throw new ApiError("登录响应包含了不安全数据，已中止连接");
      if (String(result?.status || "").toLowerCase() !== "connected") {
        throw new ApiError("店铺识别没有完成，请重试");
      }
      await finishQrLogin(generation, result);
    } catch (error) {
      if (state.qrLogin !== login) return;
      applyQrLoginError(error, "complete");
    } finally {
      if (state.qrLogin === login) {
        login.operation = "";
        renderQrLogin();
      }
    }
  }

  async function pollQrLogin(generation) {
    const login = state.qrLogin;
    const loginId = login.loginId;
    const accountKey = login.accountKey;
    if (!loginId || !accountKey || login.generation !== generation || login.polling || login.operation || qrRemaining(login.retryAt)) return;
    if (login.expiresAt && !qrRemaining(login.expiresAt)) { renderQrLogin(); return; }
    login.polling = true;
    try {
      const result = await api(
        "/api/bot/login/" + encodeURIComponent(loginId) + "/status",
        qrLoginRequestOptions(accountKey),
      );
      if (state.qrLogin.generation !== generation) return;
      if (!loginResponseIsSafe(result)) throw new ApiError("登录响应包含了不安全数据，已中止连接");
      const status = String(result?.status || "").toLowerCase();
      recordQrExpiry(result?.expires_in);
      state.qrLogin.retryAt = 0;
      state.qrLogin.failures = 0;
      if (status === "connected") {
        await finishQrLogin(generation);
        return;
      }
      if (status === "confirmed") {
        await completeQrLogin(generation);
        return;
      }
      if (status === "syncing") {
        state.qrLogin.status = "syncing";
      } else if (status === "scanned") {
        state.qrLogin.status = "scanned";
        state.qrLogin.pollAttempts = 0;
      } else if (status === "expired") {
        state.qrLogin.status = "expired";
      } else {
        state.qrLogin.status = "waiting";
        state.qrLogin.pollAttempts += 1;
      }
      state.qrLogin.message = typeof result?.message === "string" ? result.message : "";
      renderQrLogin();
      if (state.qrLogin.status !== "expired") {
        const delay = state.qrLogin.status === "scanned"
          ? QR_LOGIN_POLL_MS
          : Math.min(6000, Math.round(QR_LOGIN_POLL_MS * (1.45 ** Math.min(state.qrLogin.pollAttempts, 4))));
        scheduleQrLoginPoll(generation, delay);
      }
    } catch (error) {
      if (state.qrLogin !== login) return;
      applyQrLoginError(error, "poll");
      renderQrLogin();
    } finally {
      if (state.qrLogin === login) login.polling = false;
    }
  }

  async function ensureConnectionAccount() {
    const account = currentAccount();
    // A reconnect action belongs to the selected account.  Creating another
    // account here would leave an empty row and make the user's current shop
    // appear to change underneath the QR flow.  Account creation is handled
    // explicitly by the add-shop form; this fallback is only for a legacy
    // session that has no account context at all.
    if (account) return account;
    const result = await api("/api/bot/accounts", {
      method: "POST",
      body: JSON.stringify({ name: "" }),
    });
    const created = result?.account;
    if (!created?.key) throw new ApiError("新店铺账号创建结果无效");
    state.accounts = state.accounts.concat([created]);
    const previousView = state.view;
    // Reuse the normal account switch path so every store-scoped loader is
    // reset before the QR session starts.
    await switchShopAccount(created.key);
    showView(previousView === "chat" ? "chat" : "shops", true);
    return currentAccount();
  }

  async function startXianyuLogin() {
    if (!state.me) {
      showToast("请先登录工作台", "warning");
      return;
    }
    if (state.qrLogin.operation) return;
    if (qrRemaining(state.qrLogin.retryAt)) {
      openQrLoginDialog();
      renderQrLogin();
      return;
    }
    try {
      await ensureConnectionAccount();
    } catch (error) {
      showToast(error.message || "无法创建新的店铺连接", "error");
      return;
    }
    if (!state.me || state.qrLogin.operation) return;
    const accountKey = String(state.activeAccountKey || "default");
    const previousId = state.qrLogin.loginId;
    const previousAccount = state.qrLogin.accountKey;
    resetQrLogin();
    const login = state.qrLogin;
    login.accountKey = accountKey;
    login.operation = "start";
    login.status = "starting";
    const generation = login.generation;
    openQrLoginDialog();
    renderQrLogin();
    try {
      await cancelQrLoginId(previousId, previousAccount);
      if (state.qrLogin !== login) return;
      const result = await api("/api/bot/login/start", qrLoginRequestOptions(accountKey, { method: "POST" }));
      const loginId = typeof result?.login_id === "string" ? result.login_id.trim() : "";
      const validLoginId = /^[A-Za-z0-9_-]{16,128}$/.test(loginId);
      if (state.qrLogin !== login) {
        if (validLoginId) await cancelQrLoginId(loginId, accountKey);
        return;
      }
      if (!loginResponseIsSafe(result)) {
        if (validLoginId) await cancelQrLoginId(loginId, accountKey);
        throw new ApiError("登录响应包含了不安全数据，已中止连接");
      }
      if (!validLoginId) throw new ApiError("登录会话无效，请重试");
      login.loginId = loginId;
      login.status = String(result?.status || "waiting").toLowerCase() === "scanned" ? "scanned" : "waiting";
      recordQrExpiry(result?.expires_in);
      await loadQrLoginImage(generation);
      if (state.qrLogin === login) scheduleQrLoginPoll(generation);
    } catch (error) {
      if (state.qrLogin === login) applyQrLoginError(error, "start");
    } finally {
      if (state.qrLogin === login) {
        login.operation = "";
        renderQrLogin();
      }
    }
  }

  async function loadAuthCapabilities({ preferFirstRegistration = false } = {}) {
    let loadError = null;
    try {
      const capabilities = await api("/api/auth/capabilities");
      state.registrationAllowed = capabilities?.registration_enabled === true;
      state.firstRegistrationAvailable = capabilities?.first_registration_available === true;
      state.bootstrapAvailable = capabilities?.bootstrap_available === true;
      const passwordMinLength = Number(capabilities?.password_min_length || 12);
      state.passwordMinLength = Number.isFinite(passwordMinLength) ? Math.max(12, passwordMinLength) : 12;
    } catch (error) {
      // Fail closed, but never disguise database initialization errors as a
      // normal registration-disabled state or lower the last known password rule.
      state.registrationAllowed = false;
      state.firstRegistrationAvailable = false;
      state.bootstrapAvailable = false;
      loadError = error;
    }
    const registerTab = $("#registerTab");
    const bootstrapTab = $("#bootstrapTab");
    if (registerTab) registerTab.hidden = !state.registrationAllowed;
    if (bootstrapTab) bootstrapTab.hidden = !state.bootstrapAvailable;
    text("#registerTab", state.firstRegistrationAvailable ? "创建管理员" : "注册");
    $("#authPassword")?.setAttribute("minlength", String(state.passwordMinLength));
    $("#newPasswordInput")?.setAttribute("minlength", String(state.passwordMinLength));
    setAuthMode(
      preferFirstRegistration && state.firstRegistrationAvailable ? "register" : state.authMode,
      { clearMessage: false },
    );
    if (loadError) throw loadError;
  }

  function setAuthMode(mode, { clearMessage = true } = {}) {
    const register = mode === "register" && state.registrationAllowed;
    const legacyBootstrap = mode === "bootstrap" && state.bootstrapAvailable;
    const firstRegistration = register && state.firstRegistrationAvailable;
    state.authMode = register ? "register" : legacyBootstrap ? "bootstrap" : "login";
    $("#loginTab").setAttribute("aria-selected", String(state.authMode === "login"));
    $("#registerTab").setAttribute("aria-selected", String(state.authMode === "register"));
    $("#bootstrapTab").setAttribute("aria-selected", String(state.authMode === "bootstrap"));
    const tokenField = $("#bootstrapTokenField");
    if (tokenField) tokenField.hidden = !legacyBootstrap;
    const tokenInput = $("#bootstrapToken");
    if (tokenInput) {
      tokenInput.required = legacyBootstrap;
      tokenInput.disabled = !legacyBootstrap;
      if (!legacyBootstrap) tokenInput.value = "";
    }
    text(
      "#authTitle",
      firstRegistration || legacyBootstrap ? "创建首个管理员账号" : register ? "创建工作台账号" : "登录工作台",
    );
    text(
      "#authDescription",
      firstRegistration
        ? "首次使用，设置账号和密码即可创建管理员。创建后默认关闭后续注册。"
        : legacyBootstrap ? "仅限受信任初始化入口；令牌成功使用后立即失效。"
          : register ? "注册后连接你的闲鱼店铺。" : "连接店铺后，商品会自动整理。",
    );
    text("#authSubmit span", firstRegistration ? "创建管理员并进入工作台" : legacyBootstrap ? "完成初始化" : register ? "创建账号" : "登录");
    $("#authPassword").setAttribute("autocomplete", state.authMode === "login" ? "current-password" : "new-password");
    if (clearMessage) formMessage("#authError", "");
  }

  async function loginAndEnterWorkspace(username, password) {
    await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    $("#authPassword").value = "";
    await bootstrap();
  }

  async function submitAuth(event) {
    event.preventDefault();
    const button = $("#authSubmit");
    if (button.disabled) return;
    const mode = state.authMode;
    const wasFirstRegistration = mode === "register" && state.firstRegistrationAvailable;
    const username = $("#authUsername").value.trim();
    let password = $("#authPassword").value;
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/.test(username)) {
      formMessage("#authError", "账号需为 3 至 32 位字母、数字、点、下划线或短横线");
      return;
    }
    if (password.length < state.passwordMinLength || password.length > 1024) {
      formMessage("#authError", "密码长度需要在 " + state.passwordMinLength + " 至 1024 位之间");
      return;
    }
    let createdRole = "";
    setBusy(button, true);
    ["#loginTab", "#registerTab", "#bootstrapTab"].forEach((selector) => { $(selector).disabled = true; });
    formMessage("#authError", "");
    try {
      if (mode === "register") {
        const result = await api("/api/auth/register", { method: "POST", body: JSON.stringify({ username, password }) });
        createdRole = result?.role === "admin" ? "admin" : "owner";
        $("#authPassword").value = "";
        await loadAuthCapabilities();
        setAuthMode("login");
        if (createdRole === "admin") {
          state.view = "home";
          await loginAndEnterWorkspace(username, password);
        } else {
          formMessage("#authError", "注册成功，请登录", true);
        }
      } else if (mode === "bootstrap") {
        const bootstrapToken = $("#bootstrapToken").value.trim();
        if (bootstrapToken.length < 32 || bootstrapToken.length > 256) {
          formMessage("#authError", "一次性初始化令牌无效");
          return;
        }
        await api("/api/auth/bootstrap", {
          method: "POST",
          headers: { "X-Bootstrap-Token": bootstrapToken },
          body: JSON.stringify({ username, password }),
        });
        createdRole = "admin";
        $("#bootstrapToken").value = "";
        $("#authPassword").value = "";
        await loadAuthCapabilities();
        setAuthMode("login");
        formMessage("#authError", "管理员账号已创建，请登录", true);
      } else {
        await loginAndEnterWorkspace(username, password);
      }
    } catch (error) {
      let message = error.message || "操作失败，请稍后重试";
      if (createdRole) {
        setAuthMode("login");
        $("#authUsername").value = username;
        $("#authPassword").value = "";
        message = (createdRole === "admin" ? "管理员账号" : "账号") + "已创建，但后续操作未完成：" + message + "。请稍后登录。";
      } else if (mode === "register" || mode === "bootstrap") {
        try {
          await loadAuthCapabilities();
          if (wasFirstRegistration && !state.firstRegistrationAvailable && [403, 409].includes(error.status)) {
            setAuthMode("login");
            $("#authPassword").value = "";
            message = "首次管理员注册已结束" + (state.registrationAllowed ? "，当前仅开放店主注册。" : "，后续注册已关闭。")
              + message + "；请使用已有账号登录或联系管理员。";
          }
        } catch (capabilitiesError) {
          message += "；注册状态刷新失败：" + (capabilitiesError.message || "请刷新页面后重试");
        }
      }
      formMessage("#authError", message);
    } finally {
      password = "";
      setBusy(button, false);
      ["#loginTab", "#registerTab", "#bootstrapTab"].forEach((selector) => { $(selector).disabled = false; });
    }
  }

  function clearSession(showMessage = true) {
    resetPlatformUpdates();
    resetQrLogin();
    stopMerchantPolling();
    ["xianyuLoginDialog", "quickRepliesDialog", "batchDeliveryDialog", "templateEditorDialog", "cardsEditorDialog", "confirmDialog", "docsHelpModal"].forEach(closeDialog);
    closeVersionBadgePopover();
    state.confirmAction = null;
    resetManualReplyContext();
    state.view = "home";
    state.me = null;
    state.accounts = [];
    state.activeAccountKey = "";
    state.shopAccountsPage = 1;
    state.accountEpoch += 1;
    state.messageLoadGeneration += 1;
    state.messageSelectionInFlight = false;
    resetConversationCommands();
    state.config = null;
    state.bot = null;
    state.automation = { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true };
    resetAiState();
    resetOpsState();
    state.settingsAi = { connection: null, verificationToken: "", testedFingerprint: "", draft: null };
    setBusy($("#aiTestConnection"), false);
    if ($("#aiApiKey")) $("#aiApiKey").value = "";
    if ($("#aiModel")) $("#aiModel").value = "";
    if ($("#aiBaseUrl")) $("#aiBaseUrl").value = "";
    if ($("#aiSaveConnection")) $("#aiSaveConnection").disabled = true;
    resetReplyRuleForm();
    resetAutomationMutations();
    state.attention = [];
    state.summary = null;
    state.analytics = null;
    state.todayAnalytics = null;
    state.trendAnalytics = null;
    state.trendPeriod = 7;
    state.analyticsPage = null;
    state.analyticsPeriod = 7;
    state.resources = null;
    state.resourcesError = null;
    state.resourcesLoading = false;
    state.resourcesInflightPromise = null;
    state.resourcesPendingReload = false;
    state.resourcesGeneration = Number(state.resourcesGeneration || 0) + 1;
    state.resourceSettings = null;
    state.resourceRevision = 0;
    state.resourceDraft = null;
    state.resourceDraftDirty = false;
    state.resourceBounds = null;
    stopResourcePolling();
    state.products = [];
    state.productsAccountKey = "";
    state.productsLoad = null;
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    state.catalogStatus = null;
    state.batchDelivery = { enabled: true, previewToken: "", preview: null, generation: Number(state.batchDelivery?.generation || 0) + 1 };
    resetAccountInboxState();
    resetOrdersPageState();
    state.orders = [];
    state.templates = [];
    state.cards = null;
    state.cardsAccountKey = "";
    state.cardsLoad = null;
    state.templateEditorOpenGeneration += 1;
    state.templateEditor = { editingId: "", productIds: [] };
    state.cardsEditor = { editingId: "", mode: "import" };
    stopDocsPolling();
    state.docs = newDocsState();
    state.version = state.publicVersion || null;
    state.versionUpdate = null;
    closeVersionBadgePopover();
    renderVersionBadge();
    ["templateEditorForm", "cardsEditorForm", "cardsCreateForm", "batchDeliveryForm", "passwordChangeForm", "platformSettingsForm", "resourceSettingsForm", "ordersFilterForm", "aiConnectionForm", "opsPromptForm"].forEach((id) => $("#" + id)?.reset());
    const resSettingsMsg = $("#resourceSettingsMessage");
    if (resSettingsMsg) { resSettingsMsg.textContent = ""; resSettingsMsg.className = "form-message"; }
    ["homeResourcesMessage", "shopResourcesMessage"].forEach((id) => {
      const el = $("#" + id);
      if (el) { el.textContent = ""; el.className = "resources-message"; }
    });
    [
      "homeStatCards", "homeProductGrid", "homeOrderList", "attentionList", "shopAccountsPanelList",
      "productGrid", "conversationItems", "chatMessages", "orderList", "ordersStatusTabs", "replyRuleList",
      "templateGrid", "cardsStats", "cardsList", "analyticsCards", "analyticsChart",
      "homeResourceBody", "shopResourcesBody",
    ].forEach((id) => {
      const node = $("#" + id);
      if (node) node.innerHTML = "";
    });
    const ordersMsg = $("#ordersMessage");
    if (ordersMsg) {
      ordersMsg.hidden = true;
      ordersMsg.textContent = "";
    }
    ["automationFirstReply", "automationFallbackReply", "batchDeliveryMaterial", "manualReplyInput", "opsPromptInput"].forEach((id) => {
      const node = $("#" + id);
      if (node) node.value = "";
    });
    $("#authUsername").value = "";
    $("#authPassword").value = "";
    if ($("#bootstrapToken")) $("#bootstrapToken").value = "";
    if ($("#updateAdminPassword")) $("#updateAdminPassword").value = "";
    ["#updateActionMessage", "#passwordChangeMessage", "#versionLoadMessage", "#aiConnectionMessage", "#opsPromptMessage"].forEach((selector) => text(selector, ""));
    $("#workspace").hidden = true;
    $("#authScreen").hidden = false;
    void loadPublicVersionOnce();
    if (showMessage) showToast("已退出登录");
  }

  function logout() {
    state.refreshEpoch = Number(state.refreshEpoch || 0) + 1;
    state.accountEpoch = Number(state.accountEpoch || 0) + 1;
    state.resourcesGeneration = Number(state.resourcesGeneration || 0) + 1;
    state.resourcesInflightPromise = null;
    state.resourcesPendingReload = false;
    stopResourcePolling();
    if (state.ai) {
      state.ai.knowledgeGeneration = Number(state.ai.knowledgeGeneration || 0) + 1;
      state.ai.loadGeneration = Number(state.ai.loadGeneration || 0) + 1;
    }
    return runManualReplyDestructiveAction(async () => {
      await cancelQrLogin(true, true);
      const accountKey = state.activeAccountKey;
      try {
        await prepareManualReplyForDestructiveAction(accountKey);
      } catch (error) {
        if (error?.status !== 401) {
          showToast(error.message || "待发送图片清理失败，请稍后重试", "error");
          return;
        }
        // An already-expired session cannot authorize cleanup; local logout must still finish.
      }
      await resetManualReplyContext({ cleanupUploaded: false, accountKey });
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (error) {
        // The session may already have expired.
      }
      clearSession(true);
    });
  }

  async function loadAccounts() {
    const context = captureAccountContext();
    const data = await api("/api/bot/accounts");
    const accounts = Array.isArray(data?.accounts) ? data.accounts.filter((item) => item && item.key && item.enabled !== false) : [];
    if (!accountContextMatches(context)) return state.accounts;
    state.accounts = accounts;
    state.shopAccountsPage = 1;
    const stored = readStoredAccountKey();
    const preferred = [state.activeAccountKey, stored, "default"].find((key) => accounts.some((item) => item.key === key))
      || accounts[0]?.key
      || state.activeAccountKey
      || "default";
    state.activeAccountKey = preferred;
    ensureCurrentAccount();
    renderAccountSwitcher();
    return state.accounts;
  }

  async function switchShopAccount(accountKey) {
    const key = String(accountKey || "").trim();
    const account = state.accounts.find((item) => item.key === key && item.enabled !== false);
    if (!account || key === state.activeAccountKey) return;
    if (manualReplyOperationInFlight()) {
      showToast("请等待当前回复处理完成后再切换店铺", "warning");
      return;
    }
    if (!confirmDiscardAiChanges("店铺")) return;
    if ($("#aiModel")?.value || $("#aiBaseUrl")?.value || $("#aiApiKey")?.value) {
      if (!state.settingsAi) state.settingsAi = {};
      state.settingsAi.draft = {
        provider: $("#aiProvider")?.value || "",
        base_url: $("#aiBaseUrl")?.value || "",
        model: $("#aiModel")?.value || "",
        api_key: $("#aiApiKey")?.value || "",
      };
    }
    resetManualReplyContext();
    state.activeAccountKey = key;
    state.accountEpoch += 1;
    resetOpsState();
    const context = captureAccountContext();
    state.messageLoadGeneration += 1;
    state.messageSelectionInFlight = false;
    resetConversationCommands();
    resetAutomationMutations();
    persistAccountKey(key);
    void cancelQrLogin(true, true);
    ["quickRepliesDialog", "batchDeliveryDialog", "templateEditorDialog", "cardsEditorDialog", "confirmDialog"].forEach(closeDialog);
    state.confirmAction = null;
    state.config = null;
    state.bot = null;
    state.automation = { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true };
    resetAiState();
    resetReplyRuleForm();
    state.attention = [];
    state.summary = null;
    state.analytics = null;
    state.todayAnalytics = null;
    state.trendAnalytics = null;
    state.trendPeriod = 7;
    state.analyticsPage = null;
    state.resources = null;
    state.products = [];
    state.productsAccountKey = "";
    state.productsLoad = null;
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    state.deliveryStatus = { available: false, items: new Map(), loaded: false, error: null };
    state.deliveryStatusAccountKey = "";
    state.deliveryStatusEpoch = state.accountEpoch;
    state.goodsSearch = "";
    state.goodsStatusFilter = "all";
    state.goodsPage = 1;
    const goodsSearchInput = $("#productSearch");
    if (goodsSearchInput) goodsSearchInput.value = "";
    const goodsFilterSelect = $("#productStatusFilter");
    if (goodsFilterSelect) goodsFilterSelect.value = "all";
    state.catalogStatus = null;
    state.batchDelivery = { enabled: true, previewToken: "", preview: null, generation: Number(state.batchDelivery?.generation || 0) + 1 };
    resetAccountInboxState({ restorePreferences: true });
    resetOrdersPageState();
    $("#ordersFilterForm")?.reset();
    if ($("#ordersSearchField")) $("#ordersSearchField").value = "all";
    state.orders = [];
    state.templates = [];
    state.cards = null;
    state.cardsAccountKey = "";
    state.cardsLoad = null;
    state.templateEditorOpenGeneration += 1;
    state.templateEditor = { editingId: "", productIds: [] };
    state.cardsEditor = { editingId: "", mode: "import" };
    const preserveViews = new Set(["shops", "orders", "settings", "ops", "goods", "templates", "cards", "auto-reply", "chat", "ai-config"]);
    const returnView = preserveViews.has(state.view) ? state.view : "home";
    renderAccountSwitcher();
    renderOverview();
    renderChat();
    renderOrders();
    renderTemplates();
    renderCards();
    renderAnalytics();
    showView(returnView, true);
    try {
      await refreshState();
      if (!accountContextMatches(context)) return;
      showToast("已切换到「" + accountLabel(account) + "」");
    } catch (error) {
      if (accountContextMatches(context)) showToast(error.message || "店铺切换失败，请稍后重试", "error");
    }
  }

  async function handleShopAction(event) {
    const button = event.currentTarget;
    const key = String(button.dataset.shopKey || "").trim();
    if (!key) return;
    if (key !== state.activeAccountKey) await switchShopAccount(key);
    if (button.dataset.shopAction === "reconnect") {
      await startXianyuLogin();
      return;
    }
    await syncShop({ currentTarget: button });
  }

  async function focusRenameShopAccount(accountKey) {
    const key = String(accountKey || "").trim();
    if (!key) return;
    if (key !== state.activeAccountKey) await switchShopAccount(key);
    showView("shops", true);
    const form = $("#renameShopAccountForm");
    const input = $("#shopDisplayNameInput");
    if (form) form.hidden = false;
    if (input) {
      input.focus();
      input.select();
    }
  }

  function openShopAccountForm() {
    showView("shops", true);
    const block = $(".shop-add-block");
    const input = $("#shopAccountPanelNameInput");
    if (block) block.scrollIntoView({ block: "center" });
    if (input) input.focus({ preventScroll: true });
  }

  async function createShopAccount(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const input = $("input[name=\"name\"]", form);
    const message = $(".shop-account-message", form);
    const button = event.submitter;
    const name = String(input?.value || "").trim();
    if (name.length > 160) {
      formMessage(message, "店铺备注不能超过 160 个字");
      return;
    }
    setBusy(button, true);
    try {
      const result = await api("/api/bot/accounts", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      const account = result?.account;
      if (!account?.key) throw new ApiError("店铺账号创建结果无效");
      const previous = state.activeAccountKey;
      await loadAccounts().catch(() => {});
      if (!state.accounts.some((item) => item.key === account.key)) state.accounts.push(account);
      state.activeAccountKey = previous;
      if (input) input.value = "";
      await switchShopAccount(account.key);
      showView("shops");
      await startXianyuLogin();
    } catch (error) {
      formMessage(message, error.message || "店铺账号创建失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function saveShopAccountName(event) {
    event.preventDefault();
    const account = currentAccount();
    const input = $("#shopDisplayNameInput");
    const message = $("#renameShopAccountMessage");
    if (!account || !input) return;
    const name = String(input.value || "").trim();
    if (name.length > 160) {
      formMessage(message, "店铺名称不能超过 160 个字");
      return;
    }
    const button = event.submitter;
    const context = captureAccountContext();
    const requestToken = Symbol("shop-rename");
    button._requestToken = requestToken;
    setBusy(button, true);
    try {
      const result = await api("/api/bot/accounts/" + encodeURIComponent(account.key), {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      if (!accountContextMatches(context)) return;
      const updated = result?.account;
      if (!updated?.key) throw new ApiError("店铺名称保存结果无效");
      state.accounts = state.accounts.map((item) => item.key === updated.key ? updated : item);
      if (state.bot) state.bot.account = Object.assign({}, state.bot.account || {}, updated);
      renderAccountSwitcher();
      renderShopStatus();
      formMessage(message, "名称已保存", true);
      showToast("店铺名称已保存");
    } catch (error) {
      if (accountContextMatches(context)) formMessage(message, error.message || "店铺名称保存失败");
    } finally {
      if (button._requestToken === requestToken) {
        delete button._requestToken;
        setBusy(button, false);
      }
    }
  }

  function confirmDeleteShopAccount(accountKey) {
    const account = state.accounts.find((item) => item.key === accountKey);
    if (!account || account.key === "default") return;
    text("#confirmTitle", "删除店铺");
    text("#confirmMessage", "删除后会停止该店铺的自动处理并从列表隐藏，其他店铺不受影响。");
    text("#confirmAction", "确认删除");
    state.confirmAction = () => runManualReplyDestructiveAction(async () => {
      const wasActive = state.activeAccountKey === account.key;
      if (wasActive) {
        await prepareManualReplyForDestructiveAction(account.key);
        await resetManualReplyContext({ cleanupUploaded: false, accountKey: account.key });
      }
      await api("/api/bot/accounts/" + encodeURIComponent(account.key), { method: "DELETE" });
      await loadAccounts();
      if (wasActive) {
        const next = state.accounts.find((item) => item.enabled !== false);
        if (next) {
          state.activeAccountKey = next.key;
          persistAccountKey(next.key);
          state.accountEpoch += 1;
          state.messageLoadGeneration += 1;
          state.messageSelectionInFlight = false;
          resetConversationCommands();
          state.config = null;
          state.bot = null;
          state.products = [];
          state.productsAccountKey = "";
          state.productsLoad = null;
          state.productsTruncated = null;
          state.productsTruncatedAccountKey = "";
          state.catalogStatus = null;
          resetAccountInboxState({ restorePreferences: true });
          resetOrdersPageState();
          state.orders = [];
          state.templates = [];
          state.cards = null;
          state.cardsAccountKey = "";
          state.cardsLoad = null;
          state.templateEditorOpenGeneration += 1;
          state.templateEditor = { editingId: "", productIds: [] };
          state.cardsEditor = { editingId: "", mode: "import" };
          loadInboxPreferences();
          await refreshState();
        }
      }
      renderAccountSwitcher();
      renderOverview();
      showView("shops", true);
      showToast("店铺已删除");
    });
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function renderNav() {
    const items = [
      { view: "home", label: "店铺概览", icon: "layout-dashboard" },
      { view: "chat", label: "智能客服", icon: "message-square-text" },
      { view: "goods", label: "商品与发货", icon: "box" },
      { view: "orders", label: "订单管理", icon: "package-check" },
      { view: "ops", label: "智能运维", icon: "sparkles" },
    ];
    $("#sideNav").innerHTML = items.map((item) => (
      '<button type="button" class="side-nav-item" data-view="' + item.view + '" aria-label="' + item.label + '" title="' + item.label + '">' +
      '<svg class="icon"><use href="' + ICONS + item.icon + '"></use></svg>' +
      '<span class="side-nav-tooltip">' + item.label + "</span></button>"
    )).join("");
  }

  function renderAccount() {
    ensurePlatformUpdateSession();
    const username = state.me?.username || "店主";
    text("#userAvatarBadge", username.slice(0, 1).toUpperCase());
  }

  function renderShopStatus() {
    const bot = state.bot || {};
    const account = currentAccount();
    const view = shopStateView(bot);
    const cookieStatus = view.cookie;
    const syncStatus = view.code;
    const connected = view.connection === "connected";
    const shopName = String(account?.name || bot.shop_name || (connected ? "已连接闲鱼店铺" : "未连接店铺"));
    const count = state.products.length || view.productCount;
    const lastSync = bot.last_sync_at || (state.products[0] && state.products[0].updated_at);
    const statusLabel = view.restricted ? "部分能力受限" : connected ? (cookieStatus.label || "已验证") : cookieStatus.label || "需要处理";
    const statusClass = connected && !view.restricted ? "badge-green" : ["risk_control", "risk_cooldown"].includes(syncStatus) ? "badge-amber" : COOKIE_BLOCKING_CODES.has(syncStatus) || view.restricted ? "badge-red" : "badge-muted";
    text("#shopAccountValue", shopName);
    text("#shopCookieState", connected ? "已验证" : statusLabel);
    text("#shopProductState", count ? count + " 个商品" : view.catalog === "empty" ? "暂无在售商品" : view.catalog === "blocked" ? "平台限制中" : view.connection === "checking" ? "正在整理" : "等待检测");
    text("#shopLastSync", lastSync ? formatDate(lastSync) : "--");
    $("#shopConnectionBadge").textContent = statusLabel;
    $("#shopConnectionBadge").className = "badge " + statusClass;

    text("#shopConnectionTitle", view.selected.title);
    text("#shopConnectionDescription", view.selected.description);
    const connectButton = $("#xianyuConnectButton");
    if (connectButton) {
      text($("span", connectButton), view.selected.button);
      connectButton.disabled = view.connection === "checking";
      connectButton.setAttribute("aria-label", view.selected.button);
    }
    const renameForm = $("#renameShopAccountForm");
    const renameInput = $("#shopDisplayNameInput");
    if (renameForm) renameForm.hidden = !account;
    if (renameInput && document.activeElement !== renameInput) renameInput.value = String(account?.name || bot.shop_name || "");

    const notice = $("#cookieStatusNotice");
    if (notice) {
      const visible = Boolean(bot.cookies_set && (COOKIE_BLOCKING_CODES.has(syncStatus) || syncStatus === "pending" || view.connection === "degraded"));
      notice.hidden = !visible;
      notice.className = "cookie-status-notice" + (syncStatus === "risk_control" || syncStatus === "risk_cooldown" || syncStatus === "verification_required" || view.restricted ? " is-risk" : " is-expired");
      text("#cookieStatusTitle", cookieStatus.label);
      text("#cookieStatusMessage", cookieStatus.message);
      text("#cookieStatusAction", cookieStatus.action);
      const checkButton = $("#checkCookieButton");
      if (checkButton) checkButton.disabled = !bot.cookies_set || syncStatus === "risk_cooldown";
    }
    renderAccountSwitcher();
  }

  async function loadProductDeliveryStatus() {
    const context = captureAccountContext();
    const epoch = context.epoch;
    const accountKey = context.accountKey;
    try {
      const data = await accountScopedApi(context, "/api/bot/products/delivery-status");
      if (!accountContextMatches(context) || epoch !== state.accountEpoch || accountKey !== state.activeAccountKey) {
        return;
      }
      const items = Array.isArray(data?.items) ? data.items : [];
      state.deliveryStatus = {
        available: data?.available === true,
        items: new Map(items.map((it) => [String(it.item_id), it])),
        loaded: true,
        error: null,
      };
      state.deliveryStatusAccountKey = accountKey;
      state.deliveryStatusEpoch = epoch;
      renderProducts();
    } catch (error) {
      if (!accountContextMatches(context) || epoch !== state.accountEpoch) return;
      state.deliveryStatus.error = error;
    }
  }

  function getProductDeliveryInfo(product) {
    const itemId = String(product.id || "");
    let item = null;
    if (state.deliveryStatus?.loaded && state.deliveryStatus?.items) {
      item = state.deliveryStatus.items.get(itemId);
    }

    if (item) {
      const delivery = item.delivery || "material";
      const configured = Boolean(item.configured);
      const enabled = item.enabled !== false;
      const templateId = item.template_id || null;

      let filterCategory = "unconfigured";
      if (delivery === "conflict") {
        filterCategory = "conflict";
      } else if (configured && !enabled) {
        filterCategory = "paused";
      } else if (configured && enabled) {
        filterCategory = "configured";
      } else {
        filterCategory = "unconfigured";
      }

      return {
        delivery,
        configured,
        enabled,
        templateId,
        filterCategory,
      };
    }

    if (state.deliveryStatus?.loaded && state.deliveryStatus?.available) {
      return {
        delivery: "unconfigured",
        configured: false,
        enabled: false,
        templateId: null,
        filterCategory: "unconfigured",
      };
    }

    const legacyDelivery = (state.automation?.deliveries || []).find((d) => String(d.item_id) === itemId);
    if (legacyDelivery) {
      const configured = Boolean(String(legacyDelivery.material || "").trim());
      const enabled = legacyDelivery.enabled !== false;
      return {
        delivery: "material",
        configured,
        enabled,
        templateId: null,
        filterCategory: configured ? (enabled ? "configured" : "paused") : "unconfigured",
      };
    }

    return {
      delivery: "unconfigured",
      configured: false,
      enabled: false,
      templateId: null,
      filterCategory: "unconfigured",
    };
  }

  function renderProductDeliveryBadge(info) {
    if (info.delivery === "pan") {
      return info.enabled
        ? '<span class="badge badge-green">网盘自动发货</span>'
        : '<span class="badge badge-amber">网盘已暂停</span>';
    }
    if (info.delivery === "redeem") {
      return info.enabled
        ? '<span class="badge badge-green">卡密自动发货</span>'
        : '<span class="badge badge-amber">卡密已暂停</span>';
    }
    if (info.delivery === "conflict") {
      return '<span class="badge badge-red">配置冲突</span>';
    }
    if (info.delivery === "material") {
      if (info.configured) {
        return info.enabled
          ? '<span class="badge badge-green">已设置资料</span>'
          : '<span class="badge badge-amber">资料已暂停</span>';
      }
      return '<span class="badge badge-muted">未设置资料</span>';
    }
    return '<span class="badge badge-muted">未设置资料</span>';
  }

  function renderProductActions(product, itemId, info) {
    if (info.delivery === "pan" || info.delivery === "redeem") {
      return '<button class="button button-secondary button-compact" type="button" data-view="templates" aria-label="查看' + esc(product.title || "未命名商品") + '的发货模板"><span>查看发货模板</span></button>';
    }
    if (info.delivery === "conflict") {
      return '<button class="button button-secondary button-compact" type="button" data-view="templates" aria-label="查看发货模板排查配置冲突"><span>查看模板</span></button>';
    }
    if (info.delivery === "material" && info.configured) {
      return '<button class="button button-secondary button-compact" type="button" data-edit-delivery data-item-id="' + esc(itemId) + '" aria-label="编辑' + esc(product.title || "未命名商品") + '的资料"><span>编辑资料</span></button>' +
        '<button class="button button-secondary button-compact" type="button" data-delivery-toggle="' + esc(itemId) + '" aria-label="' + (info.enabled ? "暂停" : "恢复") + esc(product.title || "未命名商品") + '的资料"><span>' + (info.enabled ? "暂停资料" : "恢复资料") + "</span></button>";
    }
    return '<button class="button button-secondary button-compact" type="button" data-edit-delivery data-item-id="' + esc(itemId) + '" aria-label="编辑' + esc(product.title || "未命名商品") + '的资料"><span>编辑资料</span></button>';
  }

  function renderProducts() {
    const grid = $("#productGrid");
    const empty = $("#productsEmpty");
    const notice = $("#productsNotice");
    const view = shopStateView(state.bot || {});

    const cardsBtn = $("#productViewCards");
    const listBtn = $("#productViewList");
    if (cardsBtn) {
      cardsBtn.classList.toggle("is-active", state.goodsViewMode === "cards");
      cardsBtn.setAttribute("aria-pressed", String(state.goodsViewMode === "cards"));
    }
    if (listBtn) {
      listBtn.classList.toggle("is-active", state.goodsViewMode === "list");
      listBtn.setAttribute("aria-pressed", String(state.goodsViewMode === "list"));
    }
    const searchInput = $("#productSearch");
    if (searchInput && document.activeElement !== searchInput && searchInput.value !== state.goodsSearch) {
      searchInput.value = state.goodsSearch;
    }
    const statusSelect = $("#productStatusFilter");
    if (statusSelect && statusSelect.value !== state.goodsStatusFilter) {
      statusSelect.value = state.goodsStatusFilter;
    }
    const pageSizeSelect = $("#productPageSize");
    if (pageSizeSelect && pageSizeSelect.value !== String(state.goodsPageSize)) {
      pageSizeSelect.value = String(state.goodsPageSize);
    }

    const setProductsNotice = (message) => {
      if (!notice) return;
      if (message) {
        notice.hidden = false;
        text($("span", notice), message);
      } else {
        notice.hidden = true;
      }
    };

    const homeGrid = $("#homeProductGrid");
    if (homeGrid) {
      const featured = state.products.slice(0, 6);
      homeGrid.innerHTML = featured.length ? featured.map((product) => {
        const info = getProductDeliveryInfo(product);
        const active = info.configured && info.enabled;
        const hasPrice = Boolean(product.price_display && product.price_display !== "价格待同步");
        const priceClass = hasPrice ? "home-product-price" : "home-product-price is-pending";
        const priceText = product.price_display || "价格待同步";
        return '<a class="home-product-card" href="#" data-view="goods" aria-label="查看商品：' + esc(product.title || "未命名商品") + '">' +
          productThumb(product, "home") +
          '<strong class="home-product-name">' + esc(product.title || "未命名商品") + "</strong>" +
          '<span class="' + priceClass + '">' + esc(priceText) + "</span>" +
          '<span class="badge ' + (active ? "badge-green" : "badge-muted") + '">' + (active ? "已设置资料" : "未设置") + "</span></a>";
      }).join("") : '<div class="automation-empty">还没有商品，连接店铺后自动整理。</div>';
    }

    if (!state.products.length) {
      if (grid) grid.innerHTML = "";
      if (empty) empty.hidden = false;
      const paginationWrap = $("#productPagination");
      if (paginationWrap) paginationWrap.hidden = true;
      const copies = {
        not_started: ["还没有连接店铺", "先连接闲鱼店铺，系统会自动读取商品名称、简介和价格。", "连接店铺", "view"],
        syncing: ["商品正在整理", "登录已确认，系统正在后台读取商品，完成后会自动显示。", "正在整理商品", "disabled"],
        empty: ["店铺已连接，暂时没有商品", "账号已经连接成功，但当前没有识别到可展示的商品。", "重新检测商品", "sync"],
        blocked: ["商品整理受到平台限制", "账号仍然连接，但闲鱼当前限制了部分操作；请先在闲鱼官方页面查看通知。", "查看账号状态", "view"],
        stale: ["商品列表需要更新", "暂时无法完成最新检测，已有商品不会被清空。", "重新检测商品", "sync"],
        unavailable: ["暂时无法读取商品", "登录状态需要再次确认，稍后可以重新检测。", "重新检测商品", "sync"],
        not_available: ["暂时无法读取商品", "店铺连接后，系统会自动整理商品。", "查看店铺状态", "view"],
      };
      const copy = copies[view.catalog] || copies.unavailable;
      text("#productsEmptyTitle", copy[0]);
      text("#productsEmptyMessage", copy[1]);
      const action = $("#productsEmptyAction");
      if (action) {
        text($("span", action), copy[2]);
        action.disabled = copy[3] === "disabled";
        if (copy[3] === "sync") {
          action.removeAttribute("data-view");
          action.setAttribute("data-sync-products", "true");
        } else {
          action.removeAttribute("data-sync-products");
          action.setAttribute("data-view", "shops");
        }
      }
      setProductsNotice(view.restricted
        ? "账号已连接，但商品相关操作受到闲鱼限制。"
        : ["blocked", "stale"].includes(view.catalog)
          ? "暂时无法完成最新商品整理，重新检测后状态会自动更新。"
          : "");
      $$('[data-open-batch-delivery]').forEach((button) => { button.disabled = true; });
      return;
    }

    if (empty) empty.hidden = true;
    setProductsNotice(view.restricted
      ? "账号已连接，当前商品来自上次成功检测；发布相关操作受到闲鱼限制。"
      : view.catalog === "stale"
        ? "当前显示上次成功整理的商品，新的检测暂未完成。"
        : "");
    $$('[data-open-batch-delivery]').forEach((button) => { button.disabled = false; });

    const query = String(state.goodsSearch || "").trim().toLowerCase();
    const filterCat = state.goodsStatusFilter || "all";

    const filtered = state.products.filter((product) => {
      const info = getProductDeliveryInfo(product);
      if (filterCat !== "all" && info.filterCategory !== filterCat) {
        return false;
      }
      if (query) {
        const idStr = String(product.id || "").toLowerCase();
        const titleStr = String(product.title || "").toLowerCase();
        const descStr = String(product.description || "").toLowerCase();
        if (!idStr.includes(query) && !titleStr.includes(query) && !descStr.includes(query)) {
          return false;
        }
      }
      return true;
    });

    const pageSize = state.goodsPageSize === 24 ? 24 : 12;
    const totalCount = filtered.length;
    const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
    state.goodsPage = Math.min(pageCount, Math.max(1, state.goodsPage));
    const startIndex = (state.goodsPage - 1) * pageSize;
    const pageItems = filtered.slice(startIndex, startIndex + pageSize);

    const paginationWrap = $("#productPagination");
    if (paginationWrap) {
      paginationWrap.hidden = false;
      text("#productPageLabel", "第 " + state.goodsPage + " / " + pageCount + " 页");
      const prevBtn = $("#productPrevPage");
      if (prevBtn) prevBtn.disabled = state.goodsPage <= 1;
      const nextBtn = $("#productNextPage");
      if (nextBtn) nextBtn.disabled = state.goodsPage >= pageCount;
    }

    if (!pageItems.length) {
      if (grid) {
        grid.className = state.goodsViewMode === "list" ? "product-card-grid is-list-view" : "product-card-grid";
        grid.innerHTML = '<div class="table-cell-empty product-grid-empty">' +
          (query || filterCat !== "all" ? "没有找到符合筛选条件的商品" : "暂无可展示的商品") + "</div>";
      }
      return;
    }

    if (state.goodsViewMode === "list") {
      if (grid) {
        grid.className = "product-card-grid is-list-view";
        grid.innerHTML = pageItems.map((product) => {
          const itemId = String(product.id || "");
          const info = getProductDeliveryInfo(product);
          const badge = renderProductDeliveryBadge(info);
          const actions = renderProductActions(product, itemId, info);
          return '<div class="product-row" data-product-id="' + esc(itemId) + '">' +
            '<div class="product-cell product-cell-main">' +
            productThumb(product, "product") +
            '<div><strong class="product-title" title="' + esc(product.title || "未命名商品") + '">' + esc(product.title || "未命名商品") + '</strong>' +
            '<small class="product-desc">' + esc(product.description || "暂无商品简介") + "</small></div></div>" +
            '<span class="product-price">' + esc(product.price_display || "价格待同步") + "</span>" +
            badge +
            '<div class="product-actions">' + actions + "</div>" +
            "</div>";
        }).join("");
      }
    } else {
      if (grid) {
        grid.className = "product-card-grid";
        grid.innerHTML = pageItems.map((product) => {
          const itemId = String(product.id || "");
          const info = getProductDeliveryInfo(product);
          const badge = renderProductDeliveryBadge(info);
          const actions = renderProductActions(product, itemId, info);
          const titleChar = String(product.title || "").trim().slice(0, 1) || "商";
          const image = productImageUrl(product);
          return '<div class="product-card" data-product-id="' + esc(itemId) + '">' +
            '<div class="product-card-cover">' +
            (image
              ? '<img src="' + esc(image) + '" alt="' + esc(product.title || "") + '" loading="lazy" referrerpolicy="no-referrer" data-image-fallback="product-cover">' +
                '<span class="cover-monogram" hidden aria-hidden="true">' + esc(titleChar) + "</span>"
              : '<span class="cover-monogram" aria-hidden="true">' + esc(titleChar) + "</span>"
            ) +
            "</div>" +
            '<div class="product-card-body">' +
            '<strong class="product-card-title" title="' + esc(product.title || "未命名商品") + '">' + esc(product.title || "未命名商品") + "</strong>" +
            '<div class="product-card-meta">' +
            '<span class="product-card-price">' + esc(product.price_display || "价格待同步") + "</span>" +
            badge +
            "</div>" +
            '<div class="product-card-actions">' + actions + "</div>" +
            "</div>" +
            "</div>";
        }).join("");
      }
    }
    $$('[data-open-batch-delivery]').forEach((button) => { button.disabled = false; });
  }

  function canonicalTemplateDelivery(value, fallback = "redeem") {
    const lower = String(value || "").trim().toLowerCase();
    if (["redeem", "account", "card", "key", "code", "卡密", "兑换码", "激活码"].includes(lower)) return "redeem";
    if (["pan", "file", "link", "text", "网盘", "网盘资料"].includes(lower)) return "pan";
    return fallback;
  }

  function templateDeliveryInfo(template) {
    const raw = String(template?.delivery || template?.delivery_type || "").trim();
    const lower = raw.toLowerCase();
    const description = String(template?.description || "").trim();
    const payload = template?.payload_set && typeof template.payload_set === "object" ? template.payload_set : {};
    if (lower === "redeem" || lower === "pan") {
      return {
        delivery: lower,
        label: lower === "redeem" ? "兑换码" : "网盘资料",
        poolName: lower === "redeem" ? String(payload.pool_name || payload.name || "").trim() : "",
        script: description,
      };
    }
    const poolName = String(payload.pool_name || payload.name || template.resource_match || "").trim();
    const delivery = canonicalTemplateDelivery(lower, poolName ? "redeem" : "pan");
    const typeOnly = /^(account|card|key|code|file|link|text|网盘资料?|网盘|兑换码|卡密)$/i.test(raw.trim());
    const script = (typeOnly && description) || raw || description;
    return {
      delivery,
      label: delivery === "redeem" ? "兑换码" : "网盘资料",
      poolName: delivery === "redeem" ? poolName : "",
      script,
    };
  }

  function renderTemplates() {
    const templates = Array.isArray(state.templates) ? state.templates : [];
    const grid = $("#templateGrid");
    const empty = $("#templatesEmpty");
    const count = $("#templateCount");
    if (count) count.textContent = templates.length + " 个模板";
    if (!templates.length) {
      if (grid) grid.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    if (!grid) return;
    grid.innerHTML = templates.map((template) => {
      const id = String(template.id || "");
      const itemIds = Array.isArray(template.item_ids) ? template.item_ids.map(String) : [];
      const itemCount = Number(template.item_count || itemIds.length || 0);
      const info = templateDeliveryInfo(template);
      const chips = itemIds.slice(0, 4).map((itemId) =>
        '<span class="template-card-product-chip">' + esc(itemId) + "</span>"
      ).join("") + (itemIds.length > 4 ? '<span class="template-card-product-chip">+' + (itemIds.length - 4) + "</span>" : "");
      const script = info.script
        ? (info.script.length > 72 ? info.script.slice(0, 72) + "…" : info.script)
        : "未填写发货话术";
      const poolLine = info.poolName || (info.delivery === "redeem" ? "兑换码库存" : "无（纯话术/网盘链接）");
      return '<article class="template-card" data-template-id="' + esc(id) + '">' +
        '<div class="template-card-head"><h3>' + esc(template.name || "未命名模板") + '</h3>' +
        '<span class="eyebrow">已绑定 ' + esc(itemCount) + " 个商品</span></div>" +
        '<div class="template-card-meta">' +
        '<div><strong>类型</strong><span>' + esc(info.label) + "</span></div>" +
        '<div><strong>绑定卡密池</strong><span>' + esc(poolLine) + "</span></div>" +
        '<div><strong>发货话术</strong><span class="template-card-preview">' + esc(script) + "</span></div>" +
        '<div><strong>绑定商品</strong><span class="template-card-products">' + (chips || '<span class="template-card-product-chip">未绑定商品</span>') + "</span></div>" +
        "</div>" +
        '<div class="template-card-actions">' +
        '<button class="button button-secondary button-compact" type="button" data-template-edit="' + esc(id) + '"><svg class="icon"><use href="' + ICONS + 'edit-3"></use></svg><span>编辑</span></button>' +
        '<button class="button button-secondary button-danger-soft button-compact" type="button" data-template-delete="' + esc(id) + '"><svg class="icon"><use href="' + ICONS + 'trash-2"></use></svg><span>删除</span></button>' +
        "</div></article>";
    }).join("");
  }

  function cardsPools() {
    if (Array.isArray(state.cards?.pools)) return state.cards.pools;
    if (state.cards?.pool) return [state.cards.pool];
    return [];
  }

  function cardsStatsFallback(pools) {
    const total = pools.reduce((sum, pool) => sum + Number(pool.total || 0), 0);
    const available = pools.reduce((sum, pool) => sum + Number(pool.available || 0), 0);
    const used = pools.reduce((sum, pool) => sum + Number(pool.used || 0), 0);
    const reserved = pools.reduce((sum, pool) => sum + Number(pool.reserved ?? Math.max(0, Number(pool.total || 0) - Number(pool.available || 0) - Number(pool.used || 0))), 0);
    return { pools: pools.length, total, available, reserved, used };
  }

  function renderCards() {
    const pools = cardsPools();
    const stats = state.cards?.stats && typeof state.cards.stats === "object"
      ? state.cards.stats
      : cardsStatsFallback(pools);
    const statsHost = $("#cardsStats");
    if (statsHost) {
      statsHost.innerHTML =
        statCard("卡密池", String(stats.pools ?? pools.length), "key-round", "tone-blue") +
        statCard("总库存", String(stats.total ?? 0), "layers", "tone-amber") +
        statCard("可用", String(stats.available ?? 0), "circle-check", "tone-green") +
        statCard("预占", String(stats.reserved ?? 0), "clock", "tone-amber") +
        statCard("已消耗", String(stats.used ?? 0), "box", "tone-purple");
    }
    const list = $("#cardsList");
    const empty = $("#cardsEmpty");
    if (!pools.length) {
      if (list) list.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    if (!list) return;
    list.innerHTML = pools.map((pool) => {
      const id = String(pool.id ?? pool.key ?? "");
      const total = Number(pool.total || 0);
      const available = Number(pool.available || 0);
      const used = Number(pool.used || 0);
      const reserved = Number(pool.reserved ?? Math.max(0, total - available - used));
      const enabled = pool.enabled !== false;
      const stock = "可用 " + available + " / 总 " + total + (reserved > 0 ? " / 预占 " + reserved : "");
      return '<div class="cards-row" data-cards-id="' + esc(id) + '">' +
        '<span class="cards-row-id">' + esc(id || "--") + "</span>" +
        '<div class="cards-row-name"><strong>' + esc(pool.name || "未命名卡密池") + '</strong><small>' + esc(pool.note || "无备注") + "</small></div>" +
        '<span class="badge ' + (available > 0 ? "badge-green" : "badge-muted") + '">' + esc(stock) + "</span>" +
        '<span class="badge ' + (enabled ? "badge-green" : "badge-muted") + '">' + (enabled ? "启用" : "停用") + "</span>" +
        '<div class="cards-row-actions">' +
        '<button class="button button-secondary button-compact" type="button" data-cards-import="' + esc(id) + '"><svg class="icon"><use href="' + ICONS + 'plus"></use></svg><span>批量导入</span></button>' +
        '<button class="button button-secondary button-compact" type="button" data-cards-edit="' + esc(id) + '"><svg class="icon"><use href="' + ICONS + 'edit-3"></use></svg><span>编辑</span></button>' +
        "</div></div>";
    }).join("");
  }

  async function loadTemplates() {
    const context = captureAccountContext();
    const data = await api("/api/bot/templates");
    if (!accountContextMatches(context)) return;
    state.templates = Array.isArray(data?.templates) ? data.templates : [];
    renderTemplates();
  }

  function loadCards(options = {}) {
    const context = captureAccountContext();
    const loadKey = context.epoch + ":" + context.accountKey;
    if (state.cardsLoad?.key === loadKey) return state.cardsLoad.promise;
    if (!options.force && state.cardsAccountKey === context.accountKey) {
      return Promise.resolve(state.cards);
    }
    const load = { key: loadKey, promise: null };
    load.promise = accountScopedApi(context, "/api/bot/cards").then((data) => {
      if (!accountContextMatches(context)) return null;
      state.cards = data && typeof data === "object" ? data : null;
      state.cardsAccountKey = context.accountKey;
      renderCards();
      return state.cards;
    }).finally(() => {
      if (state.cardsLoad === load) state.cardsLoad = null;
    });
    state.cardsLoad = load;
    return load.promise;
  }

  async function createCardPool(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("#cardsCreateSubmit");
    const name = String($("#cardsCreateName").value || "").trim();
    const note = String($("#cardsCreateNote").value || "").trim();
    if (!name) {
      formMessage("#cardsCreateMessage", "卡密池名称必填");
      return;
    }
    const context = captureAccountContext();
    const requestToken = Symbol("cards-create");
    button._requestToken = requestToken;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/cards", {
        method: "PUT",
        body: JSON.stringify({ name, note, codes: [] }),
      });
      if (!accountContextMatches(context)) return;
      state.cards = result && typeof result === "object" ? result : state.cards;
      state.cardsAccountKey = context.accountKey;
      $("#cardsCreateName").value = "";
      $("#cardsCreateNote").value = "";
      formMessage("#cardsCreateMessage", "");
      renderCards();
      showToast("卡密池已保存");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#cardsCreateMessage", error.message || "卡密池保存失败");
    } finally {
      if (button._requestToken === requestToken) {
        delete button._requestToken;
        setBusy(button, false);
      }
    }
  }

  async function openTemplateEditor(templateId = "") {
    const context = captureAccountContext();
    const generation = ++state.templateEditorOpenGeneration;
    try {
      await Promise.all([loadCards(), loadProducts()]);
    } catch (error) {
      if (accountContextMatches(context) && generation === state.templateEditorOpenGeneration) {
        showToast(error.message || "卡密池加载失败，请稍后重试", "error");
      }
      return;
    }
    if (!accountContextMatches(context) || generation !== state.templateEditorOpenGeneration || state.view !== "templates") return;
    const editingId = String(templateId || "");
    const template = editingId ? state.templates.find((item) => String(item.id) === editingId) : null;
    const editing = Boolean(template);
    const deliveryInfo = editing ? templateDeliveryInfo(template) : { delivery: "redeem" };
    state.templateEditor = {
      editingId: editing ? String(template.id) : "",
      productIds: editing ? (Array.isArray(template.item_ids) ? template.item_ids.map(String) : []) : [],
      resourceMatch: editing && Array.isArray(template.resource_match) ? template.resource_match.map(String) : [],
      delivery: deliveryInfo.delivery,
    };
    text("#templateEditorTitle", editing ? "编辑发货模板" : "创建发货模板");
    const deliveryTypeInput = $("#templateDeliveryTypeInput");
    if (deliveryTypeInput) deliveryTypeInput.value = deliveryInfo.delivery;
    $("#templateNameInput").value = template?.name || "";
    $("#templateDeliveryInput").value = template?.description || "";
    $("#templatePriceInput").value = typeof template?.price === "number" && template.price ? String(template.price) : (template?.price || "");
    const select = $("#templateCardPoolSelect");
    const pools = cardsPools();
    const selectedPool = deliveryInfo.delivery === "redeem" ? String(pools[0]?.name || pools[0]?.id || "") : "";
    if (select) {
      select.innerHTML = '<option value="">无（纯话术/网盘链接）</option>' +
        pools.map((pool) => '<option value="' + esc(String(pool.name || pool.id || "")) + '">' + esc(pool.name || "未命名卡密池") + "</option>").join("");
      select.value = String(selectedPool || "");
    }
    const picker = $("#templateProductPicker");
    const products = state.products.slice(0, 20);
    if (picker) {
      if (!products.length) {
        picker.innerHTML = '<div class="template-product-empty">当前店铺还没有可绑定的商品，请先同步商品。</div>';
      } else {
        picker.innerHTML = products.map((product) => {
          const id = String(product.id || "");
          const checked = state.templateEditor.productIds.includes(id);
          return '<label class="template-product-option"><input type="checkbox" data-template-product="' + esc(id) + '" value="' + esc(id) + '"' + (checked ? " checked" : "") + '><span>' + esc(product.title || "未命名商品") + '</span><small>' + esc(product.price_display || "") + "</small></label>";
        }).join("");
      }
    }
    formMessage("#templateEditorMessage", "");
    const dialog = $("#templateEditorDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function reconcileTemplateItemSelection() {
    const renderedInputs = $$('#templateProductPicker [data-template-product]');
    const renderedIds = new Set(renderedInputs.map((input) => String(input.value)));
    const checkedIds = renderedInputs.filter((input) => input.checked).map((input) => String(input.value));
    const originalIds = (state.templateEditor?.productIds || []).map(String);
    const catalogStatusKnown = state.productsAccountKey === state.activeAccountKey
      && state.productsTruncatedAccountKey === state.activeAccountKey;
    if (!catalogStatusKnown) {
      return Array.from(new Set(checkedIds.concat(originalIds.filter((itemId) => !renderedIds.has(itemId)))));
    }
    const validProductIds = new Set(state.products.map((product) => String(product.id || "")).filter(Boolean));
    const validCheckedIds = checkedIds.filter((itemId) => validProductIds.has(itemId));
    const preservedIds = originalIds.filter((itemId) => {
      if (renderedIds.has(itemId)) return false;
      return state.productsTruncated === true || validProductIds.has(itemId);
    });
    return Array.from(new Set(validCheckedIds.concat(preservedIds)));
  }

  function collectTemplateEditor() {
    const name = $("#templateNameInput").value.trim();
    const description = $("#templateDeliveryInput").value.trim();
    const price = $("#templatePriceInput").value.trim();
    const poolName = $("#templateCardPoolSelect").value.trim();
    const itemIds = reconcileTemplateItemSelection();
    const configuredDelivery = canonicalTemplateDelivery($("#templateDeliveryTypeInput")?.value || state.templateEditor?.delivery, "redeem");
    const delivery = poolName ? "redeem" : configuredDelivery;
    const template = {
      name,
      description,
      price,
      delivery,
      item_ids: itemIds,
      enabled: true,
    };
    if (delivery === "pan") {
      const existingTags = Array.isArray(state.templateEditor?.resourceMatch) ? state.templateEditor.resourceMatch.filter(Boolean) : [];
      template.resource_match = existingTags.length ? existingTags : [name];
    }
    if (state.templateEditor.editingId) template.id = state.templateEditor.editingId;
    const valid = Boolean(name) && Boolean(description);
    return { valid, error: !name ? "请填写模板名称" : !description ? "请填写模板说明" : "", template };
  }

  async function saveTemplate(event) {
    event.preventDefault();
    const collected = collectTemplateEditor();
    if (!collected.valid) {
      formMessage("#templateEditorMessage", collected.error);
      return;
    }
    const button = $("#templateEditorCommit");
    const context = captureAccountContext();
    const requestToken = Symbol("template-save");
    button._requestToken = requestToken;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/templates", { method: "PUT", body: JSON.stringify({ template: collected.template }) });
      if (!accountContextMatches(context)) return;
      const saved = result?.template;
      if (!saved) throw new ApiError("模板保存结果无效");
      const index = state.templates.findIndex((item) => String(item.id) === String(saved.id));
      if (index >= 0) state.templates[index] = saved;
      else state.templates = state.templates.concat([saved]);
      renderTemplates();
      closeDialog("templateEditorDialog");
      showToast(state.templateEditor.editingId ? "模板已保存" : "模板创建成功");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#templateEditorMessage", error.message || "模板保存失败");
    } finally {
      if (button._requestToken === requestToken) {
        delete button._requestToken;
        setBusy(button, false);
      }
    }
  }

  function confirmDeleteTemplate(templateId) {
    const template = state.templates.find((item) => String(item.id) === String(templateId));
    if (!template) return;
    const context = captureAccountContext();
    text("#confirmTitle", "删除发货模板");
    text("#confirmMessage", "删除后该模板不再用于自动发货，已绑定的商品不会受影响。");
    text("#confirmAction", "确认删除");
    state.confirmAction = async () => {
      if (!accountContextMatches(context)) return;
      await accountScopedApi(context, "/api/bot/templates/" + encodeURIComponent(String(templateId)), { method: "DELETE" });
      if (!accountContextMatches(context)) return;
      state.templates = state.templates.filter((item) => String(item.id) !== String(templateId));
      renderTemplates();
      showToast("模板已删除");
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function openCardsEditor(poolId = "", mode = "import") {
    const pools = cardsPools();
    const pool = poolId ? pools.find((item) => String(item.id ?? item.key ?? "") === String(poolId)) : null;
    state.cardsEditor = { editingId: pool ? String(pool.id ?? pool.key ?? "") : "", mode };
    text("#cardsEditorTitle", pool ? "编辑卡密池" : "导入卡密");
    $("#cardsPoolNameInput").value = pool?.name || "";
    $("#cardsNoteInput").value = pool?.note || "";
    $("#cardsCodesInput").value = "";
    formMessage("#cardsEditorMessage", "");
    const dialog = $("#cardsEditorDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function collectCardsEditor() {
    const name = $("#cardsPoolNameInput").value.trim();
    const note = $("#cardsNoteInput").value.trim();
    const codes = Array.from(new Set(
      String($("#cardsCodesInput").value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    ));
    if (!name) return { valid: false, error: "请填写卡密池名称", payload: null };
    if (!state.cardsEditor?.editingId && !codes.length) return { valid: false, error: "请至少粘贴一个卡密", payload: null };
    return {
      valid: true,
      error: "",
      payload: { name, note, codes: codes.map((code) => ({ code })) },
    };
  }

  async function saveCards(event) {
    event.preventDefault();
    const collected = collectCardsEditor();
    if (!collected.valid) {
      formMessage("#cardsEditorMessage", collected.error);
      return;
    }
    const button = $("#cardsEditorCommit");
    const context = captureAccountContext();
    const requestToken = Symbol("cards-save");
    button._requestToken = requestToken;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/cards", { method: "PUT", body: JSON.stringify(collected.payload) });
      if (!accountContextMatches(context)) return;
      const pool = result?.pool;
      if (!pool) throw new ApiError("卡密池保存结果无效");
      state.cards = { pool, stats: result?.stats || state.cards?.stats || null };
      state.cardsAccountKey = context.accountKey;
      renderCards();
      closeDialog("cardsEditorDialog");
      showToast(collected.payload.codes.length ? "卡密已导入并保存" : "卡密池信息已保存");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#cardsEditorMessage", error.message || "卡密保存失败");
    } finally {
      if (button._requestToken === requestToken) {
        delete button._requestToken;
        setBusy(button, false);
      }
    }
  }

  function renderAutomation() {
    const rules = Array.isArray(state.automation?.rules) ? state.automation.rules : [];

    const shopSelect = $("#automationShopSelect");
    if (shopSelect) {
      const accounts = state.accounts.length ? state.accounts : [{ key: state.activeAccountKey || "default", name: currentAccount()?.name || state.bot?.shop_name || "默认店铺", status: "unconfigured" }];
      const activeKey = String(state.activeAccountKey || "default");
      shopSelect.innerHTML = accounts.map((account) => '<option value="' + esc(String(account.key || "default")) + '">' + esc(accountLabel(account)) + "</option>").join("");
      shopSelect.value = activeKey;
    }

    const fields = {
      enabled: $("#automationEnabledToggle"),
      firstReply: $("#automationFirstReply"),
      fallbackReply: $("#automationFallbackReply"),
      delayMin: $("#automationDelayMin"),
      delayMax: $("#automationDelayMax"),
      triggerCooldown: $("#automationTriggerCooldown"),
      manualCooldown: $("#automationManualCooldown"),
      businessHours: $("#automationBusinessHoursEnabled"),
      businessStart: $("#automationBusinessStart"),
      businessEnd: $("#automationBusinessEnd"),
    };
    if (fields.enabled && document.activeElement !== fields.enabled) fields.enabled.checked = state.automation?.enabled !== false;
    if (fields.firstReply && document.activeElement !== fields.firstReply) fields.firstReply.value = state.automation?.first_reply || "";
    if (fields.fallbackReply && document.activeElement !== fields.fallbackReply) fields.fallbackReply.value = state.automation?.fallback_reply || "";
    if (fields.delayMin && document.activeElement !== fields.delayMin) fields.delayMin.value = Number(state.automation?.delay_min_seconds || 0);
    if (fields.delayMax && document.activeElement !== fields.delayMax) fields.delayMax.value = Number(state.automation?.delay_max_seconds || 0);
    if (fields.triggerCooldown && document.activeElement !== fields.triggerCooldown) fields.triggerCooldown.value = Number(state.automation?.trigger_cooldown_seconds || 0);
    if (fields.manualCooldown && document.activeElement !== fields.manualCooldown) fields.manualCooldown.value = Number(state.automation?.manual_takeover_cooldown_seconds || 0);
    if (fields.businessHours && document.activeElement !== fields.businessHours) fields.businessHours.checked = Boolean(state.automation?.business_hours_enabled);
    if (fields.businessStart && document.activeElement !== fields.businessStart) fields.businessStart.value = state.automation?.business_start || "09:00";
    if (fields.businessEnd && document.activeElement !== fields.businessEnd) fields.businessEnd.value = state.automation?.business_end || "23:30";

    const ruleProductOptions = $("#replyRuleProductOptions");
    if (ruleProductOptions) {
      ruleProductOptions.innerHTML = state.products.map((product) => '<option value="' + esc(String(product.id || "")) + '" label="' + esc(product.title || ("商品 " + product.id)) + '"></option>').join("");
    }

    const productById = new Map(state.products.map((product) => [String(product.id || ""), product]));
    const ruleList = $("#replyRuleList");
    const ruleEmpty = $("#replyRuleEmpty");
    if (ruleList && ruleEmpty) {
      ruleEmpty.hidden = Boolean(rules.length);
      ruleList.innerHTML = rules.map((rule, index) => {
        const keywords = (rule.keywords || []).filter(Boolean);
        const keywordChips = keywords.slice(0, 4).map((keyword) => '<span class="rule-keyword">' + esc(keyword) + "</span>").join("")
          + (keywords.length > 4 ? '<span class="rule-keyword rule-keyword-more">+' + (keywords.length - 4) + "</span>" : "");
        const reply = String(rule.reply || "").trim();
        const preview = reply ? (reply.length > 68 ? reply.slice(0, 68) + "…" : reply) : "未填写回复内容";
        const itemId = String(rule.item_id || "");
        const product = productById.get(itemId);
        const scope = itemId ? (product?.title || ("商品 ID：" + itemId)) : "通用规则";
        const name = String(rule.name || ("规则 " + (index + 1)));
        return '<tr class="rule-row" data-rule-index="' + index + '">' +
          '<td data-label="规则"><div class="rule-name"><strong>' + esc(name) + '</strong><small title="' + esc(scope) + '">' + esc(scope) + '</small></div></td>' +
          '<td data-label="关键词"><div class="rule-keywords">' + (keywordChips || '<span class="rule-keyword rule-keyword-more">未设置</span>') + '</div></td>' +
          '<td data-label="回复话术"><span class="rule-reply-preview" title="' + esc(reply) + '">' + esc(preview) + '</span></td>' +
          '<td data-label="状态"><span class="badge ' + (rule.enabled !== false ? "badge-green" : "badge-muted") + ' rule-state-badge">' + (rule.enabled !== false ? "启用" : "停用") + '</span></td>' +
          '<td data-label="操作"><div class="rule-row-actions"><button type="button" data-edit-rule aria-label="编辑规则：' + esc(name) + '">编辑</button><button type="button" data-remove-rule aria-label="删除规则：' + esc(name) + '">删除</button></div></td>' +
          '</tr>';
      }).join("");
    }

    $$('[data-open-batch-delivery]').forEach((button) => { button.disabled = !state.products.length; });
    text("#replyRuleCount", rules.length + " 条");
  }

  function collectAutomation(enabledOverride = null) {
    return {
      strategy: state.automation?.strategy || "standard",
      enabled: typeof enabledOverride === "boolean" ? enabledOverride : $("#automationEnabledToggle").checked,
      first_reply: $("#automationFirstReply").value.trim(),
      fallback_reply: $("#automationFallbackReply").value.trim(),
      delay_min_seconds: Number($("#automationDelayMin").value) || 0,
      delay_max_seconds: Number($("#automationDelayMax").value) || 0,
      trigger_cooldown_seconds: Number($("#automationTriggerCooldown").value) || 0,
      manual_takeover_cooldown_seconds: Number($("#automationManualCooldown").value) || 0,
      business_hours_enabled: $("#automationBusinessHoursEnabled").checked,
      business_start: $("#automationBusinessStart").value || "09:00",
      business_end: $("#automationBusinessEnd").value || "23:30",
    };
  }

  function attentionPendingTotal() {
    return (Array.isArray(state.attention) ? state.attention : []).filter((item) => !item?.resolved).length;
  }

  function attentionCopy(item) {
    const code = !item?.kind || item.kind === "shop_account" ? item?.error_code || item?.code : "";
    const requestStatus = ["risk_control", "risk_cooldown", "verification_required"].includes(code);
    return {
      title: String((requestStatus && COOKIE_STATUS_LABELS[code]) || item?.title || "需要处理"),
      message: String((requestStatus && COOKIE_ERROR_COPY[code]) || item?.message || "当前店铺有一项真实运行状态需要确认。"),
      action: String(item?.action_label || "查看店铺"),
      tone: item?.severity === "error" ? "error" : "warning",
      view: String(item?.action_view || "shops"),
    };
  }

  function renderAttention() {
    const panel = $("#attentionPanel");
    const list = $("#attentionList");
    if (!panel || !list) return;
    const items = (Array.isArray(state.attention) ? state.attention : [])
      .filter((item) => item && typeof item === "object" && item.id)
      .slice(0, 8);
    const pendingTotal = attentionPendingTotal();
    panel.hidden = false;
    text("#attentionCount", pendingTotal);
    const count = $("#attentionCount");
    if (count) count.className = "badge " + (pendingTotal ? "badge-red" : "badge-green");
    if (!items.length) {
      list.innerHTML = '<div class="attention-empty">当前没有需要处理的事项</div>';
      return;
    }
    list.innerHTML = items.map((item) => {
      const copy = attentionCopy(item);
      const resolved = Boolean(item.resolved);
      const icon = resolved ? "circle-check" : copy.tone === "error" ? "circle-alert" : "clock";
      const statusIcon = resolved ? '<svg class="icon"><use href="' + ICONS + 'check"></use></svg>' : "";
      return '<div class="attention-row' + (copy.tone === "warning" ? " is-warning" : "") + (resolved ? " is-resolved" : "") + '" data-attention-id="' + esc(item.id) + '">' +
        '<svg class="icon"><use href="' + ICONS + '' + icon + '"></use></svg>' +
        '<div class="attention-copy"><strong>' + esc(copy.title) + '</strong><p>' + esc(copy.message) + '</p></div>' +
        '<div class="attention-actions">' +
          '<button class="button button-secondary" type="button" data-view="' + esc(copy.view) + '">' + esc(copy.action) + '</button>' +
          '<button class="button attention-status-button' + (resolved ? " is-resolved" : "") + '" type="button" data-attention-toggle="' + esc(item.id) + '" aria-pressed="' + String(resolved) + '" aria-label="' + (resolved ? "恢复为待处理" : "标记为已处理") + '">' + statusIcon + '<span>' + (resolved ? "已处理" : "待处理") + '</span></button>' +
        '</div>' +
      '</div>';
    }).join("");
  }

  async function toggleAttentionResolution(attentionId, button) {
    const selected = state.attention.find((item) => String(item?.id || "") === String(attentionId || ""));
    if (!selected) return;
    const context = captureAccountContext();
    setBusy(button, true);
    try {
      const data = await api("/api/bot/attention/" + encodeURIComponent(selected.id), {
        method: "PUT",
        body: JSON.stringify({ resolved: !selected.resolved }),
      });
      if (!accountContextMatches(context)) return;
      state.attention = Array.isArray(data?.items) ? data.items : [];
      renderAttention();
      renderHomeStats();
      showToast(selected.resolved ? "已恢复为待处理" : "已标记为处理完成");
    } catch (error) {
      if (accountContextMatches(context)) showToast(error.message || "预警状态更新失败", "error");
    } finally {
      if (accountContextMatches(context)) setBusy(button, false);
    }
  }

  function renderHomeStats() {
    const host = $("#homeStatCards");
    if (!host) return;
    const totals = state.todayAnalytics?.totals && typeof state.todayAnalytics.totals === "object"
      ? state.todayAnalytics.totals
      : (state.analytics?.totals && typeof state.analytics.totals === "object" ? state.analytics.totals : null);
    if (totals) {
      const buyerMessages = Number(totals.buyer_messages_total ?? totals.messages_total ?? 0);
      const autoReplies = Number(totals.auto_replies_total || 0);
      const fulfillmentSuccess = Number(totals.fulfillment_success_total || 0);
      const unreadConversations = Number(totals.unread_conversations_total || 0);
      host.innerHTML =
        statCard("今日买家消息", buyerMessages, "message-square-text", "tone-yellow") +
        statCard("今日自动回复", autoReplies, "bot", "tone-blue") +
        statCard("今日发货成功", fulfillmentSuccess, "package-check", "tone-green") +
        statCard("当前未读会话", unreadConversations, "clock", "tone-amber");
      return;
    }
    host.innerHTML =
      statCard("今日买家消息", "--", "message-square-text", "tone-yellow") +
      statCard("今日自动回复", "--", "bot", "tone-blue") +
      statCard("今日发货成功", "--", "package-check", "tone-green") +
      statCard("当前未读会话", "--", "clock", "tone-amber");
  }

  function renderAnalyticsChart() {
    const chart = $("#analyticsChart");
    const periodHost = $("#analyticsPeriod");
    if (!chart) return;
    if (periodHost) {
      $$("[data-period]", periodHost).forEach((button) => {
        const active = Number(button.dataset.period || 7) === Number(state.trendPeriod || 7);
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    }
    const buckets = Array.isArray(state.trendAnalytics?.buckets) ? state.trendAnalytics.buckets : [];
    const emptyNote = chart.parentElement?.querySelector(".chart-empty") || null;
    if (!buckets.length) {
      chart.innerHTML = "";
      if (emptyNote) emptyNote.hidden = false;
      return;
    }
    if (emptyNote) emptyNote.hidden = true;
    const peak = Math.max(
      ...buckets.map((b) => Math.max(Number(b?.buyer_messages_total ?? b?.messages_total ?? 0), Number(b?.auto_replies_total || 0))),
      1
    );
    chart.innerHTML = buckets.map((bucket) => {
      const buyerVal = Number(bucket?.buyer_messages_total ?? bucket?.messages_total ?? 0);
      const replyVal = Number(bucket?.auto_replies_total || 0);
      const buyerHeight = peak > 0 && buyerVal > 0 ? Math.max(4, Math.round((buyerVal / peak) * 100)) : 0;
      const replyHeight = peak > 0 && replyVal > 0 ? Math.max(4, Math.round((replyVal / peak) * 100)) : 0;
      const date = String(bucket?.date || "");
      const label = date.length >= 10 ? date.slice(5, 10).replace("-", "/") : date;
      const tip = `${date || label} · 买家消息 ${buyerVal} 条 · 自动回复 ${replyVal} 条`;
      return '<div class="chart-bar" title="' + esc(tip) + '">' +
        '<div class="chart-bar-bars">' +
          '<svg class="chart-bar-fill is-buyer" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="' + (100 - buyerHeight) + '" width="100" height="' + buyerHeight + '" rx="10"></rect></svg>' +
          '<svg class="chart-bar-fill is-reply" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="' + (100 - replyHeight) + '" width="100" height="' + replyHeight + '" rx="10"></rect></svg>' +
        '</div>' +
        '<span class="chart-bar-label">' + esc(label) + '</span></div>';
    }).join("");
  }

  function renderAnalytics() {
    renderHomeStats();
    renderAnalyticsChart();
  }

  async function loadTrendAnalytics(period = state.trendPeriod) {
    const context = captureAccountContext();
    const p = Number(period) === 30 ? 30 : 7;
    state.trendPeriod = p;
    state.analyticsPeriod = p;
    try {
      const data = await api("/api/bot/analytics?period=" + p);
      if (!accountContextMatches(context)) return null;
      state.trendAnalytics = data || null;
      state.analyticsPage = data || null;
      renderAnalyticsChart();
      return data;
    } catch {
      return null;
    }
  }

  async function loadTodayAnalytics(epoch = state.accountEpoch) {
    const context = captureAccountContext(epoch);
    try {
      const data = await api("/api/bot/analytics?period=1");
      if (!accountContextMatches(context)) return null;
      state.todayAnalytics = data || null;
      state.analytics = data || null;
      renderHomeStats();
      return data;
    } catch {
      return null;
    }
  }

  async function loadAnalytics(period) {
    const p = Number(period);
    if (p === 7 || p === 30) {
      return loadTrendAnalytics(p);
    }
    return loadTodayAnalytics();
  }

  function formatCpuUsage(account) {
    const s = account.metrics_state;
    const w = account.worker_state;
    if (s === "sampling" || (w === "running" && account.cpu_percent === null && s !== "stopped" && s !== "unavailable")) {
      return "采样中...";
    }
    if (s === "stopped" || w === "stopped" || w === "disabled") {
      return "已停止";
    }
    if (s === "unavailable" || account.cpu_percent === null || account.cpu_percent === undefined) {
      return "--";
    }
    const num = Number(account.cpu_percent);
    if (Number.isFinite(num)) {
      return num.toFixed(1) + "%";
    }
    return "--";
  }

  function formatMemoryBytes(bytes) {
    if (bytes === null || bytes === undefined) return "--";
    const num = Number(bytes);
    if (!Number.isFinite(num)) return "--";
    const mib = Math.round(num / (1024 * 1024));
    return mib + " MiB";
  }

  function formatMemoryLimitCell(account) {
    if (account.memory_limit_bytes === null || account.memory_limit_bytes === undefined) return "--";
    return formatMemoryBytes(account.memory_limit_bytes);
  }

  function formatNextStartupMemoryCell(account) {
    if (account.configured_memory_limit_bytes === null || account.configured_memory_limit_bytes === undefined) return "--";
    const base = formatMemoryBytes(account.configured_memory_limit_bytes);
    if (account.pending_restart === true) {
      return base + ' <span class="badge badge-amber" title="新配置将在该店铺下次重启时生效">待重启生效</span>';
    }
    return base;
  }

  function formatUptime(account) {
    if (!account) return "--";
    if (account.worker_state === "stopped" || account.metrics_state === "stopped") {
      return "未运行";
    }
    if (account.uptime_seconds === null || account.uptime_seconds === undefined) {
      return "--";
    }
    const sec = Math.floor(Number(account.uptime_seconds));
    if (!Number.isFinite(sec) || sec < 0) return "--";
    if (sec === 0) return "刚刚启动";
    if (sec < 60) return sec + "秒";
    if (sec < 3600) {
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return s > 0 ? `${m}分${s}秒` : `${m}分钟`;
    }
    if (sec < 86400) {
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      return m > 0 ? `${h}小时${m}分` : `${h}小时`;
    }
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    return h > 0 ? `${d}天${h}小时` : `${d}天`;
  }

  function formatWorkerStatusBadge(account) {
    if (!account) return '<span class="badge badge-muted">--</span>';
    if (account.metrics_state === "sampling") {
      return '<span class="badge badge-amber">采样中</span>';
    }
    if (account.worker_state === "running" || account.metrics_state === "ready") {
      return '<span class="badge badge-green">运行中</span>';
    }
    if (account.worker_state === "starting") {
      return '<span class="badge badge-blue">启动中</span>';
    }
    if (account.worker_state === "stopping") {
      return '<span class="badge badge-amber">停止中</span>';
    }
    if (account.worker_state === "disabled" || account.enabled === false) {
      return '<span class="badge badge-muted">已停用</span>';
    }
    if (account.worker_state === "stopped" || account.metrics_state === "stopped") {
      return '<span class="badge badge-muted">已停止</span>';
    }
    return '<span class="badge badge-muted">未知</span>';
  }

  function formatSampledTime(timestamp) {
    if (!timestamp) return "未知时间";
    const numeric = Number(timestamp);
    if (!Number.isFinite(numeric) || numeric <= 0) return "未知时间";
    const ms = numeric > 1e11 ? numeric : numeric * 1000;
    const date = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function isResourcesStale() {
    if (!state.resources?.sampled_at) return false;
    const staleAfter = Number(state.resources.stale_after_seconds) || 15;
    const sampledAt = Number(state.resources.sampled_at);
    if (!Number.isFinite(sampledAt) || sampledAt <= 0) return false;
    const sampledMs = sampledAt > 1e11 ? sampledAt : sampledAt * 1000;
    const ageSeconds = (Date.now() - sampledMs) / 1000;
    return ageSeconds > staleAfter;
  }

  function renderResourcesMessages() {
    const homeMsg = $("#homeResourcesMessage");
    const shopMsg = $("#shopResourcesMessage");
    if (!homeMsg && !shopMsg) return;

    let text = "";
    let className = "resources-message";

    if (state.resourcesError) {
      if (state.resources) {
        const sampledText = state.resources.sampled_at ? ` · 上次采样 ${formatSampledTime(state.resources.sampled_at)}` : "";
        const staleLabel = isResourcesStale() ? "（数据已过期）" : "";
        text = `读取失败（当前显示上次采样数据${staleLabel}${sampledText}）：${state.resourcesError}`;
      } else {
        text = `读取失败：${state.resourcesError}`;
      }
      className = "resources-message resources-message-error";
    } else if (isResourcesStale()) {
      const sampledText = state.resources?.sampled_at ? `（上次采样 ${formatSampledTime(state.resources.sampled_at)}）` : "";
      text = `采样数据已过期${sampledText}，正在等待重新采样`;
      className = "resources-message resources-message-warning";
    } else if (state.resourcesLoading && !state.resources) {
      text = "正在读取店铺运行数据...";
      className = "resources-message resources-message-loading";
    }

    [homeMsg, shopMsg].forEach((el) => {
      if (!el) return;
      el.textContent = text;
      el.className = className;
    });
  }

  function renderHomeResources() {
    const tbody = $("#homeResourceBody");
    if (!tbody) return;
    if (!state.resources) {
      if (state.resourcesError) {
        tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">店铺运行情况读取失败，请稍后重试</td></tr>';
      } else {
        tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">正在加载店铺运行情况...</td></tr>';
      }
      renderResourcesMessages();
      return;
    }
    const accounts = Array.isArray(state.resources.accounts) ? state.resources.accounts.slice(0, 5) : [];
    if (!accounts.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">暂无店铺客服运行记录</td></tr>';
      renderResourcesMessages();
      return;
    }
    tbody.innerHTML = accounts.map((account) => {
      const isActive = account.key === state.activeAccountKey;
      const name = esc(account.name || account.key || "未命名店铺");
      const activeTag = isActive ? ' <span class="badge badge-muted resource-current-badge">当前店</span>' : '';
      return '<tr class="' + (isActive ? "resource-row-active" : "") + '">' +
        '<td><strong>' + name + '</strong>' + activeTag + '</td>' +
        '<td>' + formatWorkerStatusBadge(account) + '</td>' +
        '<td><code>' + esc(formatCpuUsage(account)) + '</code></td>' +
        '<td><strong class="resource-mem-val">' + esc(formatMemoryBytes(account.rss_bytes)) + '</strong></td>' +
        '<td>' + esc(formatMemoryLimitCell(account)) + '</td>' +
        '<td>' + formatNextStartupMemoryCell(account) + '</td>' +
        '<td class="resource-uptime-cell">' + esc(formatUptime(account)) + '</td>' +
        '</tr>';
    }).join("");
    renderResourcesMessages();
  }

  function renderShopResources() {
    const tbody = $("#shopResourcesBody");
    if (!tbody) return;
    if (!state.resources) {
      if (state.resourcesError) {
        tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">店铺运行情况读取失败，请稍后重试</td></tr>';
      } else {
        tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">正在加载店铺运行情况...</td></tr>';
      }
      renderResourcesMessages();
      return;
    }
    const accounts = Array.isArray(state.resources.accounts) ? state.resources.accounts : [];
    if (!accounts.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-cell-empty">暂无店铺客服运行记录</td></tr>';
      renderResourcesMessages();
      return;
    }
    tbody.innerHTML = accounts.map((account) => {
      const isActive = account.key === state.activeAccountKey;
      const name = esc(account.name || account.key || "未命名店铺");
      const activeTag = isActive ? ' <span class="badge badge-muted resource-current-badge">当前店</span>' : '';
      return '<tr class="' + (isActive ? "resource-row-active" : "") + '">' +
        '<td><strong>' + name + '</strong>' + activeTag + '</td>' +
        '<td>' + formatWorkerStatusBadge(account) + '</td>' +
        '<td><code>' + esc(formatCpuUsage(account)) + '</code></td>' +
        '<td><strong class="resource-mem-val">' + esc(formatMemoryBytes(account.rss_bytes)) + '</strong></td>' +
        '<td>' + esc(formatMemoryLimitCell(account)) + '</td>' +
        '<td>' + formatNextStartupMemoryCell(account) + '</td>' +
        '<td class="resource-uptime-cell">' + esc(formatUptime(account)) + '</td>' +
        '</tr>';
    }).join("");
    renderResourcesMessages();
  }

  async function loadShopResources({ silent = false, force = false } = {}) {
    if (!state.me) return null;
    if (state.resourcesInflightPromise) {
      if (force) {
        state.resourcesPendingReload = true;
      }
      return state.resourcesInflightPromise;
    }

    const generation = ++state.resourcesGeneration;
    const username = state.me.username;
    state.resourcesLoading = true;
    renderResourcesMessages();

    state.resourcesInflightPromise = (async () => {
      try {
        let cursor = 0;
        let allAccounts = [];
        let lastData = null;
        const visitedCursors = new Set([0]);

        while (cursor !== null) {
          const data = await api(`/api/bot/resources?cursor=${cursor}&limit=50`);
          if (generation !== state.resourcesGeneration || state.me?.username !== username) {
            return null;
          }
          lastData = data;
          const pageAccounts = Array.isArray(data?.accounts) ? data.accounts : [];
          allAccounts = allAccounts.concat(pageAccounts);

          const rawNext = data?.next_cursor;
          if (rawNext === null || rawNext === undefined || rawNext === "" || pageAccounts.length === 0) {
            break;
          }

          const nextCursor = typeof rawNext === "number" ? rawNext : (typeof rawNext === "string" && /^\d+$/.test(rawNext) ? Number(rawNext) : null);
          if (nextCursor === null || !Number.isSafeInteger(nextCursor) || nextCursor <= cursor || visitedCursors.has(nextCursor)) {
            throw new Error("资源分页游标异常");
          }
          visitedCursors.add(nextCursor);
          cursor = nextCursor;
        }

        state.resources = {
          ...(lastData || {}),
          accounts: allAccounts,
        };
        state.resourcesError = null;
        renderHomeResources();
        renderShopResources();
        renderResourcesMessages();
        if (state.docs?.tab === "resources") {
          renderResourceSettings();
        }
        return state.resources;
      } catch (error) {
        if (generation !== state.resourcesGeneration || state.me?.username !== username) {
          return null;
        }
        state.resourcesError = error?.message || "店铺运行情况读取失败";
        renderHomeResources();
        renderShopResources();
        renderResourcesMessages();
        if (!silent && (state.view === "home" || state.view === "shops")) {
          showToast(state.resourcesError, "error");
        }
        return null;
      } finally {
        if (generation === state.resourcesGeneration) {
          state.resourcesLoading = false;
          state.resourcesInflightPromise = null;
          renderResourcesMessages();
          if (state.resourcesPendingReload) {
            state.resourcesPendingReload = false;
            if (shouldPollResources() && state.me?.username === username) {
              void loadShopResources({ silent: true });
            }
          } else if (shouldPollResources() && !state.resourcesPollTimer) {
            state.resourcesPollTimer = setTimeout(pollResourcesTick, 5000);
          }
        }
      }
    })();

    return state.resourcesInflightPromise;
  }

  function shouldPollResources() {
    return (state.view === "home" || state.view === "shops") && !document.hidden && Boolean(state.me);
  }

  function stopResourcePolling() {
    if (state.resourcesPollTimer) {
      clearTimeout(state.resourcesPollTimer);
      state.resourcesPollTimer = null;
    }
  }

  function syncResourcePolling() {
    if (!shouldPollResources()) {
      stopResourcePolling();
      return;
    }
    if (!state.resourcesPollTimer && !state.resourcesInflightPromise) {
      state.resourcesPollTimer = setTimeout(pollResourcesTick, 5000);
    }
  }

  async function pollResourcesTick() {
    state.resourcesPollTimer = null;
    if (!shouldPollResources()) return;
    const generation = state.resourcesGeneration;
    const username = state.me?.username;
    try {
      await loadShopResources({ silent: true });
    } catch {
      // Polling errors handled within loadShopResources
    } finally {
      if (shouldPollResources() && !state.resourcesPollTimer && !state.resourcesInflightPromise && state.me?.username === username && generation === state.resourcesGeneration) {
        state.resourcesPollTimer = setTimeout(pollResourcesTick, 5000);
      }
    }
  }

  function renderOverview() {
    renderShopStatus();
    renderAttention();
    renderHomeStats();
    renderAnalyticsChart();
    renderHomeOrders();
    renderHomeResources();
    renderResourcesMessages();
    renderProducts();
    renderAutomation();
    renderAiStatus();
  }

  function renderAiStatus() {
    const aiRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules_ai");
    const rulesRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules");
    const available = aiConnectionVerified() && aiStoreHasContent();
    const connected = shopStateView(state.bot || {}).connection === "connected";
    text("#chatAiStatus", aiRunning ? "AI 已开启" : rulesRunning ? "规则回复运行中" : available ? "AI 已暂停" : "AI 连接待配置");
    $("#chatAiStatus").className = "badge " + (aiRunning || rulesRunning ? "badge-green" : available ? "badge-amber" : "badge-muted");
    $("#chatAiStart").hidden = aiRunning || !connected;
    $("#chatAiStart").disabled = aiRunning || !connected;
    $("#chatAiStop").hidden = !aiRunning;
    $("#chatAiStop").disabled = !aiRunning;
  }

  function productTitle(itemId) {
    const product = state.products.find((item) => String(item.id) === String(itemId));
    return product?.title || (itemId ? "商品 " + itemId : "店铺对话");
  }

  function conversationDateValue(conversation) {
    const raw = conversation?.time;
    if (raw === null || raw === undefined || raw === "") return 0;
    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 1000000000) return numeric * 1000;
    const parsed = new Date(raw).getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function serverConversationUnread(conversation) {
    if (!conversation || typeof conversation !== "object") return false;
    if (Object.prototype.hasOwnProperty.call(conversation, "unread_count")) return Number(conversation.unread_count || 0) > 0;
    if (Object.prototype.hasOwnProperty.call(conversation, "unread")) return Boolean(conversation.unread);
    if (Object.prototype.hasOwnProperty.call(conversation, "is_unread")) return Boolean(conversation.is_unread);
    // Older workers do not expose a read cursor.  A latest buyer message is a
    // useful, conservative visual hint until the server grows that field.
    return String(conversation.last_role || "").toLowerCase() === "user";
  }

  function conversationUnread(conversation) {
    const chatId = String(conversation?.chat_id || "");
    const hasServerState = conversation && typeof conversation === "object"
      && (Object.prototype.hasOwnProperty.call(conversation, "unread_count")
        || Object.prototype.hasOwnProperty.call(conversation, "unread")
        || Object.prototype.hasOwnProperty.call(conversation, "is_unread"));
    // Once the API supplies a read cursor, it is authoritative.  The local
    // timestamp is only a compatibility hint for older deployments.
    if (hasServerState) return serverConversationUnread(conversation);
    const readAt = Number(state.inbox?.readAt?.[chatId] || 0);
    const latest = conversationDateValue(conversation);
    if (readAt && (!latest || latest <= readAt)) return false;
    return serverConversationUnread(conversation);
  }

  function serverConversationTakeover(conversation) {
    if (!conversation || typeof conversation !== "object") return false;
    return conversation.manual_mode === true
      || conversation.manual_takeover === true
      || conversation.takeover === true
      || ["manual", "human", "takeover"].includes(String(conversation.processing_mode || conversation.control_mode || conversation.status || "").toLowerCase());
  }

  function conversationTakeover(conversation) {
    const chatId = String(conversation?.chat_id || "");
    const hasServerState = conversation && typeof conversation === "object"
      && (Object.prototype.hasOwnProperty.call(conversation, "takeover")
        || Object.prototype.hasOwnProperty.call(conversation, "manual_mode")
        || Object.prototype.hasOwnProperty.call(conversation, "manual_takeover"));
    if (hasServerState) return serverConversationTakeover(conversation);
    if (Object.prototype.hasOwnProperty.call(state.inbox?.takeover || {}, chatId)) return Boolean(state.inbox.takeover[chatId]);
    return serverConversationTakeover(conversation);
  }

  function filteredConversations() {
    const query = String(state.inbox?.search || "").trim().toLowerCase();
    const unreadOnly = state.inbox?.filter === "unread";
    const takeoverOnly = state.inbox?.filter === "takeover";
    return (state.conversations || []).filter((conversation) => {
      if (unreadOnly && !conversationUnread(conversation)) return false;
      if (takeoverOnly && !conversationTakeover(conversation)) return false;
      if (!query || conversation.search_match === true) return true;
      const haystack = [
        conversation.buyer_label,
        conversation.preview,
        conversation.item_id,
        productTitle(conversation.item_id),
      ].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(query);
    });
  }

  function renderInboxControls() {
    const search = $("#conversationSearch");
    const clear = $("#clearConversationSearch");
    const category = $("#conversationCategory");
    if (search && search.value !== String(state.inbox?.search || "")) search.value = state.inbox?.search || "";
    if (clear) clear.hidden = !String(state.inbox?.search || "").trim();
    if (category && category.value !== String(state.inbox?.filter || "all")) category.value = state.inbox?.filter || "all";
    $$("[data-inbox-filter]").forEach((button) => {
      const active = button.dataset.inboxFilter === (state.inbox?.filter || "all");
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const unread = (state.conversations || []).filter(conversationUnread).length;
    const count = $("#conversationUnreadCount");
    const filterCount = $("#conversationUnreadFilterCount");
    if (count) {
      count.hidden = unread < 1;
      count.textContent = String(unread);
    }
    if (filterCount) filterCount.textContent = String(unread);
  }

  function renderConversations() {
    const list = $("#conversationItems");
    const conversations = filteredConversations();
    text("#conversationCount", conversations.length);
    renderInboxControls();
    if (!conversations.length) {
      const hasAny = (state.conversations || []).length > 0;
      const copy = state.inbox?.search
        ? "没有找到匹配的对话"
        : state.inbox?.filter === "unread" && hasAny
          ? "当前没有未读对话"
          : state.inbox?.filter === "takeover" && hasAny
            ? "当前没有人工接管对话"
            : "还没有对话记录";
      list.innerHTML = '<div class="conversation-empty">' + esc(copy) + "</div>";
      return;
    }
    list.innerHTML = conversations.map((conversation) => {
      const active = conversation.chat_id === state.selectedChatId;
      const unread = conversationUnread(conversation);
      const takeover = conversationTakeover(conversation);
      return '<button class="conversation-item' + (active ? " is-active" : "") + (unread ? " is-unread" : "") + '" type="button" data-chat-id="' + esc(conversation.chat_id) + '" aria-label="' + esc((conversation.buyer_label || "买家咨询") + (unread ? "，未读" : "")) + '">' +
        '<span class="conversation-avatar">买</span>' +
        '<span class="conversation-copy"><strong>' + esc(conversation.buyer_label || "买家咨询") + '</strong><small>' + esc(conversation.preview || "暂无消息") + '</small></span>' +
        '<span class="conversation-item-meta"><span class="conversation-time">' + esc(formatDate(conversation.time)) + '</span>' + (takeover ? '<span class="conversation-mode">人工</span>' : unread ? '<i class="conversation-unread-dot" aria-label="未读"></i>' : "") + '</span></button>';
    }).join("");
  }

  function renderQuickReplies() {
    const bar = $("#quickRepliesBar");
    const manager = $("#quickRepliesManager");
    const replies = Array.isArray(state.quickReplies) ? state.quickReplies : [];
    if (bar) {
      const pills = replies.map((reply) => (
        '<button type="button" class="quick-phrase-pill" data-quick-reply="' + esc(reply.id) + '" title="' + esc(reply.content) + '">' +
        '<svg class="icon"><use href="' + ICONS + 'zap"></use></svg><span>' + esc(reply.title) + "</span></button>"
      )).join("");
      bar.innerHTML = pills + '<button type="button" class="quick-phrase-action-btn" data-open-quick-replies><svg class="icon"><use href="' + ICONS + 'plus"></use></svg><span>自定义</span></button>';
    }
    if (manager) {
      manager.innerHTML = replies.length ? replies.map((reply) => (
        '<div class="quick-reply-manager-row" data-quick-reply-row="' + esc(reply.id) + '">' +
        '<div><strong><svg class="icon"><use href="' + ICONS + 'zap"></use></svg>' + esc(reply.title) + '</strong><small>' + esc(reply.content) + '</small></div>' +
        '<button class="button button-danger button-compact" type="button" data-delete-quick-reply="' + esc(reply.id) + '">删除</button></div>'
      )).join("") : '<div class="automation-empty">还没有快捷短语，可在上方添加。</div>';
    }
  }

  async function loadQuickReplies() {
    const context = captureAccountContext();
    const generation = ++state.quickRepliesGeneration;
    const data = await accountScopedApi(context, "/api/bot/quick-replies");
    if (!accountContextMatches(context) || generation !== state.quickRepliesGeneration) return;
    state.quickReplies = Array.isArray(data?.quick_replies) ? data.quick_replies : [];
    renderQuickReplies();
  }

  async function persistQuickReplies(replies, button = null) {
    const context = captureAccountContext();
    const generation = ++state.quickRepliesGeneration;
    if (button) setBusy(button, true);
    try {
      const data = await accountScopedApi(context, "/api/bot/quick-replies", {
        method: "PUT",
        body: JSON.stringify({ quick_replies: replies }),
      });
      if (!accountContextMatches(context) || generation !== state.quickRepliesGeneration) return false;
      state.quickReplies = Array.isArray(data?.quick_replies) ? data.quick_replies : [];
      renderQuickReplies();
      return true;
    } finally {
      if (button && generation === state.quickRepliesGeneration) setBusy(button, false);
    }
  }

  function openQuickRepliesDialog() {
    renderQuickReplies();
    formMessage("#quickReplyMessage", "");
    const dialog = $("#quickRepliesDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function injectQuickReply(replyId) {
    const reply = state.quickReplies.find((item) => String(item.id) === String(replyId));
    const input = $("#manualReplyInput");
    if (!reply || !input) return;
    input.value = String(reply.content || "");
    input.focus();
    formMessage("#replyMessage", "已填入快捷短语", true);
  }

  async function addQuickReply(event) {
    event.preventDefault();
    const title = String($("#quickReplyTitle")?.value || "").trim();
    const content = String($("#quickReplyContent")?.value || "").trim();
    if (!title || title.length > 10) {
      formMessage("#quickReplyMessage", "短语标题需为 1 至 10 个字符");
      return;
    }
    if (!content || content.length > 1000) {
      formMessage("#quickReplyMessage", "回复内容需为 1 至 1000 个字符");
      return;
    }
    if (state.quickReplies.length >= 20) {
      formMessage("#quickReplyMessage", "快捷短语最多保存 20 条");
      return;
    }
    const button = event.submitter;
    const next = state.quickReplies.concat([{ id: "quick-" + newClientRequestId().replace(/-/g, "").slice(0, 12), title, content }]);
    try {
      const saved = await persistQuickReplies(next, button);
      if (!saved) return;
      event.currentTarget.reset();
      formMessage("#quickReplyMessage", "快捷短语已添加", true);
      showToast("快捷短语已添加");
    } catch (error) {
      formMessage("#quickReplyMessage", error.message || "快捷短语保存失败");
    }
  }

  async function deleteQuickReply(replyId, button) {
    const next = state.quickReplies.filter((item) => String(item.id) !== String(replyId));
    try {
      const saved = await persistQuickReplies(next, button);
      if (saved) showToast("快捷短语已移除");
    } catch (error) {
      formMessage("#quickReplyMessage", error.message || "快捷短语删除失败");
    }
  }

  function parseMessageMediaValue(value) {
    let parsed = value;
    if (typeof parsed === "string") {
      const raw = parsed.trim();
      if (!raw) return [];
      try {
        parsed = JSON.parse(raw);
      } catch (error) {
        return [];
      }
    }
    if (parsed && !Array.isArray(parsed) && typeof parsed === "object" && Array.isArray(parsed.media)) {
      parsed = parsed.media;
    } else if (parsed && !Array.isArray(parsed) && typeof parsed === "object") {
      parsed = [parsed];
    }
    return Array.isArray(parsed) ? parsed : [];
  }

  function looksLikeMediaJson(value) {
    if (typeof value !== "string") return false;
    const raw = value.trim();
    if (!raw || !/[\\[{]/.test(raw)) return false;
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.some((item) => item && typeof item === "object");
      if (!parsed || typeof parsed !== "object") return false;
      return Boolean(parsed.media || parsed.image || parsed.pics || parsed.type || parsed.url || parsed.path);
    } catch (error) {
      return false;
    }
  }

  function messageMediaTypeLabel(type) {
    return ({ image: "图片", emoji: "表情", audio: "音频", video: "视频", file: "文件", link: "链接", unknown: "富媒体" }[type] || "富媒体");
  }

  function isInternalMediaLabel(value, privatePath = "") {
    const label = String(value || "").trim();
    if (!label) return false;
    const path = String(privatePath || "").trim();
    const basename = path ? path.split(/[\\\\/]/).pop() : "";
    return /[\\\\/]/.test(label) || /\.(?:jpe?g|png|gif|webp|bmp|heic)$/i.test(label) || (path && label === path) || (basename && label === basename);
  }

  function normaliseMessageMedia(value, fallbackType = "", content = "") {
    const allowed = new Set(["image", "emoji", "audio", "video", "file", "link", "unknown"]);
    const items = parseMessageMediaValue(value).slice(0, 8).filter((item) => item && typeof item === "object").map((item) => {
      const type = allowed.has(String(item.type || "").toLowerCase()) ? String(item.type).toLowerCase() : "unknown";
      const url = typeof item.url === "string" && /^https:\/\//i.test(item.url.trim()) && item.url.trim().length <= 2048 ? item.url.trim() : "";
      const generic = messageMediaTypeLabel(type);
      const rawLabel = String(item.label || item.alt || "").trim();
      const safeLabel = rawLabel && !isInternalMediaLabel(rawLabel, item.path) ? rawLabel.slice(0, 160) : generic;
      return {
        type,
        url,
        alt: safeLabel || generic,
        label: safeLabel || generic,
        name: "",
      };
    });
    const fallback = String(fallbackType || "").toLowerCase();
    if (!items.length && allowed.has(fallback) && (fallback !== "unknown" || content || looksLikeMediaJson(content))) {
      if (!String(content || "").trim() || looksLikeMediaJson(content)) {
        const label = messageMediaTypeLabel(fallback);
        return [{ type: fallback, url: "", alt: label, label, name: "" }];
      }
    }
    return items;
  }

  function messageContentText(item) {
    const content = typeof item?.content === "string" ? item.content : "";
    return looksLikeMediaJson(content) ? "" : content;
  }

  function handleImageFallback(target) {
    const mode = target?.dataset?.imageFallback || "";
    if (!mode) return;
    if (mode === "message-media") {
      target.closest?.(".message-media-image")?.classList.add("is-broken");
      target.remove?.();
      return;
    }
    target.hidden = true;
    if (target.nextElementSibling) target.nextElementSibling.hidden = false;
  }

  function messageMediaMarkup(value, fallbackType = "", content = "") {
    return normaliseMessageMedia(value, fallbackType, content).map((item) => {
      const label = item.label || messageMediaTypeLabel(item.type);
      if (item.type === "image" && item.url) {
        return '<a class="message-media-image" href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer"><img src="' + esc(item.url) + '" alt="' + esc(item.alt || "图片") + '" loading="lazy" referrerpolicy="no-referrer" data-image-fallback="message-media"><span>' + esc(label) + '</span></a>';
      }
      if (item.url) {
        return '<a class="message-media-link" href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + ' · 查看</a>';
      }
      return '<span class="message-media-placeholder">' + esc(label) + '</span>';
    }).join("");
  }

  function manualImageFileName(file) {
    const name = String(file?.name || "").trim();
    if (name) return name.slice(0, 160);
    const suffix = String(file?.type || "").split("/")[1] || "png";
    return "粘贴的图片." + suffix;
  }

  function manualImageFileSize(size) {
    const bytes = Number(size || 0);
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + " MB";
  }

  function manualImageAttachmentKey(file) {
    return [
      manualImageFileName(file),
      String(file?.type || "").toLowerCase(),
      Number(file?.size || 0),
      Number(file?.lastModified || 0),
    ].join(":");
  }

  function manualReplyAttachmentSnapshot() {
    return JSON.stringify(state.manualReply.attachments.map((attachment) => String(attachment?.key || "")));
  }

  function validateManualImageFile(file) {
    const mime = String(file?.type || "").split(";", 1)[0].trim().toLowerCase();
    const size = Number(file?.size || 0);
    if (!MANUAL_IMAGE_TYPES.has(mime) || size <= 0 || size > MANUAL_IMAGE_MAX_BYTES) {
      formMessage("#replyMessage", "仅支持 8 MB 以内的 JPG、PNG、GIF 或 WebP 图片");
      return false;
    }
    return true;
  }

  function renderManualReplyAttachment() {
    const attachments = state.manualReply.attachments;
    const preview = $("#manualReplyPreview");
    const count = $("#manualReplyImageCount");
    const dropzone = $("#manualReplyDropzone");
    const locked = state.manualReply.submitting || state.manualReply.uploading || state.manualReply.cleaning || manualReplyOperationInFlight();
    if (count) count.textContent = attachments.length + " / " + MANUAL_IMAGE_MAX_COUNT + " 张";
    if (preview) {
      preview.hidden = !attachments.length;
      preview.innerHTML = attachments.map((attachment, index) => {
        const file = attachment?.file;
        const name = manualImageFileName(file);
        const placeholderMarkup = '<span class="reply-image-placeholder"' + (attachment?.previewUrl ? " hidden" : "") + '><svg class="icon"><use href="' + ICONS + 'file-text"></use></svg></span>';
        const previewMarkup = '<span class="reply-image-thumb">' + (attachment?.previewUrl
          ? '<img src="' + esc(attachment.previewUrl) + '" alt="第 ' + (index + 1) + ' 张待发送图片" data-image-fallback="manual-reply">'
          : "") + placeholderMarkup + '</span>';
        const status = state.manualReply.uploadingIndex === index
          ? "正在上传"
          : attachment?.media
            ? "已上传，等待提交"
            : "待上传";
        return '<article class="reply-image-card" data-manual-attachment="' + index + '">' +
          '<span class="reply-image-order" aria-hidden="true">' + (index + 1) + '</span>' +
          previewMarkup +
          '<div class="reply-image-copy"><strong>' + esc(name) + '</strong><small>' + esc(status + " · " + manualImageFileSize(file?.size)) + '</small></div>' +
          '<button class="icon-button" type="button" data-remove-manual-attachment="' + index + '" aria-label="移除第 ' + (index + 1) + ' 张待发送图片" title="移除这张图片"' + (locked ? " disabled" : "") + '><svg class="icon"><use href="' + ICONS + 'x"></use></svg></button>' +
          '</article>';
      }).join("");
    }
    if (dropzone) dropzone.classList.toggle("has-attachment", attachments.length > 0);
    setManualReplyDragActive(state.manualReply.dragging);
  }

  function appendManualReplyImages(files) {
    if (state.manualReply.submitting || state.manualReply.uploading || state.manualReply.cleaning || manualReplyOperationInFlight()) return false;
    const selectedFiles = Array.from(files || []).filter(Boolean);
    if (!selectedFiles.length) return false;
    const selected = state.conversations.find((item) => String(item.chat_id) === String(state.selectedChatId));
    if (!selected || !conversationTakeover(selected)) {
      formMessage("#replyMessage", "请先人工接管当前对话再发送图片");
      return false;
    }
    if (state.manualReply.attachments.length + selectedFiles.length > MANUAL_IMAGE_MAX_COUNT) {
      formMessage("#replyMessage", "人工回复最多可添加 8 张图片，本次选择未加入");
      return false;
    }
    if (!selectedFiles.every(validateManualImageFile)) return false;
    state.manualReply.request = null;
    selectedFiles.forEach((file) => {
      let previewUrl = "";
      try {
        previewUrl = URL.createObjectURL(file);
      } catch (error) {
        previewUrl = "";
      }
      state.manualReply.attachments.push({
        key: manualImageAttachmentKey(file),
        file,
        media: null,
        previewUrl,
      });
    });
    formMessage(
      "#replyMessage",
      selectedFiles.length + " 张图片已追加，将按当前顺序逐张发送",
      true,
    );
    renderChat();
    return true;
  }

  function handleManualReplyFileSelection(event) {
    const files = Array.from(event.currentTarget.files || []);
    event.currentTarget.value = "";
    appendManualReplyImages(files);
  }

  async function uploadManualReplyFile(file, chatId) {
    const result = await api("/api/bot/messages/image?chat_id=" + encodeURIComponent(chatId), {
      method: "POST",
      headers: {
        "Content-Type": file.type,
        "X-File-Name": manualImageFileName(file),
      },
      body: file,
      timeoutMs: MANUAL_REPLY_UPLOAD_TIMEOUT_MS,
      suppressSessionReset: true,
    });
    const media = result?.media;
    if (!media || media.type !== "image" || !media.path) {
      throw new ApiError("图片上传结果无效，请重试");
    }
    return media;
  }

  function removeManualReplyAttachment(index) {
    if (state.manualReply.submitting || state.manualReply.uploading || state.manualReply.cleaning || manualReplyOperationInFlight()) return;
    const attachmentIndex = Number(index);
    if (!Number.isInteger(attachmentIndex) || attachmentIndex < 0 || attachmentIndex >= state.manualReply.attachments.length) return;
    const accountKey = state.activeAccountKey;
    const [attachment] = state.manualReply.attachments.splice(attachmentIndex, 1);
    state.manualReply.request = null;
    revokeManualReplyAttachmentPreview(attachment);
    const file = $("#manualReplyFile");
    if (file) file.value = "";
    formMessage("#replyMessage", "已移除第 " + (attachmentIndex + 1) + " 张待发送图片", true);
    renderChat();
    if (attachment?.media?.path) void deleteManualReplyUploadedMedia(attachment.media, accountKey);
  }

  function handleManualReplyAttachmentClick(event) {
    const button = event.target.closest("[data-remove-manual-attachment]");
    if (!button) return;
    removeManualReplyAttachment(button.dataset.removeManualAttachment);
  }

  function transferHasFiles(transfer) {
    return Array.from(transfer?.types || []).some((type) => String(type).toLowerCase() === "files");
  }

  function clipboardImageFiles(clipboard) {
    const items = Array.from(clipboard?.items || []);
    const files = items
      .filter((item) => item.kind === "file" && String(item.type || "").toLowerCase().startsWith("image/"))
      .map((item) => item.getAsFile?.())
      .filter(Boolean);
    if (files.length) return files;
    return Array.from(clipboard?.files || []).filter((file) => String(file?.type || "").toLowerCase().startsWith("image/"));
  }

  function handleManualReplyPaste(event) {
    const files = clipboardImageFiles(event.clipboardData);
    if (!files.length) return;
    event.preventDefault();
    event.stopPropagation();
    appendManualReplyImages(files);
  }

  function handleManualReplyDragEnter(event) {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    setManualReplyDragActive(true);
  }

  function handleManualReplyDragOver(event) {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    setManualReplyDragActive(true);
  }

  function handleManualReplyDragLeave(event) {
    if (!transferHasFiles(event.dataTransfer)) return;
    if (event.currentTarget.contains(event.relatedTarget)) return;
    event.preventDefault();
    event.stopPropagation();
    setManualReplyDragActive(false);
  }

  function handleManualReplyDrop(event) {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    setManualReplyDragActive(false);
    appendManualReplyImages(event.dataTransfer.files);
  }

  const MANUAL_REPLY_STATUS_LABELS = {
    draft: "未发送",
    queued: "等待发送",
    sending: "正在发送",
    retry: "正在重试",
    acknowledged: "闲鱼已接收",
    dead_letter: "发送终止，需处理",
    manual_review: "未发送，需重新处理",
    failed: "未发送",
    waiting: "等待前序消息",
    unknown: "状态待确认",
  };

  function manualReplyStatusClass(status) {
    if (status === "acknowledged") return "is-success";
    if (["dead_letter", "manual_review", "failed"].includes(status)) return "is-error";
    if (status === "retry") return "is-retry";
    if (["sending", "queued"].includes(status)) return "is-active";
    return "is-waiting";
  }

  function normaliseManualReplyParts(value) {
    if (!Array.isArray(value)) return [];
    const statuses = new Set(Object.keys(MANUAL_REPLY_STATUS_LABELS));
    return value.slice(0, MANUAL_IMAGE_MAX_COUNT + 1).map((part, position) => {
      const kind = String(part?.kind || "").toLowerCase();
      if (!["image", "text"].includes(kind)) return null;
      const index = Number(part?.index);
      const status = String(part?.status || "unknown").toLowerCase();
      return {
        index: Number.isInteger(index) && index >= 0 ? index : position,
        kind,
        status: statuses.has(status) ? status : "unknown",
      };
    }).filter(Boolean).sort((left, right) => left.index - right.index);
  }

  function manualReplyProgressSummary(parts, parentStatus) {
    const imageParts = parts.filter((part) => part.kind === "image");
    const textPart = parts.find((part) => part.kind === "text");
    const acknowledgedImages = imageParts.filter((part) => part.status === "acknowledged").length;
    const current = parts.find((part) => part.status !== "acknowledged") || null;
    if (parentStatus === "acknowledged" || !current) return "全部分段已被闲鱼接收";
    const terminal = ["dead_letter", "manual_review", "failed"].includes(parentStatus)
      || ["dead_letter", "manual_review", "failed"].includes(current.status);
    if (current.kind === "image") {
      const imageNumber = imageParts.findIndex((part) => part.index === current.index) + 1;
      if (terminal) {
        return (acknowledgedImages ? "已接收 " + acknowledgedImages + "/" + imageParts.length + " 张图片，" : "")
          + "第 " + imageNumber + " 张及后续内容未发送";
      }
      const currentCopy = {
        queued: "等待发送",
        sending: "正在发送",
        retry: "正在重试",
        waiting: "等待前序消息",
        unknown: "状态待确认",
      }[current.status] || MANUAL_REPLY_STATUS_LABELS[current.status];
      return (acknowledgedImages ? "已接收 " + acknowledgedImages + "/" + imageParts.length + " 张图片，" : "")
        + "第 " + imageNumber + "/" + imageParts.length + " 张图片" + currentCopy;
    }
    if (current.kind === "text") {
      const prefix = imageParts.length ? acknowledgedImages + " 张图片已接收，" : "";
      if (terminal) return prefix + "文字未发送，需重新处理";
      const currentCopy = {
        queued: "等待发送",
        sending: "正在发送",
        retry: "正在重试",
        waiting: "等待前序消息",
        unknown: "状态待确认",
      }[current.status] || MANUAL_REPLY_STATUS_LABELS[current.status];
      return prefix + "文字" + currentCopy;
    }
    return textPart ? "图片按顺序发送，文字最后发送" : "图片按顺序逐张发送";
  }

  function manualReplyDeliveryMarkup(item) {
    const status = String(item?.delivery_status || item?.status || "unknown").toLowerCase();
    const safeStatus = Object.prototype.hasOwnProperty.call(MANUAL_REPLY_STATUS_LABELS, status) ? status : "unknown";
    const parts = normaliseManualReplyParts(item?.parts);
    const statusClass = manualReplyStatusClass(safeStatus);
    if (!parts.length) {
      const fallback = item?.role === "assistant_manual_draft" ? "未发送" : "";
      const label = MANUAL_REPLY_STATUS_LABELS[safeStatus] || fallback;
      return label ? '<span class="message-status ' + statusClass + '">' + esc(label) + '</span>' : "";
    }
    let imageNumber = 0;
    const partsMarkup = parts.map((part) => {
      const label = part.kind === "image" ? "图片 " + (++imageNumber) : "文字";
      return '<li class="message-part-status ' + manualReplyStatusClass(part.status) + '" data-part-kind="' + part.kind + '" data-part-status="' + part.status + '"><span>' + esc(label) + '</span><strong>' + esc(MANUAL_REPLY_STATUS_LABELS[part.status]) + '</strong></li>';
    }).join("");
    return '<div class="message-delivery" data-parent-status="' + safeStatus + '">' +
      '<div class="message-delivery-parent ' + statusClass + '"><span>父任务</span><strong>' + esc(MANUAL_REPLY_STATUS_LABELS[safeStatus]) + '</strong></div>' +
      '<p class="message-delivery-summary">' + esc(manualReplyProgressSummary(parts, safeStatus)) + '</p>' +
      '<ol class="message-part-list" aria-label="发送分段状态">' + partsMarkup + '</ol>' +
      '</div>';
  }

  function manualReplyFeedbackText(reply) {
    const status = String(reply?.status || reply?.delivery_status || "queued").toLowerCase();
    const safeStatus = Object.prototype.hasOwnProperty.call(MANUAL_REPLY_STATUS_LABELS, status) ? status : "unknown";
    const parts = normaliseManualReplyParts(reply?.parts);
    if (safeStatus === "acknowledged") return parts.length > 1 ? "全部发送分段已被闲鱼接收" : "闲鱼已接收这条回复";
    if (["dead_letter", "manual_review", "failed"].includes(safeStatus)) return "父任务未完成，剩余分段需要重新处理";
    if (parts.length) return "父任务已提交；" + manualReplyProgressSummary(parts, safeStatus);
    return safeStatus === "unknown" ? "回复状态暂时无法确认，请刷新查看" : "回复已排队，等待闲鱼确认";
  }

  function renderChat(options = {}) {
    renderConversations();
    renderQuickReplies();
    const selected = state.conversations.find((item) => item.chat_id === state.selectedChatId);
    const selectedProduct = selected ? state.products.find((item) => String(item.id) === String(selected.item_id || "")) : null;
    const last = state.messages[state.messages.length - 1];
    text("#chatBuyerName", selected?.buyer_label || "买家咨询");
    text("#chatItemName", selected ? productTitle(selected.item_id) : "店铺对话");
    text("#chatPinnedProductTitle", selected ? productTitle(selected.item_id) : "当前会话未关联商品");
    text("#chatPinnedProductMeta", selected?.item_id ? "商品 ID：" + selected.item_id : "等待选择买家会话");
    text("#chatPinnedProductPrice", selectedProduct?.price_display || selectedProduct?.price || "--");
    text("#chatPinnedProductThumb", String(selectedProduct?.title || selected?.item_id || "商").trim().slice(0, 1) || "商");
    const messageSearch = $("#chatMessageSearch");
    if (messageSearch && messageSearch.value !== state.messageSearch) messageSearch.value = state.messageSearch;
    text("#chatMessageMatchCount", state.messageSearch ? state.messageMatchCount + " 条匹配" : "0 条匹配");
    const matchBadge = $("#chatMessageMatchCount");
    if (matchBadge) matchBadge.classList.toggle("has-matches", state.messageMatchCount > 0);
    const selectedTakeover = conversationTakeover(selected);
    const selectedUnread = conversationUnread(selected);
    const takeoverBadge = $("#chatTakeoverBadge");
    const takeoverButton = $("#toggleChatTakeover");
    const readButton = $("#markConversationRead");
    if (takeoverBadge) {
      takeoverBadge.hidden = !selected || !selectedTakeover;
      takeoverBadge.textContent = "人工接管";
      takeoverBadge.className = "badge badge-amber";
    }
    if (takeoverButton) {
      takeoverButton.hidden = !selected;
      takeoverButton.setAttribute("aria-label", selectedTakeover ? "恢复 AI 自动处理" : "人工接管当前对话");
      takeoverButton.innerHTML = '<svg class="icon"><use href="' + ICONS + '' + (selectedTakeover ? "bot" : "shield-check") + '"></use></svg><span>' + (selectedTakeover ? "恢复 AI" : "人工接管") + "</span>";
    }
    if (readButton) {
      readButton.hidden = !selected || !selectedUnread;
      readButton.disabled = !selected || !selectedUnread;
    }
    const input = $("#manualReplyInput");
    const submit = $("#manualReplyForm button[type=submit]");
    const upload = $("#manualReplyFile");
    const uploadButton = $(".reply-image-button");
    const dropzone = $("#manualReplyDropzone");
    const hasSelection = Boolean(state.selectedChatId);
    const replyLocked = state.manualReply.submitting || state.manualReply.uploading || state.manualReply.cleaning || manualReplyOperationInFlight();
    input.disabled = !hasSelection || replyLocked;
    submit.disabled = !hasSelection || !selectedTakeover || replyLocked;
    if (upload) upload.disabled = !hasSelection || !selectedTakeover || replyLocked;
    if (uploadButton) uploadButton.classList.toggle("is-disabled", !hasSelection || !selectedTakeover || replyLocked);
    if (dropzone) dropzone.classList.toggle("is-disabled", !hasSelection || !selectedTakeover || replyLocked);
    if (!hasSelection) input.placeholder = "选择一个对话后回复";
    else input.placeholder = selectedTakeover ? "输入回复内容（Enter 发送，Shift+Enter 换行）" : "需要人工处理时，先点击“人工接管”";
    renderManualReplyAttachment();
    const area = $("#chatMessages");
    const nearBottom = area ? area.scrollHeight - area.scrollTop - area.clientHeight < 48 : true;
    if (!state.messages.length) {
      area.innerHTML = '<div class="chat-empty"><svg class="icon"><use href="' + ICONS + 'message-square-text"></use></svg><p>' + (hasSelection ? "这个对话还没有消息" : "还没有对话记录") + '</p></div>';
      return;
    }
    area.innerHTML = state.messages.map((item) => {
      const buyer = item.role === "user";
      const manual = item.role === "assistant_manual" || item.role === "assistant_manual_draft";
      const role = buyer ? "买家" : item.role === "assistant_manual_draft" ? "仅草稿" : manual ? "人工回复" : "AI 客服";
      const content = messageContentText(item);
      const mediaMarkup = messageMediaMarkup(item.media, item.content_type, item.content);
      const contentMarkup = content ? '<div class="message-text">' + esc(content) + '</div>' : '';
      const matched = state.messageSearch && item.matched === true;
      const deliveryMarkup = manual ? manualReplyDeliveryMarkup(item) : "";
      return '<div class="message-row ' + (buyer ? "is-buyer" : "is-seller") + '"><span class="message-role">' + role + '</span><div class="message-bubble' + (matched ? " is-matched" : "") + '">' + contentMarkup + mediaMarkup + '</div><time>' + esc(formatDate(item.time)) + '</time>' + deliveryMarkup + "</div>";
    }).join("");
    if (!options.preserveScroll || nearBottom) area.scrollTop = area.scrollHeight;
    if (last && !selected) text("#chatItemName", productTitle(last.item_id));
  }

  function orderRowMarkup(order) {
    const status = String(order.status || "queued");
    const labels = { delivered: "已发送", manual_review: "待人工", retry: "待重试", failed: "发送失败", queued: "处理中" };
    const badges = { delivered: "badge-green", manual_review: "badge-amber", retry: "badge-blue", failed: "badge-red", queued: "badge-muted" };
    const icons = { delivered: "package-check", manual_review: "clock", retry: "refresh-cw", failed: "circle-alert", queued: "clock" };
    const itemSummary = (order.item_id || "未命名商品") + (order.paid_amount ? " · 已支付 ¥" + order.paid_amount : "");
    const iconTone = { delivered: "is-delivered", manual_review: "is-manual", retry: "is-retry", failed: "is-failed" }[status] || "is-manual";
    return '<div class="order-row">' +
      '<span class="order-icon ' + iconTone + '"><svg class="icon"><use href="' + ICONS + (icons[status] || "clock") + '"></use></svg></span>' +
      '<div class="order-main"><strong class="order-id">' + esc(order.order_key || "--") + '</strong><small class="order-item">' + esc(itemSummary) + "</small></div>" +
      '<time class="order-time">' + esc(formatDate(order.paid_at || order.time || order.created_at)) + "</time>" +
      '<span class="badge ' + (badges[status] || "badge-muted") + '">' + esc(labels[status] || status) + "</span>" +
      "</div>";
  }

  function renderHomeOrders() {
    const homeList = $("#homeOrderList");
    if (!homeList) return;
    const orders = Array.isArray(state.orders) ? state.orders.slice(0, 5) : [];
    if (!orders.length) {
      homeList.innerHTML = '<tr><td colspan="5" class="table-cell-empty">暂无最近订单</td></tr>';
      return;
    }
    const statusLabels = { delivered: "已发送", manual_review: "待人工", retry: "待重试", failed: "发送失败", queued: "处理中", processing: "处理中", sent: "已发送" };
    const statusBadges = { delivered: "badge-green", manual_review: "badge-amber", retry: "badge-blue", failed: "badge-red", queued: "badge-muted", processing: "badge-blue", sent: "badge-green" };
    homeList.innerHTML = orders.map((order) => {
      const status = String(order.status || "queued");
      const badge = statusBadges[status] || "badge-muted";
      const label = statusLabels[status] || status;
      const buyer = esc(order.buyer_nick || order.buyer || "--");
      const itemTitle = esc(order.title || order.item_title || order.item_id || "--");
      const price = esc(order.paid_amount ? "¥" + order.paid_amount : (order.price_display || (order.amount ? "¥" + order.amount : "--")));
      const time = esc(formatDate(order.paid_at || order.time || order.created_at));
      return '<tr>' +
        '<td><strong>' + buyer + '</strong></td>' +
        '<td class="order-item-cell" title="' + itemTitle + '">' + itemTitle + '</td>' +
        '<td class="price-cell">' + price + '</td>' +
        '<td><span class="badge ' + badge + '">' + esc(label) + '</span></td>' +
        '<td><time class="order-time">' + time + '</time></td>' +
        '</tr>';
    }).join("");
  }

  const DEFAULT_STATUS_OPTIONS = [
    { value: "all", label: "全部" },
    { value: "processing", label: "处理中" },
    { value: "sent", label: "已发送" },
    { value: "manual", label: "待人工" },
    { value: "retry", label: "待重试" },
    { value: "ended", label: "已结束" },
    { value: "exception", label: "其他异常" },
  ];

  function createInitialOrdersPageState() {
    return {
      generation: 0,
      activeRequest: null,
      loading: false,
      error: null,
      items: [],
      page: 1,
      pageSize: 15,
      total: 0,
      totalPages: 0,
      status: "all",
      searchField: "all",
      q: "",
      dateFrom: "",
      dateTo: "",
      statusCounts: {},
      statusOptions: [],
      expandedKeys: new Set(),
    };
  }

  function resetOrdersPageState() {
    state.ordersPage = createInitialOrdersPageState();
  }

  function parseDateRangeToIso(dateFromStr, dateToStr) {
    let fromIso = "";
    let toIso = "";
    if (dateFromStr && /^\d{4}-\d{2}-\d{2}$/.test(dateFromStr)) {
      const parts = dateFromStr.split("-").map(Number);
      const dt = new Date(parts[0], parts[1] - 1, parts[2]);
      if (!Number.isNaN(dt.getTime())) {
        fromIso = dt.toISOString();
      }
    }
    if (dateToStr && /^\d{4}-\d{2}-\d{2}$/.test(dateToStr)) {
      const parts = dateToStr.split("-").map(Number);
      // next day local midnight: left-closed, right-open
      const dt = new Date(parts[0], parts[1] - 1, parts[2] + 1);
      if (!Number.isNaN(dt.getTime())) {
        toIso = dt.toISOString();
      }
    }
    return { fromIso, toIso };
  }

  function formatPaidAmount(value) {
    if (value === null || value === undefined || value === "") return "未获取";
    let str = String(value).trim();
    if (!str) return "未获取";
    str = str.replace(/^[¥￥$]\s*/, "");
    if (!/^[-+]?\d+(\.\d+)?$/.test(str)) {
      return str ? "¥" + str : "未获取";
    }
    const isNeg = str.startsWith("-");
    if (isNeg || str.startsWith("+")) str = str.slice(1);
    const parts = str.split(".");
    const intPart = parts[0] || "0";
    let decPart = parts[1] || "";
    if (decPart.length === 0) decPart = "00";
    else if (decPart.length === 1) decPart += "0";
    return (isNeg ? "-¥" : "¥") + intPart + "." + decPart;
  }

  function formatDetailDateTime(value) {
    if (!value) return "--";
    const num = Number(value);
    const date = Number.isFinite(num) && num > 1000000000 ? new Date(num * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function orderStatusBadgeClass(statusGroup, status) {
    switch (statusGroup || status) {
      case "sent":
      case "delivered":
        return "badge-green";
      case "processing":
      case "queued":
        return "badge-blue";
      case "manual":
      case "manual_review":
        return "badge-amber";
      case "retry":
        return "badge-purple";
      case "ended":
        return "badge-muted";
      case "exception":
      case "failed":
        return "badge-red";
      default:
        return "badge-muted";
    }
  }

  function orderProductThumb(order) {
    const fakeProduct = {
      image_url: order.item_image_url,
      title: order.item_title || order.item_id || "闲",
    };
    const title = String(fakeProduct.title).trim();
    const image = productImageUrl(fakeProduct);
    if (image) {
      return '<span class="orders-product-thumb product-thumb"><img src="' + esc(image) + '" alt="" loading="lazy" referrerpolicy="no-referrer"></span>';
    }
    if (title) {
      return '<span class="orders-product-thumb product-thumb"><span class="product-monogram" aria-hidden="true">' + esc(title.slice(0, 1)) + '</span></span>';
    }
    return '<span class="orders-product-thumb product-thumb"><svg class="icon"><use href="' + ICONS + 'box"></use></svg></span>';
  }

  async function copyOrderText(textToCopy) {
    const value = String(textToCopy || "").trim();
    if (!value) return;
    let copied = false;
    if (navigator?.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        copied = true;
      } catch {
        copied = false;
      }
    }
    if (!copied) {
      try {
        const input = document.createElement("input");
        input.value = value;
        input.className = "clipboard-fallback-input";
        document.body.appendChild(input);
        input.focus();
        input.select();
        copied = document.execCommand("copy");
        document.body.removeChild(input);
      } catch {
        copied = false;
      }
    }
    if (copied) {
      showToast("已复制编号: " + value);
    } else {
      window.prompt("请手动复制编号：", value);
    }
  }

  async function handleOrderChat(chatId) {
    const targetChatId = String(chatId || "").trim();
    if (!targetChatId) {
      showToast("该订单暂无关联的买家会话", "warning");
      return;
    }
    const context = captureAccountContext();
    showView("chat");
    if (!accountContextMatches(context)) return;
    try {
      await selectConversation(targetChatId);
      if (!accountContextMatches(context)) return;
      if (!state.selectedChatId || state.selectedChatId !== targetChatId) {
        showToast("未找到该订单关联的买家会话记录", "warning");
      }
    } catch (error) {
      if (accountContextMatches(context)) {
        showToast(error.message || "买家会话记录读取失败", "error");
      }
    }
  }

  function orderDetailCardMarkup(order) {
    const fields = [
      { label: "平台订单号", value: order.platform_order_id || "--" },
      { label: "内部记录号", value: order.order_key || "--" },
      { label: "商品ID", value: order.item_id || "--" },
      { label: "商品标题", value: order.item_title || "--" },
      { label: "买家ID", value: order.buyer_id || "--" },
      { label: "会话ID", value: order.chat_id || "--" },
      { label: "履约方式", value: order.delivery_type_label || order.delivery_type || "--" },
      { label: "平台状态", value: order.platform_status_label || (order.platform_status ? "状态码 " + order.platform_status : "--") },
      { label: "履约状态", value: order.status_label || order.status || "--" },
      { label: "异常原因", value: order.reason_label || (order.reason_code ? "原因码 " + order.reason_code : "--") },
      { label: "人工复核", value: order.review_resolution_label || (order.review_status ? (order.review_status === "open" ? "待复核" : order.review_status) : "--") },
      { label: "实付金额", value: formatPaidAmount(order.paid_amount) },
      { label: "购买数量", value: order.quantity != null ? String(order.quantity) : "--" },
      { label: "记录创建时间", value: formatDetailDateTime(order.created_at) },
      { label: "最近更新时间", value: formatDetailDateTime(order.updated_at) },
      { label: "消息发送时间", value: formatDetailDateTime(order.delivered_at) },
      { label: "平台发货标记时间", value: formatDetailDateTime(order.platform_shipped_at) },
    ];
    return '<div class="orders-detail-card">' +
      fields.map((field) => '<div class="orders-detail-item"><strong>' + esc(field.label) + '</strong><span>' + esc(field.value) + '</span></div>').join("") +
      '</div>';
  }

  function orderTableRowMarkup(order) {
    const key = String(order.order_key || "");
    const expanded = state.ordersPage.expandedKeys.has(key);
    const platformId = order.platform_order_id ? String(order.platform_order_id).trim() : "";
    const orderKey = key || "--";
    const primaryId = platformId || orderKey;
    const subId = platformId && orderKey !== platformId ? orderKey : "";
    const copyId = platformId || orderKey;
    const productTitle = order.item_title || order.item_id || "未命名商品";
    const badgeClass = orderStatusBadgeClass(order.status_group, order.status);
    const statusLabel = order.status_label || order.status || "--";
    const reasonText = order.reason_label || "--";
    const hasChat = Boolean(order.conversation_available && order.chat_id);

    let html = '<tr class="order-table-row' + (expanded ? " is-expanded" : "") + '" data-order-key="' + esc(key) + '">' +
      '<td class="col-expand">' +
        '<button type="button" class="orders-toggle-btn' + (expanded ? " is-expanded" : "") + '" data-order-toggle="' + esc(key) + '" aria-expanded="' + String(expanded) + '" aria-label="' + (expanded ? "收起详情" : "展开详情") + '">' +
          '<svg class="icon"><use href="' + ICONS + 'chevron-down"></use></svg>' +
        '</button>' +
      '</td>' +
      '<td class="col-main">' +
        '<div class="orders-id-cell">' +
          '<strong class="orders-id-text" title="' + esc(primaryId) + '">' + esc(primaryId) + '</strong>' +
          (subId ? '<small class="orders-id-sub" title="内部记录号: ' + esc(subId) + '">内部: ' + esc(subId) + '</small>' : "") +
        '</div>' +
      '</td>' +
      '<td class="col-product">' +
        '<div class="orders-product-cell">' +
          orderProductThumb(order) +
          '<div class="orders-product-info">' +
            '<span class="orders-product-title" title="' + esc(productTitle) + '">' + esc(productTitle) + '</span>' +
            (order.item_id ? '<small class="orders-product-id">ID: ' + esc(order.item_id) + '</small>' : "") +
          '</div>' +
        '</div>' +
      '</td>' +
      '<td class="col-buyer">' +
        '<span class="orders-buyer-text" title="' + esc(order.buyer_id || "--") + '">' + esc(order.buyer_id || "--") + '</span>' +
      '</td>' +
      '<td class="col-amount">' +
        '<span>' + esc(formatPaidAmount(order.paid_amount)) + '</span>' +
      '</td>' +
      '<td class="col-quantity">' +
        '<span>' + esc(order.quantity != null ? String(order.quantity) : "--") + '</span>' +
      '</td>' +
      '<td class="col-status">' +
        '<span class="badge ' + badgeClass + '">' + esc(statusLabel) + '</span>' +
      '</td>' +
      '<td class="col-time">' +
        '<time title="' + esc(order.created_at || "") + '">' + esc(formatDate(order.created_at)) + '</time>' +
      '</td>' +
      '<td class="col-reason">' +
        '<span class="orders-reason-text" title="' + esc(reasonText) + '">' + esc(reasonText) + '</span>' +
      '</td>' +
      '<td class="col-actions">' +
        '<div class="orders-table-actions">' +
          '<button type="button" class="button button-secondary button-compact orders-action-btn" data-order-copy="' + esc(copyId) + '" title="复制编号: ' + esc(copyId) + '" aria-label="复制编号">' +
            '<svg class="icon"><use href="' + ICONS + 'copy"></use></svg><span>复制</span>' +
          '</button>' +
          '<button type="button" class="button button-secondary button-compact orders-action-btn" data-order-chat="' + esc(order.chat_id || "") + '" ' + (hasChat ? "" : "disabled ") + 'title="' + (hasChat ? "查看会话" : "暂无可用会话") + '" aria-label="查看会话">' +
            '<svg class="icon"><use href="' + ICONS + 'message-square-text"></use></svg><span>会话</span>' +
          '</button>' +
          '<button type="button" class="button button-secondary button-compact orders-action-btn" data-order-toggle="' + esc(key) + '" aria-expanded="' + String(expanded) + '">' +
            '<span>' + (expanded ? "收起" : "详情") + '</span>' +
          '</button>' +
        '</div>' +
      '</td>' +
    '</tr>';

    if (expanded) {
      html += '<tr class="order-detail-row" data-detail-key="' + esc(key) + '"><td colspan="10">' + orderDetailCardMarkup(order) + '</td></tr>';
    }
    return html;
  }

  function syncOrdersShopSelect() {
    const shopSelect = $("#ordersShopSelect");
    if (!shopSelect) return;
    const enabledAccounts = (Array.isArray(state.accounts) ? state.accounts : []).filter((item) => item && item.enabled !== false);
    const currentKey = state.activeAccountKey || "";
    if (!enabledAccounts.length) {
      shopSelect.innerHTML = '<option value="' + esc(currentKey || "default") + '">' + esc(currentKey ? "当前店铺 (" + currentKey + ")" : "默认店铺") + '</option>';
      shopSelect.value = currentKey || "default";
      return;
    }
    shopSelect.innerHTML = enabledAccounts.map((account) => {
      const selected = account.key === currentKey ? " selected" : "";
      return '<option value="' + esc(account.key) + '"' + selected + '>' + esc(accountLabel(account)) + '</option>';
    }).join("");
    shopSelect.value = currentKey;
  }

  function renderOrderPage() {
    syncOrdersShopSelect();
    const statusContainer = $("#ordersStatusTabs");
    if (statusContainer) {
      const options = state.ordersPage.statusOptions.length ? state.ordersPage.statusOptions : DEFAULT_STATUS_OPTIONS;
      const counts = state.ordersPage.statusCounts || {};
      statusContainer.innerHTML = options.map((option) => {
        const active = state.ordersPage.status === option.value;
        const count = counts[option.value];
        const countMarkup = count != null ? '<span class="orders-status-count">' + Number(count) + '</span>' : "";
        return '<button type="button" class="orders-status-tab' + (active ? " is-active" : "") + '" data-order-status="' + esc(option.value) + '" role="tab" aria-selected="' + String(active) + '">' +
          '<span class="orders-status-label">' + esc(option.label) + '</span>' + countMarkup +
        '</button>';
      }).join("");
    }

    const messageNode = $("#ordersMessage");
    if (messageNode) {
      if (state.ordersPage.error) {
        const err = state.ordersPage.error;
        const msg = err?.detail?.message || err?.message || "订单记录暂时无法读取，请稍后重试";
        messageNode.hidden = false;
        messageNode.className = "orders-message is-error";
        messageNode.textContent = msg;
      } else {
        messageNode.hidden = true;
        messageNode.className = "orders-message";
        messageNode.textContent = "";
      }
    }

    const list = $("#orderList");
    const emptyPanel = $("#ordersEmpty");
    const pagination = $("#ordersPagination");
    if (!list) return;

    if (state.ordersPage.loading) {
      if (emptyPanel) emptyPanel.hidden = true;
      if (pagination) pagination.hidden = true;
      list.innerHTML = '<tr><td colspan="10" class="orders-table-status"><div class="orders-loading"><svg class="icon is-spinning"><use href="' + ICONS + 'refresh-cw"></use></svg><span>正在读取订单记录...</span></div></td></tr>';
      return;
    }

    if (state.ordersPage.error) {
      if (emptyPanel) emptyPanel.hidden = true;
      if (pagination) pagination.hidden = true;
      const err = state.ordersPage.error;
      const msg = err?.detail?.message || err?.message || "订单记录暂时无法读取，请稍后重试";
      list.innerHTML = '<tr><td colspan="10" class="orders-table-status is-error"><div class="orders-error-box"><svg class="icon"><use href="' + ICONS + 'circle-alert"></use></svg><span>' + esc(msg) + '</span><button type="button" class="button button-secondary button-compact" id="ordersRetryBtn">重试</button></div></td></tr>';
      return;
    }

    if (!state.ordersPage.items.length) {
      const hasFilter = Boolean(
        state.ordersPage.q ||
        state.ordersPage.dateFrom ||
        state.ordersPage.dateTo ||
        (state.ordersPage.status && state.ordersPage.status !== "all") ||
        (state.ordersPage.searchField && state.ordersPage.searchField !== "all")
      );
      if (hasFilter) {
        if (emptyPanel) emptyPanel.hidden = true;
        list.innerHTML = '<tr><td colspan="10" class="orders-table-status"><div class="orders-empty-filter"><svg class="icon"><use href="' + ICONS + 'search"></use></svg><p>未找到匹配条件的订单记录</p><button type="button" class="button button-secondary button-compact" id="ordersClearFilterBtn">清空筛选条件</button></div></td></tr>';
        if (pagination) pagination.hidden = false;
      } else {
        list.innerHTML = "";
        if (emptyPanel) {
          emptyPanel.hidden = false;
          text("#ordersEmptyTitle", "还没有订单");
          text("#ordersEmptyDesc", "支付订单出现后会自动显示在这里。");
        }
        if (pagination) pagination.hidden = true;
      }
    } else {
      if (emptyPanel) emptyPanel.hidden = true;
      list.innerHTML = state.ordersPage.items.map(orderTableRowMarkup).join("");
      if (pagination) pagination.hidden = false;
    }

    if (pagination && !pagination.hidden) {
      const total = state.ordersPage.total;
      const totalPages = Math.max(0, state.ordersPage.totalPages);
      const page = state.ordersPage.page;
      text("#ordersTotal", "共 " + total + " 条记录，第 " + page + " / " + Math.max(1, totalPages) + " 页");
      text("#ordersPageCurrent", page + " / " + Math.max(1, totalPages));
      const prevBtn = $("#ordersPrev");
      if (prevBtn) prevBtn.disabled = page <= 1;
      const nextBtn = $("#ordersNext");
      if (nextBtn) nextBtn.disabled = page >= totalPages || totalPages === 0;
      const pageInput = $("#ordersPageInput");
      if (pageInput) {
        pageInput.value = page;
        pageInput.max = Math.max(1, totalPages);
      }
      const pageSizeSelect = $("#ordersPageSize");
      if (pageSizeSelect) pageSizeSelect.value = String(state.ordersPage.pageSize);
    }
  }

  function renderOrders() {
    renderHomeOrders();
    renderOrderPage();
  }

  async function loadOrderPage() {
    if (!state.me || !state.activeAccountKey) return;
    const context = captureAccountContext();
    const generation = ++state.ordersPage.generation;
    const requestToken = {};
    state.ordersPage.activeRequest = requestToken;
    state.ordersPage.loading = true;
    state.ordersPage.error = null;
    const refreshBtn = $("#refreshOrders");
    if (refreshBtn) refreshBtn.disabled = true;
    renderOrderPage();

    const params = new URLSearchParams();
    params.set("page", String(Math.max(1, state.ordersPage.page || 1)));
    params.set("page_size", String([15, 30, 50].includes(Number(state.ordersPage.pageSize)) ? Number(state.ordersPage.pageSize) : 15));
    params.set("status", state.ordersPage.status || "all");
    params.set("search_field", state.ordersPage.searchField || "all");
    if (state.ordersPage.q) params.set("q", state.ordersPage.q.slice(0, 128));

    const { fromIso, toIso } = parseDateRangeToIso(state.ordersPage.dateFrom, state.ordersPage.dateTo);
    if (fromIso) params.set("created_from", fromIso);
    if (toIso) params.set("created_to", toIso);

    try {
      const data = await accountScopedApi(context, "/api/bot/orders?" + params.toString());
      if (!accountContextMatches(context) || generation !== state.ordersPage.generation || state.ordersPage.activeRequest !== requestToken) {
        return;
      }
      state.ordersPage.loading = false;
      state.ordersPage.error = null;
      state.ordersPage.items = Array.isArray(data.orders) ? data.orders : [];
      state.ordersPage.page = Math.max(1, Number(data.page || 1));
      state.ordersPage.pageSize = Number(data.page_size || state.ordersPage.pageSize || 15);
      state.ordersPage.total = Math.max(0, Number(data.total || 0));
      state.ordersPage.totalPages = Math.max(0, Number(data.total_pages || 0));
      state.ordersPage.statusCounts = data.status_counts && typeof data.status_counts === "object" ? data.status_counts : {};
      if (Array.isArray(data.status_options) && data.status_options.length) {
        state.ordersPage.statusOptions = data.status_options;
      }
      renderOrderPage();
    } catch (error) {
      if (!accountContextMatches(context) || generation !== state.ordersPage.generation || state.ordersPage.activeRequest !== requestToken) {
        return;
      }
      state.ordersPage.loading = false;
      state.ordersPage.error = error;
      renderOrderPage();
      throw error;
    } finally {
      if (state.ordersPage.activeRequest === requestToken) {
        if (refreshBtn) refreshBtn.disabled = false;
      }
    }
  }

  function productLoadRequest(context, options = {}) {
    const catalogStatus = catalogStatusMatches(options.catalogStatus, context) ? options.catalogStatus : null;
    return {
      token: catalogStatus ? "status:" + catalogStatus.token : "request:" + (++state.productsRequestGeneration),
      catalogStatus,
      context,
    };
  }

  async function runProductsLoadPipeline(pipeline) {
    try {
      while (pipeline.queued && accountContextMatches(pipeline.context) && state.productsLoad === pipeline) {
        const request = pipeline.queued;
        pipeline.queued = null;
        const active = { request, superseded: false };
        pipeline.current = active;
        let data;
        let loadError = null;
        try {
          data = await accountScopedApi(pipeline.context, "/api/bot/products?limit=500");
        } catch (error) {
          loadError = error;
        } finally {
          if (pipeline.current === active) pipeline.current = null;
        }
        if (!accountContextMatches(pipeline.context) || state.productsLoad !== pipeline) return null;
        if (active.superseded || pipeline.queued) continue;
        if (loadError) throw loadError;
        const pairedStatus = request.catalogStatus;
        if (pairedStatus && state.catalogStatus?.token !== pairedStatus.token) continue;
        const products = Array.isArray(data?.products) ? data.products : [];
        state.products = products;
        state.productsAccountKey = pipeline.context.accountKey;
        state.productsTruncated = pairedStatus ? pairedStatus.truncated : null;
        state.productsTruncatedAccountKey = pairedStatus ? pipeline.context.accountKey : "";
        void loadProductDeliveryStatus();
        renderProducts();
        renderShopStatus();
        renderAutomation();
      }
      return accountContextMatches(pipeline.context) ? state.products : null;
    } finally {
      if (state.productsLoad === pipeline) state.productsLoad = null;
    }
  }

  function loadProducts(options = {}) {
    const context = captureAccountContext();
    const loadKey = context.epoch + ":" + context.accountKey;
    const pipeline = state.productsLoad?.key === loadKey ? state.productsLoad : null;
    if (pipeline && !options.force) return pipeline.promise;
    if (!options.force && state.productsAccountKey === context.accountKey) {
      return Promise.resolve(state.products);
    }
    const request = productLoadRequest(context, options);
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    if (pipeline) {
      const currentToken = pipeline.current?.request?.token;
      const queuedToken = pipeline.queued?.token;
      if (currentToken === request.token || queuedToken === request.token) return pipeline.promise;
      if (pipeline.current) pipeline.current.superseded = true;
      pipeline.queued = request;
      return pipeline.promise;
    }
    const nextPipeline = {
      key: loadKey,
      context,
      current: null,
      queued: request,
      promise: null,
    };
    state.productsLoad = nextPipeline;
    nextPipeline.promise = runProductsLoadPipeline(nextPipeline);
    return nextPipeline.promise;
  }

  async function loadAutomation() {
    const context = captureAccountContext();
    const generation = ++state.automationLoadGeneration;
    const data = await accountScopedApi(context, "/api/automation");
    if (
      !accountContextMatches(context)
      || generation !== state.automationLoadGeneration
      || Object.values(state.automationMutations).some(Boolean)
    ) return;
    // Rule edits address the displayed array by index, so keep that snapshot
    // until an explicit reset. New drafts must still receive existing rules.
    const editingRules = state.automationEditor?.type === "rule" ? state.automation.rules : null;
    state.automation = data || { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true };
    if (editingRules) state.automation.rules = editingRules;
    renderAutomation();
    // Loading is not an editor reset: a same-account GET can finish after typing.
    renderProducts();
  }

  function unsupportedInboxCommand(error) {
    return error?.status === 404 || error?.status === 405 || error?.code === "not_found" || error?.code === "method_not_allowed";
  }

  function mergeConversationUpdate(update) {
    if (!update || typeof update !== "object" || !update.chat_id) return;
    const index = state.conversations.findIndex((item) => String(item.chat_id) === String(update.chat_id));
    if (index < 0) return;
    state.conversations[index] = Object.assign({}, state.conversations[index], update);
  }

  async function markConversationRead(chatId, options = {}) {
    const selected = String(chatId || "").trim();
    if (!selected) return { synced: false };
    const conversation = state.conversations.find((item) => String(item.chat_id) === selected);
    if (!conversation) return { synced: false };
    const command = beginConversationCommand("read", selected);
    const previous = state.inbox.readAt[selected];
    state.inbox.readAt[selected] = Date.now();
    if (Object.prototype.hasOwnProperty.call(conversation, "unread_count")) conversation.unread_count = 0;
    if (Object.prototype.hasOwnProperty.call(conversation, "unread")) conversation.unread = false;
    if (Object.prototype.hasOwnProperty.call(conversation, "is_unread")) conversation.is_unread = false;
    persistInboxPreferences();
    renderChat();
    try {
      const result = await api("/api/bot/conversations/" + encodeURIComponent(selected) + "/read", {
        method: "POST",
        body: JSON.stringify({ read: true }),
      });
      if (!conversationCommandMatches(command)) return { synced: false, stale: true };
      mergeConversationUpdate(result?.conversation || result?.item);
      renderChat();
      return { synced: true };
    } catch (error) {
      if (!conversationCommandMatches(command)) return { synced: false, stale: true };
      if (unsupportedInboxCommand(error)) {
        if (!options.silent) showToast("已在当前设备标记已读", "warning");
        return { synced: false, local: true };
      }
      if (previous) state.inbox.readAt[selected] = previous;
      else delete state.inbox.readAt[selected];
      persistInboxPreferences();
      renderChat();
      if (!options.silent) showToast(error.message || "标记已读失败", "error");
      throw error;
    }
  }

  async function toggleConversationTakeover() {
    const selected = state.conversations.find((item) => String(item.chat_id) === String(state.selectedChatId));
    if (!selected) return;
    const chatId = String(selected.chat_id);
    const command = beginConversationCommand("takeover", chatId);
    const previous = conversationTakeover(selected);
    const next = !previous;
    state.inbox.takeover[chatId] = next;
    // Reflect the optimistic command in the same fields used by the server
    // response.  This keeps the UI deterministic while the request is in flight.
    selected.takeover = next;
    persistInboxPreferences();
    renderChat();
    try {
      const result = await api("/api/bot/conversations/" + encodeURIComponent(chatId) + "/takeover", {
        method: "POST",
        body: JSON.stringify({ enabled: next }),
      });
      if (!conversationCommandMatches(command)) return;
      mergeConversationUpdate(result?.conversation || result?.item);
      renderChat();
      showToast(next ? "已暂停 AI，当前对话由人工处理" : "已恢复 AI 自动处理");
    } catch (error) {
      if (!conversationCommandMatches(command)) return;
      if (unsupportedInboxCommand(error)) {
        showToast(next ? "已暂停本机视图中的自动处理" : "已恢复本机视图中的 AI", "warning");
        return;
      }
      selected.takeover = previous;
      if (previous) state.inbox.takeover[chatId] = true;
      else delete state.inbox.takeover[chatId];
      persistInboxPreferences();
      renderChat();
      showToast(error.message || "人工接管切换失败", "error");
    }
  }

  async function loadMessages(chatId = "", options = {}) {
    // A user-selected conversation owns the message pane until its response
    // settles. Background refreshes must not advance the shared generation
    // and discard that response, otherwise the pane can remain empty.
    if (state.messageSelectionInFlight) return;
    const context = Object.assign(captureAccountContext(), {
      generation: ++state.messageLoadGeneration,
    });
    const contextMatches = () => context.generation === state.messageLoadGeneration
      && accountContextMatches(context);
    const requestedChatId = String(chatId || "");
    const params = new URLSearchParams({ limit: "100" });
    if (state.inbox?.search) params.set("search", state.inbox.search);
    if (state.inbox?.filter === "unread") params.set("unread_only", "true");
    const conversationData = await api("/api/bot/conversations?" + params.toString());
    if (!contextMatches()) return;
    state.conversations = Array.isArray(conversationData?.conversations) ? conversationData.conversations : [];
    const hasConversation = (candidate) => state.conversations.some((item) => String(item.chat_id || "") === candidate);
    const currentChatId = String(state.selectedChatId || "");
    let nextChatId = requestedChatId && hasConversation(requestedChatId)
      ? requestedChatId
      : currentChatId && hasConversation(currentChatId)
        ? currentChatId
        : String(state.conversations[0]?.chat_id || "");
    if (manualReplyOperationInFlight() && currentChatId && nextChatId !== currentChatId) {
      nextChatId = currentChatId;
    }
    if (nextChatId !== currentChatId) {
      resetManualReplyContext();
      state.selectedChatId = nextChatId;
      state.messages = [];
      state.messageMatchCount = 0;
    }
    renderChat({ preserveScroll: Boolean(options.preserveScroll) });
    if (!nextChatId) {
      state.messageMatchCount = 0;
      return;
    }
    const messageParams = new URLSearchParams({ limit: "200", chat_id: nextChatId });
    if (state.messageSearch) messageParams.set("search", state.messageSearch);
    const data = await api("/api/bot/messages?" + messageParams.toString());
    if (!contextMatches() || nextChatId !== String(state.selectedChatId || "")) return;
    state.messages = data.messages || [];
    state.messageMatchCount = Number(data.match_count || 0);
    renderChat({ preserveScroll: Boolean(options.preserveScroll) });
    pollVisibleManualReplies();
  }

  function scheduleInboxReload() {
    if (state.inboxSearchTimer) window.clearTimeout(state.inboxSearchTimer);
    state.inboxSearchTimer = window.setTimeout(() => {
      state.inboxSearchTimer = 0;
      void loadMessages(state.selectedChatId).catch((error) => showToast(error.message || "会话搜索失败", "error"));
    }, 220);
  }

  function scheduleMessageSearch() {
    if (state.messageSearchTimer) window.clearTimeout(state.messageSearchTimer);
    state.messageSearchTimer = window.setTimeout(() => {
      state.messageSearchTimer = 0;
      void loadMessages(state.selectedChatId, { preserveScroll: false }).catch((error) => showToast(error.message || "历史消息搜索失败", "error"));
    }, 220);
  }

  function merchantDisplayVisible() {
    return !document.hidden && state.view === "chat";
  }

  function stopMerchantPolling() {
    if (state.merchantPollTimer) window.clearTimeout(state.merchantPollTimer);
    state.merchantPollTimer = 0;
  }

  function scheduleMerchantPolling() {
    stopMerchantPolling();
    if (!merchantDisplayVisible()) return;
    state.merchantPollTimer = window.setTimeout(async () => {
      state.merchantPollTimer = 0;
      if (merchantDisplayVisible() && !state.merchantPollInFlight) {
        state.merchantPollInFlight = true;
        try {
          await loadMessages(state.selectedChatId, { preserveScroll: true });
        } catch (error) {
          // The next cycle retries transient platform/API failures silently.
        } finally {
          state.merchantPollInFlight = false;
        }
      }
      scheduleMerchantPolling();
    }, 5000);
  }

  function syncMerchantPolling() {
    if (merchantDisplayVisible()) scheduleMerchantPolling();
    else stopMerchantPolling();
  }

  async function selectConversation(chatId) {
    const selected = String(chatId || "");
    if (!selected) return;
    if (selected === state.selectedChatId) {
      if (conversationUnread(state.conversations.find((item) => item.chat_id === selected))) {
        void markConversationRead(selected, { silent: true });
      }
      return;
    }
    if (manualReplyOperationInFlight()) {
      showToast("请等待当前回复处理完成后再切换对话", "warning");
      return;
    }
    state.messageSelectionInFlight = true;
    const context = Object.assign(captureAccountContext(), {
      generation: ++state.messageLoadGeneration,
    });
    const contextMatches = () => context.generation === state.messageLoadGeneration
      && accountContextMatches(context)
      && selected === String(state.selectedChatId || "");
    resetManualReplyContext();
    state.selectedChatId = selected;
    state.messages = [];
    state.messageMatchCount = 0;
    renderChat();
    try {
      const params = new URLSearchParams({ limit: "200", chat_id: selected });
      if (state.messageSearch) params.set("search", state.messageSearch);
      const data = await api("/api/bot/messages?" + params.toString());
      if (!contextMatches()) return;
      state.messages = data.messages || [];
      state.messageMatchCount = Number(data.match_count || 0);
      renderChat();
      pollVisibleManualReplies();
      if (conversationUnread(state.conversations.find((item) => String(item.chat_id) === selected))) {
        await markConversationRead(selected, { silent: true });
      }
    } catch (error) {
      if (contextMatches()) showToast(error.message, "error");
    } finally {
      if (context.generation === state.messageLoadGeneration) state.messageSelectionInFlight = false;
    }
  }

  async function loadOrders() {
    if (!state.me) return;
    const context = captureAccountContext();
    if (state.view === "orders") {
      await loadAccounts();
      if (!accountContextMatches(context)) return;
      syncOrdersShopSelect();
    }
    try {
      const data = await accountScopedApi(context, "/api/bot/orders?limit=50");
      if (accountContextMatches(context)) {
        state.orders = Array.isArray(data?.orders) ? data.orders : [];
        renderHomeOrders();
      }
    } catch {
      // Home order preview error does not block orders workbench
    }
    if (accountContextMatches(context) && state.view === "orders") {
      await loadOrderPage();
    }
  }

  async function loadOverviewSignals(epoch = state.accountEpoch) {
    const context = captureAccountContext(epoch);
    const attentionResult = await api("/api/bot/attention").catch(() => ({ items: [] }));
    if (!accountContextMatches(context)) return;
    const [summary, todayData, trendData] = await Promise.all([
      api("/api/bot/summary").catch(() => null),
      api("/api/bot/analytics?period=1").catch(() => null),
      api("/api/bot/analytics?period=" + (state.trendPeriod || 7)).catch(() => null),
    ]);
    if (!accountContextMatches(context)) return;
    state.attention = Array.isArray(attentionResult?.items) ? attentionResult.items : [];
    state.summary = summary;
    state.todayAnalytics = todayData;
    state.analytics = todayData;
    state.trendAnalytics = trendData;
    state.analyticsPage = trendData;
    renderAttention();
    renderHomeStats();
    renderAnalyticsChart();
    renderHomeOrders();
    if (["home", "shops"].includes(state.view)) {
      void loadShopResources({ silent: true });
    }
  }

  function isPlatformAdmin() {
    return state.me?.is_admin === true || String(state.me?.role || "") === "admin";
  }

  function newDocsState() {
    return {
      tab: "ai", version: null, update: null, settings: null, users: [], audit: [],
      availableVersion: "", stagedVersion: "", loading: {}, loaded: {}, errors: {}, requests: {},
      operation: "", pollTimer: 0, pollAttempts: 0,
    };
  }

  function stopDocsPolling() {
    window.clearTimeout(state.docs.pollTimer);
    state.docs.pollTimer = 0;
  }

  function setSettingsTab(tab, { load = true } = {}) {
    const admin = isPlatformAdmin();
    const allowed = new Set(["ai", "resources", "security", ...(admin ? ["accounts", "audit"] : [])]);
    const selectedTab = allowed.has(tab) ? tab : "ai";
    if (state.docs.tab !== selectedTab) {
      stopDocsPolling();
      state.docs.pollAttempts = 0;
      if (state.docs.tab === "security") $("#passwordChangeForm")?.reset();
    }
    state.docs.tab = selectedTab;
    $$('[data-settings-tab], [data-docs-tab]').forEach((button) => {
      const target = button.dataset.settingsTab || button.dataset.docsTab;
      const selected = target === selectedTab;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    $$('[data-settings-panel], [data-docs-panel]').forEach((panel) => {
      const target = panel.dataset.settingsPanel || panel.dataset.docsPanel;
      panel.hidden = target !== selectedTab || (panel.hasAttribute("data-admin-only") && !admin);
    });
    text("#securityUsernameValue", state.me?.username || "--");
    text("#securityRoleValue", admin ? "管理员" : "店主");
    if (selectedTab === "ai") {
      renderSettingsAiPanel();
      if (load) void loadUnifiedAiConnection();
    } else if (selectedTab === "resources") {
      renderResourceSettings();
      if (load) void loadResourceSettings();
    } else if (load) {
      void loadSettingsData();
    }
  }

  const setDocsTab = setSettingsTab;

  const UPDATE_ACTIVE_STATES = new Set(["apply_requested", "rollback_requested", "preparing", "stopping", "migrating", "switching", "verifying", "rolling_back"]);
  const UPDATE_INSTALL_LABELS = {
    available: "旧检查记录，请重新检查发布信息", staged: "已下载并校验，尚未应用",
    apply_requested: "已提交应用请求，等待更新服务", rollback_requested: "已提交回滚请求，等待更新服务",
    preparing: "正在准备安装", stopping: "正在停止服务", migrating: "正在迁移数据",
    switching: "正在切换代码版本", verifying: "正在验证服务", rolling_back: "正在回滚",
    applied: "更新已完成", succeeded: "更新已完成", rolled_back: "已回滚", failed: "执行失败，请联系维护者查看记录",
  };
  const UPDATE_REASON_LABELS = {
    update_installation_unsupported: "当前部署不支持网页安装。",
    update_installation_unavailable: "签名发布目录或权限未就绪。",
    update_public_key_missing: "尚未配置更新签名公钥。",
    update_public_key_invalid: "更新签名公钥不可用。",
    update_service_unavailable: "独立更新服务未就绪。",
    update_source_auth_failed: "发布查询被拒绝，请检查权限或请求限额。",
    update_source_rate_limited: "发布查询次数已达上限，请稍后重试。",
    update_source_failed: "无法连接发布来源，请检查网络后重试。",
    update_source_invalid: "发布来源返回了无法识别的数据。",
    update_release_not_found: "无法访问发布仓库，请检查权限。",
    update_redirect_rejected: "发布来源返回了不受信任的重定向。",
    release_assets_missing: "发布缺少签名安装制品。",
    release_assets_invalid: "发布安装制品不完整。",
  };

  function scheduleDocsPolling() {
    stopDocsPolling();
    const docs = state.docs;
    const caps = docs.update?.capabilities || docs.version?.capabilities || {};
    if (!state.me || (state.view !== "docs" && state.view !== "settings") || docs.tab !== "version" || !isPlatformAdmin()
      || !(caps.apply || caps.rollback) || !UPDATE_ACTIVE_STATES.has(docs.update?.latest_update?.status)) return;
    if (docs.pollAttempts >= 30) {
      formMessage("#versionLoadMessage", "自动刷新已暂停，可点击刷新状态继续查看；请求已提交不代表安装成功。");
      return;
    }
    docs.pollTimer = window.setTimeout(() => {
      if (state.docs !== docs || (state.view !== "docs" && state.view !== "settings") || docs.tab !== "version") return;
      docs.pollAttempts += 1;
      void loadSettingsData({ force: true });
    }, 4000);
  }

  function renderVersionPanel() {
    state.version = state.docs?.version || state.version;
    state.versionUpdate = state.docs?.update || state.versionUpdate;
    renderVersionBadge();
  }

  function renderPlatformSettings() {
    const settings = state.docs.settings || null;
    if (!settings) return;
    const registration = settings.registration || {};
    const toggle = $("#registrationOpenToggle");
    if (toggle) {
      toggle.checked = registration.database_open === true;
      toggle.disabled = registration.environment_allowed === false;
    }
    text(
      "#registrationCeilingHint",
      registration.environment_allowed
        ? (registration.effective ? "公开注册当前已生效。" : "环境允许注册；保存后台开关后才会生效。")
        : "部署环境上限已关闭公开注册，后台开关不能越过该上限。",
    );
  }

  function renderAdminUsers() {
    const body = $("#adminUsersBody");
    if (!body) return;
    const users = Array.isArray(state.docs.users) ? state.docs.users : [];
    if (!users.length) {
      body.innerHTML = '<tr><td colspan="5">暂无平台账号</td></tr>';
      return;
    }
    body.innerHTML = users.map((user) => {
      const self = String(user.username) === String(state.me?.username || "");
      const enabled = user.enabled !== false;
      const locked = user.locked === true;
      return '<tr data-admin-user-id="' + esc(user.id) + '">' +
        '<td><strong>' + esc(user.username) + '</strong>' + (self ? '<small>当前账号</small>' : "") + '</td>' +
        '<td><select data-admin-user-role ' + (self ? "disabled" : "") + '><option value="owner" ' + (user.role === "owner" ? "selected" : "") + '>店主</option><option value="admin" ' + (user.role === "admin" ? "selected" : "") + '>管理员</option></select></td>' +
        '<td><span class="badge ' + (enabled ? "badge-green" : "badge-muted") + '">' + (enabled ? "启用" : "停用") + '</span>' + (locked ? '<span class="badge badge-red">登录锁定</span>' : "") + '</td>' +
        '<td>' + esc(user.session_count || 0) + '</td>' +
        '<td><div class="table-actions">' +
          '<button type="button" class="button button-secondary" data-admin-user-action="save" ' + (self ? "disabled" : "") + '>保存角色</button>' +
          '<button type="button" class="button button-secondary" data-admin-user-action="toggle" data-next-enabled="' + String(!enabled) + '" ' + (self ? "disabled" : "") + '>' + (enabled ? "停用" : "启用") + '</button>' +
          '<button type="button" class="button button-secondary" data-admin-user-action="unlock" ' + (locked ? "" : "disabled") + '>解锁</button>' +
          '<button type="button" class="button button-secondary" data-admin-user-action="revoke">撤销会话</button>' +
        '</div></td></tr>';
    }).join("");
  }

  const AUDIT_LABELS = {
    "auth.bootstrap_succeeded": "首位管理员创建成功",
    "auth.bootstrap_failed": "首位管理员初始化失败",
    "auth.registration_succeeded": "账号注册成功",
    "auth.registration_failed": "账号注册失败",
    "auth.login_succeeded": "登录成功",
    "auth.login_failed": "登录失败",
    "auth.logout": "退出登录",
    "auth.password_changed": "密码已修改",
    "platform.settings_changed": "平台设置已修改",
    "platform.resource_settings_changed": "运行限制已修改",
    "platform.user_changed": "账号角色或状态已修改",
    "platform.user_unlocked": "账号登录锁已清除",
    "platform.sessions_revoked": "账号会话已撤销",
    "platform.update_checked": "已检查更新",
    "platform.update_downloaded": "更新已下载并校验",
    "platform.update_requested": "已请求应用更新",
    "platform.rollback_requested": "已请求回滚版本",
    "operations.proposed": "运维方案已生成",
    "operations.executing": "运维方案执行中",
    "operations.succeeded": "运维方案执行成功",
    "operations.partial_failed": "运维方案部分失败",
    "operations.failed": "运维方案执行失败",
    "operations.cancelled": "运维方案已取消",
    "operations.expired": "运维方案已过期",
    "operations.needs_review": "运维方案待人工审核",
  };

  function renderAuditEvents() {
    const host = $("#auditEventList");
    if (!host) return;
    const events = Array.isArray(state.docs.audit) ? state.docs.audit : [];
    host.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "muted-copy";
      empty.textContent = "暂无安全记录";
      host.append(empty);
      return;
    }
    events.forEach((event) => {
      const article = document.createElement("article");
      article.className = "audit-event";
      const title = document.createElement("strong");
      title.textContent = AUDIT_LABELS[event.event_type] || String(event.event_type || "安全事件");
      const details = document.createElement("p");
      const metadata = event.metadata && typeof event.metadata === "object"
        ? Object.entries(event.metadata).map(([key, value]) => key + "=" + String(value)).join(" · ") : "";
      details.textContent = [
        formatDate(event.created_at),
        event.outcome === "success" ? "成功" : String(event.outcome || ""),
        event.target_type && event.target_id ? String(event.target_type) + " #" + String(event.target_id) : "",
        metadata,
      ].filter(Boolean).join(" · ");
      article.append(title, details);
      host.append(article);
    });
  }

  function renderSettings() {
    const admin = isPlatformAdmin();
    $$('[data-admin-only]').forEach((node) => {
      if (!node.hasAttribute("data-settings-panel") && !node.hasAttribute("data-docs-panel")) node.hidden = !admin;
    });
    if (!admin && ["accounts", "audit"].includes(state.docs.tab)) state.docs.tab = "ai";
    setSettingsTab(state.docs.tab || "ai", { load: false });
    renderSettingsAiPanel();
    renderResourceSettings();
    if (admin) {
      renderPlatformSettings();
      renderAdminUsers();
      renderAuditEvents();
    }
  }

  const renderDocs = renderSettings;

  async function loadSettingsData({ force = false } = {}) {
    if (!state.me) return;
    const docs = state.docs;
    const tab = docs.tab;
    if (["guide", "security"].includes(tab)) { renderSettings(); return; }
    if (tab === "ai") { renderSettings(); void loadUnifiedAiConnection(); return; }
    if (tab === "resources") { renderResourceSettings(); void loadResourceSettings({ force }); return; }
    if (!force && (docs.loading[tab] || docs.loaded[tab])) { renderSettings(); scheduleDocsPolling(); return; }
    const id = (docs.requests[tab] || 0) + 1;
    docs.requests[tab] = id;
    const username = state.me.username;
    const admin = isPlatformAdmin();
    const valid = () => state.docs === docs && state.me?.username === username && isPlatformAdmin() === admin && docs.requests[tab] === id;
    docs.loading[tab] = true;
    docs.errors[tab] = "";
    renderSettings();
    try {
      if (tab === "accounts" && isPlatformAdmin()) {
        const [settings, users] = await Promise.all([api("/api/admin/settings"), api("/api/admin/users?limit=100")]);
        if (!valid()) return;
        docs.settings = settings;
        docs.users = users.users || [];
      } else if (tab === "audit" && isPlatformAdmin()) {
        const audit = await api("/api/admin/audit?limit=100");
        if (!valid()) return;
        docs.audit = audit.events || [];
      }
      docs.loaded[tab] = true;
    } catch (error) {
      if (!valid()) return;
      docs.loaded[tab] = false;
      docs.errors[tab] = error.message || "信息读取失败，请重试";
      showToast(docs.errors[tab], "error");
    } finally {
      if (valid()) {
        docs.loading[tab] = false;
        renderSettings();
        scheduleDocsPolling();
      }
    }
  }

  const loadDocsData = loadSettingsData;

  async function changeCurrentPassword(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const docs = state.docs;
    if (!state.me || form.querySelector('button[type="submit"]').disabled) return;
    const currentPassword = $("#currentPasswordInput").value;
    const newPassword = $("#newPasswordInput").value;
    if (newPassword.length < state.passwordMinLength || newPassword.length > 1024) {
      formMessage("#passwordChangeMessage", "新密码长度需要在 " + state.passwordMinLength + " 至 1024 位之间");
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    setBusy(button, true);
    try {
      await api("/api/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      if (state.docs !== docs || !state.me) return;
      form.reset();
      formMessage("#passwordChangeMessage", "密码已更新，其他会话已撤销", true);
    } catch (error) {
      if (state.docs === docs && state.me) formMessage("#passwordChangeMessage", error.message || "密码更新失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function savePlatformSettings(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    setBusy(button, true);
    try {
      const docs = state.docs;
      const toggle = $("#registrationOpenToggle");
      const settings = await api("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          registration_open: Boolean(toggle?.checked),
        }),
      });
      if (state.docs !== docs || !isPlatformAdmin()) return;
      docs.settings = settings;
      formMessage("#platformSettingsMessage", "平台设置已保存", true);
      await loadDocsData({ force: true });
    } catch (error) {
      formMessage("#platformSettingsMessage", error.message || "平台设置保存失败");
    } finally {
      setBusy(button, false);
    }
  }

  function parseStrictInteger(value) {
    if (value === null || value === undefined) return null;
    const str = String(value).trim();
    if (!str || !/^-?\d+$/.test(str)) return null;
    const num = Number(str);
    return Number.isSafeInteger(num) ? num : null;
  }

  function renderResourceSettings() {
    const form = $("#resourceSettingsForm");
    if (!form) return;
    const admin = isPlatformAdmin();
    const shopsInput = $("#resourceMaxShops");
    const workersInput = $("#resourceMaxWorkers");
    const memInput = $("#resourceMemoryMiB");
    const saveBtn = $("#saveResourceSettings");

    let shopsVal = "";
    let workersVal = "";
    let memVal = "";

    if (admin) {
      if (state.resourceDraftDirty && state.resourceDraft) {
        shopsVal = state.resourceDraft.max_shop_accounts ?? "";
        workersVal = state.resourceDraft.max_running_workers ?? "";
        memVal = state.resourceDraft.worker_memory_mib ?? "";
      } else if (state.resourceSettings) {
        shopsVal = state.resourceSettings.max_shop_accounts ?? "";
        workersVal = state.resourceSettings.max_running_workers ?? "";
        memVal = state.resourceSettings.worker_memory_mib ?? "";
      } else if (state.resources?.limits) {
        shopsVal = state.resources.limits.max_shop_accounts ?? "";
        workersVal = state.resources.limits.max_running_workers ?? "";
        memVal = state.resources.limits.worker_memory_mib ?? "";
      }
    } else {
      const limits = state.resources?.limits;
      if (limits) {
        shopsVal = limits.max_shop_accounts ?? "";
        workersVal = limits.max_running_workers ?? "";
        memVal = limits.worker_memory_mib ?? "";
      }
    }

    const active = document.activeElement;
    if (shopsInput && (active !== shopsInput || !state.resourceDraftDirty)) {
      shopsInput.value = shopsVal !== "" ? String(shopsVal) : "";
    }
    if (workersInput && (active !== workersInput || !state.resourceDraftDirty)) {
      workersInput.value = workersVal !== "" ? String(workersVal) : "";
    }
    if (memInput && (active !== memInput || !state.resourceDraftDirty)) {
      memInput.value = memVal !== "" ? String(memVal) : "";
    }

    [shopsInput, workersInput, memInput].forEach((input) => {
      if (!input) return;
      if (admin) {
        input.disabled = false;
        input.removeAttribute("readonly");
      } else {
        input.disabled = true;
        input.setAttribute("readonly", "readonly");
      }
    });

    if (saveBtn) {
      saveBtn.hidden = !admin;
      saveBtn.disabled = !admin || Boolean(state.resourceLoading);
    }
  }

  async function loadResourceSettings({ force = false } = {}) {
    if (!state.me) return;
    if (!isPlatformAdmin()) {
      if (!state.resources || force) {
        try {
          await loadShopResources({ silent: true });
        } catch {
          // ignore
        }
      }
      renderResourceSettings();
      return;
    }

    state.resourceLoading = true;
    try {
      const data = await api("/api/admin/resource-settings");
      state.resourceSettings = data?.settings || null;
      state.resourceRevision = Number(data?.settings?.revision || 0);
      state.resourceBounds = data?.bounds || null;
      if (force) {
        state.resourceDraftDirty = false;
        state.resourceDraft = null;
        formMessage("#resourceSettingsMessage", "");
      }
      renderResourceSettings();
    } catch (error) {
      showToast(error.message || "运行限制读取失败", "error");
    } finally {
      state.resourceLoading = false;
      renderResourceSettings();
    }
  }

  async function saveResourceSettings(event) {
    if (event) event.preventDefault();
    if (!state.me) return;
    if (!isPlatformAdmin()) {
      showToast("需要管理员权限才能修改运行限制", "error");
      return;
    }

    const saveBtn = $("#saveResourceSettings") || event?.submitter;
    const rawShops = $("#resourceMaxShops")?.value;
    const rawWorkers = $("#resourceMaxWorkers")?.value;
    const rawMemory = $("#resourceMemoryMiB")?.value;

    const shops = parseStrictInteger(rawShops);
    const workers = parseStrictInteger(rawWorkers);
    const memory = parseStrictInteger(rawMemory);

    if (shops === null || shops < 1 || shops > 1000) {
      formMessage("#resourceSettingsMessage", "店铺上限需为 1 到 1000 的整数", false);
      return;
    }
    if (workers === null || workers < 1 || workers > 1000) {
      formMessage("#resourceSettingsMessage", "全局客服并发上限需为 1 到 1000 的整数", false);
      return;
    }
    if (memory === null || memory < 128 || memory > 16384) {
      formMessage("#resourceSettingsMessage", "单店内存上限需为 128 到 16384 MiB 的整数", false);
      return;
    }

    const payload = {
      expected_revision: Number(state.resourceRevision || 0),
      max_shop_accounts: shops,
      max_running_workers: workers,
      worker_memory_mib: memory,
    };

    formMessage("#resourceSettingsMessage", "");
    setBusy(saveBtn, true);

    try {
      const data = await api("/api/admin/resource-settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      state.resourceSettings = data?.settings || null;
      state.resourceRevision = Number(data?.settings?.revision ?? (payload.expected_revision + 1));
      state.resourceBounds = data?.bounds || null;
      state.resourceDraftDirty = false;
      state.resourceDraft = null;

      formMessage("#resourceSettingsMessage", "运行限制已保存，新配置将在下次启动客服时生效", true);
      showToast("运行限制已更新");
      renderResourceSettings();
      void loadShopResources({ silent: true, force: true });
    } catch (error) {
      if (error?.status === 409 || error?.code === "resource_revision_conflict") {
        formMessage("#resourceSettingsMessage", error.message || "运行限制已被修改，请刷新后再保存", false);
        showToast(error.message || "运行限制已被其他操作修改，请刷新后再试", "warning");
      } else {
        formMessage("#resourceSettingsMessage", error?.message || "运行限制保存失败，请检查输入格式后重试", false);
        showToast(error?.message || "运行限制保存失败", "error");
      }
    } finally {
      setBusy(saveBtn, false);
    }
  }

  async function reloadResourceSettings() {
    if (isPlatformAdmin()) {
      await loadResourceSettings({ force: true });
      showToast("已刷新运行限制配置");
    } else {
      await loadShopResources({ silent: true, force: true });
      renderResourceSettings();
      showToast("已刷新店铺运行情况");
    }
  }

  function onResourceInputChange() {
    if (!isPlatformAdmin()) return;
    state.resourceDraftDirty = true;
    state.resourceDraft = {
      max_shop_accounts: $("#resourceMaxShops")?.value ?? "",
      max_running_workers: $("#resourceMaxWorkers")?.value ?? "",
      worker_memory_mib: $("#resourceMemoryMiB")?.value ?? "",
    };
  }

  async function checkPlatformUpdate() {
    const session = ensurePlatformUpdateSession();
    if (!session || !isPlatformAdmin() || session.manual) return;
    const probe = state.versionUpdate?.update_probe || state.version?.update_probe || {};
    if (Number(probe.manual_cooldown_until || 0) * 1000 > Date.now()) {
      session.probeError = UPDATE_UI_COPY.messages.probe_cooldown;
      renderPlatformUpdate();
      return;
    }
    session.manual = true;
    const generation = ++session.generation;
    setBusy($("#versionBadgeRefresh"), true);
    try {
      const result = await api("/api/admin/updates/check", { method: "POST", suppressSessionReset: true });
      if (!platformUpdateSessionMatches(session) || generation !== session.generation) return;
      session.probeError = "";
      applyVersionSnapshot(session, state.version, { ...state.versionUpdate, update_check: result, update_probe: result.update_probe || state.versionUpdate?.update_probe });
      session.cacheAt = Date.now();
    } catch (error) {
      if (!platformUpdateSessionMatches(session) || generation !== session.generation) return;
      session.probeError = updateErrorMessage(error, "probe_failed");
      applyVersionSnapshot(session, state.version, { ...state.versionUpdate, update_check: { status: "error", available: false } });
    } finally {
      if (platformUpdateSessionMatches(session) && generation === session.generation) {
        session.manual = false;
        setBusy($("#versionBadgeRefresh"), false);
        renderVersionBadge();
        renderPlatformUpdate();
        scheduleVersionCacheRefresh(session);
      }
    }
  }

  async function handleAdminUserAction(button) {
    const row = button.closest("[data-admin-user-id]");
    const userId = row?.dataset.adminUserId;
    if (!userId) return;
    const action = button.dataset.adminUserAction;
    setBusy(button, true);
    try {
      if (action === "save") {
        await api("/api/admin/users/" + encodeURIComponent(userId), {
          method: "PATCH",
          body: JSON.stringify({ role: row.querySelector("[data-admin-user-role]").value }),
        });
      } else if (action === "toggle") {
        await api("/api/admin/users/" + encodeURIComponent(userId), {
          method: "PATCH",
          body: JSON.stringify({ enabled: button.dataset.nextEnabled === "true" }),
        });
      } else if (action === "unlock") {
        await api("/api/admin/users/" + encodeURIComponent(userId) + "/unlock", { method: "POST" });
      } else if (action === "revoke") {
        await api("/api/admin/users/" + encodeURIComponent(userId) + "/sessions/revoke", { method: "POST" });
      }
      showToast("账号操作已完成");
      await loadDocsData({ force: true });
    } catch (error) {
      showToast(error.message || "账号操作失败", "error");
    } finally {
      setBusy(button, false);
    }
  }

  function openDocsHelpModal() {
    openDialog("docsHelpModal");
  }

  async function loadPublicVersionOnce() {
    if (state.versionLoadedPublic) return;
    if (state.publicVersionRequest) return state.publicVersionRequest;
    state.publicVersionRequest = (async () => {
      try {
        const version = await api("/api/version/public", { suppressSessionReset: true });
        if (!version?.version) return;
        state.publicVersion = version;
        state.versionLoadedPublic = true;
        if (!state.me) state.version = version;
        renderVersionBadge();
      } catch (_) {
        // An optional metadata failure must not call an authenticated fallback.
      } finally { state.publicVersionRequest = null; }
    })();
    return state.publicVersionRequest;
  }

  const VERSION_CACHE_REFRESH_MS = 5 * 60 * 1000;
  const UPDATE_OPERATION_POLL_MS = 4000;
  const UPDATE_READ_TIMEOUT_MS = 15000;
  // Reader-facing copy is owned by AGY (.local/update-ui-copy.json).
  const UPDATE_UI_COPY = {
    deployments: { docker_compose: "Docker Compose 容器部署", systemd: "systemd 服务部署", source: "源码手动部署", unknown: "未知部署方式" },
    phases: {
      staged: "升级包已就绪", queued: "排队中", verifying_package: "校验升级包与签名", building: "构建新镜像", preflighting: "升级预检",
      preparing: "准备运行环境", stopping: "停止旧服务", backing_up: "备份运行时数据", switching: "切换新版本服务", verifying: "验证服务健康与数据库",
      succeeded: "升级成功", rolling_back: "正在回退至旧版本", rolled_back: "已安全回退至旧版本", failed: "升级失败", recovery_failed: "恢复失败，需人工排查",
    },
    messages: {
      loading: "正在获取版本与更新状态...", unchecked: "尚未检查更新", no_update: "当前已是最新版本，无需更新",
      updater_missing: "未检测到独立更新器，请参考文档为当前环境安装并启动更新组件", source_manual: "源码部署不支持网页自动升级，请拉取最新代码手动构建",
      readiness_unknown: "当前环境暂不具备网页更新条件，具体原因未识别，请参考部署文档或联系维护者",
      preparing: "正在下载并校验升级制品，请稍候...", confirm_password: "升级需要验证管理员身份，请输入当前管理员密码", risk_required: "请勾选确认已知晓升级风险",
      reconnecting: "服务正在重启与健康检查，正在尝试重新连接...", completed: "系统升级已完成，请重新加载界面以应用最新资源", restored: "升级未完成，系统已安全回退至上一稳定版本",
      failed: "升级失败，请查看下方具体原因或服务端日志", probe_failed: "版本检查失败，无法连接到更新源", probe_cooldown: "检查过于频繁，请稍后再试",
      confirm_expired: "身份确认已过期，请重新输入密码确认", download_failed: "升级制品下载失败，请检查网络连接后重试", signature_failed: "升级包签名校验失败，制品已被拒绝",
      recovery_failed: "自动恢复失败，系统处于维护状态，请查看服务端日志进行人工处理", unknown_error: "发生未知错误，请稍后重试或查看服务端日志",
    },
    errors: {
      update_in_progress: "已有正在进行的更新任务，请等待完成", authentication_failed: "管理员密码错误或身份验证失败", permission_denied: "权限不足，仅管理员可执行升级操作",
      signature_verification_failed: "升级包数字签名校验失败", signature_missing: "缺少升级包数字签名文件", manifest_invalid: "升级清单格式无效",
      manifest_version_mismatch: "升级清单版本与目标版本不一致", manifest_sha_mismatch: "升级清单校验和不匹配", source_sha_mismatch: "源码包校验和不匹配",
      unsupported_deployment: "当前部署方式不支持网页自动升级", updater_unhealthy: "更新器未运行或响应超时", updater_missing: "未安装或未启用独立更新器",
      target_version_invalid: "目标版本号格式不合法", already_latest_version: "当前已是最新版本", preflight_check_failed: "升级预检失败，当前环境不满足升级条件",
      build_failed: "新版本镜像构建失败", switch_failed: "切换新服务容器失败", health_check_failed: "新版本服务健康检查未通过", rollback_failed: "回滚到旧版本失败",
      confirmation_expired: "确认令牌已失效，请重新确认", confirmation_invalid: "确认信息不匹配或已失效", maintenance_active: "系统处于维护模式，暂不可执行新更新",
      rate_limited: "请求过于频繁，请稍后再试", download_failed: "下载升级资产失败", download_too_large: "升级文件超出允许的最大体积", network_error: "网络连接失败，无法访问更新服务器",
      database_unavailable: "控制面数据库不可用", release_version_invalid: "发布版本号格式无效", update_source_rejected: "更新源不受信任或已被拒绝",
      update_source_auth_failed: "更新源身份验证失败", update_release_not_found: "未找到指定的发布版本", update_source_rate_limited: "更新源访问频次超限，请稍后重试",
      update_source_failed: "访问更新源失败", update_redirect_rejected: "更新源重定向目标不受信任", update_source_invalid: "更新源返回数据无效",
      update_download_size_mismatch: "下载文件大小与清单声明不符", update_staging_failed: "升级制品暂存写入失败", release_assets_invalid: "发布制品结构或文件格式不合法",
      release_assets_missing: "发布版本缺少必要的升级制品", update_channel_invalid: "更新渠道无效", update_public_key_missing: "服务端未配置更新签名公钥",
      update_public_key_invalid: "更新签名公钥格式无效", update_service_unavailable: "更新服务暂不可用", update_installation_unavailable: "当前环境不具备自动升级条件",
      update_operation_invalid: "更新操作标识或参数无效", update_not_staged: "目标版本的升级包尚未下载就绪", update_probe_failed: "版本检查失败，无法连接到更新源",
      update_probe_cooldown: "版本检查过于频繁，请稍后再试", update_interrupted: "更新执行被意外中断", update_executor_error: "更新执行器内部错误",
      update_executor_busy: "更新执行器繁忙，已有其他任务占用", update_request_expired: "更新请求已过期", update_invalid_admin: "操作发起人管理员身份无效",
      update_downgrade_rejected: "不能安装当前版本或更旧版本", update_current_version_changed: "当前运行版本已发生变化，更新终止", update_current_version_mismatch: "当前运行版本已发生变化，请重新准备更新",
      update_rollback_not_verified: "指定的回滚版本未通过可信校验", update_target_not_unique: "目标服务容器不唯一，无法安全升级", update_unsafe_permissions: "更新目录权限不符合安全要求",
      update_unsafe_file: "升级文件包含不安全路径或符号链接", update_artifact_too_large: "升级制品大小超出安全限制", update_manifest_mismatch: "升级清单与实际制品不符",
      update_dependency_change_rejected: "发布制品包含未审批的依赖变化", update_signature_invalid: "发布签名校验失败", update_artifact_hash_mismatch: "发布制品完整性校验失败",
      update_archive_hash_mismatch: "发布文件完整性校验失败", update_intent_pending: "已有更新操作等待执行", update_candidate_invalid: "候选版本已失效，请重新下载",
      update_installation_unsupported: "此部署方式不支持网页安装，请按部署说明更新", update_already_staged: "该版本已经下载校验，无需重复下载", update_busy: "已有安装或回滚操作正在进行，请先查看执行状态",
      update_maintenance_active: "系统更新维护中，暂不接受新的业务操作", update_service_stale: "独立更新器心跳已过期，请联系维护者", update_protocol_mismatch: "独立更新器协议不匹配，请联系维护者",
      update_deployment_mismatch: "独立更新器与当前部署不匹配", update_public_key_mismatch: "独立更新器与应用的签名公钥不匹配", update_version_changed: "发布版本已变化，请重新检查",
      rollback_version_unavailable: "该回退版本或已验证制品当前不可用", update_lock_lost: "更新租约已失效，请重试并查看现有操作状态", update_maintenance_protocol_unsupported: "当前或目标版本缺少维护协议，请先人工接入可信新版本基线",
      confirmation_action_invalid: "确认操作无效", reauthentication_failed: "管理员密码校验失败", update_database_unavailable: "升级状态数据库不可用",
      update_lock_unavailable: "更新锁暂时不可用", update_not_available: "暂无可用的更新版本", update_release_exists: "目标更新版本已存在",
      update_version_already_current: "目标版本与当前运行版本一致，无需更新", update_download_too_large: "升级文件超出允许的最大体积", update_manifest_invalid: "升级清单格式无效",
      update_manifest_version_mismatch: "升级清单版本与目标版本不一致", docker_public_key_invalid: "Docker 更新公钥配置无效", docker_protocol_unsupported: "Docker 更新协议不受支持",
      update_cli_invalid: "更新命令行参数无效", update_root_required: "执行更新操作需要 root 权限", update_import_path_invalid: "导入的基线资产路径无效或不唯一",
      update_intent_owner_unavailable: "无法解析更新意图所属应用用户身份", update_service_command_failed: "执行服务管理命令失败", update_state_release_overlap: "状态目录与版本发布目录重叠，配置不合法",
      update_staging_release_overlap: "暂存目录与版本发布目录重叠，配置不合法", update_current_layout_invalid: "现役版本软链接结构不合法", update_service_name_invalid: "服务单元名称不合法",
      update_private_layout_invalid: "私有更新状态目录结构不合法", update_status_layout_invalid: "公开状态目录结构不合法", update_intent_expiry_invalid: "更新意图过期时间配置无效",
      update_runtime_layout_invalid: "运行时目录结构不合法", update_directory_invalid: "更新相关目录不合法", update_directory_untrusted: "更新目录权限或属主不符合安全要求",
      update_journal_invalid: "更新事务日志损坏或不合法", update_nonce_conflict: "更新凭据标识冲突，请重试", update_recovery_required: "系统处于异常恢复状态，请先执行故障恢复",
      update_current_missing: "未找到现役版本软链接", update_current_invalid: "现役版本软链接目标无效", update_archive_path_invalid: "升级归档包内路径不合法",
      update_runtime_path_rejected: "升级包内包含受保护或不允许的文件路径", update_lock_invalid: "更新锁文件无效", update_already_running: "已有更新任务正在运行中",
      update_installation_migration_required: "当前安装尚未满足受管更新要求，需先完成安装迁移后才能使用网页更新", update_updater_not_initialized: "独立更新器尚未完成可信初始化，请先在服务端完成基线初始化", update_updater_identity_mismatch: "独立更新器文件身份与初始化记录不一致，已被系统拒绝",
      update_compose_not_initialized: "Docker 更新器尚未完成 Compose 部署配置登记，请先在宿主机执行初始化登记", update_compose_unavailable: "Docker Compose 插件不可用或未正确安装", update_compose_version_mismatch: "Docker Compose 插件版本不符合要求（须为 5.5.1）",
      update_compose_registration_invalid: "Compose 部署登记记录格式无效或已损坏", update_compose_config_invalid: "Compose 配置文件格式无效或解析失败", update_compose_configuration_unsupported: "Compose 配置包含不受支持的拓扑结构、依赖或字段",
      update_compose_config_mismatch: "输入的 Compose 配置与当前运行的容器不匹配", update_compose_config_changed: "Compose 部署配置发生未登记的变更", update_compose_resource_changed: "数据卷、网络或挂载身份发生未登记的变更",
      update_compose_bind_source_invalid: "Compose 目录挂载源路径无效或未通过验证", update_compose_literal_roundtrip_failed: "Compose 配置内容无法原样保留，请检查特殊字符或变量格式", update_compose_command_failed: "执行 Docker Compose 管理命令失败",
      update_required_mounts_missing: "缺少必需的应用数据卷或挂载点配置", update_updater_mounts_invalid: "更新器容器挂载点配置不合法", update_public_key_mount_mismatch: "更新公钥挂载路径与系统配置不一致",
      update_recovery_alias_conflict: "回退镜像别名冲突，请检查本地镜像标签", update_compose_config_too_large: "Compose 配置文件体积超出允许上限", update_compose_initialization_conflict: "系统已有登记记录或更新历史，不支持重复初始化",
      update_invalid_cli: "更新器命令行子命令或参数无效",
      update_data_version_unknown: "当前或目标镜像未声明数据兼容版本，请按部署说明手动升级",
      update_data_backward_incompatible: "目标版本与当前数据格式不兼容，已停止自动更新，请按发布说明手动迁移",
    },
  };
  const UPDATE_PHASE_ALIASES = { apply_requested: "queued", rollback_requested: "queued", applied: "succeeded", migrating: "preflighting" };
  const UPDATE_TERMINAL_PHASES = new Set(["succeeded", "rolled_back", "failed", "recovery_failed"]);

  function updateIdentity() {
    return state.me ? JSON.stringify([state.me.id, state.me.username, state.me.role, isPlatformAdmin()]) : "";
  }

  function platformUpdateSessionMatches(session) {
    return Boolean(session && state.platformUpdate === session && state.docs === session.docs && session.identity === updateIdentity());
  }

  function resetPlatformUpdates() {
    const previous = state.platformUpdate;
    if (previous) {
      window.clearTimeout(previous.cacheTimer);
      window.clearTimeout(previous.pollTimer);
    }
    state.platformUpdate = null;
    $("#updatePasswordForm")?.reset();
    closeDialog("platformUpdateDialog");
    setBusy($("#versionBadgeRefresh"), false);
  }

  function ensurePlatformUpdateSession() {
    if (!state.me) return null;
    if (platformUpdateSessionMatches(state.platformUpdate)) return state.platformUpdate;
    resetPlatformUpdates();
    state.versionUpdate = null;
    state.version = state.publicVersion || null;
    state.docs.version = null;
    state.docs.badgeLoaded = false;
    state.docs.update = null;
    const session = state.platformUpdate = {
      identity: updateIdentity(), docs: state.docs, generation: 0, cacheAt: 0, cacheAttemptAt: 0, cacheTimer: 0, loading: false, manual: false,
      confirmedCheck: null, error: "", probeError: "", stage: null, operation: null, operationRevision: 0, pollTimer: 0, pollLoading: false,
      dialogEpoch: 0, busy: "", submitted: false, reconnecting: false,
    };
    scheduleVersionCacheRefresh(session);
    renderVersionBadge();
    return session;
  }

  function scheduleVersionCacheRefresh(session) {
    if (!platformUpdateSessionMatches(session)) return;
    window.clearTimeout(session.cacheTimer);
    session.cacheTimer = 0;
    if (document.hidden) return;
    const lastRead = Math.max(session.cacheAt, session.cacheAttemptAt) || Date.now();
    session.cacheTimer = window.setTimeout(() => {
      if (platformUpdateSessionMatches(session) && !document.hidden) void loadVersionInfo({ force: true });
    }, Math.max(1, VERSION_CACHE_REFRESH_MS - (Date.now() - lastRead)));
  }

  function updateErrorMessage(error, fallback = "unknown_error") {
    const code = String(error?.code || error?.error_code || "");
    return UPDATE_UI_COPY.errors[code] || (error?.status === 401 ? UPDATE_UI_COPY.errors.authentication_failed : UPDATE_UI_COPY.messages[fallback]);
  }

  function updatePhase(operation) {
    const status = UPDATE_PHASE_ALIASES[operation?.status] || operation?.status;
    if (UPDATE_TERMINAL_PHASES.has(status)) return status;
    return UPDATE_PHASE_ALIASES[operation?.phase] || operation?.phase || status || "";
  }

  function updateOperationActive(session) {
    return Boolean(session?.submitted || (session?.operation && !UPDATE_TERMINAL_PHASES.has(updatePhase(session.operation))));
  }

  function applyVersionSnapshot(session, version, update) {
    const current = version || state.version || {};
    let check = update?.update_check || current.update_check || {};
    const failed = check.status === "error" || check.error_code || check.error;
    if (failed && isConfirmedHigherRelease(session.confirmedCheck, current.version)) check = session.confirmedCheck;
    else session.confirmedCheck = isConfirmedHigherRelease(check, current.version) ? check : null;
    session.docs.version = state.version = { ...current, update_check: check };
    session.docs.update = state.versionUpdate = update ? { ...update, update_check: check } : null;
    session.docs.badgeLoaded = true;
    renderVersionBadge();
    renderPlatformUpdate();
  }

  async function loadVersionInfo({ force = false } = {}) {
    if (!state.me) { await loadPublicVersionOnce(); return; }
    const session = ensurePlatformUpdateSession();
    if (document.hidden || (session.loading && session.loading === session.generation) || session.manual || (!force && session.docs.badgeLoaded)) return;
    const generation = ++session.generation;
    const operationRevision = session.operationRevision;
    session.loading = generation;
    session.cacheAttemptAt = Date.now();
    try {
      const [version, update] = await Promise.all([
        api("/api/version", { suppressSessionReset: true, timeoutMs: UPDATE_READ_TIMEOUT_MS }),
        isPlatformAdmin() ? api("/api/admin/updates", { suppressSessionReset: true, timeoutMs: UPDATE_READ_TIMEOUT_MS }) : Promise.resolve(null),
      ]);
      if (!platformUpdateSessionMatches(session) || generation !== session.generation) return;
      session.cacheAt = Date.now();
      session.probeError = "";
      applyVersionSnapshot(session, version, update);
      if (operationRevision === session.operationRevision) acceptUpdateOperation(session, update?.operation);
    } catch (error) {
      if (platformUpdateSessionMatches(session) && generation === session.generation) {
        session.probeError = updateErrorMessage(error, "probe_failed");
        renderPlatformUpdate();
      }
    } finally {
      if (platformUpdateSessionMatches(session) && session.loading === generation) {
        session.loading = false;
        renderVersionBadge();
        scheduleVersionCacheRefresh(session);
      }
    }
  }

  function updateCapabilities() {
    return state.versionUpdate?.capabilities || state.version?.capabilities || {};
  }

  function updateRollbackCandidates() {
    const candidates = state.versionUpdate?.rollback_versions;
    return Array.isArray(candidates) ? candidates.filter((item) => item && typeof item === "object" && parseSemVer(item.version) && /^[a-f0-9]{64}$/.test(item.manifest_sha256)) : [];
  }

  function updateActionAllowed(action) {
    const caps = updateCapabilities();
    return isPlatformAdmin() && caps[action] === true && caps.ready !== false && (action === "rollback" || caps.download === true);
  }

  function renderPlatformUpdate() {
    const session = state.platformUpdate;
    if (!platformUpdateSessionMatches(session)) return;
    const caps = updateCapabilities();
    const check = state.versionUpdate?.update_check || state.version?.update_check || {};
    const operation = session.operation;
    const phase = updatePhase(operation);
    const active = updateOperationActive(session);
    const target = session.stage?.version || operation?.version || (isConfirmedHigherRelease(check, state.version?.version) ? check.version : "");
    text("#updateCurrentVersion", state.version?.version || "--");
    text("#updateTargetVersion", target || "--");
    text("#updateDeployment", UPDATE_UI_COPY.deployments[caps.deployment === "docker" ? "docker_compose" : caps.deployment] || UPDATE_UI_COPY.deployments.unknown);
    text("#updateReleaseNotes", session.stage?.release_notes || check.release_notes || state.version?.release_notes || "");
    let readiness = !session.docs.badgeLoaded ? UPDATE_UI_COPY.messages.loading : !updateActionAllowed("apply")
      ? (caps.deployment === "source" ? UPDATE_UI_COPY.messages.source_manual : updateErrorMessage({ code: caps.reason || caps.error_code }, "readiness_unknown"))
      : target ? "" : check.status === "current" ? UPDATE_UI_COPY.messages.no_update : UPDATE_UI_COPY.messages.unchecked;
    const probe = state.versionUpdate?.update_probe || state.version?.update_probe;
    if (probe?.state === "error") readiness = updateErrorMessage(probe, "probe_failed");
    if (session.stage) readiness = UPDATE_UI_COPY.messages.confirm_password;
    if (session.busy === "prepare") readiness = UPDATE_UI_COPY.messages.preparing;
    text("#updateReadinessMessage", session.error || readiness || session.probeError);
    const download = $("#updateDownloadButton");
    if (download) { download.hidden = !isPlatformAdmin() || Boolean(session.stage) || active; download.disabled = Boolean(session.busy) || !target || !updateActionAllowed("apply"); }
    const form = $("#updatePasswordForm");
    if (form) form.hidden = !session.stage || active || !isPlatformAdmin();
    const confirm = $("#updateConfirmButton");
    if (confirm) confirm.disabled = Boolean(session.busy) || !session.stage || !updateActionAllowed(session.stage?.action);
    if ($("#updateAdminPassword")) $("#updateAdminPassword").disabled = Boolean(session.busy);
    if ($("#updateConfirmRisk")) $("#updateConfirmRisk").disabled = Boolean(session.busy);
    const progress = $("#updateProgress");
    if (progress) progress.hidden = !operation && !session.reconnecting;
    text("#updatePhaseLabel", UPDATE_UI_COPY.phases[phase] || UPDATE_UI_COPY.messages.loading);
    const operationMessage = session.reconnecting ? UPDATE_UI_COPY.messages.reconnecting
      : phase === "succeeded" ? UPDATE_UI_COPY.messages.completed : phase === "rolled_back" ? (operation.action === "rollback" ? UPDATE_UI_COPY.phases.rolled_back : UPDATE_UI_COPY.messages.restored)
        : phase === "recovery_failed" ? UPDATE_UI_COPY.messages.recovery_failed : operation?.error_code ? updateErrorMessage(operation)
          : phase === "failed" ? UPDATE_UI_COPY.messages.failed : UPDATE_UI_COPY.phases[phase] || "";
    text("#updateOperationMessage", operationMessage);
    if ($("#updateReloadButton")) $("#updateReloadButton").hidden = !["succeeded", "rolled_back"].includes(phase);
    if ($("#updateRetryButton")) $("#updateRetryButton").hidden = active || !["failed", "recovery_failed"].includes(phase);
    const rollback = $("#updateRollbackSelect");
    if (rollback) {
      const selected = rollback.value;
      const candidates = updateRollbackCandidates();
      const show = isPlatformAdmin() && candidates.length > 0 && !active && !session.stage;
      rollback.closest(".update-rollback-card").hidden = !show;
      rollback.hidden = !show;
      rollback.innerHTML = candidates.map((item) => '<option value="' + esc(item.version) + '">' + esc(item.version) + '</option>').join("");
      if (candidates.some((item) => item.version === selected)) rollback.value = selected;
      rollback.disabled = Boolean(session.busy);
      $("#updateRollbackButton").hidden = !show;
      $("#updateRollbackButton").disabled = Boolean(session.busy) || !updateActionAllowed("rollback");
    }
  }

  function trapPlatformUpdateFocus(event) {
    if (event.key !== "Tab") return;
    const dialog = event.currentTarget;
    const focusable = $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', dialog)
      .filter((node) => !node.hidden && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden");
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }

  function closePlatformUpdate(event) {
    if (event?.type === "close" && $("#platformUpdateDialog")?.open) return;
    const session = state.platformUpdate;
    $("#updatePasswordForm")?.reset();
    if (!session) return;
    session.dialogEpoch += 1;
    if (!session.submitted) { session.stage = null; session.busy = ""; }
    session.error = "";
    if (event?.type === "close") $("#versionBadgeButton")?.focus({ preventScroll: true });
  }

  function openPlatformUpdate() {
    if (!isPlatformAdmin()) return;
    const session = ensurePlatformUpdateSession();
    session.dialogEpoch += 1;
    closeVersionBadgePopover();
    openDialog("platformUpdateDialog");
    renderPlatformUpdate();
    $("#updateDialogClose")?.focus({ preventScroll: true });
    void loadVersionInfo({ force: true });
  }

  async function preparePlatformUpdate(action = "apply") {
    const session = ensurePlatformUpdateSession();
    if (!session || session.busy || session.stage || updateOperationActive(session) || !updateActionAllowed(action) || !$("#platformUpdateDialog")?.open) return;
    const candidate = action === "rollback" ? updateRollbackCandidates().find((item) => item.version === $("#updateRollbackSelect")?.value) : null;
    const check = state.versionUpdate?.update_check || state.version?.update_check;
    const version = action === "rollback" ? candidate?.version : isConfirmedHigherRelease(check, state.version?.version) ? check.version : "";
    if (!version) return;
    const epoch = session.dialogEpoch;
    const valid = () => platformUpdateSessionMatches(session) && session.dialogEpoch === epoch && $("#platformUpdateDialog")?.open && isPlatformAdmin();
    session.busy = "prepare";
    session.error = "";
    session.reconnecting = false;
    session.operationRevision += 1;
    renderPlatformUpdate();
    try {
      const result = await api("/api/admin/updates/download", { method: "POST", body: JSON.stringify({ version, action }), suppressSessionReset: true });
      if (!valid()) return;
      if (result.status !== "staged" || result.version !== version || !/^[a-f0-9]{32}$/.test(result.operation_id) || !/^[a-f0-9]{64}$/.test(result.manifest_sha256)
        || (candidate && candidate.manifest_sha256 !== result.manifest_sha256)) throw new ApiError(UPDATE_UI_COPY.messages.unknown_error, 502, "manifest_invalid");
      session.stage = { ...result, action };
      session.operation = null;
      $("#updatePasswordForm")?.reset();
    } catch (error) {
      if (valid()) session.error = updateErrorMessage(error, "download_failed");
    } finally {
      if (valid()) {
        session.busy = "";
        renderPlatformUpdate();
        if (session.stage) $("#updateAdminPassword")?.focus({ preventScroll: true });
      }
    }
  }

  async function confirmPlatformUpdate(event) {
    event.preventDefault();
    const session = ensurePlatformUpdateSession();
    const stage = session?.stage;
    if (!stage || session.busy || !updateActionAllowed(stage.action) || !$("#platformUpdateDialog")?.open) return;
    if (!$("#updateConfirmRisk").checked) { session.error = UPDATE_UI_COPY.messages.risk_required; renderPlatformUpdate(); return; }
    let password = $("#updateAdminPassword").value;
    if (!password) { session.error = UPDATE_UI_COPY.messages.confirm_password; renderPlatformUpdate(); return; }
    const epoch = session.dialogEpoch;
    const valid = () => platformUpdateSessionMatches(session) && session.dialogEpoch === epoch && session.stage === stage && $("#platformUpdateDialog")?.open && isPlatformAdmin();
    session.busy = "confirm";
    session.error = "";
    renderPlatformUpdate();
    let token = "";
    let didSubmit = false;
    try {
      const confirmation = await api("/api/admin/confirm", { method: "POST", body: JSON.stringify({ password, action: "update." + stage.action, version: stage.version, operation_id: stage.operation_id }), suppressSessionReset: true });
      password = "";
      if (!valid()) return;
      $("#updateAdminPassword").value = "";
      token = confirmation.confirmation_token || confirmation.confirmation_id || "";
      if (!token || (confirmation.operation_id && confirmation.operation_id !== stage.operation_id)) throw new ApiError(UPDATE_UI_COPY.errors.confirmation_invalid, 403, "confirmation_invalid");
      // From this point closing the dialog only hides it: the operation may
      // already be queued even when a service restart interrupts the response.
      session.submitted = true;
      didSubmit = true;
      session.operationRevision += 1;
      session.operation = { operation_id: stage.operation_id, action: stage.action, version: stage.version, status: "submitting", phase: "" };
      renderPlatformUpdate();
      scheduleUpdateOperationPoll(session);
      const queued = await api("/api/admin/updates/" + stage.action, { method: "POST", body: JSON.stringify({ version: stage.version, operation_id: stage.operation_id, confirmation_token: token }), suppressSessionReset: true });
      if (!platformUpdateSessionMatches(session)) return;
      const acknowledged = queued.operation || queued;
      const completed = UPDATE_TERMINAL_PHASES.has(updatePhase(acknowledged));
      if ((!completed && acknowledged.status !== "queued" && queued.queued !== true) || (acknowledged.operation_id && acknowledged.operation_id !== stage.operation_id)) throw new ApiError(UPDATE_UI_COPY.messages.unknown_error, 502);
      if (!completed && !UPDATE_TERMINAL_PHASES.has(updatePhase(session.operation))) session.operation = { ...session.operation, status: "queued", phase: "queued" };
      session.stage = null;
      session.error = "";
      // A duplicate submission may return its terminal state with HTTP 200;
      // read the operation endpoint before displaying installation success.
      if (completed) void pollUpdateOperation(session);
    } catch (error) {
      if (!platformUpdateSessionMatches(session)) return;
      if (didSubmit && UPDATE_TERMINAL_PHASES.has(updatePhase(session.operation))) return;
      if (session.submitted && (!error.status || error.status >= 500 || error.status === 408)) session.reconnecting = true;
      else if (session.submitted || valid()) {
        session.submitted = false;
        session.operation = null;
        if (!valid()) session.stage = null;
        session.error = updateErrorMessage(error);
      }
    } finally {
      password = "";
      token = "";
      if (platformUpdateSessionMatches(session) && (didSubmit || valid())) {
        $("#updateAdminPassword").value = "";
        session.busy = "";
        renderPlatformUpdate();
        if (session.submitted) scheduleUpdateOperationPoll(session);
      }
    }
  }

  function acceptUpdateOperation(session, operation) {
    if (!operation || !platformUpdateSessionMatches(session) || !isPlatformAdmin()) return;
    const id = String(operation.operation_id || operation.id || "");
    if (!id || ["staged", "available"].includes(operation.status) || (session.stage && !session.submitted) || session.busy === "prepare") return;
    if (session.operation && String(session.operation.operation_id || session.operation.id) !== id && updateOperationActive(session)) return;
    if (session.operation?.operation_id === id && UPDATE_TERMINAL_PHASES.has(updatePhase(session.operation)) && updatePhase(session.operation) !== updatePhase(operation)) return;
    const wasTerminal = session.operation?.operation_id === id && UPDATE_TERMINAL_PHASES.has(updatePhase(session.operation));
    session.operation = { ...operation, operation_id: id };
    session.operationRevision += 1;
    session.reconnecting = false;
    const terminal = UPDATE_TERMINAL_PHASES.has(updatePhase(operation));
    session.submitted = !terminal;
    if (terminal) {
      session.stage = null;
      if (!wasTerminal) {
        session.generation += 1;
        session.cacheAt = 0;
        session.manual = false;
        setBusy($("#versionBadgeRefresh"), false);
      }
      window.clearTimeout(session.pollTimer);
    } else scheduleUpdateOperationPoll(session);
    renderPlatformUpdate();
  }

  function scheduleUpdateOperationPoll(session) {
    if (!platformUpdateSessionMatches(session) || !isPlatformAdmin() || !updateOperationActive(session)) return;
    window.clearTimeout(session.pollTimer);
    session.pollTimer = window.setTimeout(() => void pollUpdateOperation(session), UPDATE_OPERATION_POLL_MS);
  }

  async function pollUpdateOperation(session) {
    if (!platformUpdateSessionMatches(session) || session.pollLoading || !isPlatformAdmin()) return;
    session.pollLoading = true;
    const revision = session.operationRevision;
    try {
      const update = await api("/api/admin/updates", { suppressSessionReset: true, timeoutMs: UPDATE_READ_TIMEOUT_MS });
      if (!platformUpdateSessionMatches(session) || revision !== session.operationRevision) return;
      acceptUpdateOperation(session, update?.operation);
      if (!updateOperationActive(session)) void loadVersionInfo({ force: true });
    } catch (_) {
      if (platformUpdateSessionMatches(session)) { session.reconnecting = true; renderPlatformUpdate(); }
    } finally {
      if (platformUpdateSessionMatches(session)) { session.pollLoading = false; scheduleUpdateOperationPoll(session); }
    }
  }

  const SEMVER_RE = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$/;

  function parseSemVer(v) {
    if (typeof v !== "string") return null;
    const m = v.trim().match(SEMVER_RE);
    if (!m) return null;
    return {
      major: parseInt(m[1], 10),
      minor: parseInt(m[2], 10),
      patch: parseInt(m[3], 10),
      prerelease: m[4] ? m[4].split(".") : [],
    };
  }

  function compareSemVer(aStr, bStr) {
    const a = parseSemVer(aStr);
    const b = parseSemVer(bStr);
    if (!a || !b) return null;
    if (a.major !== b.major) return a.major > b.major ? 1 : -1;
    if (a.minor !== b.minor) return a.minor > b.minor ? 1 : -1;
    if (a.patch !== b.patch) return a.patch > b.patch ? 1 : -1;
    if (a.prerelease.length === 0 && b.prerelease.length === 0) return 0;
    if (a.prerelease.length === 0) return 1;
    if (b.prerelease.length === 0) return -1;
    const maxLen = Math.max(a.prerelease.length, b.prerelease.length);
    for (let i = 0; i < maxLen; i++) {
      if (i >= a.prerelease.length) return -1;
      if (i >= b.prerelease.length) return 1;
      const ap = a.prerelease[i];
      const bp = b.prerelease[i];
      if (ap === bp) continue;
      const aNum = /^\d+$/.test(ap);
      const bNum = /^\d+$/.test(bp);
      if (aNum && bNum) {
        const diff = parseInt(ap, 10) - parseInt(bp, 10);
        return diff > 0 ? 1 : -1;
      }
      if (aNum !== bNum) return aNum ? -1 : 1;
      return ap > bp ? 1 : -1;
    }
    return 0;
  }

  function isHigherVersion(candidate, current) {
    return compareSemVer(candidate, current) === 1;
  }

  function isConfirmedHigherRelease(check, currentVersion) {
    if (!check || typeof check !== "object") return false;
    if (check.status !== "available") return false;
    if (check.available !== true) return false;
    if (check.error_code || check.error) return false;
    if (!currentVersion || typeof currentVersion !== "string") return false;
    if (check.current_version && check.current_version !== currentVersion) return false;
    if (!check.version || typeof check.version !== "string") return false;
    return isHigherVersion(check.version, currentVersion);
  }

  function renderVersionBadgeRollback() {
    const rollbackSection = $("#versionRollbackDetails");
    const divider = $("#versionRollbackDivider");
    if (!isPlatformAdmin()) {
      if (rollbackSection) rollbackSection.hidden = true;
      if (divider) divider.hidden = true;
      return;
    }
    if (rollbackSection) rollbackSection.hidden = false;
    if (divider) divider.hidden = false;

    const listEl = $("#versionRollbackList");
    const actionRow = $("#versionRollbackActionRow");
    const rollbackBtn = $("#versionPopoverRollbackBtn");
    const candidates = updateRollbackCandidates();
    const canRollback = updateActionAllowed("rollback");
    const session = state.platformUpdate;
    const hasLoaded = Boolean(state.versionUpdate || session?.cacheAt);
    const isLoading = Boolean(session?.loading || (!hasLoaded && !session?.probeError));
    const hasError = Boolean(session?.probeError);

    if (isLoading) {
      if (listEl) {
        listEl.innerHTML = '<p class="version-rollback-empty">正在获取回退历史...</p>';
      }
      if (actionRow) actionRow.hidden = true;
      return;
    }

    if (hasError) {
      if (listEl) {
        listEl.innerHTML = '<p class="version-rollback-empty">获取回退记录失败</p>';
      }
      if (actionRow) actionRow.hidden = true;
      return;
    }

    if (!candidates || candidates.length === 0) {
      if (listEl) {
        listEl.innerHTML = '<p class="version-rollback-empty">暂无可回退版本</p>';
      }
      if (actionRow) actionRow.hidden = true;
      return;
    }

    const displayCandidates = candidates.slice(0, 3);
    const existingSelected = $('input[name="versionRollbackChoice"]:checked', listEl)?.value;
    const selectedVersion = displayCandidates.some((c) => c.version === existingSelected)
      ? existingSelected
      : displayCandidates[0].version;

    if (listEl) {
      listEl.innerHTML = displayCandidates.map((item) => {
        const isChecked = item.version === selectedVersion ? "checked" : "";
        const rawDate = item.date || item.release_date || item.published_at || item.created_at;
        const dateHtml = (rawDate && typeof rawDate === "string")
          ? '<span class="version-rollback-date">' + esc(rawDate.slice(0, 10)) + '</span>'
          : "";
        return (
          '<label class="version-rollback-item">' +
            '<input type="radio" name="versionRollbackChoice" value="' + esc(item.version) + '" ' + isChecked + '>' +
            '<span class="version-rollback-ver">v' + esc(item.version) + '</span>' +
            dateHtml +
          '</label>'
        );
      }).join("");
    }

    if (actionRow) actionRow.hidden = false;
    if (rollbackBtn) {
      rollbackBtn.disabled = !canRollback;
      const caps = updateCapabilities();
      if (!canRollback) {
        rollbackBtn.title = updateErrorMessage({ code: caps.reason || caps.error_code }, "readiness_unknown");
      } else {
        rollbackBtn.removeAttribute("title");
      }
    }
  }

  function renderVersionBadge() {
    const versionObj = state.version || state.docs?.version || {};
    const updateObj = state.versionUpdate || state.docs?.update || {};
    const check = updateObj.update_check || versionObj.update_check || {};
    const probe = updateObj.update_probe || versionObj.update_probe || check.update_probe || {};
    const curVer = versionObj.version ? "v" + versionObj.version : "v--";
    const currentVersion = typeof versionObj.version === "string" ? versionObj.version : "";
    text("#versionBadgeValue", curVer);
    text("#versionBadgeCurrent", versionObj.version ? "v" + versionObj.version : "--");

    const hasHigher = isConfirmedHigherRelease(check, currentVersion);
    const hasUpdate = Boolean(state.me && hasHigher);
    const btn = $("#versionBadgeButton");
    const dot = $("#versionBadgeDot");
    if (btn) {
      btn.classList.toggle("has-update", hasUpdate);
      btn.classList.toggle("is-warning", hasUpdate);
      btn.classList.toggle("update-available", hasUpdate);
    }
    if (dot) dot.hidden = !hasUpdate;

    let statusText = "尚未检查";
    let isUpToDate = false;

    const isVersionMismatch = Boolean(check.current_version && currentVersion && check.current_version !== currentVersion);
    const hasValidCurrentSemVer = Boolean(currentVersion && parseSemVer(currentVersion));
    const hasValidCheckSemVer = Boolean(check.version && parseSemVer(check.version));

    if (hasHigher) {
      statusText = "发现更高版本 v" + (check.version || "");
    } else if (probe.state === "error" && probe.has_success !== true) {
      statusText = UPDATE_UI_COPY.messages.probe_failed;
    } else if (check.status === "incomplete") {
      statusText = "发现更高版本（安装包不完整）";
    } else if (check.status === "no_release") {
      statusText = "尚无发布版本";
    } else if (check.status === "error" || check.error_code || check.error) {
      statusText = "更新检查失败";
    } else if (isVersionMismatch) {
      statusText = "更新检查失败";
    } else if (check.status === "current") {
      if (!check.error && !check.error_code && (!check.current_version || check.current_version === currentVersion)) {
        statusText = "已是最新版本，无需更新";
        isUpToDate = true;
      } else {
        statusText = "更新检查失败";
      }
    } else if (check.status === "available" && check.available === true && !check.error && !check.error_code && !isVersionMismatch) {
      if (hasValidCurrentSemVer && hasValidCheckSemVer && compareSemVer(check.version, currentVersion) <= 0) {
        statusText = "未发现更高版本，无需更新";
        isUpToDate = true;
      } else {
        statusText = "更新检查数据无效";
      }
    }
    text("#versionBadgeStatus", statusText);

    const statusIcon = $("#versionBadgeStatusIcon");
    if (statusIcon) statusIcon.hidden = !isUpToDate;

    const canUpgrade = isPlatformAdmin() && hasHigher;
    const updateBtn = $("#versionBadgeUpdate");
    const updateActions = $("#versionBadgeUpdateActions");
    if (updateBtn) {
      updateBtn.hidden = !canUpgrade;
      if (canUpgrade && check.version) {
        text("#versionBadgeUpdateText", "立即升级到 v" + check.version);
      }
    }
    if (updateActions) {
      updateActions.hidden = !canUpgrade;
    }

    const session = state.platformUpdate;
    const operation = session?.operation || state.versionUpdate?.operation;
    const hasOp = Boolean(isPlatformAdmin() && (operation || session?.reconnecting));
    const opBtn = $("#versionBadgeOperation");
    const opWrap = $("#versionBadgeOperationContainer");
    if (opBtn) {
      opBtn.hidden = !hasOp;
      if (hasOp) {
        const phase = updatePhase(operation);
        const isDone = ["succeeded", "rolled_back"].includes(phase);
        text("#versionBadgeOperationText", isDone ? "查看更新结果" : "查看更新进度");
      }
    }
    if (opWrap) {
      opWrap.hidden = !hasOp;
    }

    const refreshBtn = $("#versionBadgeRefresh");
    if (refreshBtn) {
      const admin = isPlatformAdmin();
      refreshBtn.hidden = !admin;
    }
    const relLink = $("#versionBadgeDetails") || $("#versionBadgeReleaseLink");
    if (relLink) {
      relLink.hidden = false;
      relLink.href = "https://github.com/tswawa/xianyu-saas/releases";
      relLink.target = "_blank";
      relLink.rel = "noopener noreferrer";
    }

    renderVersionBadgeRollback();
  }

  function toggleVersionBadgePopover(force) {
    const popover = $("#versionBadgePopover");
    const button = $("#versionBadgeButton");
    if (!popover) return;
    const shouldOpen = typeof force === "boolean" ? force : popover.hidden;
    popover.hidden = !shouldOpen;
    button?.setAttribute("aria-expanded", String(shouldOpen));
    if (shouldOpen) {
      renderVersionBadge();
      if (isPlatformAdmin() && !state.versionUpdate) {
        void loadVersionInfo();
      }
    }
  }

  function closeVersionBadgePopover(options = {}) {
    const popover = $("#versionBadgePopover");
    const button = $("#versionBadgeButton");
    if (!popover || popover.hidden) return;
    popover.hidden = true;
    button?.setAttribute("aria-expanded", "false");
    if (options?.restoreFocus) {
      button?.focus({ preventScroll: true });
    }
  }

  async function loadUnifiedAiConnection() {
    if (!state.me) return;
    const settings = state.settingsAi;
    if (settings.loading) return settings.loading;
    const username = state.me.username;
    const revision = settings.connection?.revision;
    const valid = () => state.settingsAi === settings && state.me?.username === username;
    settings.loading = (async () => {
      try {
        const res = await api("/api/settings/ai/connection");
        if (!valid() || settings.operation || settings.connection?.revision !== revision) return;
        if (res?.scope !== "user" || !Number.isInteger(res.revision)) throw new ApiError("连接信息格式无效");
        if (revision !== undefined && res.revision !== revision) {
          settings.verificationToken = "";
          settings.testedFingerprint = "";
        }
        settings.connection = res;
        settings.error = "";
        if (Array.isArray(res.providers)) populateAiProviders(res.providers);
      } catch (error) {
        if (valid()) {
          settings.error = error.message || "连接信息读取失败，请重试";
          formMessage("#aiConnectionMessage", settings.error);
        }
      } finally {
        if (valid()) {
          settings.loading = null;
          renderSettingsAiPanel();
          renderAiConfigSummary();
          updateOpsConnectionBadge();
        }
      }
    })();
    return settings.loading;
  }

  const loadSettingsAiConnection = loadUnifiedAiConnection;

  function populateAiProviders(providers) {
    const select = $("#aiProvider");
    if (!select || !Array.isArray(providers) || !providers.length) return;
    const cur = select.value;
    select.replaceChildren();
    providers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.code;
      opt.textContent = p.label || p.code;
      select.append(opt);
    });
    if (cur && Array.from(select.options).some((o) => o.value === cur)) {
      select.value = cur;
    }
  }

  function renderSettingsAiPanel() {
    const panel = $("#settingsAiPanel");
    if (!panel) return;
    const conn = state.settingsAi?.connection;
    const badge = $("#settingsAiConnectionBadge");
    const verified = conn?.initialized === true && conn?.connection_status === "verified";
    if (badge) {
      if (verified) {
        badge.textContent = "已连接 (" + (conn.model || "") + ")";
        badge.className = "badge badge-green";
      } else if (conn?.initialized && (conn?.base_url || conn?.model)) {
        badge.textContent = "已配置 (未验证)";
        badge.className = "badge badge-amber";
      } else {
        badge.textContent = "未连接";
        badge.className = "badge badge-muted";
      }
    }
    const testBtn = $("#aiTestConnection");
    if (testBtn) {
      setBusy(testBtn, state.settingsAi.operation === "test");
      testBtn.disabled = Boolean(state.settingsAi.operation);
    }
    const delBtn = $("#aiDeleteKey");
    if (delBtn) {
      delBtn.hidden = !conn?.initialized || (!conn?.api_key_configured && conn?.connection_status !== "verified");
      delBtn.disabled = Boolean(state.settingsAi.operation);
    }
    $("#aiSaveConnection").disabled = Boolean(state.settingsAi.operation) || !state.settingsAi.verificationToken;
    if (state.settingsAi?.draft) {
      if ($("#aiProvider") && state.settingsAi.draft.provider) $("#aiProvider").value = state.settingsAi.draft.provider;
      if ($("#aiBaseUrl") && typeof state.settingsAi.draft.base_url === "string") $("#aiBaseUrl").value = state.settingsAi.draft.base_url;
      if ($("#aiModel") && typeof state.settingsAi.draft.model === "string") $("#aiModel").value = state.settingsAi.draft.model;
      if ($("#aiApiKey") && typeof state.settingsAi.draft.api_key === "string") $("#aiApiKey").value = state.settingsAi.draft.api_key;
    } else if (conn && !state.settingsAi?.testedFingerprint) {
      if ($("#aiProvider") && conn.provider) $("#aiProvider").value = conn.provider;
      if ($("#aiBaseUrl")) $("#aiBaseUrl").value = conn.base_url || "";
      if ($("#aiModel")) $("#aiModel").value = conn.model || "";
    }
    renderAiProviderFields();
  }

  function newOpsState() {
    return {
      session: null,
      messages: [],
      activeRun: null,
      runEvents: [],
      pendingChat: null,
      loading: false,
      sending: false,
      stopping: false,
      loadGeneration: 0,
      pollTimer: null,
      error: null,
    };
  }

  function resizeOpsPrompt(input) {
    if (!input) return;
    const visualRows = String(input.value || "").split(/\r?\n/).reduce(
      (total, line) => total + Math.max(1, Math.ceil(line.length / 46)),
      0,
    );
    input.rows = Math.min(6, Math.max(3, visualRows));
  }

  function resetOpsState() {
    if (state.ops?.pollTimer) {
      clearTimeout(state.ops.pollTimer);
      state.ops.pollTimer = null;
    }
    state.ops = newOpsState();
    const form = $("#opsPromptForm");
    if (form) form.reset();
    const input = $("#opsPromptInput");
    if (input) {
      input.value = "";
      resizeOpsPrompt(input);
    }
    renderOpsChat();
  }

  function opsContextMatches(ops, context) {
    return state.ops === ops && Boolean(state.me) && accountContextMatches(context);
  }

  function newOpsRequestId() {
    return "opr-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function updateOpsConnectionBadge() {
    // Retained for backward compatibility
  }

  function formatOpsError(err, fallbackRequestId = "") {
    const detail = err?.detail || {};
    return {
      source: detail.source || (err?.status === 401 ? "authentication" : "application"),
      message: detail.message || err?.message || "运维请求执行异常",
      code: detail.code || err?.code || "",
      upstream_status: detail.upstream_status ?? null,
      upstream_code: detail.upstream_code ?? null,
      upstream_type: detail.upstream_type ?? null,
      upstream_request_id: detail.upstream_request_id ?? null,
      request_id: detail.request_id || fallbackRequestId,
    };
  }

  function renderErrorDetails(err) {
    if (!err) return "";
    const sourceLabel = {
      provider: "上游模型服务提供方",
      transport: "网络传输超时或中断",
      application: "系统业务逻辑异常",
      authentication: "用户登录凭证异常",
    }[err.source] || (err.source ? String(err.source) : "系统异常");

    let meta = `<span>错误来源: <strong>${escapeHtml(sourceLabel)}</strong></span>`;
    if (err.upstream_status != null) meta += `<span>上游HTTP状态: <code>${escapeHtml(String(err.upstream_status))}</code></span>`;
    if (err.upstream_code != null) meta += `<span>上游错误代码: <code>${escapeHtml(String(err.upstream_code))}</code></span>`;
    if (err.upstream_type != null) meta += `<span>上游错误类型: <code>${escapeHtml(String(err.upstream_type))}</code></span>`;
    if (err.upstream_request_id != null) meta += `<span>上游请求ID: <code>${escapeHtml(String(err.upstream_request_id))}</code></span>`;
    if (err.code) meta += `<span>系统错误代码: <code>${escapeHtml(err.code)}</code></span>`;
    if (err.request_id) meta += `<span>请求ID: <code>${escapeHtml(err.request_id)}</code></span>`;

    const providerHint = err.source === "provider"
      ? '<p class="ops-error-hint">提示：此错误由模型提供方返回，您的本站系统登录状态依然有效，无需重新登录。</p>'
      : "";

    return `
      <div class="ops-error-card">
        <div class="ops-error-head">
          <svg class="icon"><use href="/xianyu-saas/assets/icons.svg?v=20260908-02#circle-alert"></use></svg>
          <strong>${escapeHtml(err.message || "操作执行异常")}</strong>
        </div>
        <div class="ops-error-meta">${meta}</div>
        ${providerHint}
      </div>
    `;
  }

  async function loadOpsContext() {
    if (!state.me) return;
    const context = captureAccountContext();
    const ops = state.ops;
    ops.loadGeneration += 1;
    const gen = ops.loadGeneration;
    const valid = () => opsContextMatches(ops, context) && ops.loadGeneration === gen;

    if (ops.pollTimer) {
      clearTimeout(ops.pollTimer);
      ops.pollTimer = null;
    }

    ops.loading = true;
    ops.error = null;
    renderOpsChat();

    try {
      const res = await accountScopedApi(context, "/api/ops/sessions/current");
      if (!valid()) return;

      const session = res?.session || null;
      let activeRun = res?.active_run || null;

      if (!session) {
        // Zero write on empty GET. Render empty state.
        ops.session = null;
        ops.messages = [];
        ops.activeRun = null;
        ops.runEvents = [];
        ops.loading = false;
        renderOpsChat();
        return;
      }

      ops.session = session;
      ops.activeRun = activeRun;

      // Cursor-based message loop without truncation
      let allMessages = [];
      let cursor = 0;
      while (true) {
        const msgRes = await accountScopedApi(context, "/api/ops/sessions/" + encodeURIComponent(session.id) + "/messages?cursor=" + cursor);
        if (!valid()) return;
        const msgs = Array.isArray(msgRes?.messages) ? msgRes.messages : [];
        allMessages = allMessages.concat(msgs);
        if (msgRes?.active_run) {
          ops.activeRun = msgRes.active_run;
          activeRun = msgRes.active_run;
        }
        if (msgRes?.next_cursor != null) {
          cursor = msgRes.next_cursor;
        } else {
          break;
        }
      }

      ops.messages = allMessages;

      if (activeRun && ["queued", "running", "cancel_requested"].includes(activeRun.status)) {
        ops.loading = false;
        renderOpsChat();
        startOpsRunPolling(ops, context, session.id, activeRun.id || activeRun.run_id, gen);
        return;
      }

      // Restore last run details for retry eligibility and summary counts
      const lastRunId = activeRun?.id || [...allMessages].reverse().find((m) => m.run_id)?.run_id;
      if (lastRunId) {
        try {
          const runDetail = await accountScopedApi(context, "/api/ops/runs/" + encodeURIComponent(lastRunId));
          if (valid()) {
            ops.activeRun = runDetail;
          }
        } catch (_) {
          // Metadata fetch failure is non-fatal
        }
      }

      ops.loading = false;
      renderOpsChat();
    } catch (err) {
      if (!valid()) return;
      ops.loading = false;
      ops.error = formatOpsError(err);
      renderOpsChat();
    }
  }

  async function createNewOpsSession() {
    const ops = state.ops;
    const context = captureAccountContext();
    if (ops.sending || ops.stopping) {
      showToast("当前有任务正在执行，请先等待或停止", "warning");
      return;
    }
    if (ops.pollTimer) {
      clearTimeout(ops.pollTimer);
      ops.pollTimer = null;
    }
    ops.loadGeneration += 1;
    const gen = ops.loadGeneration;
    const valid = () => opsContextMatches(ops, context) && ops.loadGeneration === gen;

    ops.loading = true;
    renderOpsChat();

    try {
      const res = await accountScopedApi(context, "/api/ops/sessions", {
        method: "POST",
      });
      if (!valid()) return;
      ops.session = res?.session || null;
      ops.activeRun = null;
      ops.runEvents = [];
      ops.messages = [];
      ops.pendingChat = null;
      ops.error = null;
      ops.loading = false;
      renderOpsChat();
      showToast("已开启新会话，旧会话已安全归档在服务端");
    } catch (err) {
      if (!valid()) return;
      ops.loading = false;
      showToast(err.message || "新建会话失败", "error");
      renderOpsChat();
    }
  }

  async function sendOpsChat(event) {
    if (event) event.preventDefault();
    const ops = state.ops;
    if (ops.sending || ops.stopping || ops.loading) return;

    const input = $("#opsPromptInput");
    const rawText = input ? input.value : "";
    if (!rawText.trim()) return;

    // Lock sending synchronously before any await to avoid double submits
    ops.sending = true;
    ops.error = null;

    const context = captureAccountContext();
    const gen = ops.loadGeneration;
    const currentSessionId = ops.session?.id || "";

    // Reuse requestId on failure retry of identical message
    let requestId;
    if (ops.pendingChat && ops.pendingChat.session_id === currentSessionId && ops.pendingChat.message === rawText) {
      requestId = ops.pendingChat.request_id;
    } else {
      requestId = newOpsRequestId();
      ops.pendingChat = { session_id: currentSessionId, message: rawText, request_id: requestId };
    }

    renderOpsChat();

    const valid = (targetSessionId) => {
      if (!opsContextMatches(ops, context)) return false;
      if (ops.loadGeneration !== gen) return false;
      if (targetSessionId !== undefined && (ops.session?.id || "") !== targetSessionId) return false;
      return true;
    };

    try {
      const payload = {
        session_id: currentSessionId,
        request_id: requestId,
        message: rawText,
      };
      const res = await accountScopedApi(context, "/api/ops/chat", {
        method: "POST",
        headers: {
          "Idempotency-Key": requestId,
        },
        body: JSON.stringify(payload),
      });

      if (!valid(currentSessionId)) return;

      // Successful dispatch: clear input and pendingChat
      ops.pendingChat = null;
      if (input) {
        input.value = "";
        resizeOpsPrompt(input);
      }

      ops.messages.push({
        id: "local-" + Date.now(),
        role: "user",
        content: rawText,
      });

      const newSessionId = res?.session_id || currentSessionId;
      if (!ops.session || ops.session.id !== newSessionId) {
        ops.session = { id: newSessionId, account_key: context.accountKey };
      }

      const runId = res?.run_id;
      ops.activeRun = {
        id: runId,
        run_id: runId,
        session_id: newSessionId,
        status: res?.status || "queued",
      };
      ops.runEvents = [];
      renderOpsChat();

      if (runId) {
        startOpsRunPolling(ops, context, newSessionId, runId, gen);
      } else {
        ops.sending = false;
        renderOpsChat();
      }
    } catch (err) {
      if (!valid(currentSessionId)) return;
      ops.sending = false;
      // Do not clear input on failure so user can retry or edit
      ops.error = formatOpsError(err, requestId);
      renderOpsChat();
    }
  }

  function startOpsRunPolling(ops, context, sessionId, runId, gen, initialAfterSeq = 0) {
    if (ops.pollTimer) {
      clearTimeout(ops.pollTimer);
      ops.pollTimer = null;
    }

    let afterSeq = initialAfterSeq;
    const seenSeqs = new Set((ops.runEvents || []).map((e) => e.seq));

    const poll = async () => {
      if (!opsContextMatches(ops, context)) return;
      if (ops.loadGeneration !== gen) return;
      if ((ops.session?.id || "") !== sessionId) return;
      if ((ops.activeRun?.id || ops.activeRun?.run_id || "") !== runId) return;

      try {
        const run = await accountScopedApi(context, "/api/ops/runs/" + encodeURIComponent(runId) + "?after_seq=" + afterSeq);
        if (!opsContextMatches(ops, context)) return;
        if (ops.loadGeneration !== gen) return;
        if ((ops.session?.id || "") !== sessionId) return;
        if ((ops.activeRun?.id || ops.activeRun?.run_id || "") !== runId) return;

        ops.activeRun = run;

        // Advance afterSeq with run.next_seq
        if (run.next_seq != null && run.next_seq > afterSeq) {
          afterSeq = run.next_seq;
        }

        // Deduplicate and accumulate live events
        if (Array.isArray(run.events)) {
          ops.runEvents = ops.runEvents || [];
          for (const ev of run.events) {
            if (!seenSeqs.has(ev.seq)) {
              seenSeqs.add(ev.seq);
              ops.runEvents.push(ev);
            }
          }
        }

        const isActive = ["queued", "running", "cancel_requested"].includes(run.status);
        if (isActive) {
          renderOpsChat();
          ops.pollTimer = setTimeout(poll, 1000);
        } else {
          ops.pollTimer = null;
          ops.sending = false;
          ops.stopping = false;

          // Reload committed messages from server upon reaching terminal status
          if (sessionId) {
            let allMessages = [];
            let cursor = 0;
            while (true) {
              const msgRes = await accountScopedApi(context, "/api/ops/sessions/" + encodeURIComponent(sessionId) + "/messages?cursor=" + cursor);
              if (!opsContextMatches(ops, context) || ops.loadGeneration !== gen || (ops.session?.id || "") !== sessionId) return;
              const msgs = Array.isArray(msgRes?.messages) ? msgRes.messages : [];
              allMessages = allMessages.concat(msgs);
              if (msgRes?.next_cursor != null) {
                cursor = msgRes.next_cursor;
              } else {
                break;
              }
            }
            ops.messages = allMessages;
          }
          renderOpsChat();
        }
      } catch (err) {
        if (!opsContextMatches(ops, context) || ops.loadGeneration !== gen) return;
        ops.pollTimer = setTimeout(poll, 2000);
      }
    };

    ops.pollTimer = setTimeout(poll, 500);
  }

  async function cancelOpsRun() {
    const ops = state.ops;
    const context = captureAccountContext();
    const sessionId = ops.session?.id || "";
    const runId = ops.activeRun?.id || ops.activeRun?.run_id;
    const gen = ops.loadGeneration;
    if (!runId || ops.stopping) return;

    ops.stopping = true;
    renderOpsChat();

    const valid = () => opsContextMatches(ops, context) && ops.loadGeneration === gen && (ops.session?.id || "") === sessionId && (ops.activeRun?.id || ops.activeRun?.run_id || "") === runId;

    try {
      const res = await accountScopedApi(context, "/api/ops/runs/" + encodeURIComponent(runId) + "/cancel", {
        method: "POST",
      });
      if (!valid()) return;
      if (res) {
        ops.activeRun = res;
      }
      if (res && !["queued", "running", "cancel_requested"].includes(res.status)) {
        if (ops.pollTimer) {
          clearTimeout(ops.pollTimer);
          ops.pollTimer = null;
        }
        ops.stopping = false;
        ops.sending = false;
        await loadOpsContext();
      } else {
        renderOpsChat();
      }
    } catch (err) {
      if (!valid()) return;
      ops.stopping = false;
      showToast(err.message || "停止请求失败", "error");
      renderOpsChat();
    }
  }

  async function retryOpsRun(runId) {
    const ops = state.ops;
    const context = captureAccountContext();
    const sessionId = ops.session?.id || "";
    const gen = ops.loadGeneration;
    if (!runId || ops.sending || ops.stopping) return;
    if (ops.activeRun && (ops.activeRun.id === runId || ops.activeRun.run_id === runId) && ops.activeRun.recoverable !== true) return;

    const requestId = newOpsRequestId();
    ops.sending = true;
    ops.error = null;
    renderOpsChat();

    const valid = () => opsContextMatches(ops, context) && ops.loadGeneration === gen && (ops.session?.id || "") === sessionId;

    try {
      const res = await accountScopedApi(context, "/api/ops/runs/" + encodeURIComponent(runId) + "/retry", {
        method: "POST",
        headers: { "Idempotency-Key": requestId },
        body: JSON.stringify({ request_id: requestId }),
      });
      if (!valid()) return;
      const newRunId = res?.run_id || runId;
      ops.activeRun = {
        id: newRunId,
        run_id: newRunId,
        session_id: sessionId,
        status: res?.status || "queued",
      };
      ops.runEvents = [];
      renderOpsChat();
      startOpsRunPolling(ops, context, sessionId, newRunId, gen);
    } catch (err) {
      if (!valid()) return;
      ops.sending = false;
      showToast(err.message || "重试失败", "error");
      renderOpsChat();
    }
  }

  function renderOpsChat() {
    const ops = state.ops;
    const shopBadge = $("#opsCurrentShopBadge");
    if (shopBadge) {
      const current = currentAccount();
      const shopName = current?.name || state.activeAccountKey || "默认店铺";
      shopBadge.textContent = "当前店铺：" + shopName;
    }

    const sendBtn = $("#opsSendBtn");
    const stopBtn = $("#opsStopBtn");
    const newSessionBtn = $("#opsNewSessionBtn");

    const isActiveRun = Boolean(ops.activeRun && ["queued", "running", "cancel_requested"].includes(ops.activeRun.status));
    const isBusy = ops.loading || ops.sending || isActiveRun;

    if (sendBtn) {
      sendBtn.disabled = isBusy;
    }
    if (newSessionBtn) {
      newSessionBtn.disabled = isBusy;
    }
    if (stopBtn) {
      stopBtn.hidden = !isActiveRun;
      stopBtn.disabled = ops.stopping || ops.activeRun?.status === "cancel_requested";
      const stopTextNode = stopBtn.querySelector("span");
      if (stopTextNode) {
        stopTextNode.textContent = ops.stopping || ops.activeRun?.status === "cancel_requested" ? "正在请求停止…" : "停止执行";
      }
    }

    const container = $("#opsChatHistory");
    if (!container) return;

    if (!ops.messages.length && !ops.loading && !isActiveRun && !ops.error) {
      container.innerHTML = `
        <div class="ops-empty-panel">
          <div class="ops-empty-icon">
            <svg class="icon"><use href="/xianyu-saas/assets/icons.svg?v=20260908-02#sparkles"></use></svg>
          </div>
          <h3>智能运维 Agent 已就绪</h3>
          <p>请在下方输入自然语言指令。Agent 将分析您的意图，自动安全地配置客服知识库、回复规则或发货策略。</p>
          <div class="ops-empty-tips">
            <strong>支持的运维领域：</strong>
            <ul>
              <li><strong>知识库维护：</strong>新增、更新常见问答与咨询知识</li>
              <li><strong>规则管理：</strong>关键词匹配、欢迎语、自动回复规则</li>
              <li><strong>发货策略：</strong>绑定已有网盘链接或卡密池库存，自动化履约</li>
            </ul>
          </div>
        </div>
      `;
      return;
    }

    let html = "";

    if (ops.loading && !ops.messages.length) {
      html += `
        <div class="ops-msg-wrap ops-msg-assistant">
          <div class="ops-bubble-assistant ops-run-progress">
            <svg class="icon spin"><use href="/xianyu-saas/assets/icons.svg?v=20260908-02#refresh-cw"></use></svg>
            <span>正在加载运维历史记录…</span>
          </div>
        </div>
      `;
    }

    // Render messages: MUST prioritize kind === "tool" over role === "assistant"
    for (const msg of ops.messages) {
      if (msg.kind === "tool" || msg.role === "tool") {
        const isSucceeded = msg.status === "succeeded";
        const isFailed = msg.status === "failed";
        const statusBadgeClass = isSucceeded ? "badge-green" : isFailed ? "badge-red" : "badge-amber";
        const statusBadgeText = isSucceeded ? "执行成功" : isFailed ? "执行失败" : "部分完成";
        const title = msg.summary || "执行回执";
        const targets = Array.isArray(msg.targets) && msg.targets.length ? `<div class="ops-receipt-targets">目标: ${msg.targets.map((t) => `<code>${escapeHtml(String(t))}</code>`).join(" ")}</div>` : "";
        const bodyContent = msg.content && msg.content !== title ? `<p>${escapeHtml(msg.content)}</p>` : "";
        const errorContent = msg.error ? renderErrorDetails(msg.error) : "";

        html += `
          <div class="ops-msg-wrap ops-msg-receipt">
            <details open class="ops-receipt-card">
              <summary class="ops-receipt-summary">
                <span class="ops-receipt-icon ${isFailed ? 'is-error' : 'is-success'}"></span>
                <span class="ops-receipt-title">${escapeHtml(title)}</span>
                <span class="badge ${statusBadgeClass}">${statusBadgeText}</span>
              </summary>
              <div class="ops-receipt-body">
                ${targets}
                ${bodyContent}
                ${errorContent}
              </div>
            </details>
          </div>
        `;
      } else if (msg.kind === "status") {
        html += `
          <div class="ops-msg-wrap ops-msg-assistant">
            <div class="ops-bubble-assistant ops-bubble-status">${escapeHtml(msg.content || msg.summary || "")}</div>
          </div>
        `;
      } else if (msg.role === "user") {
        html += `
          <div class="ops-msg-wrap ops-msg-user">
            <div class="ops-bubble-user">${escapeHtml(msg.content)}</div>
          </div>
        `;
      } else if (msg.role === "assistant") {
        html += `
          <div class="ops-msg-wrap ops-msg-assistant">
            <div class="ops-bubble-assistant">${escapeHtml(msg.content)}</div>
          </div>
        `;
      }
    }

    // Render live active run events if currently running
    if (isActiveRun) {
      if (Array.isArray(ops.runEvents)) {
        for (const ev of ops.runEvents) {
          if (ev.kind === "tool") {
            const isSucceeded = ev.status === "succeeded";
            const isFailed = ev.status === "failed";
            const statusBadgeClass = isSucceeded ? "badge-green" : isFailed ? "badge-red" : "badge-amber";
            const statusBadgeText = isSucceeded ? "执行成功" : isFailed ? "执行失败" : "执行中";
            const targets = Array.isArray(ev.targets) && ev.targets.length ? `<div class="ops-receipt-targets">目标: ${ev.targets.map((t) => `<code>${escapeHtml(String(t))}</code>`).join(" ")}</div>` : "";
            html += `
              <div class="ops-msg-wrap ops-msg-receipt">
                <details open class="ops-receipt-card">
                  <summary class="ops-receipt-summary">
                    <span class="ops-receipt-icon ${isFailed ? 'is-error' : 'is-success'}"></span>
                    <span class="ops-receipt-title">${escapeHtml(ev.summary || "正在执行操作")}</span>
                    <span class="badge ${statusBadgeClass}">${statusBadgeText}</span>
                  </summary>
                  <div class="ops-receipt-body">
                    ${targets}
                    ${ev.error ? renderErrorDetails(ev.error) : ""}
                  </div>
                </details>
              </div>
            `;
          } else if (ev.kind === "assistant" && ev.content) {
            html += `
              <div class="ops-msg-wrap ops-msg-assistant">
                <div class="ops-bubble-assistant">${escapeHtml(ev.content)}</div>
              </div>
            `;
          }
        }
      }

      const status = ops.activeRun.status;
      const progressText = status === "cancel_requested"
        ? "已请求停止；正在处理当前步骤，后续操作将被中止…"
        : status === "queued"
        ? "任务已保存，正在排队等待执行…"
        : "正在执行运维任务…";

      html += `
        <div class="ops-msg-wrap ops-msg-assistant">
          <div class="ops-bubble-assistant ops-run-progress">
            <svg class="icon spin"><use href="/xianyu-saas/assets/icons.svg?v=20260908-02#refresh-cw"></use></svg>
            <span>${escapeHtml(progressText)}</span>
          </div>
        </div>
      `;
    }

    // Terminal run summary & retry button if recoverable
    if (ops.activeRun && !isActiveRun) {
      const r = ops.activeRun;
      const isTerminalFailure = ["failed", "partial_failed", "cancelled"].includes(r.status);
      const isRecoverable = r.recoverable === true;
      const changedCount = Number(r.changed_count || 0);
      const failedCount = Number(r.failed_count || 0);

      let summaryMeta = `<span>生效配置: <strong>${changedCount}</strong> 项</span> · <span>失败: <strong>${failedCount}</strong> 项</span>`;
      let retryBtn = "";
      if (isTerminalFailure && isRecoverable) {
        const runId = r.id || r.run_id;
        retryBtn = `<button type="button" class="button button-secondary button-compact ops-retry-btn" data-retry-run="${escapeHtml(runId)}"><svg class="icon"><use href="/xianyu-saas/assets/icons.svg?v=20260908-02#refresh-cw"></use></svg><span>重试失败任务</span></button>`;
      }

      html += `
        <div class="ops-run-summary-bar">
          <div class="ops-run-summary-info">${summaryMeta}</div>
          ${retryBtn}
        </div>
      `;

      if (r.error) {
        html += `<div class="ops-msg-wrap ops-msg-error">${renderErrorDetails(r.error)}</div>`;
      }
    }

    if (ops.error) {
      html += `<div class="ops-msg-wrap ops-msg-error">${renderErrorDetails(ops.error)}</div>`;
    }

    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
  }

  async function bootstrap() {
    state.accountEpoch += 1;
    const epoch = state.accountEpoch;
    state.me = await api("/api/me");
    const stored = readStoredAccountKey();
    state.activeAccountKey = stored || state.activeAccountKey || "default";
    // Restore a non-default shop only when the browser explicitly selected
    // it before.  This keeps the first paint compatible with older servers
    // while still making account choice durable per signed-in owner.
    if (stored && stored !== "default") {
      try {
        await loadAccounts();
      } catch (error) {
        state.activeAccountKey = "default";
      }
    }
    loadInboxPreferences();
    const [config, bot] = await Promise.all([api("/api/config"), api("/api/bot/status")]);
    if (epoch !== state.accountEpoch) return;
    state.config = config;
    state.bot = bot;
    ensureCurrentAccount(bot);
    const catalogStatus = registerCatalogStatus(bot);
    renderNav();
    renderAccount();
    renderSettings();
    renderOverview();
    void loadVersionInfo().catch(() => {});
    void loadUnifiedAiConnection().catch(() => {});
    await Promise.all([loadProducts({ force: true, catalogStatus }), loadProductDeliveryStatus().catch(() => {}), loadAutomation().catch(() => {}), loadAiConfig().catch(() => {}), loadOverviewSignals(epoch)]);
    await Promise.all([loadMessages().catch(() => {}), loadOrders().catch(() => {}), loadQuickReplies().catch(() => {})]);
    $("#authScreen").hidden = true;
    $("#workspace").hidden = false;
    showView(state.view || "home", true);
  }

  function isMobileSidebar() {
    return window.matchMedia("(max-width: 768px)").matches;
  }

  function syncSidebarAccessibility() {
    const sidebar = $("#sidebar");
    const menu = $("#mobileMenu");
    const mobile = isMobileSidebar();
    if (!mobile) sidebar.classList.remove("is-open");
    const open = mobile && sidebar.classList.contains("is-open");
    sidebar.inert = mobile && !open;
    if (mobile) sidebar.setAttribute("aria-hidden", String(!open));
    else sidebar.removeAttribute("aria-hidden");
    menu.setAttribute("aria-expanded", String(open));
  }

  function setSidebarOpen(open, { restoreFocus = true } = {}) {
    const sidebar = $("#sidebar");
    const menu = $("#mobileMenu");
    if (!isMobileSidebar()) {
      sidebar.classList.remove("is-open");
      syncSidebarAccessibility();
      return;
    }
    const shouldRestoreFocus = !open && restoreFocus && sidebar.contains(document.activeElement);
    if (shouldRestoreFocus) menu.focus();
    sidebar.classList.toggle("is-open", Boolean(open));
    syncSidebarAccessibility();
    if (open) {
      window.requestAnimationFrame(() => $("#closeSidebar")?.focus());
    }
  }

  function showView(view, quiet = false) {
    view = normalizeView(view);
    if (!quiet && state.view === "ai-config" && view !== "ai-config" && !confirmDiscardAiChanges("子页")) return false;
    const requestedView = view;
    const panel = $("[data-panel=\"" + requestedView + "\"]");
    if (!panel) {
      view = "home";
    }
    if (state.view === "templates" && view !== "templates") {
      state.templateEditorOpenGeneration += 1;
    }
    if (state.view === "ai-config" && view !== "ai-config" && state.ai.previewBusy) {
      state.ai.previewGeneration += 1;
      state.ai.previewBusy = false;
      setBusy($("#aiRunPreview"), false);
      text("#aiPreviewOutput", "已离开沙盘，本次结果不再显示。");
    }
    state.view = view;
    $$("[data-panel]").forEach((node) => {
      node.hidden = node.dataset.panel !== view;
      node.classList.toggle("is-visible", node.dataset.panel === view);
    });
    const activeDomain = domainView(view);
    $$("#sideNav [data-view], .sidebar-bottom [data-view], .sidebar-logo[data-view]").forEach((node) => {
      node.classList.toggle("is-active", domainView(node.dataset.view) === activeDomain);
    });
    $$(".sub-tab-btn[data-view]").forEach((node) => {
      const active = node.dataset.view === view;
      node.classList.toggle("is-active", active);
      node.setAttribute("aria-selected", String(active));
    });
    if (view === "home") {
      void loadOverviewSignals();
      void loadShopResources({ silent: true });
    }
    if (view === "shops") {
      renderAccountSwitcher();
      loadAccounts().catch((error) => {
        if (error?.status !== 404) showToast(error.message || "店铺列表暂时无法读取", "error");
      });
      void loadShopResources({ silent: true });
    }
    if (view === "goods") {
      loadProducts({ force: true }).catch((error) => showToast(error.message, "error"));
      void loadProductDeliveryStatus();
    }
    if (view === "auto-reply") loadAutomation().catch((error) => showToast(error.message, "error"));
    if (view === "ai-config") loadAiConfig().catch((error) => showToast(error.message || "AI 配置读取失败", "error"));
    if (view === "chat") {
      loadMessages().catch((error) => showToast(error.message, "error"));
    }
    if (view === "orders") loadOrders().catch((error) => showToast(error.message, "error"));
    if (view === "settings" || view === "docs") {
      renderSettings();
      void loadSettingsData();
      if (requestedView === "docs") openDocsHelpModal();
    } else {
      stopDocsPolling();
    }
    if (view === "ops") {
      void loadOpsContext();
    } else if (state.ops?.pollTimer) {
      clearTimeout(state.ops.pollTimer);
      state.ops.pollTimer = null;
    }
    if (view === "templates") {
      loadTemplates().catch((error) => showToast(error.message, "error"));
      loadCards().catch((error) => showToast(error.message || "卡密池加载失败，请稍后重试", "error"));
    }
    if (view === "cards") loadCards({ force: true }).catch((error) => showToast(error.message, "error"));
    if (view === "analytics") loadAnalytics().catch((error) => showToast(error.message, "error"));
    setSidebarOpen(false);
    syncMerchantPolling();
    syncResourcePolling();
  }

  async function refreshState() {
    const context = beginRefreshContext();
    const me = await api("/api/me");
    if (!refreshContextMatches(context)) return false;
    const [config, bot] = await Promise.all([api("/api/config"), accountScopedApi(context, "/api/bot/status")]);
    if (!refreshContextMatches(context)) return false;
    state.me = me;
    state.config = config;
    state.bot = bot;
    ensureCurrentAccount(bot);
    if (!refreshContextMatches(context)) return false;
    const catalogStatus = registerCatalogStatus(bot);
    renderNav();
    renderAccount();
    renderSettings();
    renderOverview();
    if (["home", "shops"].includes(state.view)) {
      await loadShopResources({ silent: true });
      if (!refreshContextMatches(context)) return false;
    }
    void loadVersionInfo().catch(() => {});
    void loadUnifiedAiConnection().catch(() => {});
    await loadProducts({ force: true, catalogStatus });
    await loadProductDeliveryStatus().catch(() => {});
    if (!refreshContextMatches(context)) return false;
    await loadAutomation().catch(() => {});
    if (!refreshContextMatches(context)) return false;
    await loadAiConfig().catch(() => {});
    if (!refreshContextMatches(context)) return false;
    await Promise.all([loadTemplates().catch(() => {}), loadCards({ force: true }).catch(() => {}), loadQuickReplies().catch(() => {})]);
    if (!refreshContextMatches(context)) return false;
    await loadOverviewSignals(context.epoch);
    if (!refreshContextMatches(context)) return false;
    if (state.view === "chat" || state.view === "home") {
      await loadMessages(state.selectedChatId, { preserveScroll: true });
      if (!refreshContextMatches(context)) return false;
    }
    if (state.view === "orders") {
      await loadOrders();
      if (!refreshContextMatches(context)) return false;
    }
    if (state.view === "settings") {
      renderSettings();
    }
    if (state.view === "ops") {
      void loadOpsContext();
    }
    syncMerchantPolling();
    syncResourcePolling();
    return true;
  }

  function reflectCookieError(error, fromSavedCheck = false) {
    const code = error?.code;
    if (!code || !COOKIE_BLOCKING_CODES.has(code)) return;
    // When there was no verified account to preserve, reflect the failed
    // check immediately.  A failed replacement of an existing account is
    // intentionally kept local to the form; the backend retains that account
    // as verified and the next refresh remains authoritative.
    const hadVerifiedAccount = state.bot?.sync_status === "verified" && state.bot?.connected !== false;
    if (hadVerifiedAccount && !fromSavedCheck) return;
    state.bot = Object.assign({}, state.bot || {}, {
      cookies_set: true,
      connected: false,
      sync_status: code,
      cookie_status: {
        code,
        label: COOKIE_STATUS_LABELS[code] || "需要处理",
        message: cookieErrorMessage(error),
        action: COOKIE_STATUS_ACTIONS[code] || "处理后重新检测",
      },
    });
    renderAccount();
    renderShopStatus();
  }

  async function syncShop(event) {
    const button = event?.currentTarget || $("#refreshProducts");
    const context = captureAccountContext();
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/shop/sync", {
        method: "POST",
        headers: { Prefer: "respond-async" },
      });
      if (!accountContextMatches(context)) return;
      let completed = result;
      if (result?.job?.id) {
        showToast("正在后台整理店铺商品", "warning");
        const deadline = Date.now() + 90_000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => window.setTimeout(resolve, 900));
          if (!accountContextMatches(context)) return;
          const polled = await accountScopedApi(
            context,
            "/api/bot/jobs/" + encodeURIComponent(result.job.id),
          );
          if (!accountContextMatches(context)) return;
          if (polled?.result) {
            completed = polled.result;
            break;
          }
          const status = polled?.job?.status || "";
          if (status === "dead_letter") {
            throw new ApiError("店铺同步失败，请稍后重试", 503, polled.job.error_code || "sync_error");
          }
        }
        if (!completed?.connected) {
          throw new ApiError("店铺整理仍在进行，请稍后刷新查看", 202, "sync_pending");
        }
      }
      await refreshState();
      if (!accountContextMatches(context)) return;
      showToast("已同步 " + Number(completed.product_count || state.products.length) + " 个商品");
    } catch (error) {
      if (!accountContextMatches(context)) return;
      showToast(cookieErrorMessage(error), "error");
      reflectCookieError(error, true);
    } finally {
      setBusy(button, false);
    }
  }

  async function sendManualReply(event) {
    event.preventDefault();
    if (state.manualReply.submitting || state.manualReply.uploading || state.manualReply.cleaning || manualReplyOperationInFlight()) return;
    const input = $("#manualReplyInput");
    const content = input.value.trim();
    const hasAttachment = state.manualReply.attachments.length > 0;
    if (!content && !hasAttachment) {
      formMessage("#replyMessage", "请输入回复内容或选择图片");
      return;
    }
    if (!state.selectedChatId) {
      formMessage("#replyMessage", "请先选择一个对话");
      return;
    }
    const selected = state.conversations.find((item) => String(item.chat_id) === String(state.selectedChatId));
    if (!conversationTakeover(selected)) {
      formMessage("#replyMessage", "请先人工接管当前对话再发送");
      return;
    }
    const chatId = String(state.selectedChatId);
    const epoch = state.accountEpoch;
    const accountKey = state.activeAccountKey;
    const generation = state.manualReply.generation;
    const mediaKey = manualReplyAttachmentSnapshot();
    const existingRequest = state.manualReply.request;
    const requestId = existingRequest?.generation === generation
      && existingRequest?.chatId === chatId
      && existingRequest?.content === content
      && existingRequest?.mediaKey === mediaKey
      ? existingRequest.id
      : newClientRequestId();
    state.manualReply.request = { id: requestId, chatId, content, mediaKey, generation };
    let finishOperation;
    const operation = {
      cancelled: false,
      promise: new Promise((resolve) => { finishOperation = resolve; }),
    };
    state.manualReply.operation = operation;
    const button = event.submitter || $("#manualReplyForm button[type=submit]");
    state.manualReply.submitting = true;
    setBusy(button, true);
    renderChat();
    try {
      const media = [];
      for (let index = 0; index < state.manualReply.attachments.length; index += 1) {
        if (operation.cancelled) return;
        const attachment = state.manualReply.attachments[index];
        if (!attachment.media) {
          state.manualReply.uploading = true;
          state.manualReply.uploadingIndex = index;
          renderChat();
          const uploaded = await uploadManualReplyFile(attachment.file, chatId);
          attachment.media = uploaded;
          if (operation.cancelled) return;
          if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
            attachment.media = null;
            void deleteManualReplyUploadedMedia(uploaded, accountKey);
            return;
          }
        }
        media.push(attachment.media);
      }
      state.manualReply.uploading = false;
      state.manualReply.uploadingIndex = -1;
      renderChat();
      if (operation.cancelled) return;
      if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
      const result = await api("/api/bot/messages/reply", {
        method: "POST",
        headers: { "Idempotency-Key": requestId },
        body: JSON.stringify({ content, chat_id: chatId, media }),
        timeoutMs: MANUAL_REPLY_POST_TIMEOUT_MS,
        suppressSessionReset: true,
      });
      if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
      const reply = Object.assign({}, result?.message || {}, result?.reply || {});
      const message = result?.message || Object.assign({
        role: "assistant_manual",
        content,
        content_type: media.length ? (content ? "rich" : "image") : "text",
        media,
        time: new Date().toISOString(),
        chat_id: chatId,
        reply_id: requestId,
      }, reply, { delivery_status: reply.status || "queued" });
      mergeManualReplyMessage(message);
      if (input.value.trim() === content) input.value = "";
      if (state.manualReply.request?.id === requestId) state.manualReply.request = null;
      releaseManualReplyAttachments({ cleanupUploaded: false });
      const file = $("#manualReplyFile");
      if (file) file.value = "";
      formMessage("#replyMessage", manualReplyFeedbackText(reply), reply.status === "acknowledged");
      renderChat();
      if (ACTIVE_MANUAL_REPLY_STATUSES.has(String(reply.status || "queued"))) {
        void pollManualReply(requestId, chatId, epoch, accountKey, generation);
      } else {
        void refreshManualReplyMessages(chatId, epoch, accountKey, generation);
      }
    } catch (error) {
      if (manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
        formMessage("#replyMessage", error.message || "回复发送失败，请稍后重试");
      }
    } finally {
      try {
        if (manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
          state.manualReply.submitting = false;
          state.manualReply.uploading = false;
          state.manualReply.uploadingIndex = -1;
          setBusy(button, false);
          renderChat();
        }
      } finally {
        if (state.manualReply.operation === operation) state.manualReply.operation = null;
        finishOperation();
      }
    }
  }

  function mergeManualReplyMessage(message) {
    if (!message || typeof message !== "object") return;
    const replyId = String(message.reply_id || "");
    const outboxId = Number(message.outbox_id || 0);
    const index = state.messages.findIndex((item) => (
      (replyId && String(item.reply_id || "") === replyId)
      || (outboxId && Number(item.outbox_id || 0) === outboxId)
    ));
    if (index >= 0) state.messages[index] = Object.assign({}, state.messages[index], message);
    else state.messages.push(message);
  }

  const ACTIVE_MANUAL_REPLY_STATUSES = new Set(["queued", "sending", "retry"]);

  function manualReplyPartsSignature(value) {
    return JSON.stringify(normaliseManualReplyParts(value));
  }

  function pollVisibleManualReplies() {
    const chatId = String(state.selectedChatId || "");
    const epoch = state.accountEpoch;
    const accountKey = state.activeAccountKey;
    const generation = state.manualReply.generation;
    state.messages.forEach((message) => {
      const replyId = String(message.reply_id || "");
      const status = String(message.delivery_status || message.status || "");
      if (replyId && ACTIVE_MANUAL_REPLY_STATUSES.has(status)) {
        void pollManualReply(replyId, chatId, epoch, accountKey, generation);
      }
    });
  }

  function markManualReplyStatusUnknown(replyId) {
    const message = state.messages.find((item) => String(item.reply_id || "") === String(replyId || ""));
    if (!message) return;
    message.delivery_status = "unknown";
    message.status = "unknown";
    const currentPart = Number(message.current_part);
    if (Number.isInteger(currentPart) && Array.isArray(message.parts)) {
      message.parts = message.parts.map((part) => Number(part?.index) === currentPart && part?.status !== "acknowledged"
        ? Object.assign({}, part, { status: "unknown" })
        : part);
    }
    renderChat();
  }

  async function refreshManualReplyMessages(chatId, epoch, accountKey, generation) {
    if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
    try {
      await loadMessages(chatId, { preserveScroll: true });
    } catch (error) {
      // The next status poll or the regular inbox refresh retries this view.
    }
  }

  async function pollManualReply(replyId, chatId, epoch, accountKey, generation) {
    const pollKey = generation + ":" + epoch + ":" + chatId + ":" + replyId;
    if (state.manualReply.polling.has(pollKey)) return;
    state.manualReply.polling.add(pollKey);
    try {
      for (let attempt = 0; attempt < 24; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
        let result;
        try {
          result = await api("/api/bot/messages/reply/" + encodeURIComponent(replyId));
        } catch (error) {
          if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
          if (error?.status === 404) {
            markManualReplyStatusUnknown(replyId);
            formMessage("#replyMessage", "回复状态暂时无法确认，请刷新查看");
            return;
          }
          continue;
        }
        if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
        const reply = result?.reply;
        if (!reply) continue;
        const message = state.messages.find((item) => String(item.reply_id || "") === replyId);
        const previousParts = manualReplyPartsSignature(message?.parts);
        const previousCurrentPart = String(message?.current_part ?? "");
        if (message) {
          Object.assign(message, reply, {
            delivery_status: reply.status,
            status: reply.status,
          });
          renderChat();
        }
        const partsChanged = previousParts !== manualReplyPartsSignature(reply.parts)
          || previousCurrentPart !== String(reply.current_part ?? "");
        const active = ACTIVE_MANUAL_REPLY_STATUSES.has(String(reply.status || ""));
        if (partsChanged || !active) {
          await refreshManualReplyMessages(chatId, epoch, accountKey, generation);
          if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
        }
        formMessage("#replyMessage", manualReplyFeedbackText(reply), reply.status === "acknowledged");
        if (!active) return;
      }
      if (manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
        markManualReplyStatusUnknown(replyId);
        formMessage("#replyMessage", "回复状态暂时无法确认，请刷新查看");
      }
    } finally {
      state.manualReply.polling.delete(pollKey);
    }
  }

  function resetReplyRuleForm({ focus = false } = {}) {
    const form = $("#replyRuleForm");
    if (!form) return;
    form.reset();
    $("#replyRuleEnabled").checked = true;
    $("#replyRuleItemId").value = "";
    state.automationEditor = { type: "", index: -1 };
    text("#saveReplyRuleButton span", "保存规则");
    $("#cancelReplyRuleEdit").hidden = true;
    formMessage("#replyRuleMessage", "");
    if (focus) $("#replyRuleName").focus();
  }

  function editReplyRule(index) {
    const rules = Array.isArray(state.automation?.rules) ? state.automation.rules : [];
    const rule = rules[index];
    if (!rule) return;
    state.automationEditor = { type: "rule", index };
    $("#replyRuleName").value = rule.name || ("规则 " + (index + 1));
    $("#replyRuleItemId").value = String(rule.item_id || "");
    $("#replyRuleKeywords").value = (rule.keywords || []).join(",");
    $("#replyRuleReply").value = rule.reply || "";
    $("#replyRuleEnabled").checked = rule.enabled !== false;
    text("#saveReplyRuleButton span", "保存修改");
    $("#cancelReplyRuleEdit").hidden = false;
    formMessage("#replyRuleMessage", "");
    $("#replyRuleForm").scrollIntoView({ behavior: "smooth", block: "center" });
    $("#replyRuleName").focus();
  }

  function collectReplyRuleForm() {
    const name = $("#replyRuleName").value.trim();
    const itemId = $("#replyRuleItemId").value.trim();
    const keywords = $("#replyRuleKeywords").value.split(",").map((item) => item.trim()).filter(Boolean);
    const reply = $("#replyRuleReply").value.trim();
    if (!name) throw new ApiError("请填写规则名称");
    if (itemId && !/^\d+$/.test(itemId)) throw new ApiError("关联商品 ID 只能填写数字");
    if (!keywords.length) throw new ApiError("请至少填写一个关键词");
    if (keywords.length > 10) throw new ApiError("每条规则最多填写 10 个关键词");
    if (!reply) throw new ApiError("请填写自动回复话术");
    return { name, item_id: itemId, enabled: $("#replyRuleEnabled").checked, keywords, reply };
  }

  async function saveReplyRule(event) {
    event.preventDefault();
    const button = $("#saveReplyRuleButton");
    const rules = Array.isArray(state.automation?.rules) ? state.automation.rules.slice() : [];
    const editingIndex = state.automationEditor?.type === "rule" ? Number(state.automationEditor.index) : -1;
    if (editingIndex < 0 && rules.length >= 50) {
      formMessage("#replyRuleMessage", "最多设置 50 条回复规则");
      return;
    }
    let rule;
    try {
      rule = collectReplyRuleForm();
    } catch (error) {
      formMessage("#replyRuleMessage", error.message);
      return;
    }
    if (editingIndex >= 0 && rules[editingIndex]) rules[editingIndex] = rule;
    else rules.push(rule);
    const mutation = beginAutomationMutation("rules", "#replyRuleMessage");
    if (!mutation) return;
    const context = mutation.context;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/automation", { method: "PUT", body: JSON.stringify({ rules }) });
      if (!accountContextMatches(context)) return;
      state.automation = result.automation || Object.assign({}, state.automation, { rules });
      resetReplyRuleForm();
      renderAutomation();
      showToast(editingIndex >= 0 ? "规则已保存" : "规则新增成功");
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#replyRuleMessage", error.message || "规则保存失败");
    } finally {
      if (state.automationMutationOwner === mutation) {
        setBusy(button, false);
        endAutomationMutation(mutation);
      }
    }
  }

  function confirmRemoveReplyRule(index) {
    const rules = Array.isArray(state.automation?.rules) ? state.automation.rules : [];
    const rule = rules[index];
    if (!rule) return;
    const context = captureAccountContext();
    text("#confirmTitle", "删除回复规则");
    text("#confirmMessage", "删除“" + (rule.name || ("规则 " + (index + 1))) + "”后将立即停止匹配，是否继续？");
    text("#confirmAction", "确认删除");
    state.confirmAction = async () => {
      if (!accountContextMatches(context)) return;
      const mutation = beginAutomationMutation("rules", "#replyRuleMessage");
      if (!mutation) return;
      try {
        const nextRules = rules.filter((_item, itemIndex) => itemIndex !== index);
        const result = await accountScopedApi(context, "/api/automation", { method: "PUT", body: JSON.stringify({ rules: nextRules }) });
        if (!accountContextMatches(context)) return;
        state.automation = result.automation || Object.assign({}, state.automation, { rules: nextRules });
        resetReplyRuleForm();
        renderAutomation();
        showToast("规则已删除");
      } finally {
        endAutomationMutation(mutation);
      }
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function invalidateBatchDeliveryPreview() {
    state.batchDelivery.generation = Number(state.batchDelivery.generation || 0) + 1;
    state.batchDelivery.previewToken = "";
    state.batchDelivery.preview = null;
    const preview = $("#batchDeliveryPreview");
    if (preview) preview.hidden = true;
    const commit = $("#batchDeliveryCommit");
    if (commit) commit.disabled = true;
    formMessage("#batchDeliveryMessage", "");
  }

  function updateBatchDeliverySelection() {
    const items = $$('[data-batch-item]');
    const selected = items.filter((item) => item.checked).length;
    text("#batchDeliverySelected", "已选 " + selected + " 个");
    const all = $("#batchDeliveryAll");
    if (all) {
      all.checked = Boolean(items.length) && selected === items.length;
      all.indeterminate = selected > 0 && selected < items.length;
    }
  }

  function renderBatchDeliveryMode() {
    const enabled = state.batchDelivery.enabled !== false;
    $$('[data-batch-mode]').forEach((button) => {
      button.setAttribute("aria-pressed", String((button.dataset.batchMode === "set") === enabled));
    });
    const materialField = $("#batchDeliveryMaterialField");
    if (materialField) materialField.hidden = !enabled;
    text("#batchDeliveryTitle", enabled ? "批量设置资料" : "批量暂停资料");
  }

  function openBatchDelivery(selectedItemId = "") {
    if (!state.products.length) {
      showToast("当前店铺还没有可设置的商品", "warning");
      return;
    }
    const selectedId = String(selectedItemId || "").trim();
    const deliveryById = new Map((state.automation?.deliveries || []).map((item) => [String(item.item_id), item]));
    const selectedDelivery = deliveryById.get(selectedId);
    state.batchDelivery = {
      enabled: selectedDelivery?.enabled !== false,
      previewToken: "",
      preview: null,
      generation: Number(state.batchDelivery?.generation || 0) + 1,
    };
    $("#batchDeliveryProducts").innerHTML = state.products.map((product) => {
      const itemId = String(product.id || "");
      const delivery = deliveryById.get(itemId);
      const status = delivery ? (delivery.enabled === false ? "资料已暂停" : "资料已开启") : "未设置";
      return '<label class="batch-product-row"><input type="checkbox" data-batch-item value="' + esc(itemId) + '" ' + (itemId === selectedId ? "checked" : "") + '><span>' + esc(product.title || "未命名商品") + '</span><small>' + status + "</small></label>";
    }).join("");
    $("#batchDeliveryAll").checked = Boolean(selectedId) && state.products.length === 1;
    $("#batchDeliveryAll").indeterminate = Boolean(selectedId) && state.products.length > 1;
    $("#batchDeliveryMaterial").value = selectedDelivery?.material || "";
    renderBatchDeliveryMode();
    updateBatchDeliverySelection();
    invalidateBatchDeliveryPreview();
    const dialog = $("#batchDeliveryDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  async function toggleProductDelivery(button) {
    const itemId = String(button?.dataset.deliveryToggle || "").trim();
    const deliveries = Array.isArray(state.automation?.deliveries) ? state.automation.deliveries : [];
    const current = deliveries.find((item) => String(item.item_id) === itemId);
    if (!current) return;
    const mutation = beginAutomationMutation("deliveries");
    if (!mutation) return;
    const nextDeliveries = deliveries.map((item) => String(item.item_id) === itemId ? { ...item, enabled: item.enabled === false } : item);
    const context = mutation.context;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/automation", { method: "PUT", body: JSON.stringify({ deliveries: nextDeliveries }) });
      if (!accountContextMatches(context)) return;
      state.automation = result.automation || Object.assign({}, state.automation, { deliveries: nextDeliveries });
      renderAutomation();
      renderProducts();
      showToast(current.enabled === false ? "商品资料已恢复" : "商品资料已暂停");
    } catch (error) {
      if (accountContextMatches(context)) showToast(error.message || "商品资料状态修改失败", "error");
    } finally {
      if (state.automationMutationOwner === mutation) {
        setBusy(button, false);
        endAutomationMutation(mutation);
      }
    }
  }

  function collectBatchDelivery() {
    return {
      item_ids: $$('[data-batch-item]').filter((item) => item.checked).map((item) => item.value),
      enabled: state.batchDelivery.enabled !== false,
      material: state.batchDelivery.enabled === false ? "" : $("#batchDeliveryMaterial").value.trim(),
    };
  }

  function validateBatchDelivery(payload) {
    if (!payload.item_ids.length) return "请至少选择一个商品";
    if (payload.enabled && !payload.material) return "请填写统一发送的资料";
    return "";
  }

  async function previewBatchDelivery() {
    const payload = collectBatchDelivery();
    const validationError = validateBatchDelivery(payload);
    if (validationError) {
      formMessage("#batchDeliveryMessage", validationError);
      return;
    }
    const button = $("#batchDeliveryCheck");
    const context = captureAccountContext();
    setBusy(button, true);
    invalidateBatchDeliveryPreview();
    const generation = state.batchDelivery.generation;
    try {
      const result = await accountScopedApi(context, "/api/bot/products/batch/preview", { method: "POST", body: JSON.stringify(payload) });
      if (!accountContextMatches(context) || generation !== state.batchDelivery.generation) return;
      const preview = result.preview || result;
      const token = String(preview.preview_token || preview.token || "");
      if (!token) throw new ApiError("检查结果无效，请稍后重试");
      state.batchDelivery.previewToken = token;
      state.batchDelivery.preview = preview;
      const changes = Number(preview.change_count || 0);
      const unchanged = Number(preview.unchanged_count || 0);
      text("#batchDeliveryPreviewTitle", changes ? "检查完成，可以保存" : "当前设置无需修改");
      text("#batchDeliveryPreviewMessage", "将修改 " + changes + " 个商品" + (unchanged ? "，" + unchanged + " 个保持不变。" : "。"));
      $("#batchDeliveryPreview").hidden = false;
      $("#batchDeliveryCommit").disabled = changes < 1;
      formMessage("#batchDeliveryMessage", "");
    } catch (error) {
      if (accountContextMatches(context) && generation === state.batchDelivery.generation) {
        formMessage("#batchDeliveryMessage", error.message || "检查失败，请稍后重试");
      }
    } finally {
      setBusy(button, false);
    }
  }

  async function commitBatchDelivery(event) {
    event.preventDefault();
    const payload = collectBatchDelivery();
    const validationError = validateBatchDelivery(payload);
    if (validationError || !state.batchDelivery.previewToken) {
      formMessage("#batchDeliveryMessage", validationError || "请先检查本次修改");
      return;
    }
    payload.preview_token = state.batchDelivery.previewToken;
    const mutation = beginAutomationMutation("deliveries", "#batchDeliveryMessage");
    if (!mutation) return;
    const button = $("#batchDeliveryCommit");
    const context = mutation.context;
    const generation = state.batchDelivery.generation;
    setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/bot/products/batch/commit", { method: "POST", body: JSON.stringify(payload) });
      if (!accountContextMatches(context) || generation !== state.batchDelivery.generation) return;
      const previousById = new Map((state.automation?.deliveries || []).map((item) => [String(item.item_id), item]));
      const selectedIds = new Set(payload.item_ids.map(String));
      const serverAutomation = result.automation || state.automation;
      const serverDeliveries = Array.isArray(serverAutomation?.deliveries) ? serverAutomation.deliveries : [];
      const deliveries = serverDeliveries.map((item) => {
        const itemId = String(item.item_id || "");
        const previous = previousById.get(itemId);
        return Object.assign({}, item, {
          material: selectedIds.has(itemId) && payload.enabled ? payload.material : String(previous?.material || ""),
        });
      });
      state.automation = Object.assign({}, serverAutomation, { deliveries });
      renderAutomation();
      renderProducts();
      closeDialog("batchDeliveryDialog");
      state.batchDelivery = { enabled: true, previewToken: "", preview: null, generation: Number(state.batchDelivery?.generation || 0) + 1 };
      showToast(payload.enabled ? "批量资料已保存" : "所选商品已暂停自动发资料");
    } catch (error) {
      if (accountContextMatches(context) && generation === state.batchDelivery.generation) {
        invalidateBatchDeliveryPreview();
        formMessage("#batchDeliveryMessage", error.message || "保存失败，请重新检查");
      }
    } finally {
      if (state.automationMutationOwner === mutation) {
        setBusy(button, false);
        endAutomationMutation(mutation);
        button.disabled = !state.batchDelivery.previewToken;
      }
    }
  }

  async function saveAutomation(options = {}) {
    const button = options.button || $("#saveAutomationButton");
    const payload = collectAutomation(options.enabled);
    const mutation = beginAutomationMutation("settings");
    if (!mutation) return false;
    const context = mutation.context;
    if (options.manageBusy !== false) setBusy(button, true);
    try {
      const result = await accountScopedApi(context, "/api/automation", { method: "PUT", body: JSON.stringify(payload) });
      if (!accountContextMatches(context)) return false;
      state.automation = result.automation || state.automation;
      const connected = shopStateView(state.bot || {}).connection === "connected";
      const rulesConfigured = (state.automation.rules || []).some((rule) => rule.enabled !== false && String(rule.reply || "").trim());
      const defaultsConfigured = [payload.first_reply, payload.fallback_reply].some((item) => String(item || "").trim());
      const deliveryConfigured = (state.automation.deliveries || []).some((item) => item.enabled !== false && String(item.material || "").trim());
      const rulesRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules");
      const aiRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules_ai");
      let message = "店铺配置已保存";
      if (payload.enabled && connected && !rulesRunning && !aiRunning && (rulesConfigured || defaultsConfigured || deliveryConfigured)) {
        await accountScopedApi(context, "/api/bot/start", { method: "POST", body: JSON.stringify({ mode: "rules" }) });
        if (!accountContextMatches(context)) return false;
        await refreshState();
        if (!accountContextMatches(context)) return false;
        message = "店铺配置已保存，自动回复已开启";
      } else if (!payload.enabled) {
        await refreshState();
        if (!accountContextMatches(context)) return false;
        message = "店铺配置已保存，自动回复已关闭";
      }
      renderAutomation();
      formMessage("#automationMessage", message, true);
      showToast(message);
      return true;
    } catch (error) {
      if (accountContextMatches(context)) formMessage("#automationMessage", error.message);
      return false;
    } finally {
      if (state.automationMutationOwner === mutation) {
        if (options.manageBusy !== false) setBusy(button, false);
        endAutomationMutation(mutation);
      }
    }
  }

  function confirmStop() {
    const context = captureAccountContext();
    text("#confirmTitle", "暂停 AI 客服");
    text("#confirmMessage", "暂停后，AI 客服不会继续回复新的买家消息。");
    text("#confirmAction", "确认暂停");
    state.confirmAction = async () => {
      if (!accountContextMatches(context)) return;
      const mutation = beginAutomationMutation("runtime");
      if (!mutation) return;
      try {
        await accountScopedApi(context, "/api/bot/stop", { method: "POST" });
        if (!accountContextMatches(context)) return;
        await refreshState();
        if (!accountContextMatches(context)) return;
        showToast("AI 客服已暂停");
      } finally {
        if (state.automationMutationOwner === mutation) endAutomationMutation(mutation);
      }
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  async function startBot() {
    if (!aiConnectionVerified()) {
      showToast("请先在 AI 客服设置页测试并保存模型连接", "warning");
      showView("ai-config", true);
      return;
    }
    if (!aiStoreHasContent()) {
      showToast("请先填写并保存店铺与客服说明", "warning");
      showView("ai-config", true);
      return;
    }
    const button = $("#chatAiStart");
    const mutation = beginAutomationMutation("runtime");
    if (!mutation) return;
    const context = mutation.context;
    setBusy(button, true);
    try {
      await accountScopedApi(context, "/api/bot/start", { method: "POST", body: JSON.stringify({ mode: "rules_ai" }) });
      if (!accountContextMatches(context)) return;
      await refreshState();
      if (!accountContextMatches(context)) return;
      showToast("AI 客服已开启");
    } catch (error) {
      if (accountContextMatches(context)) showToast(error.message, "error");
    } finally {
      if (state.automationMutationOwner === mutation) {
        setBusy(button, false);
        endAutomationMutation(mutation);
      }
    }
  }

  function openDialog(id) {
    const dialog = $("#" + id);
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(id) {
    const dialog = $("#" + id);
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function bindEvents() {
    document.addEventListener("error", (event) => handleImageFallback(event.target), true);
    $("#loginTab").addEventListener("click", () => setAuthMode("login"));
    $("#registerTab").addEventListener("click", () => setAuthMode("register"));
    $("#bootstrapTab").addEventListener("click", () => setAuthMode("bootstrap"));
    $("#authForm").addEventListener("submit", submitAuth);
    $("#passwordChangeForm").addEventListener("submit", changeCurrentPassword);
    $("#platformSettingsForm").addEventListener("submit", savePlatformSettings);
    $("#resourceSettingsForm")?.addEventListener("submit", saveResourceSettings);
    $("#reloadResourceSettings")?.addEventListener("click", () => void reloadResourceSettings());
    ["resourceMaxShops", "resourceMaxWorkers", "resourceMemoryMiB"].forEach((id) => {
      const el = $("#" + id);
      if (el) {
        el.addEventListener("input", onResourceInputChange);
        el.addEventListener("change", onResourceInputChange);
      }
    });
    $("#refreshAdminUsers").addEventListener("click", () => loadDocsData({ force: true }));
    $("#refreshAuditButton").addEventListener("click", () => loadDocsData({ force: true }));
    $("#versionBadgeButton")?.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleVersionBadgePopover();
    });
    $("#versionBadgeClose")?.addEventListener("click", (e) => {
      e.stopPropagation();
      closeVersionBadgePopover({ restoreFocus: true });
    });
    $("#versionBadgeRefresh")?.addEventListener("click", (e) => {
      e.stopPropagation();
      void checkPlatformUpdate();
    });
    $("#versionBadgeUpdate")?.addEventListener("click", openPlatformUpdate);
    $("#versionBadgeOperation")?.addEventListener("click", openPlatformUpdate);
    $("#versionPopoverRollbackBtn")?.addEventListener("click", () => {
      if (!isPlatformAdmin() || !updateActionAllowed("rollback")) return;
      const selected = $('input[name="versionRollbackChoice"]:checked', $("#versionRollbackList"))?.value;
      if (!selected) return;
      closeVersionBadgePopover();
      const rollbackSelect = $("#updateRollbackSelect");
      if (rollbackSelect) {
        rollbackSelect.value = selected;
      }
      openPlatformUpdate();
      if (rollbackSelect) {
        rollbackSelect.value = selected;
      }
      void preparePlatformUpdate("rollback");
    });
    $("#updateDownloadButton")?.addEventListener("click", () => void preparePlatformUpdate("apply"));
    $("#updateRollbackButton")?.addEventListener("click", () => void preparePlatformUpdate("rollback"));
    $("#updatePasswordForm")?.addEventListener("submit", confirmPlatformUpdate);
    $("#platformUpdateDialog")?.addEventListener("keydown", trapPlatformUpdateFocus);
    $("#platformUpdateDialog")?.addEventListener("close", closePlatformUpdate);
    $("#platformUpdateDialog")?.addEventListener("cancel", closePlatformUpdate);
    $("#updateReloadButton")?.addEventListener("click", () => {
      if (isPlatformAdmin() && ["succeeded", "rolled_back"].includes(updatePhase(state.platformUpdate?.operation))) window.location.reload();
    });
    $("#updateRetryButton")?.addEventListener("click", () => {
      const session = ensurePlatformUpdateSession();
      if (!session || !isPlatformAdmin() || updateOperationActive(session)) return;
      session.operation = null;
      session.stage = null;
      session.error = "";
      renderPlatformUpdate();
      void loadVersionInfo({ force: true });
    });
    document.addEventListener("visibilitychange", () => {
      const session = ensurePlatformUpdateSession();
      if (!session) return;
      window.clearTimeout(session.cacheTimer);
      if (document.hidden) return;
      if (!session.cacheAt || Date.now() - session.cacheAt >= VERSION_CACHE_REFRESH_MS) void loadVersionInfo({ force: true });
      else scheduleVersionCacheRefresh(session);
    });
    document.addEventListener("click", (event) => {
      const container = event.target.closest(".version-badge-container");
      if (!container) closeVersionBadgePopover();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeVersionBadgePopover({ restoreFocus: true });
      }
    });

    $("#settingsDocsBtn")?.addEventListener("click", openDocsHelpModal);
    $$('[data-settings-tab]').forEach((button) => button.addEventListener("click", () => setSettingsTab(button.dataset.settingsTab)));
    $$('[data-settings-go]').forEach((button) => button.addEventListener("click", () => {
      showView("settings");
      setSettingsTab(button.dataset.settingsGo);
    }));

    // Ops Agent events
    $("#opsPromptForm").addEventListener("submit", sendOpsChat);
    $("#opsPromptInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void sendOpsChat();
      }
    });
    $("#opsPromptInput").addEventListener("input", (e) => resizeOpsPrompt(e.target));
    $("#opsStopBtn").addEventListener("click", cancelOpsRun);
    $("#opsNewSessionBtn").addEventListener("click", createNewOpsSession);
    document.addEventListener("click", (e) => {
      const chip = e.target.closest(".ops-chip-mini");
      if (chip && chip.dataset.fill) {
        const input = $("#opsPromptInput");
        if (input) {
          input.value = chip.dataset.fill;
          input.focus();
          resizeOpsPrompt(input);
        }
      }
      const retryBtn = e.target.closest(".ops-retry-btn");
      if (retryBtn && retryBtn.dataset.retryRun) {
        void retryOpsRun(retryBtn.dataset.retryRun);
      }
    });

    $$('[data-docs-tab]').forEach((button) => button.addEventListener("click", () => setDocsTab(button.dataset.docsTab)));
    $$('[data-docs-go]').forEach((button) => button.addEventListener("click", () => {
      if (["home", "chat", "goods", "orders", "shops"].includes(button.dataset.docsGo)) showView(button.dataset.docsGo);
    }));
    $("#adminUsersBody").addEventListener("click", (event) => {
      const button = event.target.closest("[data-admin-user-action]");
      if (button) void handleAdminUserAction(button);
    });
    $("#logoutButton").addEventListener("click", logout);
    $("#refreshButton").addEventListener("click", () => refreshState().then(() => showToast("已刷新")).catch((error) => showToast(error.message, "error")));
    $("#mobileMenu").addEventListener("click", () => setSidebarOpen(true));
    $("#closeSidebar").addEventListener("click", () => setSidebarOpen(false));
    $(".sidebar-scrim").addEventListener("click", () => setSidebarOpen(false));
    window.addEventListener("resize", syncSidebarAccessibility);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("#sidebar").classList.contains("is-open")) {
        setSidebarOpen(false);
      }
    });
    $("#addShopAccountPanelForm").addEventListener("submit", createShopAccount);
    $("#renameShopAccountForm").addEventListener("submit", saveShopAccountName);
    $("#refreshShopAccounts")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      setBusy(button, true);
      try {
        await loadAccounts();
        await refreshState();
        showToast("店铺列表已刷新");
      } catch (error) {
        showToast(error.message || "店铺列表刷新失败", "error");
      } finally {
        setBusy(button, false);
      }
    });
    $("#shopAccountsPageSize").addEventListener("change", (event) => {
      state.shopAccountsPageSize = Math.max(1, Number(event.currentTarget.value || 5));
      state.shopAccountsPage = 1;
      renderAccountSwitcher();
    });
    $("#headerLogoutButton").addEventListener("click", logout);
    $("#xianyuConnectButton").addEventListener("click", startXianyuLogin);
    $("#refreshXianyuLogin").addEventListener("click", () => {
      const login = state.qrLogin;
      if (login.operation || login.polling || qrRemaining(login.retryAt)) return;
      if (login.retryAction !== "start" && login.expiresAt && !qrRemaining(login.expiresAt)) renderQrLogin();
      if (login.retryAction === "complete" && login.loginId) void completeQrLogin(login.generation);
      else if (login.retryAction === "poll" && login.loginId) void pollQrLogin(login.generation);
      else void startXianyuLogin();
    });
    $("#closeXianyuLogin").addEventListener("click", () => { void cancelQrLogin(true, true); });
    $("#xianyuLoginDialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      void cancelQrLogin(true, true);
    });
    $("#xianyuQrImage").addEventListener("load", (event) => {
      if (!state.qrLogin.loginId || !state.qrLogin.objectUrl) { event.currentTarget.hidden = true; return; }
      renderQrLogin();
    });
    $("#xianyuQrImage").addEventListener("error", () => {
      if (!state.qrLogin.loginId) return;
      clearQrLoginPoll();
      state.qrLogin.status = "error";
      state.qrLogin.message = "二维码加载失败，请刷新后重试";
      renderQrLogin();
    });
    $("#refreshProducts").addEventListener("click", syncShop);
    $("#productViewCards")?.addEventListener("click", () => setGoodsViewMode("cards"));
    $("#productViewList")?.addEventListener("click", () => setGoodsViewMode("list"));
    $("#productSearch")?.addEventListener("input", (e) => {
      state.goodsSearch = e.target.value;
      state.goodsPage = 1;
      renderProducts();
    });
    $("#productStatusFilter")?.addEventListener("change", (e) => {
      state.goodsStatusFilter = e.target.value;
      state.goodsPage = 1;
      renderProducts();
    });
    $("#productPageSize")?.addEventListener("change", (e) => {
      setGoodsPageSize(e.target.value);
    });
    $("#productPrevPage")?.addEventListener("click", () => {
      if (state.goodsPage > 1) {
        state.goodsPage -= 1;
        renderProducts();
      }
    });
    $("#productNextPage")?.addEventListener("click", () => {
      state.goodsPage += 1;
      renderProducts();
    });
    $("#checkCookieButton").addEventListener("click", syncShop);
    $("#replyRuleForm").addEventListener("submit", saveReplyRule);
    $("#cancelReplyRuleEdit").addEventListener("click", () => resetReplyRuleForm({ focus: true }));
    $$('[data-open-batch-delivery]').forEach((button) => button.addEventListener("click", () => openBatchDelivery()));
    $("#batchDeliveryAll").addEventListener("change", (event) => {
      $$('[data-batch-item]').forEach((item) => { item.checked = event.currentTarget.checked; });
      updateBatchDeliverySelection();
      invalidateBatchDeliveryPreview();
    });
    $("#batchDeliveryProducts").addEventListener("change", (event) => {
      if (!event.target.matches('[data-batch-item]')) return;
      updateBatchDeliverySelection();
      invalidateBatchDeliveryPreview();
    });
    $("#batchDeliveryMaterial").addEventListener("input", invalidateBatchDeliveryPreview);
    $$('[data-batch-mode]').forEach((button) => button.addEventListener("click", () => {
      state.batchDelivery.enabled = button.dataset.batchMode === "set";
      renderBatchDeliveryMode();
      invalidateBatchDeliveryPreview();
    }));
    $("#batchDeliveryCheck").addEventListener("click", previewBatchDelivery);
    $("#batchDeliveryForm").addEventListener("submit", commitBatchDelivery);
    $("#saveAutomationButton").addEventListener("click", saveAutomation);
    $("#aiConnectionForm").addEventListener("submit", saveAiConnection);
    $("#aiTestConnection").addEventListener("click", testAiConnection);
    $("#aiDeleteKey").addEventListener("click", confirmDeleteAiKey);
    $("#aiOpenTemplates").addEventListener("click", openAiTemplates);
    $("#aiTemplateForm").addEventListener("submit", saveAiTemplate);
    $("#aiTemplateList").addEventListener("click", (event) => {
      const loadButton = event.target.closest("[data-ai-template-load]");
      if (loadButton) {
        loadAiTemplate(loadButton.dataset.aiTemplateLoad);
        return;
      }
      const deleteButton = event.target.closest("[data-ai-template-delete]");
      if (deleteButton) confirmDeleteAiTemplate(deleteButton.dataset.aiTemplateDelete);
    });
    $("#aiSavePersona").addEventListener("click", saveAiPersona);
    $("#aiExtractKnowledge").addEventListener("click", extractAiKnowledge);
    $("#aiDiscardGeneratedKnowledge").addEventListener("click", () => clearAiGeneratedKnowledge({ message: "已放弃本次 AI 返回内容，当前配置未修改" }));
    $("#aiApplyGeneratedKnowledge").addEventListener("click", confirmApplyAiGeneratedKnowledge);
    $("#aiSaveKnowledge").addEventListener("click", saveAiKnowledge);
    $("#aiDisableKnowledge").addEventListener("click", confirmDisableAiKnowledge);
    $("#aiRunPreview").addEventListener("click", runAiPreview);
    $("#aiClearPreview").addEventListener("click", clearAiPreview);
    $("#aiProvider").addEventListener("change", () => {
      if ($("#aiApiKey")) $("#aiApiKey").value = "";
      renderAiProviderFields();
      invalidateAiConnectionTest();
    });
    ["#aiBaseUrl", "#aiModel", "#aiApiKey"].forEach((selector) => $(selector).addEventListener("input", invalidateAiConnectionTest));
    $$("#aiStoreForm input, #aiStoreForm textarea, #aiStoreForm select").forEach((control) => control.addEventListener("input", () => { state.ai.dirty.config = true; text("#aiPersonaStatus", "有未保存修改"); formMessage("#aiPersonaMessage", "有未保存修改"); }));
    $("#aiKnowledgeContent").addEventListener("input", () => { state.ai.dirty.knowledge = true; text("#aiKnowledgeEditMode", "有未保存修改"); formMessage("#aiKnowledgeMessage", "有未保存修改"); });
    $("#aiExtractInput").addEventListener("input", () => {
      clearAiGeneratedKnowledge();
    });
    $("#aiProductSearch").addEventListener("input", (event) => { state.ai.productSearch = String(event.currentTarget.value || "").slice(0, 120); renderAiProducts(); });
    $("#aiPreviewInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void runAiPreview(); }
    });
    $("#automationEnabledToggle").addEventListener("change", (event) => {
      state.automation.enabled = event.currentTarget.checked;
    });
    $("#automationShopSelect")?.addEventListener("change", (event) => {
      const key = String(event.currentTarget.value || "").trim();
      if (key && key !== state.activeAccountKey) void switchShopAccount(key);
    });
    $$("#analyticsPeriod [data-period]").forEach((button) => button.addEventListener("click", () => {
      void loadTrendAnalytics(Number(button.dataset.period || 7)).catch((error) => showToast(error.message, "error"));
    }));
    document.addEventListener("visibilitychange", () => {
      syncMerchantPolling();
      syncResourcePolling();
      if (!document.hidden && shouldPollResources()) {
        void loadShopResources({ silent: true });
      }
    });
    $("#refreshShopResources")?.addEventListener("click", () => {
      void loadShopResources().then((data) => {
        if (data) showToast("店铺运行资源已更新");
      }).catch((error) => showToast(error.message, "error"));
    });
    $("#conversationSearch").addEventListener("input", (event) => {
      state.inbox.search = String(event.currentTarget.value || "").slice(0, 120);
      persistInboxPreferences();
      renderChat();
      scheduleInboxReload();
    });
    $("#clearConversationSearch").addEventListener("click", () => {
      state.inbox.search = "";
      persistInboxPreferences();
      renderChat();
      scheduleInboxReload();
      $("#conversationSearch")?.focus();
    });
    const selectInboxFilter = (filter) => {
      state.inbox.filter = ["unread", "takeover"].includes(filter) ? filter : "all";
      persistInboxPreferences();
      renderChat();
      scheduleInboxReload();
    };
    $("#conversationCategory")?.addEventListener("change", (event) => selectInboxFilter(event.currentTarget.value));
    $$('[data-inbox-filter]').forEach((button) => button.addEventListener("click", () => {
      selectInboxFilter(button.dataset.inboxFilter);
    }));
    $("#chatMessageSearch")?.addEventListener("input", (event) => {
      state.messageSearch = String(event.currentTarget.value || "").trim().slice(0, 120);
      state.messageMatchCount = 0;
      renderChat({ preserveScroll: true });
      scheduleMessageSearch();
    });
    $("#markConversationRead").addEventListener("click", () => {
      void markConversationRead(state.selectedChatId).catch(() => {});
    });
    $("#toggleChatTakeover").addEventListener("click", () => {
      void toggleConversationTakeover();
    });
    $("#refreshOrders")?.addEventListener("click", () => loadOrders().catch((error) => showToast(error.message, "error")));
    $("#ordersFilterForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const searchField = $("#ordersSearchField")?.value || "all";
      let q = ($("#ordersSearchInput")?.value || "").trim();
      if (q.length > 128) q = q.slice(0, 128);
      const dateFrom = $("#ordersDateFrom")?.value || "";
      const dateTo = $("#ordersDateTo")?.value || "";
      if (dateFrom && dateTo && dateFrom > dateTo) {
        showToast("起始日期不能晚于截止日期", "warning");
        return;
      }
      state.ordersPage.searchField = searchField;
      state.ordersPage.q = q;
      state.ordersPage.dateFrom = dateFrom;
      state.ordersPage.dateTo = dateTo;
      state.ordersPage.page = 1;
      state.ordersPage.expandedKeys.clear();
      void loadOrderPage().catch((error) => showToast(error.message, "error"));
    });
    $("#ordersReset")?.addEventListener("click", () => {
      if ($("#ordersSearchField")) $("#ordersSearchField").value = "all";
      if ($("#ordersSearchInput")) $("#ordersSearchInput").value = "";
      if ($("#ordersDateFrom")) $("#ordersDateFrom").value = "";
      if ($("#ordersDateTo")) $("#ordersDateTo").value = "";
      state.ordersPage.searchField = "all";
      state.ordersPage.q = "";
      state.ordersPage.dateFrom = "";
      state.ordersPage.dateTo = "";
      state.ordersPage.status = "all";
      state.ordersPage.page = 1;
      state.ordersPage.expandedKeys.clear();
      void loadOrderPage().catch((error) => showToast(error.message, "error"));
    });
    $("#ordersShopSelect")?.addEventListener("change", (event) => {
      const nextKey = event.currentTarget.value;
      if (nextKey && nextKey !== state.activeAccountKey) {
        void switchShopAccount(nextKey);
      }
    });
    $("#ordersStatusTabs")?.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-order-status]");
      if (!tab) return;
      const status = tab.dataset.orderStatus;
      if (status && status !== state.ordersPage.status) {
        state.ordersPage.status = status;
        state.ordersPage.page = 1;
        state.ordersPage.expandedKeys.clear();
        void loadOrderPage().catch((error) => showToast(error.message, "error"));
      }
    });
    $("#orderList")?.addEventListener("click", (event) => {
      const toggleBtn = event.target.closest("[data-order-toggle]");
      if (toggleBtn) {
        event.preventDefault();
        const key = toggleBtn.dataset.orderToggle;
        if (key) {
          if (state.ordersPage.expandedKeys.has(key)) {
            state.ordersPage.expandedKeys.delete(key);
          } else {
            state.ordersPage.expandedKeys.add(key);
          }
          renderOrderPage();
        }
        return;
      }
      const copyBtn = event.target.closest("[data-order-copy]");
      if (copyBtn) {
        event.preventDefault();
        void copyOrderText(copyBtn.dataset.orderCopy);
        return;
      }
      const chatBtn = event.target.closest("[data-order-chat]");
      if (chatBtn && !chatBtn.disabled) {
        event.preventDefault();
        void handleOrderChat(chatBtn.dataset.orderChat);
        return;
      }
      const retryBtn = event.target.closest("#ordersRetryBtn");
      if (retryBtn) {
        event.preventDefault();
        void loadOrderPage().catch((error) => showToast(error.message, "error"));
        return;
      }
      const clearFilterBtn = event.target.closest("#ordersClearFilterBtn");
      if (clearFilterBtn) {
        event.preventDefault();
        $("#ordersReset")?.click();
        return;
      }
    });
    $("#ordersPageSize")?.addEventListener("change", (event) => {
      const nextSize = Number(event.currentTarget.value);
      if ([15, 30, 50].includes(nextSize)) {
        state.ordersPage.pageSize = nextSize;
        state.ordersPage.page = 1;
        state.ordersPage.expandedKeys.clear();
        void loadOrderPage().catch((error) => showToast(error.message, "error"));
      }
    });
    $("#ordersPrev")?.addEventListener("click", () => {
      if (state.ordersPage.page > 1) {
        state.ordersPage.page -= 1;
        state.ordersPage.expandedKeys.clear();
        void loadOrderPage().catch((error) => showToast(error.message, "error"));
      }
    });
    $("#ordersNext")?.addEventListener("click", () => {
      if (state.ordersPage.page < state.ordersPage.totalPages) {
        state.ordersPage.page += 1;
        state.ordersPage.expandedKeys.clear();
        void loadOrderPage().catch((error) => showToast(error.message, "error"));
      }
    });
    const handleOrdersJump = () => {
      const input = $("#ordersPageInput");
      if (!input) return;
      const val = parseInt(input.value, 10);
      if (Number.isInteger(val)) {
        const clamped = Math.max(1, Math.min(state.ordersPage.totalPages || 1, val));
        if (clamped !== state.ordersPage.page) {
          state.ordersPage.page = clamped;
          state.ordersPage.expandedKeys.clear();
          void loadOrderPage().catch((error) => showToast(error.message, "error"));
        } else {
          input.value = clamped;
        }
      }
    };
    $("#ordersPageJump")?.addEventListener("click", handleOrdersJump);
    $("#ordersPageInput")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        handleOrdersJump();
      }
    });
    $("#createTemplateButton").addEventListener("click", () => { void openTemplateEditor(); });
    $("#templatesEmptyAction").addEventListener("click", () => { void openTemplateEditor(); });
    $("#templateEditorForm").addEventListener("submit", saveTemplate);
    $("#templateCardPoolSelect").addEventListener("change", (event) => {
      const deliveryTypeInput = $("#templateDeliveryTypeInput");
      if (deliveryTypeInput) deliveryTypeInput.value = event.currentTarget.value ? "redeem" : "pan";
    });
    $("#importCardsButton").addEventListener("click", () => openCardsEditor());
    $("#cardsEmptyAction").addEventListener("click", () => openCardsEditor());
    $("#cardsEditorForm").addEventListener("submit", saveCards);
    $("#cardsCreateForm").addEventListener("submit", createCardPool);
    $("#quickReplyForm")?.addEventListener("submit", addQuickReply);
    $("#manualReplyForm").addEventListener("submit", sendManualReply);
    $("#manualReplyInput").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (!event.currentTarget.disabled && !state.manualReply.submitting) {
        void $("#manualReplyForm").requestSubmit();
      }
    });
    $("#manualReplyFile").addEventListener("change", handleManualReplyFileSelection);
    $("#manualReplyPreview").addEventListener("click", handleManualReplyAttachmentClick);
    $(".chat-window").addEventListener("paste", handleManualReplyPaste);
    $(".chat-window").addEventListener("dragenter", handleManualReplyDragEnter);
    $(".chat-window").addEventListener("dragover", handleManualReplyDragOver);
    $(".chat-window").addEventListener("dragleave", handleManualReplyDragLeave);
    $(".chat-window").addEventListener("drop", handleManualReplyDrop);
    $("#chatAiStart").addEventListener("click", startBot);
    $("#chatAiStop").addEventListener("click", confirmStop);
    $("#confirmCancel").addEventListener("click", () => closeDialog("confirmDialog"));
    window.addEventListener("beforeunload", (event) => {
      if (syncAiDirtyFlags()) {
        event.preventDefault();
        event.returnValue = "";
      }
      const loginId = state.qrLogin.loginId;
      const accountKey = state.qrLogin.accountKey;
      if (!loginId || !accountKey) return;
      void fetch(API_PREFIX + "/api/bot/login/" + encodeURIComponent(loginId) + "/cancel", {
        method: "POST",
        headers: {
          "X-Shop-Account": accountKey,
          "X-SaaS-Browser-Intent": "browser-write",
        },
        credentials: "same-origin",
        keepalive: true,
      });
    });
    $("#confirmAction").addEventListener("click", async () => {
      if (!state.confirmAction) return;
      const action = state.confirmAction;
      state.confirmAction = null;
      closeDialog("confirmDialog");
      try { await action(); } catch (error) { showToast(error.message, "error"); }
    });
    $$("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.closeDialog)));
    document.addEventListener("click", (event) => {
      const shopAddTrigger = event.target.closest("[data-open-shop-add]");
      if (shopAddTrigger) {
        event.preventDefault();
        openShopAccountForm();
        return;
      }
      const shopAction = event.target.closest("[data-shop-action]");
      if (shopAction) {
        event.preventDefault();
        event.stopPropagation();
        void handleShopAction({ currentTarget: shopAction }).catch((error) => showToast(error.message || "店铺操作失败", "error"));
        return;
      }
      const accountRename = event.target.closest("[data-account-rename]");
      if (accountRename) {
        event.preventDefault();
        event.stopPropagation();
        void focusRenameShopAccount(accountRename.dataset.accountRename).catch((error) => showToast(error.message || "无法打开名称编辑", "error"));
        return;
      }
      const accountDelete = event.target.closest("[data-account-delete]");
      if (accountDelete) {
        event.preventDefault();
        event.stopPropagation();
        confirmDeleteShopAccount(accountDelete.dataset.accountDelete);
        return;
      }
      const accountSwitch = event.target.closest("[data-account-switch]");
      if (accountSwitch) {
        event.preventDefault();
        void switchShopAccount(accountSwitch.dataset.accountSwitch);
        return;
      }
      const pageButton = event.target.closest("[data-shop-page]");
      if (pageButton) {
        event.preventDefault();
        const enabled = state.accounts.filter((item) => item.enabled !== false);
        const pageCount = Math.max(1, Math.ceil(enabled.length / Math.max(1, Number(state.shopAccountsPageSize || 5))));
        state.shopAccountsPage += pageButton.dataset.shopPage === "next" ? 1 : -1;
        state.shopAccountsPage = Math.min(pageCount, Math.max(1, state.shopAccountsPage));
        renderAccountSwitcher();
        return;
      }
      const aiProductTrigger = event.target.closest("[data-ai-product]");
      if (aiProductTrigger) {
        event.preventDefault();
        void selectAiProduct(aiProductTrigger.dataset.aiProduct);
        return;
      }
      const conversationTrigger = event.target.closest("[data-chat-id]");
      if (conversationTrigger) {
        event.preventDefault();
        selectConversation(conversationTrigger.dataset.chatId);
        return;
      }
      const quickReplyTrigger = event.target.closest("[data-quick-reply]");
      if (quickReplyTrigger) {
        event.preventDefault();
        injectQuickReply(quickReplyTrigger.dataset.quickReply);
        return;
      }
      const quickRepliesOpen = event.target.closest("[data-open-quick-replies]");
      if (quickRepliesOpen) {
        event.preventDefault();
        openQuickRepliesDialog();
        return;
      }
      const quickReplyDelete = event.target.closest("[data-delete-quick-reply]");
      if (quickReplyDelete) {
        event.preventDefault();
        void deleteQuickReply(quickReplyDelete.dataset.deleteQuickReply, quickReplyDelete);
        return;
      }
      const attentionToggle = event.target.closest("[data-attention-toggle]");
      if (attentionToggle) {
        event.preventDefault();
        event.stopPropagation();
        void toggleAttentionResolution(attentionToggle.dataset.attentionToggle, attentionToggle);
        return;
      }
      const ruleEditorTrigger = event.target.closest("[data-edit-rule]");
      if (ruleEditorTrigger) {
        event.preventDefault();
        const row = ruleEditorTrigger.closest(".rule-row");
        const index = Number(row?.dataset.ruleIndex);
        if (Number.isInteger(index)) editReplyRule(index);
        return;
      }
      const productDeliveryToggle = event.target.closest("[data-delivery-toggle]");
      if (productDeliveryToggle) {
        event.preventDefault();
        void toggleProductDelivery(productDeliveryToggle);
        return;
      }
      const deliveryEditorTrigger = event.target.closest("[data-edit-delivery]");
      if (deliveryEditorTrigger) {
        event.preventDefault();
        openBatchDelivery(deliveryEditorTrigger.dataset.itemId || "");
        return;
      }
      const removeRule = event.target.closest("[data-remove-rule]");
      if (removeRule) {
        event.preventDefault();
        const row = removeRule.closest(".rule-row");
        const index = Number(row?.dataset.ruleIndex);
        if (Number.isInteger(index)) confirmRemoveReplyRule(index);
        return;
      }
      const templateEdit = event.target.closest("[data-template-edit]");
      if (templateEdit) {
        event.preventDefault();
        void openTemplateEditor(templateEdit.dataset.templateEdit);
        return;
      }
      const templateDelete = event.target.closest("[data-template-delete]");
      if (templateDelete) {
        event.preventDefault();
        confirmDeleteTemplate(templateDelete.dataset.templateDelete);
        return;
      }
      const cardsImport = event.target.closest("[data-cards-import]");
      if (cardsImport) {
        event.preventDefault();
        openCardsEditor(cardsImport.dataset.cardsImport, "import");
        return;
      }
      const cardsEdit = event.target.closest("[data-cards-edit]");
      if (cardsEdit) {
        event.preventDefault();
        openCardsEditor(cardsEdit.dataset.cardsEdit, "edit");
        return;
      }
      const viewTrigger = event.target.closest("[data-view]");
      if (viewTrigger) {
        event.preventDefault();
        showView(viewTrigger.dataset.view);
        return;
      }
      const syncTrigger = event.target.closest("[data-sync-products]");
      if (syncTrigger) {
        event.preventDefault();
        void syncShop({ currentTarget: syncTrigger });
      }
    });
  }

  function installProductImageFallback() {
    // Product thumbnails may reference remote images that can expire.  Keep a
    // safe monogram fallback so a broken image never leaves an empty card.
    document.addEventListener("error", (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement)) return;
      const host = image.closest(".home-product-thumb, .product-thumb");
      if (!host) return;
      const card = host.closest(".home-product-card");
      const title = card?.querySelector(".home-product-name")?.textContent
        || host.closest(".product-cell")?.querySelector(".product-title")?.textContent
        || host.closest(".orders-product-cell")?.querySelector(".orders-product-title")?.textContent
        || "闲";
      const monogramClass = host.classList.contains("home-product-thumb") ? "home-product-monogram" : "product-monogram";
      host.innerHTML = '<span class="' + monogramClass + '" aria-hidden="true">' + esc(String(title).trim().slice(0, 1) || "闲") + "</span>";
    }, true);
  }

  async function init() {
    loadGoodsPreferences();
    bindEvents();
    syncSidebarAccessibility();
    installProductImageFallback();
    void loadPublicVersionOnce();
    try {
      await loadAuthCapabilities({ preferFirstRegistration: true });
      await bootstrap();
    } catch (error) {
      clearSession(false);
      if (error.status !== 401) formMessage("#authError", error.message || "服务暂时不可用，请刷新页面后重试");
    }
  }

  window.addEventListener("DOMContentLoaded", init);
})();

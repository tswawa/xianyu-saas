/* global fetch, URL */
(() => {
  "use strict";

  const API_PREFIX = "/xianyu-saas";
  const QR_LOGIN_POLL_MS = 1500;
  const ASSET_VERSION = "20260831-01";
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
  };
  const ACTIVE_ACCOUNT_STORAGE_PREFIX = "xianyu-saas.active-account:";
  const INBOX_STORAGE_PREFIX = "xianyu-saas.inbox:";
  const MANUAL_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
  const MANUAL_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
  const state = {
    authMode: "login",
    registrationAllowed: false,
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
      generatedKnowledge: null,
      previewHistory: [],
      dirty: { connection: false, config: false, knowledge: false },
      baseline: { connection: "", config: "", knowledge: "" },
    },
    attention: [],
    summary: null,
    analytics: null,
    analyticsPeriod: 1,
    analyticsPage: null,
    products: [],
    productsAccountKey: "",
    productsLoad: null,
    productsTruncated: null,
    productsTruncatedAccountKey: "",
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
      file: null,
      media: null,
      attachmentKey: "",
      previewUrl: "",
      dragging: false,
      submitting: false,
      uploading: false,
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
    templates: [],
    cards: null,
    cardsAccountKey: "",
    cardsLoad: null,
    templateEditorOpenGeneration: 0,
    templateEditor: { editingId: "", productIds: [] },
    cardsEditor: { editingId: "", mode: "import" },
    confirmAction: null,
    docs: {
      tab: "guide",
      version: null,
      update: null,
      settings: null,
      users: [],
      audit: [],
      availableVersion: "",
      stagedVersion: "",
      loading: false,
    },
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
      risk_control: "需要安全验证",
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
    if (["restricted", "account_restricted", "risk_control", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "expired"].includes(code)) return "is-error";
    if (["degraded", "sync_cooldown", "sync_busy", "network_error", "platform_busy", "platform_error", "profile_missing", "sync_error"].includes(code)) return "is-warning";
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
      const liveAuthCode = active && ["risk_control", "session_expired"].includes(String(state.bot?.auth_code || ""))
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
      const isError = ["expired", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "restricted", "account_restricted", "risk_control"].includes(healthCode);
      const isWarning = ["degraded", "sync_cooldown", "sync_busy", "network_error", "platform_busy", "platform_error", "profile_missing", "sync_error"].includes(healthCode);
      const statusBadge = isError ? "badge-red" : healthCode === "ready" ? "badge-green" : isWarning ? "badge-amber" : "badge-muted";
      const toneClass = isError ? " is-expired" : healthCode === "ready" ? " is-ready" : " is-unconfigured";
      const needsReconnect = ["expired", "session_expired", "cookie_expired", "cookie_invalid", "cookie_incomplete", "restricted", "account_restricted", "risk_control", "degraded", "waiting_login"].includes(healthCode);
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
    const keyInput = $("#aiApiKey");
    if (keyInput) keyInput.value = "";
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
    const candidate = aiConnectionCandidate();
    state.ai.dirty.connection = JSON.stringify({ provider: candidate.provider, base_url: candidate.base_url, model: candidate.model }) !== state.ai.baseline.connection || Boolean(candidate.api_key);
    if ($("#aiStoreForm")) state.ai.dirty.config = aiStoreBaselineValue() !== state.ai.baseline.config;
    const knowledge = $("#aiKnowledgeContent");
    if (knowledge && state.ai.selectedItemId) state.ai.dirty.knowledge = knowledge.value !== state.ai.baseline.knowledge;
    return Object.values(state.ai.dirty).some(Boolean);
  }

  function confirmDiscardAiChanges(reason = "切换") {
    if (!syncAiDirtyFlags()) return true;
    const accepted = window.confirm("当前 AI 配置有未保存修改。确认" + reason + "并丢弃这些修改吗？");
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
    ["#aiSaveKnowledge", "#aiDisableKnowledge", "#aiExtractKnowledge", "#aiRunPreview"].forEach((selector) => {
      const button = $(selector);
      if (button) button.disabled = disabled;
    });
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
    const badge = $("#aiConnectionBadge");
    if (badge) {
      badge.textContent = statusInfo[0];
      badge.className = "badge " + statusInfo[1];
    }
    const overall = $("#aiOverallStatus");
    const aiRunning = Boolean(state.bot?.running && state.bot?.automation_mode === "rules_ai");
    if (overall) {
      overall.textContent = aiRunning ? "AI 运行中" : statusCode === "verified" ? "AI 已暂停" : statusInfo[0];
      overall.className = "badge " + (aiRunning ? "badge-green" : statusCode === "verified" ? "badge-amber" : statusInfo[1]);
    }
    const selectedProvider = aiProviderCode();
    const keyConfigured = aiProviderHasReusableKey(selectedProvider);
    const keyState = $("#aiKeyState");
    if (keyState) {
      const optionalKey = selectedProvider === "ollama_chat" && !keyConfigured;
      keyState.textContent = keyConfigured ? "已安全保存" : optionalKey ? "可选" : aiProviderCode(connection.provider) !== selectedProvider ? "需重新填写" : "未配置";
      keyState.className = "ai-key-state" + (keyConfigured ? " is-saved" : "");
    }
    if (!preserveEditors) {
      const provider = aiProviderCode(connection.provider);
      const baseUrl = String(connection.base_url || "");
      const model = String(connection.model || "");
      if ($("#aiProvider")) $("#aiProvider").value = provider;
      if ($("#aiBaseUrl")) $("#aiBaseUrl").value = baseUrl;
      if ($("#aiModel")) $("#aiModel").value = model;
      if ($("#aiApiKey")) $("#aiApiKey").value = "";
      state.ai.baseline.connection = JSON.stringify({ provider, base_url: baseUrl, model });
      state.ai.dirty.connection = false;
      writeAiStoreForm(aiConfigDraft(state.ai.config));
    }
    renderAiProviderFields();
    const configStatus = String(state.ai?.config?.content_status || state.ai?.config?.status || (aiStoreHasContent() ? "saved" : "unconfigured"));
    const personaInfo = aiStoreHasContent() ? aiKnowledgeStatusInfo(configStatus) : ["未填写", "badge-muted"];
    const personaBadge = $("#aiPersonaStatus");
    if (personaBadge) {
      personaBadge.textContent = personaInfo[0];
      personaBadge.className = "badge " + personaInfo[1];
    }
    setAiInlineStatus(statusInfo[2], statusInfo[1] === "badge-red" ? "error" : statusInfo[1] === "badge-green" ? "success" : "warning");
    const search = $("#aiProductSearch");
    if (search && search.value !== state.ai.productSearch) search.value = state.ai.productSearch;
    renderAiTemplates();
    renderAiKnowledgeEditor({ preserveText: preserveEditors });
    renderAiStatus();
  }

  async function loadAiKnowledge(itemId, context = captureAccountContext()) {
    const selected = String(itemId || "").trim();
    if (!selected) return;
    const productGeneration = Number(state.ai.productGeneration || 0);
    const generation = ++state.ai.knowledgeGeneration;
    const basePath = "/api/bot/ai/products/" + encodeURIComponent(selected);
    const [knowledge, versions] = await Promise.all([
      accountScopedApi(context, basePath + "/knowledge"),
      accountScopedApi(context, basePath + "/versions").catch(() => ({ versions: [] })),
    ]);
    if (!accountContextMatches(context) || generation !== state.ai.knowledgeGeneration || productGeneration !== Number(state.ai.productGeneration || 0) || selected !== state.ai.selectedItemId) return;
    state.ai.knowledge = knowledge?.knowledge || knowledge || null;
    state.ai.versions = Array.isArray(versions?.versions) ? versions.versions : [];
    state.ai.generatedKnowledge = null;
    state.ai.extractionGeneration += 1;
    renderAiKnowledgeEditor();
  }

  async function loadAiConfig({ preserveSelection = true } = {}) {
    const context = captureAccountContext();
    const generation = ++state.ai.loadGeneration;
    const [status, connection, config, templates, products] = await Promise.all([
      accountScopedApi(context, "/api/bot/ai/status"),
      accountScopedApi(context, "/api/bot/ai/connection"),
      accountScopedApi(context, "/api/bot/ai/config"),
      accountScopedApi(context, "/api/bot/ai/templates"),
      accountScopedApi(context, "/api/bot/ai/products"),
    ]);
    if (!accountContextMatches(context) || generation !== state.ai.loadGeneration) return false;
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
    if (state.ai.selectedItemId) await loadAiKnowledge(state.ai.selectedItemId, context);
    return accountContextMatches(context) && generation === state.ai.loadGeneration;
  }

  function invalidateAiConnectionTest() {
    state.ai.verificationToken = "";
    state.ai.testedFingerprint = "";
    state.ai.dirty.connection = true;
    const connection = state.ai.connection || {};
    state.ai.connection = Object.assign({}, connection, { status: "pending", verified: false });
    renderAiConfig({ preserveEditors: true });
  }

  function validateAiConnectionCandidate(candidate, { requireKey = false } = {}) {
    if (!AI_PROVIDER_COPY[candidate.provider]) throw new ApiError("请选择支持的接口格式");
    if (!candidate.base_url) throw new ApiError("请填写服务地址");
    if (!candidate.model) throw new ApiError("请填写模型名");
    if (requireKey && aiProviderRequiresKey(candidate.provider) && !candidate.api_key && !aiProviderHasReusableKey(candidate.provider)) throw new ApiError("首次配置或切换接口格式后请输入对应的 API Key");
  }

  async function testAiConnection() {
    const candidate = aiConnectionCandidate();
    try {
      validateAiConnectionCandidate(candidate, { requireKey: true });
    } catch (error) {
      setAiInlineStatus(error.message, "error");
      return;
    }
    const button = $("#aiTestConnection");
    const context = captureAccountContext();
    const fingerprint = aiConnectionFingerprint(candidate);
    setBusy(button, true);
    setAiInlineStatus("正在测试连接，不会启动客服或发送闲鱼消息", "warning");
    try {
      const result = await accountScopedApi(context, "/api/bot/ai/connection/test", {
        method: "POST",
        body: JSON.stringify(Object.assign({}, candidate, {
          expected_revision: Number(state.ai.connection?.revision || 0),
        })),
      });
      if (!accountContextMatches(context)) return;
      state.ai.verificationToken = String(result?.verification_token || "");
      state.ai.testedFingerprint = fingerprint;
      state.ai.connection = Object.assign({}, state.ai.connection || {}, {
        status: String(result?.status || "success"),
        verified: true,
        last_tested_at: result?.tested_at || result?.last_tested_at || null,
      });
      renderAiConfig({ preserveEditors: true });
      setAiInlineStatus("连接测试成功，可以保存当前候选配置", "success");
      showToast("模型连接测试成功");
    } catch (error) {
      if (!accountContextMatches(context)) return;
      state.ai.verificationToken = "";
      state.ai.testedFingerprint = "";
      const code = String(error?.code || "unavailable");
      state.ai.connection = Object.assign({}, state.ai.connection || {}, { status: code, verified: false });
      renderAiConfig({ preserveEditors: true });
      setAiInlineStatus(connectionStatusInfo(code)[2] || error.message, "error");
      showToast(connectionStatusInfo(code)[0], "error");
    } finally {
      if (accountContextMatches(context)) setBusy(button, false);
    }
  }

  async function saveAiConnection(event) {
    event.preventDefault();
    const candidate = aiConnectionCandidate();
    try {
      validateAiConnectionCandidate(candidate, { requireKey: true });
      if (!state.ai.verificationToken || state.ai.testedFingerprint !== aiConnectionFingerprint(candidate)) throw new ApiError("请先测试当前接口格式、服务地址、模型和密钥");
    } catch (error) {
      setAiInlineStatus(error.message, "error");
      return;
    }
    const button = event.submitter || $("#aiSaveConnection");
    const context = captureAccountContext();
    setBusy(button, true);
    try {
      const payload = {
        provider: candidate.provider,
        base_url: candidate.base_url,
        model: candidate.model,
        verification_token: state.ai.verificationToken,
        expected_revision: Number(state.ai.connection?.revision || 0),
      };
      if (candidate.api_key) payload.api_key = candidate.api_key;
      const result = await accountScopedApi(context, "/api/bot/ai/connection", { method: "PUT", body: JSON.stringify(payload) });
      if (!accountContextMatches(context)) return;
      state.ai.connection = result?.connection || result || state.ai.connection;
      state.ai.verificationToken = "";
      state.ai.testedFingerprint = "";
      if ($("#aiApiKey")) $("#aiApiKey").value = "";
      renderAiConfig();
      setAiInlineStatus("连接已安全保存；API Key 不会回显", "success");
      showToast("模型连接已保存");
    } catch (error) {
      if (accountContextMatches(context)) setAiInlineStatus(error.message || "连接保存失败", "error");
    } finally {
      if (accountContextMatches(context)) setBusy(button, false);
    }
  }

  function confirmDeleteAiKey() {
    const context = captureAccountContext();
    text("#confirmTitle", "删除当前店铺 API Key");
    text("#confirmMessage", "删除后当前店铺 AI 客服会关闭，固定规则与人工回复不受影响。是否继续？");
    text("#confirmAction", "确认删除 Key");
    state.confirmAction = async () => {
      if (!accountContextMatches(context)) return;
      await accountScopedApi(context, "/api/bot/ai/connection/key", {
        method: "DELETE",
        body: JSON.stringify({ confirm: true, expected_revision: Number(state.ai.connection?.revision || 0) }),
      });
      if (!accountContextMatches(context)) return;
      if ($("#aiApiKey")) $("#aiApiKey").value = "";
      state.ai.connection = Object.assign({}, state.ai.connection || {}, { api_key_configured: false, status: "unconfigured", verified: false });
      state.ai.status = Object.assign({}, state.ai.status || {}, { enabled: false, connection_verified: false });
      state.ai.verificationToken = "";
      state.ai.testedFingerprint = "";
      renderAiConfig();
      showToast("当前店铺 API Key 已删除");
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

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
    setBusy($("#aiRunPreview"), false);
    if ($("#aiPreviewInput")) $("#aiPreviewInput").value = "";
    if ($("#aiPreviewOutput")) $("#aiPreviewOutput").innerHTML = "<span>回复后会显示实际回复、使用资料、内容状态与安全状态。</span>";
    renderAiPreviewHistory();
  }

  async function runAiPreview() {
    const itemId = String(state.ai.selectedItemId || "");
    const question = String($("#aiPreviewInput")?.value || "").trim();
    if (!itemId) return;
    if (!hasMeaningfulText(question)) {
      showToast("请输入当前买家问题", "warning");
      return;
    }
    const button = $("#aiRunPreview");
    const output = $("#aiPreviewOutput");
    const scope = captureAiProductScope(itemId);
    const generation = ++state.ai.previewGeneration;
    const history = (state.ai.previewHistory || []).slice(-6).map((message) => ({ role: message.role, content: message.content }));
    setBusy(button, true);
    output.innerHTML = "<span>正在生成实际回复，不会发送闲鱼消息…</span>";
    try {
      const result = await accountScopedApi(scope.account, "/api/bot/ai/preview", {
        method: "POST",
        body: JSON.stringify({ buyer_message: question, item_id: itemId, history }),
      });
      if (!aiProductScopeMatches(scope) || generation !== state.ai.previewGeneration) return;
      const reply = String(result?.reply?.content || result?.reply || result?.answer || "").trim();
      const sources = previewSources(result);
      const knowledgeStatus = String(result?.knowledge_status || result?.content_status || aiKnowledgeStatus(null, state.ai.knowledge));
      const safety = String(result?.safety_status || result?.safety?.status || result?.safety || "已通过安全检查");
      state.ai.previewHistory = history.concat([{ role: "user", content: question }, ...(reply ? [{ role: "assistant", content: reply }] : [])]).slice(-6);
      $("#aiPreviewInput").value = "";
      renderAiPreviewHistory();
      output.innerHTML = '<div class="ai-preview-answer"><strong>实际回复</strong><div>' + esc(reply || "本次未生成可发送回复，请转人工处理") + '</div></div><div class="ai-preview-details"><div><strong>使用资料</strong><span>' + esc(sources.length ? sources.join("、") : "未标明") + '</span></div><div><strong>内容状态</strong><span>' + esc(aiKnowledgeStatusInfo(knowledgeStatus)[0]) + '</span></div><div><strong>安全状态</strong><span>' + esc(safety) + "</span></div></div>";
    } catch (error) {
      if (aiProductScopeMatches(scope) && generation === state.ai.previewGeneration) output.innerHTML = '<span>沙盘测试失败：' + esc(error.message || "请稍后重试") + "</span>";
    } finally {
      if (aiProductScopeMatches(scope) && generation === state.ai.previewGeneration) setBusy(button, false);
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

  function revokeManualReplyPreview() {
    if (state.manualReply.previewUrl) {
      URL.revokeObjectURL(state.manualReply.previewUrl);
      state.manualReply.previewUrl = "";
    }
  }

  function setManualReplyDragActive(active) {
    state.manualReply.dragging = Boolean(active);
    $("#manualReplyForm")?.classList.toggle("is-drag-active", state.manualReply.dragging);
    $("#manualReplyDropzone")?.classList.toggle("is-drag-active", state.manualReply.dragging);
    $(".chat-window")?.classList.toggle("is-drag-active", state.manualReply.dragging);
  }

  function resetManualReplyContext({ clearInput = true } = {}) {
    state.manualReply.generation += 1;
    state.manualReply.request = null;
    state.manualReply.file = null;
    state.manualReply.media = null;
    state.manualReply.attachmentKey = "";
    revokeManualReplyPreview();
    setManualReplyDragActive(false);
    state.manualReply.submitting = false;
    state.manualReply.uploading = false;
    state.manualReply.polling.clear();
    const input = $("#manualReplyInput");
    const file = $("#manualReplyFile");
    if (clearInput && input) input.value = "";
    if (file) file.value = "";
    text("#manualReplyFileName", "");
    text("#manualReplyFileMeta", "");
    $("#manualReplyPreview")?.setAttribute("hidden", "");
    $("#manualReplyPreviewImage")?.removeAttribute("src");
    $("#clearManualReplyFile")?.setAttribute("hidden", "");
    formMessage("#replyMessage", "");
    setBusy($("#manualReplyForm button[type=submit]"), false);
    renderManualReplyAttachment();
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
    risk_control: "闲鱼需要安全验证，请在已打开的官方页面完成。完成后会继续连接。",
    risk_cooldown: "闲鱼安全验证冷却中，请稍后再试。",
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
    risk_control: "需要安全验证",
    risk_cooldown: "安全验证冷却中",
    cookie_expired: "登录已失效",
    cookie_invalid: "需要重新登录",
    cookie_incomplete: "需要重新登录",
    account_restricted: "部分能力受限",
  };

  const COOKIE_STATUS_ACTIONS = {
    risk_control: "请在闲鱼官方页面完成验证",
    risk_cooldown: "等待冷却结束后重新连接",
    cookie_expired: "重新扫码授权后自动恢复服务",
    cookie_invalid: "重新登录闲鱼后自动连接",
    cookie_incomplete: "重新登录闲鱼后自动连接",
    account_restricted: "请在闲鱼官方页面查看处理通知",
  };

  const COOKIE_BLOCKING_CODES = new Set([
    "risk_control", "risk_cooldown", "cookie_expired", "cookie_invalid", "cookie_incomplete", "account_restricted",
  ]);

  function cookieErrorMessage(error) {
    return COOKIE_ERROR_COPY[error?.code] || error?.message || "登录状态检测失败，请稍后重试。";
  }

  function cookieStatusInfo(bot) {
    const authCode = bot?.auth_code === "risk_control"
      ? "risk_control"
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
    const connection = bot.connection_state || (
      !bot.cookies_set ? "unconfigured" :
        code === "account_restricted" ? "connected" :
          code === "verified" && bot.connected !== false ? "connected" :
            code === "pending" ? "checking" :
              code === "risk_control" || code === "risk_cooldown" ? "security_check" :
                code === "cookie_expired" || code === "cookie_invalid" || code === "cookie_incomplete" ? "reauth_required" :
                  bot.connected ? "connected" : "degraded"
    );
    const productCount = Number(bot.product_count || 0);
    const catalog = bot.catalog_state || (
      code === "account_restricted" || code === "risk_control" || code === "risk_cooldown" ? (productCount ? "stale" : "blocked") :
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
        action: "完成安全验证",
        title: "需要完成闲鱼安全验证",
        description: "请在闲鱼官方页面完成验证，系统不会绕过平台限制。",
        button: "重新检测店铺",
        hint: "完成官方验证后点击重新检测。",
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
    let response;
    try {
      response = await fetch(API_PREFIX + path, Object.assign({}, options, {
        credentials: "same-origin",
        headers,
      }));
    } catch (error) {
      throw new ApiError("网络连接失败，请稍后重试");
    }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      clearSession(false);
      throw new ApiError("登录已过期，请重新登录", 401);
    }
    return readResponse(response);
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

  function renderQrLogin() {
    const login = state.qrLogin;
    const connected = shopStateView(state.bot || {}).connection === "connected";
    const statusCopy = {
      idle: [connected ? "重新连接店铺" : "连接闲鱼店铺", "请使用闲鱼 App 扫码"],
      starting: ["正在生成二维码", "请稍候"],
      waiting: ["请使用闲鱼 App 扫码", "打开闲鱼 App，扫描上方二维码"],
      scanned: ["已扫码", "请在手机上确认登录"],
      syncing: ["登录成功", "正在识别店铺和商品"],
      sync_error: ["店铺识别未完成", "登录仍然有效，可以直接重试"],
      connected: ["店铺连接成功", "商品已经自动整理"],
      expired: ["二维码已过期", "刷新后重新扫码"],
      error: ["暂时无法登录", "请刷新二维码后重试"],
    };
    const copy = statusCopy[login.status] || statusCopy.error;
    text("#xianyuLoginTitle", connected ? "重新连接店铺" : "连接闲鱼店铺");
    text("#xianyuLoginStatus", copy[0]);
    text("#xianyuLoginMessage", login.message || copy[1]);
    const refresh = $("#refreshXianyuLogin");
    if (refresh) {
      refresh.hidden = !["expired", "error", "sync_error"].includes(login.status);
      text(refresh.querySelector("span"), login.status === "sync_error" ? "重试连接" : "刷新二维码");
    }
    const placeholder = $("#qrLoginPlaceholder");
    if (placeholder) placeholder.hidden = ["waiting", "scanned"].includes(login.status) && !$("#xianyuQrImage")?.hidden;
  }

  function resetQrLogin() {
    clearQrLoginPoll();
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
    const loginId = state.qrLogin.loginId;
    const accountKey = state.qrLogin.accountKey;
    if (!loginId || !accountKey || state.qrLogin.generation !== generation) return;
    state.qrLogin.status = "syncing";
    state.qrLogin.message = "";
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
      if (state.qrLogin.generation !== generation) return;
      if (error?.status === 404 || error?.status === 410 || error?.code === "login_expired") {
        state.qrLogin.status = "expired";
        state.qrLogin.message = "";
      } else {
        state.qrLogin.status = "sync_error";
        state.qrLogin.message = cookieErrorMessage(error);
        reflectCookieError(error);
      }
      renderQrLogin();
    }
  }

  async function pollQrLogin(generation) {
    const loginId = state.qrLogin.loginId;
    const accountKey = state.qrLogin.accountKey;
    if (!loginId || !accountKey || state.qrLogin.generation !== generation) return;
    try {
      const result = await api(
        "/api/bot/login/" + encodeURIComponent(loginId) + "/status",
        qrLoginRequestOptions(accountKey),
      );
      if (state.qrLogin.generation !== generation) return;
      if (!loginResponseIsSafe(result)) throw new ApiError("登录响应包含了不安全数据，已中止连接");
      const status = String(result?.status || "").toLowerCase();
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
      if (state.qrLogin.generation !== generation) return;
      if (error?.status === 404 || error?.status === 410 || error?.code === "login_expired") {
        state.qrLogin.status = "expired";
        state.qrLogin.message = "";
      } else if ((!error?.status || error?.code === "login_busy" || error?.code === "network_error") && state.qrLogin.failures < 3) {
        state.qrLogin.failures += 1;
        state.qrLogin.message = "连接不稳定，正在重试";
        renderQrLogin();
        scheduleQrLoginPoll(generation, Math.min(6000, QR_LOGIN_POLL_MS * (2 ** state.qrLogin.failures)));
        return;
      } else {
        state.qrLogin.status = "error";
        state.qrLogin.message = cookieErrorMessage(error);
        reflectCookieError(error);
      }
      renderQrLogin();
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
    try {
      await ensureConnectionAccount();
    } catch (error) {
      showToast(error.message || "无法创建新的店铺连接", "error");
      return;
    }
    const accountKey = String(state.activeAccountKey || "default");
    await cancelQrLogin(true, false);
    openQrLoginDialog();
    state.qrLogin.accountKey = accountKey;
    state.qrLogin.status = "starting";
    state.qrLogin.message = "";
    const generation = state.qrLogin.generation;
    renderQrLogin();
    try {
      const result = await api("/api/bot/login/start", qrLoginRequestOptions(accountKey, { method: "POST" }));
      const loginId = typeof result?.login_id === "string" ? result.login_id.trim() : "";
      const validLoginId = /^[A-Za-z0-9_-]{16,128}$/.test(loginId);
      if (state.qrLogin.generation !== generation) {
        if (validLoginId) await cancelQrLoginId(loginId, accountKey);
        return;
      }
      if (!loginResponseIsSafe(result)) {
        if (validLoginId) await cancelQrLoginId(loginId, accountKey);
        throw new ApiError("登录响应包含了不安全数据，已中止连接");
      }
      if (!validLoginId) throw new ApiError("登录会话无效，请重试");
      state.qrLogin.loginId = loginId;
      state.qrLogin.status = String(result?.status || "waiting").toLowerCase() === "scanned" ? "scanned" : "waiting";
      await loadQrLoginImage(generation);
      renderQrLogin();
      scheduleQrLoginPoll(generation);
    } catch (error) {
      if (state.qrLogin.generation !== generation) return;
      state.qrLogin.status = "error";
      state.qrLogin.message = error.message || "登录服务暂时不可用，请稍后重试";
      renderQrLogin();
    }
  }

  async function loadAuthCapabilities() {
    try {
      const capabilities = await api("/api/auth/capabilities");
      state.registrationAllowed = capabilities?.registration_enabled === true;
      state.bootstrapAvailable = capabilities?.bootstrap_available === true;
      state.passwordMinLength = Math.max(12, Number(capabilities?.password_min_length || 12));
    } catch (_error) {
      state.registrationAllowed = false;
      state.bootstrapAvailable = false;
      state.passwordMinLength = 12;
    }
    const registerTab = $("#registerTab");
    const bootstrapTab = $("#bootstrapTab");
    if (registerTab) registerTab.hidden = !state.registrationAllowed;
    if (bootstrapTab) bootstrapTab.hidden = !state.bootstrapAvailable;
    $("#authPassword")?.setAttribute("minlength", String(state.passwordMinLength));
    $("#newPasswordInput")?.setAttribute("minlength", String(state.passwordMinLength));
    if (
      (state.authMode === "register" && !state.registrationAllowed)
      || (state.authMode === "bootstrap" && !state.bootstrapAvailable)
    ) setAuthMode("login");
  }

  function setAuthMode(mode) {
    const register = mode === "register" && state.registrationAllowed;
    const firstAdmin = mode === "bootstrap" && state.bootstrapAvailable;
    state.authMode = register ? "register" : firstAdmin ? "bootstrap" : "login";
    $("#loginTab").setAttribute("aria-selected", String(state.authMode === "login"));
    $("#registerTab").setAttribute("aria-selected", String(state.authMode === "register"));
    $("#bootstrapTab").setAttribute("aria-selected", String(state.authMode === "bootstrap"));
    const tokenField = $("#bootstrapTokenField");
    if (tokenField) tokenField.hidden = state.authMode !== "bootstrap";
    const tokenInput = $("#bootstrapToken");
    if (tokenInput) {
      tokenInput.required = state.authMode === "bootstrap";
      if (state.authMode !== "bootstrap") tokenInput.value = "";
    }
    text(
      "#authTitle",
      state.authMode === "bootstrap" ? "创建首个管理员账号" : register ? "创建工作台账号" : "登录工作台",
    );
    text(
      "#authDescription",
      state.authMode === "bootstrap"
        ? "仅限受信任初始化入口；令牌成功使用后立即失效。"
        : register ? "注册后连接你的闲鱼店铺。" : "连接店铺后，商品会自动整理。",
    );
    text("#authSubmit span", state.authMode === "bootstrap" ? "完成初始化" : register ? "创建账号" : "登录");
    $("#authPassword").setAttribute("autocomplete", state.authMode === "login" ? "current-password" : "new-password");
    formMessage("#authError", "");
  }

  async function submitAuth(event) {
    event.preventDefault();
    const username = $("#authUsername").value.trim();
    const password = $("#authPassword").value;
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/.test(username)) {
      formMessage("#authError", "账号需为 3 至 32 位字母、数字、点、下划线或短横线");
      return;
    }
    if (password.length < state.passwordMinLength || password.length > 1024) {
      formMessage("#authError", "密码长度需要在 " + state.passwordMinLength + " 至 1024 位之间");
      return;
    }
    const button = $("#authSubmit");
    setBusy(button, true);
    try {
      if (state.authMode === "register") {
        await api("/api/auth/register", { method: "POST", body: JSON.stringify({ username, password }) });
        setAuthMode("login");
        $("#authPassword").value = "";
        formMessage("#authError", "注册成功，请登录", true);
      } else if (state.authMode === "bootstrap") {
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
        $("#bootstrapToken").value = "";
        $("#authPassword").value = "";
        await loadAuthCapabilities();
        setAuthMode("login");
        formMessage("#authError", "管理员账号已创建，请登录", true);
      } else {
        await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
        await bootstrap();
      }
    } catch (error) {
      formMessage("#authError", error.message || "操作失败，请稍后重试");
    } finally {
      setBusy(button, false);
    }
  }

  function clearSession(showMessage = true) {
    resetQrLogin();
    stopMerchantPolling();
    ["xianyuLoginDialog", "quickRepliesDialog", "batchDeliveryDialog", "templateEditorDialog", "cardsEditorDialog", "confirmDialog"].forEach(closeDialog);
    state.confirmAction = null;
    state.view = "home";
    state.me = null;
    state.accounts = [];
    state.activeAccountKey = "";
    state.shopAccountsPage = 1;
    state.accountEpoch += 1;
    state.messageLoadGeneration += 1;
    state.messageSelectionInFlight = false;
    resetManualReplyContext();
    resetConversationCommands();
    state.config = null;
    state.bot = null;
    state.automation = { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true };
    resetAiState();
    resetReplyRuleForm();
    resetAutomationMutations();
    state.attention = [];
    state.summary = null;
    state.analytics = null;
    state.analyticsPage = null;
    state.analyticsPeriod = 1;
    state.products = [];
    state.productsAccountKey = "";
    state.productsLoad = null;
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    state.catalogStatus = null;
    state.batchDelivery = { enabled: true, previewToken: "", preview: null, generation: Number(state.batchDelivery?.generation || 0) + 1 };
    resetAccountInboxState();
    state.orders = [];
    state.templates = [];
    state.cards = null;
    state.cardsAccountKey = "";
    state.cardsLoad = null;
    state.templateEditorOpenGeneration += 1;
    state.templateEditor = { editingId: "", productIds: [] };
    state.cardsEditor = { editingId: "", mode: "import" };
    state.docs = {
      tab: "guide", version: null, update: null, settings: null, users: [], audit: [],
      availableVersion: "", stagedVersion: "", loading: false,
    };
    ["templateEditorForm", "cardsEditorForm", "cardsCreateForm", "batchDeliveryForm", "passwordChangeForm", "platformSettingsForm"].forEach((id) => $("#" + id)?.reset());
    [
      "homeStatCards", "homeProductGrid", "homeOrderList", "attentionList", "shopAccountsPanelList",
      "productGrid", "conversationItems", "chatMessages", "orderList", "replyRuleList",
      "templateGrid", "cardsStats", "cardsList", "analyticsCards", "analyticsChart",
    ].forEach((id) => {
      const node = $("#" + id);
      if (node) node.innerHTML = "";
    });
    ["automationFirstReply", "automationFallbackReply", "batchDeliveryMaterial", "manualReplyInput"].forEach((id) => {
      const node = $("#" + id);
      if (node) node.value = "";
    });
    $("#authUsername").value = "";
    $("#authPassword").value = "";
    if ($("#bootstrapToken")) $("#bootstrapToken").value = "";
    $("#workspace").hidden = true;
    $("#authScreen").hidden = false;
    if (showMessage) showToast("已退出登录");
  }

  async function logout() {
    await cancelQrLogin(true, true);
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (error) {
      // The session may already have expired.
    }
    clearSession(true);
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
    if (!confirmDiscardAiChanges("店铺")) return;
    resetManualReplyContext();
    state.activeAccountKey = key;
    state.accountEpoch += 1;
    const context = captureAccountContext();
    state.messageLoadGeneration += 1;
    state.messageSelectionInFlight = false;
    resetConversationCommands();
    resetAutomationMutations();
    persistAccountKey(key);
    resetQrLogin();
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
    state.analyticsPage = null;
    state.products = [];
    state.productsAccountKey = "";
    state.productsLoad = null;
    state.productsTruncated = null;
    state.productsTruncatedAccountKey = "";
    state.catalogStatus = null;
    state.batchDelivery = { enabled: true, previewToken: "", preview: null, generation: Number(state.batchDelivery?.generation || 0) + 1 };
    resetAccountInboxState({ restorePreferences: true });
    state.orders = [];
    state.templates = [];
    state.cards = null;
    state.cardsAccountKey = "";
    state.cardsLoad = null;
    state.templateEditorOpenGeneration += 1;
    state.templateEditor = { editingId: "", productIds: [] };
    state.cardsEditor = { editingId: "", mode: "import" };
    const returnView = state.view === "shops" ? "shops" : "home";
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
    state.confirmAction = async () => {
      const wasActive = state.activeAccountKey === account.key;
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
          resetManualReplyContext();
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
    };
    const dialog = $("#confirmDialog");
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
  }

  function renderNav() {
    const items = [
      { view: "home", label: "运营概览", icon: "layout-dashboard" },
      { view: "chat", label: "智能客服", icon: "message-square-text" },
      { view: "goods", label: "履约中心", icon: "box" },
      { view: "orders", label: "订单管理", icon: "package-check" },
    ];
    $("#sideNav").innerHTML = items.map((item) => (
      '<button type="button" class="side-nav-item" data-view="' + item.view + '" aria-label="' + item.label + '" title="' + item.label + '">' +
      '<svg class="icon"><use href="' + ICONS + item.icon + '"></use></svg>' +
      '<span class="side-nav-tooltip">' + item.label + "</span></button>"
    )).join("");
  }

  function renderAccount() {
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
    const statusClass = connected && !view.restricted ? "badge-green" : COOKIE_BLOCKING_CODES.has(syncStatus) || view.restricted ? "badge-red" : "badge-muted";
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
      notice.className = "cookie-status-notice" + (syncStatus === "risk_control" || syncStatus === "risk_cooldown" || view.restricted ? " is-risk" : " is-expired");
      text("#cookieStatusTitle", cookieStatus.label);
      text("#cookieStatusMessage", cookieStatus.message);
      text("#cookieStatusAction", cookieStatus.action);
      const checkButton = $("#checkCookieButton");
      if (checkButton) checkButton.disabled = !bot.cookies_set || syncStatus === "risk_cooldown";
    }
    renderAccountSwitcher();
  }

  function renderProducts() {
    const grid = $("#productGrid");
    const empty = $("#productsEmpty");
    const notice = $("#productsNotice");
    const view = shopStateView(state.bot || {});
    const deliveryById = new Map((state.automation?.deliveries || []).map((item) => [String(item.item_id), item]));
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
        const delivery = deliveryById.get(String(product.id || ""));
        const configured = Boolean(delivery) && Boolean(String(delivery.material || "").trim());
        const active = configured && delivery.enabled !== false;
        return '<a class="home-product-card" href="#" data-view="goods" aria-label="查看商品：' + esc(product.title || "未命名商品") + '">' +
          productThumb(product, "home") +
          '<strong class="home-product-name">' + esc(product.title || "未命名商品") + "</strong>" +
          '<span class="home-product-price">' + esc(product.price_display || "价格待同步") + "</span>" +
          '<span class="badge ' + (active ? "badge-green" : "badge-muted") + '">' + (active ? "已设置资料" : "未设置") + "</span></a>";
      }).join("") : '<div class="automation-empty">还没有商品，连接店铺后自动整理。</div>';
    }
    if (!state.products.length) {
      grid.innerHTML = "";
      empty.hidden = false;
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
    empty.hidden = true;
    setProductsNotice(view.restricted
      ? "账号已连接，当前商品来自上次成功检测；发布相关操作受到闲鱼限制。"
      : view.catalog === "stale"
        ? "当前显示上次成功整理的商品，新的检测暂未完成。"
        : "");
    grid.innerHTML = state.products.map((product) => {
      const itemId = String(product.id || "");
      const delivery = deliveryById.get(itemId);
      const configured = Boolean(delivery) && Boolean(String(delivery.material || "").trim());
      const paused = configured && delivery.enabled === false;
      const badge = configured
        ? (paused ? '<span class="badge badge-amber">资料已暂停</span>' : '<span class="badge badge-green">已设置资料</span>')
        : '<span class="badge badge-muted">未设置资料</span>';
      const actions = '<button class="button button-secondary button-compact" type="button" data-edit-delivery data-item-id="' + esc(itemId) + '" aria-label="编辑' + esc(product.title || "未命名商品") + '的资料"><span>编辑资料</span></button>' +
        (configured ? '<button class="button button-secondary button-compact" type="button" data-delivery-toggle="' + esc(itemId) + '" aria-label="' + (paused ? "恢复" : "暂停") + esc(product.title || "未命名商品") + '的资料"><span>' + (paused ? "恢复资料" : "暂停资料") + "</span></button>" : "");
      return '<div class="product-row">' +
        '<div class="product-cell product-cell-main">' + productThumb(product, "product") + '<div><strong class="product-title">' + esc(product.title || "未命名商品") + '</strong><small class="product-desc">' + esc(product.description || "暂无商品简介") + "</small></div></div>" +
        '<span class="product-price">' + esc(product.price_display || "价格待同步") + "</span>" +
        badge +
        '<div class="product-actions">' + actions + "</div>" +
        "</div>";
    }).join("");
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
    return {
      title: String(item?.title || "需要处理"),
      message: String(item?.message || "当前店铺有一项真实运行状态需要确认。"),
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
      list.innerHTML = '<div class="attention-empty"><svg class="icon"><use href="' + ICONS + 'circle-check"></use></svg><span>当前没有需要处理的事项</span></div>';
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
    const rules = Array.isArray(state.automation?.rules) ? state.automation.rules : [];
    const deliveries = Array.isArray(state.automation?.deliveries) ? state.automation.deliveries : [];
    const configuredDeliveries = deliveries.filter((item) => Boolean(String(item.material || "").trim()));
    const connected = shopStateView(state.bot || {}).connection === "connected";
    const activeAnalytics = state.analyticsPage || state.analytics;
    const totals = activeAnalytics?.totals && typeof activeAnalytics.totals === "object"
      ? activeAnalytics.totals
      : null;
    if (totals) {
      const buyerMessages = Number(totals.buyer_messages_total ?? totals.messages_total ?? 0);
      const autoReplies = Number(totals.auto_replies_total || 0);
      const replyRate = buyerMessages > 0 ? Math.min(100, Math.round((autoReplies / buyerMessages) * 1000) / 10) : 0;
      const periodLabel = Number(state.analyticsPeriod || 1) === 1 ? "今日已接收" : "当前周期已接收";
      host.innerHTML =
        statCard("买家咨询总数", buyerMessages, "message-square-text", "tone-yellow", periodLabel) +
        statCard("自动回复率", replyRate + "%", "zap", "tone-blue", "规则与 AI 回复 " + autoReplies + " 次") +
        statCard("履约自动发送", Number(totals.fulfillment_success_total || 0) + " 笔", "package-check", "tone-green", "订单核验后执行") +
        statCard("异常与待办", String(attentionPendingTotal() + Number(totals.fulfillment_failed_total || 0)) + " 项", "circle-alert", "tone-red", "需要人工关注");
      return;
    }
    host.innerHTML =
      statCard("在售商品", state.products.length ? String(state.products.length) : "0", "box", "tone-yellow", "当前店铺已识别") +
      statCard("关键词规则", String(rules.length), "settings", "tone-blue", rules.length ? "按列表顺序匹配" : "尚未配置") +
      statCard("已设资料商品", String(configuredDeliveries.length), "file-text", "tone-green", configuredDeliveries.length ? "付款后自动发送" : "尚未配置") +
      statCard("店铺状态", connected ? "已连接" : "未连接", connected ? "wifi" : "wifi-off", connected ? "tone-green" : "tone-amber", connected ? "可正常自动处理" : "先连接店铺");
  }

  function renderAnalytics() {
    const cards = $("#analyticsCards");
    const chart = $("#analyticsChart");
    const periodHost = $("#analyticsPeriod");
    if (!chart) return;
    if (periodHost) {
      $$("[data-period]", periodHost).forEach((button) => {
        const active = Number(button.dataset.period || 1) === Number(state.analyticsPeriod || 1);
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    }
    const totals = state.analyticsPage?.totals && typeof state.analyticsPage.totals === "object"
      ? state.analyticsPage.totals
      : null;
    if (cards) {
      if (totals) {
        cards.innerHTML =
          statCard("买家消息", Number(totals.buyer_messages_total ?? totals.messages_total ?? 0), "message-square-text", "tone-yellow") +
          statCard("自动回复", Number(totals.auto_replies_total || 0), "bot", "tone-blue") +
          statCard("发货成功", Number(totals.fulfillment_success_total || 0), "package-check", "tone-green") +
          statCard("发货失败", Number(totals.fulfillment_failed_total || 0), "circle-alert", "tone-red");
      } else {
        cards.innerHTML = '<div class="automation-empty">暂无统计数据</div>';
      }
    }
    renderHomeStats();
    const buckets = Array.isArray(state.analyticsPage?.buckets) ? state.analyticsPage.buckets : [];
    const nonEmpty = buckets.filter((bucket) => Number(bucket?.messages_total || 0) > 0);
    const emptyNote = chart.parentElement?.querySelector(".chart-empty") || null;
    if (!nonEmpty.length) {
      chart.innerHTML = "";
      if (emptyNote) emptyNote.hidden = false;
      return;
    }
    if (emptyNote) emptyNote.hidden = true;
    const peak = Math.max(...nonEmpty.map((bucket) => Number(bucket.messages_total || 0)));
    chart.innerHTML = buckets.map((bucket) => {
      const value = Number(bucket?.messages_total || 0);
      const height = peak > 0 ? Math.max(4, Math.round((value / peak) * 100)) : 0;
      const date = String(bucket?.date || "");
      const label = date.length >= 10 ? date.slice(5, 10).replace("-", "/") : date;
      return '<div class="chart-bar" title="' + esc(label + " · " + value + " 条消息") + '">' +
        '<span class="chart-bar-fill" style="height:' + height + '%"></span>' +
        '<span class="chart-bar-label">' + esc(label) + "</span></div>";
    }).join("");
  }

  async function loadAnalytics(period = state.analyticsPeriod) {
    const context = captureAccountContext();
    state.analyticsPeriod = Number(period) === 7 || Number(period) === 30 ? Number(period) : 1;
    const data = await api("/api/bot/analytics?period=" + state.analyticsPeriod);
    if (!accountContextMatches(context)) return;
    state.analyticsPage = data || null;
    renderAnalytics();
  }

  function renderOverview() {
    renderShopStatus();
    renderAttention();
    renderHomeStats();
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

  function messageMediaMarkup(value, fallbackType = "", content = "") {
    return normaliseMessageMedia(value, fallbackType, content).map((item) => {
      const label = item.label || messageMediaTypeLabel(item.type);
      if (item.type === "image" && item.url) {
        return '<a class="message-media-image" href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer"><img src="' + esc(item.url) + '" alt="' + esc(item.alt || "图片") + '" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest(\'.message-media-image\').classList.add(\'is-broken\');this.remove();"><span>' + esc(label) + '</span></a>';
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
    const file = state.manualReply.file;
    const media = state.manualReply.media;
    const hasAttachment = Boolean(file || media);
    const name = file ? manualImageFileName(file) : String(media?.name || media?.label || "图片");
    const fileName = $("#manualReplyFileName");
    const fileMeta = $("#manualReplyFileMeta");
    const preview = $("#manualReplyPreview");
    const previewImage = $("#manualReplyPreviewImage");
    const hint = $("#manualReplyDropHint");
    const clear = $("#clearManualReplyFile");
    const dropzone = $("#manualReplyDropzone");
    if (fileName) fileName.textContent = hasAttachment ? "已选择：" + name : "";
    if (fileMeta) fileMeta.textContent = file ? "图片 · " + manualImageFileSize(file.size) : hasAttachment ? "图片已准备发送" : "";
    if (preview) preview.hidden = !file || !state.manualReply.previewUrl;
    if (previewImage) {
      if (file && state.manualReply.previewUrl) {
        previewImage.src = state.manualReply.previewUrl;
        previewImage.alt = name || "待发送图片";
      } else {
        previewImage.removeAttribute("src");
      }
    }
    if (hint) hint.hidden = hasAttachment;
    if (clear) clear.hidden = !hasAttachment;
    if (dropzone) dropzone.classList.toggle("has-attachment", hasAttachment);
    setManualReplyDragActive(state.manualReply.dragging);
  }

  function setManualReplyImage(file) {
    const selected = state.conversations.find((item) => String(item.chat_id) === String(state.selectedChatId));
    if (!selected || !conversationTakeover(selected)) {
      formMessage("#replyMessage", "请先人工接管当前对话再发送图片");
      return false;
    }
    if (!validateManualImageFile(file)) return false;
    revokeManualReplyPreview();
    state.manualReply.request = null;
    state.manualReply.file = file;
    state.manualReply.media = null;
    state.manualReply.attachmentKey = manualImageAttachmentKey(file);
    state.manualReply.previewUrl = URL.createObjectURL(file);
    formMessage("#replyMessage", "图片已加入待发送回复，点击发送后上传", true);
    renderChat();
    return true;
  }

  function chooseManualReplyImage(files) {
    const selectedFiles = Array.from(files || []).filter(Boolean);
    if (!selectedFiles.length) return false;
    if (selectedFiles.length > 1) {
      formMessage("#replyMessage", "目前一次只能发送一张图片");
      return false;
    }
    return setManualReplyImage(selectedFiles[0]);
  }

  function handleManualReplyFileSelection(event) {
    const files = Array.from(event.currentTarget.files || []);
    event.currentTarget.value = "";
    chooseManualReplyImage(files);
  }

  async function uploadManualReplyFile(file, chatId) {
    const result = await api("/api/bot/messages/image?chat_id=" + encodeURIComponent(chatId), {
      method: "POST",
      headers: {
        "Content-Type": file.type,
        "X-File-Name": manualImageFileName(file),
      },
      body: file,
    });
    const media = result?.media;
    if (!media || media.type !== "image" || !media.path) {
      throw new ApiError("图片上传结果无效，请重试");
    }
    return media;
  }

  function clearManualReplyImage() {
    if (state.manualReply.uploading) return;
    state.manualReply.request = null;
    state.manualReply.file = null;
    state.manualReply.media = null;
    state.manualReply.attachmentKey = "";
    revokeManualReplyPreview();
    const file = $("#manualReplyFile");
    if (file) file.value = "";
    formMessage("#replyMessage", "");
    renderManualReplyAttachment();
    renderChat();
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
    chooseManualReplyImage(files);
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
    chooseManualReplyImage(event.dataTransfer.files);
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
    input.disabled = !hasSelection || state.manualReply.uploading;
    submit.disabled = !hasSelection || !selectedTakeover || state.manualReply.submitting || state.manualReply.uploading;
    if (upload) upload.disabled = !hasSelection || !selectedTakeover || state.manualReply.uploading;
    if (uploadButton) uploadButton.classList.toggle("is-disabled", !hasSelection || !selectedTakeover || state.manualReply.uploading);
    if (dropzone) dropzone.classList.toggle("is-disabled", !hasSelection || !selectedTakeover || state.manualReply.uploading);
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
      const status = String(item.delivery_status || item.status || "");
      const statusLabels = {
        draft: "未发送",
        queued: "等待发送",
        sending: "正在发送",
        retry: "正在重试",
        acknowledged: "闲鱼已接收",
        manual_review: "未发送，需重新处理",
        failed: "未发送",
        unknown: "状态待确认",
      };
      const statusText = manual ? (statusLabels[status] || (item.role === "assistant_manual_draft" ? "未发送" : "")) : "";
      const content = messageContentText(item);
      const mediaMarkup = messageMediaMarkup(item.media, item.content_type, item.content);
      const contentMarkup = content ? '<div class="message-text">' + esc(content) + '</div>' : '';
      const matched = state.messageSearch && item.matched === true;
      return '<div class="message-row ' + (buyer ? "is-buyer" : "is-seller") + '"><span class="message-role">' + role + '</span><div class="message-bubble' + (matched ? " is-matched" : "") + '">' + contentMarkup + mediaMarkup + '</div><time>' + esc(formatDate(item.time)) + '</time>' + (statusText ? '<span class="message-status' + (status === "manual_review" || status === "failed" ? " is-error" : status === "acknowledged" ? " is-success" : "") + '">' + esc(statusText) + '</span>' : '') + "</div>";
    }).join("");
    if (!options.preserveScroll || nearBottom) area.scrollTop = area.scrollHeight;
    if (last && !selected) text("#chatItemName", productTitle(last.item_id));
  }

  function orderRowMarkup(order) {
    const status = String(order.status || "queued");
    const labels = { delivered: "已完成", manual_review: "待人工", retry: "待重试", failed: "发送失败", queued: "处理中" };
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

  function renderOrders() {
    const list = $("#orderList");
    if (!state.orders.length) {
      list.innerHTML = "";
      $("#ordersEmpty").hidden = false;
    } else {
      $("#ordersEmpty").hidden = true;
      list.innerHTML = state.orders.map(orderRowMarkup).join("");
    }
    const homePanel = $("#homeOrdersPanel");
    const homeList = $("#homeOrderList");
    if (homePanel && homeList) {
      const visible = state.orders.length > 0;
      homePanel.hidden = !visible;
      if (visible) homeList.innerHTML = state.orders.slice(0, 4).map(orderRowMarkup).join("");
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
    state.automation = data || { rules: [], deliveries: [], running: false, strategy: "standard", enabled: true };
    state.automationEditor = { type: "", index: -1 };
    renderAutomation();
    resetReplyRuleForm();
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
    const nextChatId = requestedChatId && hasConversation(requestedChatId)
      ? requestedChatId
      : currentChatId && hasConversation(currentChatId)
        ? currentChatId
        : String(state.conversations[0]?.chat_id || "");
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
    const context = captureAccountContext();
    const data = await api("/api/bot/orders?limit=200");
    if (!accountContextMatches(context)) return;
    state.orders = data.orders || [];
    renderOrders();
  }

  async function loadOverviewSignals(epoch = state.accountEpoch) {
    const context = captureAccountContext(epoch);
    const attentionResult = await api("/api/bot/attention").catch(() => ({ items: [] }));
    if (!accountContextMatches(context)) return;
    const [summary, analytics] = await Promise.all([
      api("/api/bot/summary").catch(() => null),
      api("/api/bot/analytics?period=1").catch(() => null),
    ]);
    if (!accountContextMatches(context)) return;
    state.attention = Array.isArray(attentionResult?.items) ? attentionResult.items : [];
    state.summary = summary;
    state.analytics = analytics;
    if (Number(state.analyticsPeriod || 1) === 1) state.analyticsPage = analytics;
    renderAttention();
    renderAnalytics();
  }

  function isPlatformAdmin() {
    return state.me?.is_admin === true || String(state.me?.role || "") === "admin";
  }

  function setDocsTab(tab) {
    const admin = isPlatformAdmin();
    const allowed = new Set(["guide", "version", ...(admin ? ["accounts", "audit"] : [])]);
    state.docs.tab = allowed.has(tab) ? tab : "guide";
    $$('[data-docs-tab]').forEach((button) => {
      const selected = button.dataset.docsTab === state.docs.tab;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    $$('[data-docs-panel]').forEach((panel) => {
      const adminOnly = panel.hasAttribute("data-admin-only");
      panel.hidden = panel.dataset.docsPanel !== state.docs.tab || (adminOnly && !admin);
    });
    if (state.docs.tab !== "guide") void loadDocsData();
  }

  function renderVersionPanel() {
    const version = state.docs.version || {};
    const update = state.docs.update || {};
    const latest = update.latest_update || version.latest_update || null;
    text("#currentVersionValue", version.version ? "v" + version.version : "--");
    const buildParts = [];
    if (version.commit) buildParts.push(String(version.commit));
    if (version.build_time) buildParts.push(String(version.build_time));
    text("#currentBuildValue", buildParts.join(" · ") || "开发构建");
    text("#currentAssetVersionValue", version.asset_version || "--");
    text("#currentUpdateChannelValue", version.update_channel === "beta" ? "测试版" : version.update_channel === "stable" ? "稳定版" : "--");
    text("#currentUpdateStatusValue", latest?.status ? "更新状态：" + latest.status : "尚未检查");
    const notes = latest?.release_notes || version.release_notes || "暂无本地更新说明";
    text("#versionReleaseNotes", notes);

    const availableVersion = state.docs.availableVersion
      || (latest?.status === "available" ? String(latest.version || "") : "");
    const stagedVersion = state.docs.stagedVersion
      || (["staged", "apply_requested", "preparing", "stopping", "migrating", "switching", "verifying"].includes(String(latest?.status || ""))
        ? String(latest.version || "") : "");
    state.docs.availableVersion = availableVersion;
    state.docs.stagedVersion = stagedVersion;
    const download = $("#downloadUpdateButton");
    const apply = $("#applyUpdateButton");
    if (download) {
      download.disabled = !availableVersion;
      text(download, availableVersion ? "下载并校验 v" + availableVersion : "下载并校验");
    }
    if (apply) {
      apply.disabled = !stagedVersion;
      text(apply, stagedVersion ? "应用 v" + stagedVersion : "应用已校验版本");
    }
    const rollbackSelect = $("#rollbackVersionSelect");
    if (rollbackSelect) {
      const selected = rollbackSelect.value;
      rollbackSelect.replaceChildren();
      const versions = Array.isArray(update.rollback_versions) ? update.rollback_versions : [];
      if (!versions.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "没有可用版本";
        rollbackSelect.append(option);
      } else {
        versions.forEach((item) => {
          const option = document.createElement("option");
          option.value = String(item);
          option.textContent = "v" + String(item);
          rollbackSelect.append(option);
        });
        if (versions.includes(selected)) rollbackSelect.value = selected;
      }
      $("#rollbackUpdateButton").disabled = !rollbackSelect.value;
    }
  }

  function renderPlatformSettings() {
    const settings = state.docs.settings || null;
    if (!settings) return;
    const registration = settings.registration || {};
    const toggle = $("#registrationOpenToggle");
    if (toggle) {
      toggle.checked = registration.database_open === true;
      toggle.disabled = registration.environment_allowed !== true;
    }
    const channel = $("#updateChannelSelect");
    if (channel) channel.value = settings.update_channel === "beta" ? "beta" : "stable";
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
    "platform.user_changed": "账号角色或状态已修改",
    "platform.user_unlocked": "账号登录锁已清除",
    "platform.sessions_revoked": "账号会话已撤销",
    "platform.update_checked": "已检查更新",
    "platform.update_downloaded": "更新已下载并校验",
    "platform.update_requested": "已请求应用更新",
    "platform.rollback_requested": "已请求回滚版本",
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

  function renderDocs() {
    const admin = isPlatformAdmin();
    $$('[data-admin-only]').forEach((node) => {
      if (!node.hasAttribute("data-docs-panel")) node.hidden = !admin;
    });
    if (!admin && ["accounts", "audit"].includes(state.docs.tab)) state.docs.tab = "guide";
    setDocsTab(state.docs.tab || "guide");
    renderVersionPanel();
    if (admin) {
      renderPlatformSettings();
      renderAdminUsers();
      renderAuditEvents();
    }
  }

  async function loadDocsData({ force = false } = {}) {
    if (!state.me || (state.docs.loading && !force)) return;
    state.docs.loading = true;
    try {
      state.docs.version = await api("/api/version");
      if (isPlatformAdmin()) {
        const [update, settings, users, audit] = await Promise.all([
          api("/api/admin/updates"),
          api("/api/admin/settings"),
          api("/api/admin/users?limit=100"),
          api("/api/admin/audit?limit=100"),
        ]);
        state.docs.update = update;
        state.docs.settings = settings;
        state.docs.users = users.users || [];
        state.docs.audit = audit.events || [];
      }
      renderDocs();
    } catch (error) {
      showToast(error.message || "项目信息读取失败", "error");
    } finally {
      state.docs.loading = false;
    }
  }

  async function changeCurrentPassword(event) {
    event.preventDefault();
    const form = event.currentTarget;
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
      form.reset();
      formMessage("#passwordChangeMessage", "密码已更新，其他会话已撤销", true);
    } catch (error) {
      formMessage("#passwordChangeMessage", error.message || "密码更新失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function savePlatformSettings(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    setBusy(button, true);
    try {
      state.docs.settings = await api("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          registration_open: $("#registrationOpenToggle").checked,
          update_channel: $("#updateChannelSelect").value,
        }),
      });
      formMessage("#platformSettingsMessage", "平台设置已保存", true);
      await loadDocsData({ force: true });
    } catch (error) {
      formMessage("#platformSettingsMessage", error.message || "平台设置保存失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function checkPlatformUpdate() {
    const button = $("#checkUpdateButton");
    setBusy(button, true);
    formMessage("#updateActionMessage", "正在检查固定 Release 来源");
    try {
      const result = await api("/api/admin/updates/check", { method: "POST" });
      state.docs.availableVersion = result.available ? String(result.version || "") : "";
      formMessage(
        "#updateActionMessage",
        result.available ? "发现新版本 v" + result.version : "当前已是最新版本",
        true,
      );
      await loadDocsData({ force: true });
    } catch (error) {
      formMessage("#updateActionMessage", error.message || "更新检查失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function downloadPlatformUpdate() {
    const version = state.docs.availableVersion;
    if (!version) return;
    const button = $("#downloadUpdateButton");
    setBusy(button, true);
    formMessage("#updateActionMessage", "正在下载并校验签名、清单和文件哈希");
    try {
      const result = await api("/api/admin/updates/download", {
        method: "POST",
        body: JSON.stringify({ version }),
      });
      state.docs.stagedVersion = String(result.version || version);
      formMessage("#updateActionMessage", "v" + state.docs.stagedVersion + " 已校验，可申请应用", true);
      await loadDocsData({ force: true });
    } catch (error) {
      formMessage("#updateActionMessage", error.message || "更新下载失败");
    } finally {
      setBusy(button, false);
    }
  }

  async function confirmAdminUpdate(action, version) {
    const password = $("#updateAdminPassword").value;
    if (!password) {
      formMessage("#updateActionMessage", "请先输入当前管理员密码");
      return;
    }
    const actionName = action === "apply" ? "update.apply" : "update.rollback";
    const confirmation = await api("/api/admin/confirm", {
      method: "POST",
      body: JSON.stringify({ password, action: actionName }),
    });
    const endpoint = action === "apply" ? "/api/admin/updates/apply" : "/api/admin/updates/rollback";
    await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ version, confirmation_token: confirmation.confirmation_token }),
    });
    $("#updateAdminPassword").value = "";
    formMessage(
      "#updateActionMessage",
      action === "apply" ? "更新请求已提交，独立 updater 将安全切换版本" : "回滚请求已提交",
      true,
    );
    await loadDocsData({ force: true });
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
    renderDocs();
    renderOverview();
    await Promise.all([loadProducts({ force: true, catalogStatus }), loadAutomation().catch(() => {}), loadAiConfig().catch(() => {}), loadOverviewSignals(epoch)]);
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
    if (view === "shops") {
      renderAccountSwitcher();
      loadAccounts().catch((error) => {
        if (error?.status !== 404) showToast(error.message || "店铺列表暂时无法读取", "error");
      });
    }
    if (view === "goods") loadProducts({ force: true }).catch((error) => showToast(error.message, "error"));
    if (view === "auto-reply") loadAutomation().catch((error) => showToast(error.message, "error"));
    if (view === "ai-config") loadAiConfig().catch((error) => showToast(error.message || "AI 配置读取失败", "error"));
    if (view === "chat") {
      loadMessages().catch((error) => showToast(error.message, "error"));
    }
    if (view === "orders") loadOrders().catch((error) => showToast(error.message, "error"));
    if (view === "docs") loadDocsData().catch((error) => showToast(error.message || "项目信息读取失败", "error"));
    if (view === "templates") {
      loadTemplates().catch((error) => showToast(error.message, "error"));
      loadCards().catch((error) => showToast(error.message || "卡密池加载失败，请稍后重试", "error"));
    }
    if (view === "cards") loadCards({ force: true }).catch((error) => showToast(error.message, "error"));
    if (view === "analytics") loadAnalytics().catch((error) => showToast(error.message, "error"));
    setSidebarOpen(false);
    syncMerchantPolling();
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
    renderDocs();
    renderOverview();
    await loadProducts({ force: true, catalogStatus });
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
    syncMerchantPolling();
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
        label: {
          risk_control: "需要安全验证",
          risk_cooldown: "安全验证冷却中",
          cookie_expired: "登录已失效",
          cookie_invalid: "需要重新登录",
          cookie_incomplete: "需要重新登录",
    account_restricted: "部分能力受限",

        }[code] || "需要处理",
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
    if (state.manualReply.submitting || state.manualReply.uploading) return;
    const input = $("#manualReplyInput");
    const content = input.value.trim();
    const hasAttachment = Boolean(state.manualReply.file || state.manualReply.media);
    if (!content && !hasAttachment) {
      formMessage("#replyMessage", "请输入回复内容或选择一张图片");
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
    const existingRequest = state.manualReply.request;
    const attachmentKey = state.manualReply.attachmentKey
      || String(state.manualReply.media?.path || state.manualReply.media?.url || state.manualReply.media?.name || "");
    const requestId = existingRequest?.generation === generation
      && existingRequest?.chatId === chatId
      && existingRequest?.content === content
      && existingRequest?.mediaKey === attachmentKey
      ? existingRequest.id
      : newClientRequestId();
    state.manualReply.request = { id: requestId, chatId, content, mediaKey: attachmentKey, generation };
    const button = event.submitter;
    state.manualReply.submitting = true;
    setBusy(button, true);
    renderChat();
    try {
      let media = state.manualReply.media ? [state.manualReply.media] : [];
      if (!media.length && state.manualReply.file) {
        state.manualReply.uploading = true;
        renderChat();
        const uploaded = await uploadManualReplyFile(state.manualReply.file, chatId);
        if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
        state.manualReply.media = uploaded;
        media = [uploaded];
        state.manualReply.uploading = false;
        renderChat();
      }
      if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
      const result = await api("/api/bot/messages/reply", {
        method: "POST",
        headers: { "Idempotency-Key": requestId },
        body: JSON.stringify({ content, chat_id: chatId, media }),
      });
      if (!manualReplyContextMatches(chatId, epoch, accountKey, generation)) return;
      mergeManualReplyMessage(result.message);
      if (input.value.trim() === content) input.value = "";
      if (state.manualReply.request?.id === requestId) state.manualReply.request = null;
      if (state.manualReply.attachmentKey === attachmentKey) {
        state.manualReply.file = null;
        state.manualReply.media = null;
        state.manualReply.attachmentKey = "";
        revokeManualReplyPreview();
        const file = $("#manualReplyFile");
        if (file) file.value = "";
      }
      formMessage("#replyMessage", result?.reply?.status === "acknowledged" ? "闲鱼已接收这条回复" : "回复已排队，等待闲鱼确认", true);
      renderChat();
      void pollManualReply(requestId, chatId, epoch, accountKey, generation);
    } catch (error) {
      if (manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
        formMessage("#replyMessage", error.message || "回复发送失败，请稍后重试");
      }
    } finally {
      if (manualReplyContextMatches(chatId, epoch, accountKey, generation)) {
        state.manualReply.submitting = false;
        state.manualReply.uploading = false;
        setBusy(button, false);
        renderChat();
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
    renderChat();
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
        if (message) {
          message.delivery_status = reply.status;
          message.status = reply.status;
          message.attempts = reply.attempts;
          renderChat();
        }
        if (!ACTIVE_MANUAL_REPLY_STATUSES.has(String(reply.status || ""))) {
          formMessage(
            "#replyMessage",
            reply.status === "acknowledged" ? "闲鱼已接收这条回复" : "这条回复未发送，需要重新处理",
            reply.status === "acknowledged",
          );
          return;
        }
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

  function closeDialog(id) {
    const dialog = $("#" + id);
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function bindEvents() {
    $("#loginTab").addEventListener("click", () => setAuthMode("login"));
    $("#registerTab").addEventListener("click", () => setAuthMode("register"));
    $("#bootstrapTab").addEventListener("click", () => setAuthMode("bootstrap"));
    $("#authForm").addEventListener("submit", submitAuth);
    $("#passwordChangeForm").addEventListener("submit", changeCurrentPassword);
    $("#platformSettingsForm").addEventListener("submit", savePlatformSettings);
    $("#refreshVersionButton").addEventListener("click", () => loadDocsData({ force: true }));
    $("#refreshAdminUsers").addEventListener("click", () => loadDocsData({ force: true }));
    $("#refreshAuditButton").addEventListener("click", () => loadDocsData({ force: true }));
    $("#checkUpdateButton").addEventListener("click", checkPlatformUpdate);
    $("#downloadUpdateButton").addEventListener("click", downloadPlatformUpdate);
    $("#applyUpdateButton").addEventListener("click", () => {
      const version = state.docs.stagedVersion;
      if (version) void confirmAdminUpdate("apply", version).catch((error) => formMessage("#updateActionMessage", error.message || "更新申请失败"));
    });
    $("#rollbackUpdateButton").addEventListener("click", () => {
      const version = $("#rollbackVersionSelect").value;
      if (version) void confirmAdminUpdate("rollback", version).catch((error) => formMessage("#updateActionMessage", error.message || "回滚申请失败"));
    });
    $("#rollbackVersionSelect").addEventListener("change", (event) => {
      $("#rollbackUpdateButton").disabled = !event.currentTarget.value;
    });
    $$('[data-docs-tab]').forEach((button) => button.addEventListener("click", () => setDocsTab(button.dataset.docsTab)));
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
      if (state.qrLogin.status === "sync_error" && state.qrLogin.loginId) {
        void completeQrLogin(state.qrLogin.generation);
      } else {
        void startXianyuLogin();
      }
    });
    $("#closeXianyuLogin").addEventListener("click", () => { void cancelQrLogin(true, true); });
    $("#xianyuLoginDialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      void cancelQrLogin(true, true);
    });
    $("#xianyuQrImage").addEventListener("load", (event) => {
      if (!state.qrLogin.loginId) return;
      event.currentTarget.hidden = false;
      $("#qrLoginPlaceholder").hidden = true;
    });
    $("#xianyuQrImage").addEventListener("error", () => {
      if (!state.qrLogin.loginId) return;
      clearQrLoginPoll();
      state.qrLogin.status = "error";
      state.qrLogin.message = "二维码加载失败，请刷新后重试";
      renderQrLogin();
    });
    $("#refreshProducts").addEventListener("click", syncShop);
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
      void loadAnalytics(Number(button.dataset.period || 1)).catch((error) => showToast(error.message, "error"));
    }));
    document.addEventListener("visibilitychange", syncMerchantPolling);
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
    $("#refreshOrders").addEventListener("click", () => loadOrders().catch((error) => showToast(error.message, "error")));
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
    $("#clearManualReplyFile").addEventListener("click", clearManualReplyImage);
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
        || "闲";
      const monogramClass = host.classList.contains("home-product-thumb") ? "home-product-monogram" : "product-monogram";
      host.innerHTML = '<span class="' + monogramClass + '" aria-hidden="true">' + esc(String(title).trim().slice(0, 1) || "闲") + "</span>";
    }, true);
  }

  async function init() {
    bindEvents();
    syncSidebarAccessibility();
    installProductImageFallback();
    await loadAuthCapabilities();
    try {
      await bootstrap();
    } catch (error) {
      clearSession(false);
    }
  }

  window.addEventListener("DOMContentLoaded", init);
})();

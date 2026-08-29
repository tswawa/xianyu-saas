(() => {
  "use strict";

  const PAGE_SOURCE = "deepwhale-xianyu-saas";
  const EXTENSION_SOURCE = "deepwhale-xianyu-connector";
  const PROTOCOL = 1;
  const SAAS_ORIGIN = "https://deepwhale.chat";
  const SAAS_PATH = "/xianyu-saas/";
  const GOOFISH_URL = "https://www.goofish.com/login?redirectURL=https%3A%2F%2Fwww.goofish.com%2F";
  const MTOP_COOKIE_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/";
  const CONNECTOR_COOKIE_URL = "https://deepwhale.chat/xianyu-saas/api/bot/connector/cookies";

  const START = "DW_XIANYU_CONNECT_START";
  const CANCEL = "DW_XIANYU_CONNECT_CANCEL";
  const POLL = "DW_XIANYU_CONNECT_POLL";
  const HEARTBEAT = "DW_XIANYU_CONNECT_HEARTBEAT";
  const BACKGROUND_EVENT = "DW_XIANYU_CONNECT_BACKGROUND_EVENT";
  const ACK = "DW_XIANYU_CONNECT_ACK";
  const STATUS = "DW_XIANYU_CONNECT_STATUS";
  const ERROR = "DW_XIANYU_CONNECT_ERROR";

  const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;
  const SESSION_ID_RE = /^[a-f0-9-]{32,64}$/;
  const HANDOFF_TOKEN_RE = /^[A-Za-z0-9._~+/=:-]{16,1024}$/;
  const SESSION_TIMEOUT_MS = 5 * 60 * 1000;
  const MAX_COOKIE_HEADER_BYTES = 32 * 1024;
  const MAX_RESPONSE_BYTES = 16 * 1024;
  const REQUIRED_COOKIES = ["unb", "_m_h5_tk"];
  const VERIFICATION_CODES = new Set([
    "risk_control",
    "risk_cooldown",
    "cookie_expired",
    "cookie_invalid",
    "cookie_incomplete",
  ]);
  const sessions = new Map();

  const ERROR_MESSAGES = Object.freeze({
    invalid_request: "连接请求格式无效。",
    untrusted_source: "连接请求来源无效。",
    untrusted_session: "连接会话已失效，请重新连接。",
    invalid_session_state: "当前连接状态无法执行此操作。",
    official_page_open_failed: "无法打开闲鱼官网，请重试。",
    official_page_unavailable: "无法读取闲鱼登录页面，请重新连接。",
    official_page_left: "登录页面已离开闲鱼官网，请重新连接。",
    official_tab_closed: "闲鱼登录页面已关闭，请重新连接。",
    cookie_read_failed: "无法读取闲鱼登录状态，请重试。",
    cookie_store_unavailable: "无法确认闲鱼登录会话，请重新发起连接。",
    cookie_header_too_large: "闲鱼登录信息过大，请重新登录后重试。",
    cookie_expired: "闲鱼登录已失效，请在官方页面重新登录。",
    cookie_invalid: "闲鱼登录信息无效，请在官方页面重新登录。",
    cookie_incomplete: "闲鱼登录信息不完整，请在官方页面重新登录。",
    connection_timeout: "连接已超时，请重新发起连接。",
    handoff_expired: "连接请求已过期，请从工作台重新发起。",
    invalid_handoff: "连接请求无效，请从工作台重新发起。",
    network_error: "暂时无法完成连接，请稍后重试。",
    platform_error: "闲鱼连接暂时不可用，请稍后重试。",
    risk_control: "闲鱼要求安全验证，请在官方页面完成后等待自动连接。",
    risk_cooldown: "安全验证仍在冷却，请稍后再试。",
    sync_cooldown: "店铺刚完成检测，请稍后重试。",
    sync_busy: "店铺正在检测，请稍后重试。",
    sync_timeout: "店铺检测超时，请稍后重试。",
    rate_limited: "请求过于频繁，请稍后重试。",
  });

  function response(type, requestId, extra = {}) {
    return Object.assign({
      source: EXTENSION_SOURCE,
      protocol: PROTOCOL,
      type,
      requestId,
    }, extra);
  }

  function ackResponse(requestId, extra) {
    return response(ACK, requestId, extra);
  }

  function statusResponse(requestId, status) {
    return response(STATUS, requestId, { status });
  }

  function errorResponse(requestId, code, message) {
    return response(ERROR, requestId, {
      code,
      message: message || ERROR_MESSAGES[code] || "连接失败，请重新尝试。",
    });
  }

  function isAllowedSaasUrl(value) {
    try {
      const url = new URL(value);
      return url.origin === SAAS_ORIGIN
        && (url.pathname === SAAS_PATH || url.pathname.startsWith(SAAS_PATH));
    } catch (_error) {
      return false;
    }
  }

  function isGoofishHost(value) {
    const hostname = String(value || "").replace(/^\./, "").toLowerCase();
    return hostname === "goofish.com" || hostname.endsWith(".goofish.com");
  }

  function targetState(tab) {
    const rawUrl = tab && (tab.url || tab.pendingUrl);
    try {
      const url = new URL(rawUrl);
      if (url.protocol !== "https:" || !isGoofishHost(url.hostname)) {
        return { allowed: false, code: "official_page_left" };
      }
      const hint = (url.hostname + url.pathname + url.search).toLowerCase();
      if (/(punish|captcha|verify|validate|challenge|security|passport)/.test(hint)) {
        return { allowed: true, status: "verification_required" };
      }
      return { allowed: true, status: "waiting_for_login" };
    } catch (_error) {
      return { allowed: false, code: "official_page_unavailable" };
    }
  }

  function trustedSender(sender) {
    if (!sender || sender.id !== chrome.runtime.id || sender.frameId !== 0) return false;
    if (!sender.tab || !Number.isInteger(sender.tab.id)) return false;
    if (!isAllowedSaasUrl(sender.url) || !isAllowedSaasUrl(sender.tab.url)) return false;
    return !sender.origin || sender.origin === SAAS_ORIGIN;
  }

  function validRequestId(value) {
    return typeof value === "string" && REQUEST_ID_RE.test(value);
  }

  function validHandoffToken(value) {
    return typeof value === "string"
      && HANDOFF_TOKEN_RE.test(value)
      && value.length >= 16
      && value.length <= 1024;
  }

  function hasExactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return actual.length === wanted.length
      && actual.every((key, index) => key === wanted[index]);
  }

  function validRequest(message, type) {
    if (type === START) {
      return hasExactKeys(message, [
        "source", "protocol", "type", "requestId", "handoffToken",
      ])
        && message.source === PAGE_SOURCE
        && message.protocol === PROTOCOL
        && message.type === type
        && validRequestId(message.requestId)
        && validHandoffToken(message.handoffToken);
    }
    if (type === POLL || type === HEARTBEAT || type === CANCEL) {
      return hasExactKeys(message, ["source", "protocol", "type", "requestId", "sessionId"])
        && message.source === PAGE_SOURCE
        && message.protocol === PROTOCOL
        && message.type === type
        && validRequestId(message.requestId)
        && typeof message.sessionId === "string"
        && SESSION_ID_RE.test(message.sessionId);
    }
    return false;
  }

  function randomSessionId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function resolveCookieStoreId(tabId, callback) {
    try {
      chrome.cookies.getAllCookieStores((stores) => {
        if (chrome.runtime.lastError || !Array.isArray(stores)) {
          callback("");
          return;
        }
        const matches = new Set();
        for (const store of stores) {
          if (
            store
            && typeof store.id === "string"
            && store.id
            && Array.isArray(store.tabIds)
            && store.tabIds.includes(tabId)
          ) matches.add(store.id);
        }
        callback(matches.size === 1 ? [...matches][0] : "");
      });
    } catch (_error) {
      callback("");
    }
  }

  function clearSession(sessionId) {
    const session = sessions.get(sessionId);
    if (!session) return null;
    sessions.delete(sessionId);
    if (session.timeoutId !== null) clearTimeout(session.timeoutId);
    if (session.abortController) session.abortController.abort();
    session.abortController = null;
    session.handoffToken = "";
    session.lastCookieFingerprint = null;
    return session;
  }

  function matchesSession(session, sender, message) {
    if (!session || message.requestId !== session.requestId) return false;
    if (sender.tab.id !== session.initiatorTabId || sender.frameId !== session.initiatorFrameId) {
      return false;
    }
    if (session.initiatorDocumentId && sender.documentId !== session.initiatorDocumentId) {
      return false;
    }
    return true;
  }

  function sendToInitiator(session, publicResponse) {
    chrome.tabs.get(session.initiatorTabId, (tab) => {
      if (chrome.runtime.lastError || !tab || !isAllowedSaasUrl(tab.url)) return;
      chrome.tabs.sendMessage(
        session.initiatorTabId,
        {
          source: EXTENSION_SOURCE,
          protocol: PROTOCOL,
          type: BACKGROUND_EVENT,
          requestId: session.requestId,
          sessionId: session.sessionId,
          response: publicResponse,
        },
        { frameId: session.initiatorFrameId },
        () => void chrome.runtime.lastError,
      );
    });
  }

  function finishSession(sessionId, publicResponse, notify = false) {
    const session = clearSession(sessionId);
    if (session && notify && publicResponse) sendToInitiator(session, publicResponse);
    return session;
  }

  function cookieScore(cookie) {
    const domain = String(cookie.domain || "").replace(/^\./, "").toLowerCase();
    return (cookie.hostOnly ? 100000 : 0)
      + domain.length * 100
      + String(cookie.path || "/").length;
  }

  function buildCookieHeader(cookies) {
    if (!Array.isArray(cookies)) return { ok: false, code: "cookie_read_failed" };
    const selected = new Map();
    for (const cookie of cookies) {
      if (!cookie || typeof cookie !== "object" || !isGoofishHost(cookie.domain)) continue;
      const name = typeof cookie.name === "string" ? cookie.name : "";
      const value = typeof cookie.value === "string" ? cookie.value : "";
      if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/.test(name)) continue;
      if (!value || /[;\r\n\0]/.test(value)) continue;
      const previous = selected.get(name);
      if (!previous || cookieScore(cookie) > cookieScore(previous)) selected.set(name, cookie);
    }
    if (!REQUIRED_COOKIES.every((name) => selected.has(name))) {
      return { ok: false, code: "required_cookies_missing" };
    }
    const header = Array.from(selected.values())
      .sort((left, right) => left.name.localeCompare(right.name))
      .map((cookie) => cookie.name + "=" + cookie.value)
      .join("; ");
    if (new TextEncoder().encode(header).length > MAX_COOKIE_HEADER_BYTES) {
      return { ok: false, code: "cookie_header_too_large" };
    }
    return { ok: true, header };
  }

  function fingerprintCookieHeader(header) {
    const bytes = new TextEncoder().encode(header);
    let first = 0x811c9dc5;
    let second = 0x9e3779b9;
    for (const byte of bytes) {
      first = Math.imul(first ^ byte, 0x01000193) >>> 0;
      second = Math.imul(second ^ byte, 0x85ebca6b) >>> 0;
    }
    return bytes.length + ":" + first.toString(16).padStart(8, "0")
      + second.toString(16).padStart(8, "0");
  }

  function sessionExpired(session) {
    return Date.now() >= session.deadline;
  }

  function safeCode(value, fallback) {
    return typeof value === "string" && /^[a-z0-9_]{1,64}$/.test(value)
      ? value
      : fallback;
  }

  function safeMessage(code) {
    return ERROR_MESSAGES[code] || "连接失败，请重新尝试。";
  }

  function fallbackCodeForStatus(status) {
    if (status === 408 || status === 504) return "sync_timeout";
    if (status === 409) return "sync_busy";
    if (status === 429) return "rate_limited";
    if (status >= 500) return "platform_error";
    return "invalid_handoff";
  }

  async function parseResponsePayload(responseObject) {
    try {
      const text = await responseObject.text();
      if (new TextEncoder().encode(text).length > MAX_RESPONSE_BYTES) return null;
      return JSON.parse(text);
    } catch (_error) {
      return null;
    }
  }

  async function submitCookie(session, cookieHeader, signal) {
    let responseObject;
    try {
      responseObject = await fetch(CONNECTOR_COOKIE_URL, {
        method: "POST",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
        cache: "no-store",
        signal,
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          handoff_token: session.handoffToken,
          cookies: cookieHeader,
        }),
      });
    } catch (_error) {
      return { ok: false, code: "network_error", message: ERROR_MESSAGES.network_error };
    }

    const payload = await parseResponsePayload(responseObject);
    if (responseObject.ok && payload && payload.ok === true && payload.connected === true) {
      return { ok: true };
    }
    const detail = payload && typeof payload === "object" && payload.detail
      && typeof payload.detail === "object"
      ? payload.detail
      : payload;
    const code = safeCode(detail && detail.code, fallbackCodeForStatus(responseObject.status));
    return {
      ok: false,
      code,
      message: safeMessage(code),
    };
  }

  function startSession(message, sender, sendResponse) {
    const deadline = Date.now() + SESSION_TIMEOUT_MS;
    for (const [sessionId, session] of sessions) {
      if (
        session.initiatorTabId === sender.tab.id
        && session.initiatorFrameId === sender.frameId
      ) clearSession(sessionId);
    }

    chrome.tabs.create({ url: GOOFISH_URL, active: true }, (tab) => {
      if (chrome.runtime.lastError || !tab || !Number.isInteger(tab.id)) {
        sendResponse(errorResponse(message.requestId, "official_page_open_failed"));
        return;
      }
      resolveCookieStoreId(tab.id, (cookieStoreId) => {
        if (!cookieStoreId) {
          sendResponse(errorResponse(message.requestId, "cookie_store_unavailable"));
          return;
        }
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          sendResponse(errorResponse(message.requestId, "connection_timeout"));
          return;
        }
        const sessionId = randomSessionId();
        const session = {
          sessionId,
          requestId: message.requestId,
          handoffToken: message.handoffToken,
          initiatorTabId: sender.tab.id,
          initiatorFrameId: sender.frameId,
          initiatorDocumentId: typeof sender.documentId === "string" ? sender.documentId : null,
          targetTabId: tab.id,
          targetCookieStoreId: cookieStoreId,
          deadline,
          timeoutId: null,
          abortController: null,
          checkingPending: true,
          submissionInFlight: false,
          lastCookieFingerprint: null,
          lastSubmissionCode: "",
        };
        session.timeoutId = setTimeout(() => {
          finishSession(
            sessionId,
            errorResponse(message.requestId, "connection_timeout"),
            true,
          );
        }, remaining);
        sessions.set(sessionId, session);
        sendResponse(ackResponse(message.requestId, { sessionId, deadline }));
      });
    });
  }

  function statusForRepeatedCookie(session) {
    return VERIFICATION_CODES.has(session.lastSubmissionCode)
      ? "verification_required"
      : "waiting_for_login";
  }

  function beginCookieSubmission(session, requestId, header) {
    const fingerprint = fingerprintCookieHeader(header);
    if (fingerprint === session.lastCookieFingerprint) {
      return false;
    }
    session.lastCookieFingerprint = fingerprint;
    session.lastSubmissionCode = "";
    session.submissionInFlight = true;
    const abortController = new AbortController();
    session.abortController = abortController;
    void submitCookie(session, header, abortController.signal).then((result) => {
      const current = sessions.get(session.sessionId);
      if (!current || current !== session) return;
      session.abortController = null;
      if (sessionExpired(session)) {
        finishSession(
          session.sessionId,
          errorResponse(requestId, "connection_timeout"),
          true,
        );
        return;
      }
      session.submissionInFlight = false;
      if (result.ok) {
        finishSession(session.sessionId, statusResponse(requestId, "connected"), true);
        return;
      }
      session.lastSubmissionCode = result.code;
      if (VERIFICATION_CODES.has(result.code)) {
        sendToInitiator(session, statusResponse(requestId, "verification_required"));
      } else {
        finishSession(
          session.sessionId,
          errorResponse(requestId, result.code, result.message),
          true,
        );
      }
    });
    return true;
  }

  function pollSession(message, sender, sendResponse) {
    const session = sessions.get(message.sessionId);
    if (!matchesSession(session, sender, message)) {
      sendResponse(errorResponse(message.requestId, "untrusted_session"));
      return;
    }
    if (sessionExpired(session)) {
      finishSession(session.sessionId, null, false);
      sendResponse(errorResponse(message.requestId, "connection_timeout"));
      return;
    }
    if (session.submissionInFlight) {
      sendResponse(statusResponse(message.requestId, "submitting"));
      return;
    }

    chrome.tabs.get(session.targetTabId, (tab) => {
      if (chrome.runtime.lastError || !tab) {
        finishSession(session.sessionId, null, false);
        sendResponse(errorResponse(message.requestId, "official_tab_closed"));
        return;
      }
      const state = targetState(tab);
      if (!state.allowed) {
        finishSession(session.sessionId, null, false);
        sendResponse(errorResponse(message.requestId, state.code));
        return;
      }
      if (state.status === "verification_required") {
        sendResponse(statusResponse(message.requestId, state.status));
        return;
      }
      if (session.checkingPending) {
        session.checkingPending = false;
        sendResponse(statusResponse(message.requestId, "checking_login"));
        return;
      }

      if (!session.targetCookieStoreId) {
        finishSession(session.sessionId, null, false);
        sendResponse(errorResponse(message.requestId, "cookie_store_unavailable"));
        return;
      }
      chrome.cookies.getAll({ url: MTOP_COOKIE_URL, storeId: session.targetCookieStoreId }, (cookies) => {
        if (chrome.runtime.lastError) {
          finishSession(session.sessionId, null, false);
          sendResponse(errorResponse(message.requestId, "cookie_read_failed"));
          return;
        }
        const result = buildCookieHeader(cookies);
        if (!result.ok) {
          if (result.code === "required_cookies_missing") {
            sendResponse(statusResponse(message.requestId, "waiting_for_login"));
            return;
          }
          finishSession(session.sessionId, null, false);
          sendResponse(errorResponse(message.requestId, result.code));
          return;
        }
        if (session.lastCookieFingerprint === fingerprintCookieHeader(result.header)) {
          sendResponse(statusResponse(message.requestId, statusForRepeatedCookie(session)));
          return;
        }
        beginCookieSubmission(session, message.requestId, result.header);
        sendResponse(statusResponse(message.requestId, "submitting"));
      });
    });
  }

  function heartbeatSession(message, sender, sendResponse) {
    const session = sessions.get(message.sessionId);
    if (!matchesSession(session, sender, message)) {
      sendResponse(errorResponse(message.requestId, "untrusted_session"));
      return;
    }
    if (sessionExpired(session)) {
      finishSession(session.sessionId, null, false);
      sendResponse(errorResponse(message.requestId, "connection_timeout"));
      return;
    }
    sendResponse(ackResponse(message.requestId));
  }

  function cancelSession(message, sender, sendResponse) {
    const session = sessions.get(message.sessionId);
    if (!matchesSession(session, sender, message)) {
      sendResponse(errorResponse(message.requestId, "untrusted_session"));
      return;
    }
    clearSession(session.sessionId);
    sendResponse(ackResponse(message.requestId));
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object") return false;
    if (![START, POLL, HEARTBEAT, CANCEL].includes(message.type)) return false;
    if (!validRequestId(message.requestId)) return false;
    if (!trustedSender(sender)) {
      sendResponse(errorResponse(message.requestId, "untrusted_source"));
      return false;
    }
    if (!validRequest(message, message.type)) {
      sendResponse(errorResponse(message.requestId, "invalid_request"));
      return false;
    }
    if (message.type === START) startSession(message, sender, sendResponse);
    if (message.type === POLL) pollSession(message, sender, sendResponse);
    if (message.type === HEARTBEAT) heartbeatSession(message, sender, sendResponse);
    if (message.type === CANCEL) cancelSession(message, sender, sendResponse);
    return true;
  });

  chrome.tabs.onRemoved.addListener((tabId) => {
    for (const [sessionId, session] of sessions) {
      if (session.initiatorTabId === tabId) {
        clearSession(sessionId);
      } else if (session.targetTabId === tabId) {
        finishSession(
          sessionId,
          errorResponse(session.requestId, "official_tab_closed"),
          true,
        );
      }
    }
  });
})();

(() => {
  "use strict";

  const PAGE_SOURCE = "deepwhale-xianyu-saas";
  const EXTENSION_SOURCE = "deepwhale-xianyu-connector";
  const PROTOCOL = 1;
  const SAAS_ORIGIN = "https://deepwhale.chat";
  const SAAS_PATH = "/xianyu-saas/";

  const START = "DW_XIANYU_CONNECT_START";
  const CANCEL = "DW_XIANYU_CONNECT_CANCEL";
  const POLL = "DW_XIANYU_CONNECT_POLL";
  const HEARTBEAT = "DW_XIANYU_CONNECT_HEARTBEAT";
  const BACKGROUND_EVENT = "DW_XIANYU_CONNECT_BACKGROUND_EVENT";
  const ACK = "DW_XIANYU_CONNECT_ACK";
  const STATUS = "DW_XIANYU_CONNECT_STATUS";
  const ERROR = "DW_XIANYU_CONNECT_ERROR";

  const POLL_INTERVAL_MS = 1500;
  const HEARTBEAT_INTERVAL_MS = 20 * 1000;
  const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;
  const SESSION_ID_RE = /^[a-f0-9-]{32,64}$/;
  const HANDOFF_TOKEN_RE = /^[A-Za-z0-9._~+/=:-]{16,1024}$/;
  const ERROR_CODE_RE = /^[a-z0-9_]{1,64}$/;
  const STATUS_VALUES = new Set([
    "login_opened",
    "waiting_for_login",
    "checking_login",
    "verification_required",
    "submitting",
    "connected",
  ]);
  let activeSession = null;
  let startGeneration = 0;

  function isTrustedPage() {
    return window === window.top
      && location.origin === SAAS_ORIGIN
      && (location.pathname === SAAS_PATH || location.pathname.startsWith(SAAS_PATH));
  }

  function hasExactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return actual.length === wanted.length
      && actual.every((key, index) => key === wanted[index]);
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

  function validPageRequest(message, type) {
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
    if (type === CANCEL) {
      return hasExactKeys(message, ["source", "protocol", "type", "requestId"])
        && message.source === PAGE_SOURCE
        && message.protocol === PROTOCOL
        && message.type === type
        && validRequestId(message.requestId);
    }
    return false;
  }

  function response(type, requestId, extra = {}) {
    return Object.assign({
      source: EXTENSION_SOURCE,
      protocol: PROTOCOL,
      type,
      requestId,
    }, extra);
  }

  function localError(requestId, code) {
    const messages = {
      extension_unavailable: "连接助手暂时不可用，请确认扩展已启用后重试。",
      invalid_extension_response: "连接助手返回了无效结果，请重新连接。",
      connection_timeout: "连接已超时，请重新发起连接。",
    };
    return response(ERROR, requestId, {
      code,
      message: messages[code] || "连接失败，请重新尝试。",
    });
  }

  function postToPage(publicResponse) {
    if (!isTrustedPage()) return;
    window.postMessage(publicResponse, SAAS_ORIGIN);
  }

  function isActiveSession(session) {
    return Boolean(session) && activeSession === session;
  }

  function publishStatus(session, status) {
    if (!isActiveSession(session) || session.lastStatus === status) return;
    session.lastStatus = status;
    postToPage(response(STATUS, session.requestId, { status }));
  }

  function clearLocalSession(session) {
    if (!isActiveSession(session)) return false;
    if (session.pollTimer !== null) clearTimeout(session.pollTimer);
    if (session.heartbeatTimer !== null) clearTimeout(session.heartbeatTimer);
    if (session.deadlineTimer !== null) clearTimeout(session.deadlineTimer);
    session.pollTimer = null;
    session.heartbeatTimer = null;
    session.deadlineTimer = null;
    activeSession = null;
    return true;
  }

  function runtimeMessage(message, requestId) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (workerResponse) => {
          if (chrome.runtime.lastError) {
            resolve(localError(requestId, "extension_unavailable"));
            return;
          }
          resolve(workerResponse && typeof workerResponse === "object"
            ? workerResponse
            : localError(requestId, "invalid_extension_response"));
        });
      } catch (_error) {
        resolve(localError(requestId, "extension_unavailable"));
      }
    });
  }

  function validPublicResponse(workerResponse, requestId) {
    if (!workerResponse || typeof workerResponse !== "object" || Array.isArray(workerResponse)) {
      return false;
    }
    if (
      workerResponse.source !== EXTENSION_SOURCE
      || workerResponse.protocol !== PROTOCOL
      || workerResponse.requestId !== requestId
    ) return false;
    if (workerResponse.type === ACK) {
      return hasExactKeys(workerResponse, ["source", "protocol", "type", "requestId"]);
    }
    if (workerResponse.type === STATUS) {
      return hasExactKeys(workerResponse, ["source", "protocol", "type", "requestId", "status"])
        && STATUS_VALUES.has(workerResponse.status);
    }
    if (workerResponse.type === ERROR) {
      return hasExactKeys(workerResponse, [
        "source", "protocol", "type", "requestId", "code", "message",
      ])
        && typeof workerResponse.code === "string"
        && ERROR_CODE_RE.test(workerResponse.code)
        && typeof workerResponse.message === "string"
        && workerResponse.message.length > 0
        && workerResponse.message.length <= 160
        && !/[\r\n\0]/.test(workerResponse.message);
    }
    return false;
  }

  function validStartResponse(workerResponse, requestId) {
    return workerResponse
      && workerResponse.source === EXTENSION_SOURCE
      && workerResponse.protocol === PROTOCOL
      && workerResponse.type === ACK
      && workerResponse.requestId === requestId
      && hasExactKeys(workerResponse, [
        "source", "protocol", "type", "requestId", "sessionId", "deadline",
      ])
      && typeof workerResponse.sessionId === "string"
      && SESSION_ID_RE.test(workerResponse.sessionId)
      && Number.isFinite(workerResponse.deadline);
  }

  function validBackgroundEvent(message) {
    return hasExactKeys(message, [
      "source", "protocol", "type", "requestId", "sessionId", "response",
    ])
      && message.source === EXTENSION_SOURCE
      && message.protocol === PROTOCOL
      && message.type === BACKGROUND_EVENT
      && validRequestId(message.requestId)
      && typeof message.sessionId === "string"
      && SESSION_ID_RE.test(message.sessionId);
  }

  function schedulePoll(session, delay = POLL_INTERVAL_MS) {
    if (!isActiveSession(session) || session.pollTimer !== null) return;
    const timer = setTimeout(() => {
      if (!isActiveSession(session) || session.pollTimer !== timer) return;
      session.pollTimer = null;
      pollSession(session);
    }, delay);
    session.pollTimer = timer;
  }

  function scheduleHeartbeat(session) {
    if (!isActiveSession(session) || session.heartbeatTimer !== null) return;
    const timer = setTimeout(() => {
      if (!isActiveSession(session) || session.heartbeatTimer !== timer) return;
      session.heartbeatTimer = null;
      heartbeatSession(session);
    }, HEARTBEAT_INTERVAL_MS);
    session.heartbeatTimer = timer;
  }

  function handleActiveResponse(workerResponse, session) {
    if (!isActiveSession(session)) return;
    if (!validPublicResponse(workerResponse, session.requestId)) {
      postToPage(localError(session.requestId, "invalid_extension_response"));
      clearLocalSession(session);
      return;
    }
    if (workerResponse.type === STATUS) {
      publishStatus(session, workerResponse.status);
      if (workerResponse.status === "connected") {
        clearLocalSession(session);
      } else {
        schedulePoll(session);
      }
      return;
    }
    if (workerResponse.type === ERROR) {
      postToPage(workerResponse);
      clearLocalSession(session);
      return;
    }
    postToPage(localError(session.requestId, "invalid_extension_response"));
    clearLocalSession(session);
  }

  async function pollSession(session) {
    if (!isActiveSession(session) || !isTrustedPage()) return;
    const workerResponse = await runtimeMessage({
      source: PAGE_SOURCE,
      protocol: PROTOCOL,
      type: POLL,
      requestId: session.requestId,
      sessionId: session.sessionId,
    }, session.requestId);
    handleActiveResponse(workerResponse, session);
  }

  async function heartbeatSession(session) {
    if (!isActiveSession(session) || !isTrustedPage()) return;
    const workerResponse = await runtimeMessage({
      source: PAGE_SOURCE,
      protocol: PROTOCOL,
      type: HEARTBEAT,
      requestId: session.requestId,
      sessionId: session.sessionId,
    }, session.requestId);
    if (!isActiveSession(session)) return;
    if (!validPublicResponse(workerResponse, session.requestId)) {
      postToPage(localError(session.requestId, "invalid_extension_response"));
      clearLocalSession(session);
      return;
    }
    if (workerResponse.type === ERROR) {
      postToPage(workerResponse);
      clearLocalSession(session);
      return;
    }
    if (workerResponse.type !== ACK) {
      postToPage(localError(session.requestId, "invalid_extension_response"));
      clearLocalSession(session);
      return;
    }
    scheduleHeartbeat(session);
  }

  async function startConnection(requestId, handoffToken) {
    const generation = ++startGeneration;
    if (activeSession) {
      const previous = activeSession;
      await runtimeMessage({
        source: PAGE_SOURCE,
        protocol: PROTOCOL,
        type: CANCEL,
        requestId: previous.requestId,
        sessionId: previous.sessionId,
      }, previous.requestId);
      if (generation !== startGeneration) return;
      clearLocalSession(previous);
    }

    const workerResponse = await runtimeMessage({
      source: PAGE_SOURCE,
      protocol: PROTOCOL,
      type: START,
      requestId,
      handoffToken,
    }, requestId);
    if (generation !== startGeneration) {
      if (validStartResponse(workerResponse, requestId)) {
        void runtimeMessage({
          source: PAGE_SOURCE,
          protocol: PROTOCOL,
          type: CANCEL,
          requestId,
          sessionId: workerResponse.sessionId,
        }, requestId);
      }
      return;
    }
    if (validPublicResponse(workerResponse, requestId) && workerResponse.type === ERROR) {
      postToPage(workerResponse);
      return;
    }
    if (!validStartResponse(workerResponse, requestId)) {
      postToPage(localError(requestId, "invalid_extension_response"));
      return;
    }

    activeSession = {
      requestId,
      sessionId: workerResponse.sessionId,
      deadline: workerResponse.deadline,
      lastStatus: "",
      pollTimer: null,
      heartbeatTimer: null,
      deadlineTimer: null,
    };
    const session = activeSession;
    postToPage(response(ACK, requestId));
    publishStatus(session, "login_opened");

    const remaining = Math.max(0, session.deadline - Date.now());
    const timer = setTimeout(() => {
      if (!isActiveSession(session) || session.deadlineTimer !== timer) return;
      session.deadlineTimer = null;
      void runtimeMessage({
        source: PAGE_SOURCE,
        protocol: PROTOCOL,
        type: CANCEL,
        requestId: session.requestId,
        sessionId: session.sessionId,
      }, session.requestId);
      postToPage(localError(session.requestId, "connection_timeout"));
      clearLocalSession(session);
    }, remaining);
    session.deadlineTimer = timer;
    scheduleHeartbeat(session);
    schedulePoll(session);
  }

  async function cancelConnection(requestId) {
    const session = activeSession;
    if (!session || session.requestId !== requestId) return;
    const workerResponse = await runtimeMessage({
      source: PAGE_SOURCE,
      protocol: PROTOCOL,
      type: CANCEL,
      requestId,
      sessionId: session.sessionId,
    }, requestId);
    if (!isActiveSession(session)) return;
    if (validPublicResponse(workerResponse, requestId)) {
      postToPage(workerResponse);
    } else {
      postToPage(localError(requestId, "invalid_extension_response"));
    }
    clearLocalSession(session);
  }

  window.addEventListener("message", (event) => {
    if (!isTrustedPage() || event.source !== window || event.origin !== SAAS_ORIGIN) return;
    if (validPageRequest(event.data, START)) {
      startConnection(event.data.requestId, event.data.handoffToken);
      return;
    }
    if (validPageRequest(event.data, CANCEL)) {
      cancelConnection(event.data.requestId);
    }
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (!validBackgroundEvent(message) || !activeSession) return false;
    if (
      message.sessionId !== activeSession.sessionId
      || message.requestId !== activeSession.requestId
      || !validPublicResponse(message.response, activeSession.requestId)
    ) return false;
    const session = activeSession;
    handleActiveResponse(message.response, session);
    return false;
  });

  window.addEventListener("pagehide", () => {
    startGeneration += 1;
    const session = activeSession;
    if (!session) return;
    runtimeMessage({
      source: PAGE_SOURCE,
      protocol: PROTOCOL,
      type: CANCEL,
      requestId: session.requestId,
      sessionId: session.sessionId,
    }, session.requestId);
    clearLocalSession(session);
  }, { once: true });
})();

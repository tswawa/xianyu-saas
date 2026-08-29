import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "connector-extension");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
const workerSource = fs.readFileSync(path.join(root, "service-worker.js"), "utf8");
const contentSource = fs.readFileSync(path.join(root, "content-script.js"), "utf8");

const PAGE_SOURCE = "deepwhale-xianyu-saas";
const EXTENSION_SOURCE = "deepwhale-xianyu-connector";
const PROTOCOL = 1;
const START = "DW_XIANYU_CONNECT_START";
const CANCEL = "DW_XIANYU_CONNECT_CANCEL";
const POLL = "DW_XIANYU_CONNECT_POLL";
const HEARTBEAT = "DW_XIANYU_CONNECT_HEARTBEAT";
const EVENT = "DW_XIANYU_CONNECT_BACKGROUND_EVENT";
const ACK = "DW_XIANYU_CONNECT_ACK";
const STATUS = "DW_XIANYU_CONNECT_STATUS";
const ERROR = "DW_XIANYU_CONNECT_ERROR";
const MTOP_COOKIE_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/";
const COOKIE_ENDPOINT = "https://deepwhale.chat/xianyu-saas/api/bot/connector/cookies";
const HANDOFF_TOKEN = "handoff-contract-token_0123456789";

assert.equal(manifest.manifest_version, 3);
assert.deepEqual(manifest.permissions, ["cookies"]);
assert.deepEqual(new Set(manifest.host_permissions), new Set([
  "https://deepwhale.chat/xianyu-saas/*",
  "https://*.goofish.com/*",
]));
assert.equal(manifest.background.service_worker, "service-worker.js");
assert.deepEqual(manifest.content_scripts[0].matches, ["https://deepwhale.chat/xianyu-saas/*"]);
assert.equal(manifest.content_scripts[0].all_frames, false);
assert.equal(JSON.stringify(manifest).includes("<all_urls>"), false);

for (const [name, source] of [["service-worker.js", workerSource], ["content-script.js", contentSource]]) {
  new vm.Script(source, { filename: name });
  assert.equal(/chrome\.storage|localStorage|sessionStorage|indexedDB/.test(source), false, `${name} must not persist secrets`);
  assert.equal(/XMLHttpRequest/.test(source), false, `${name} must not use XMLHttpRequest`);
  assert.equal(/console\.(?:log|info|warn|error|debug)/.test(source), false, `${name} must not log session data`);
  assert.equal(/password/i.test(source), false, `${name} must not accept passwords`);
  assert.equal(/DW_XIANYU_CONNECT_COOKIE/.test(source), false, `${name} must not expose Cookie page messages`);
}
assert.equal(/window\.postMessage[\s\S]*cookies\s*:/.test(contentSource), false, "page messages must never carry Cookie data");
assert.equal(/chrome\.cookies\.(?:set|remove)/.test(workerSource), false, "extension must not mutate browser cookies");
assert.match(workerSource, /fetch\(CONNECTOR_COOKIE_URL/);
assert.match(workerSource, /credentials:\s*["']omit["']/);
assert.match(workerSource, /redirect:\s*["']error["']/);
assert.match(workerSource, new RegExp(MTOP_COOKIE_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
assert.match(workerSource, /storeId:\s*session\.targetCookieStoreId/);
assert.equal((workerSource.match(/\bfetch\s*\(/g) || []).length, 1, "only one fixed SaaS fetch is allowed");

function eventSlot() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    dispatch(...args) { return listeners.map((listener) => listener(...args)); },
  };
}

function assertEnvelope(value, type, requestId) {
  assert.equal(value.source, EXTENSION_SOURCE);
  assert.equal(value.protocol, PROTOCOL);
  assert.equal(value.type, type);
  assert.equal(value.requestId, requestId);
}

function assertNoCookieField(value) {
  for (const key of ["cookies", "handoffToken", "handoff_token", "sessionId", "deadline"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(value, key), false, `public response must not expose ${key}`);
  }
  const serialized = JSON.stringify(value);
  assert.equal(serialized.includes("seller-contract"), false);
  assert.equal(serialized.includes("token-contract"), false);
  assert.equal(serialized.includes(HANDOFF_TOKEN), false);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

let now = 1_900_000_000_000;
class FakeDate extends Date {
  constructor(...args) {
    super(...(args.length ? args : [now]));
  }

  static now() {
    return now;
  }
}

const backgroundMessages = eventSlot();
const tabRemoved = eventSlot();
const tabs = new Map();
const pushedMessages = [];
const createdTabs = [];
let nextTabId = 40;
let cookieReads = 0;
let availableCookies = [];
let cookieStoresAvailable = true;
let fetchCalls = [];
const fetchQueue = [];

const backgroundChrome = {
  runtime: {
    id: "connector-contract-id",
    lastError: null,
    onMessage: backgroundMessages,
  },
  tabs: {
    onRemoved: tabRemoved,
    create(options, callback) {
      createdTabs.push(options);
      const tab = { id: nextTabId++, url: options.url };
      tabs.set(tab.id, tab);
      callback(tab);
    },
    get(tabId, callback) {
      const tab = tabs.get(tabId);
      if (!tab) {
        backgroundChrome.runtime.lastError = { message: "tab missing" };
        callback(undefined);
        backgroundChrome.runtime.lastError = null;
        return;
      }
      callback({ ...tab });
    },
    sendMessage(tabId, message, options, callback) {
      pushedMessages.push({ tabId, message, options });
      if (callback) callback();
    },
  },
  cookies: {
    getAllCookieStores(callback) {
      callback(cookieStoresAvailable
        ? [{ id: "store-contract", tabIds: [...tabs.keys()] }]
        : []);
    },
    getAll(query, callback) {
      cookieReads += 1;
      assert.deepEqual(plain(query), { url: MTOP_COOKIE_URL, storeId: "store-contract" });
      callback(availableCookies.map((cookie) => ({ ...cookie })));
    },
  },
};

function fakeFetch(url, options) {
  fetchCalls.push({ url, options: { ...options } });
  const next = fetchQueue.shift();
  if (next instanceof Error) return Promise.reject(next);
  if (next && next.deferred) {
    return new Promise((resolve, reject) => {
      next.resolve = resolve;
      options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    });
  }
  const result = next || { ok: true, status: 200, body: { ok: true, connected: true } };
  return Promise.resolve({
    ok: Boolean(result.ok),
    status: result.status || (result.ok ? 200 : 422),
    text: () => Promise.resolve(JSON.stringify(result.body)),
  });
}

const backgroundTimers = new Map();
let nextTimer = 1;
const backgroundContext = vm.createContext({
  chrome: backgroundChrome,
  crypto: {
    randomUUID: (() => {
      let id = 1;
      return () => `00000000-0000-4000-8000-${String(id++).padStart(12, "0")}`;
    })(),
  },
  URL,
  TextEncoder,
  Uint8Array,
  Date: FakeDate,
  Math,
  Number,
  String,
  JSON,
  Map,
  Set,
  Promise,
  Object,
  Array,
  RegExp,
  AbortController,
  fetch: fakeFetch,
  setTimeout(callback, delay) {
    const id = nextTimer++;
    backgroundTimers.set(id, { callback, delay });
    return id;
  },
  clearTimeout(id) { backgroundTimers.delete(id); },
});
vm.runInContext(workerSource, backgroundContext, { filename: "service-worker.js" });
assert.equal(backgroundMessages.listeners.length, 1);

const trustedSender = {
  id: backgroundChrome.runtime.id,
  origin: "https://deepwhale.chat",
  frameId: 0,
  documentId: "doc-a",
  url: "https://deepwhale.chat/xianyu-saas/",
  tab: { id: 7, url: "https://deepwhale.chat/xianyu-saas/" },
};
tabs.set(7, { id: 7, url: trustedSender.tab.url });

function sendToWorker(message, sender = trustedSender) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const result = backgroundMessages.listeners[0](message, sender, (response) => {
      settled = true;
      resolve(response);
    });
    if (result === false && !settled) reject(new Error("message was not handled"));
  });
}

const untrusted = await sendToWorker(
  {
    source: PAGE_SOURCE,
    protocol: PROTOCOL,
    type: START,
    requestId: "request-0001",
    handoffToken: HANDOFF_TOKEN,
  },
  { ...trustedSender, url: "https://evil.example/", tab: { id: 8, url: "https://evil.example/" } },
);
assertEnvelope(untrusted, ERROR, "request-0001");
assert.equal(untrusted.code, "untrusted_source");
assert.equal(createdTabs.length, 0);

const extraField = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: START,
  requestId: "request-0001",
  handoffToken: HANDOFF_TOKEN,
  secret: "must-ignore",
});
assertEnvelope(extraField, ERROR, "request-0001");
assert.equal(extraField.code, "invalid_request");
assert.equal(createdTabs.length, 0);

const invalidToken = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: START,
  requestId: "request-0001",
  handoffToken: "short",
});
assertEnvelope(invalidToken, ERROR, "request-0001");
assert.equal(invalidToken.code, "invalid_request");

const opened = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: START,
  requestId: "request-0001",
  handoffToken: HANDOFF_TOKEN,
});
assertEnvelope(opened, ACK, "request-0001");
assert.equal(typeof opened.sessionId, "string");
assert.equal(opened.deadline, now + 300000);
assert.equal(createdTabs.length, 1);
assert.deepEqual(plain(createdTabs[0]), {
  url: "https://www.goofish.com/login?redirectURL=https%3A%2F%2Fwww.goofish.com%2F",
  active: true,
});
assert.equal(backgroundTimers.size, 1, "only the fixed session deadline is held by the worker");

const targetTab = tabs.get(40);
targetTab.url = "https://passport.goofish.com/verify";
let workerResponse = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: POLL,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(workerResponse, STATUS, "request-0001");
assert.equal(workerResponse.status, "verification_required");

targetTab.url = "https://www.goofish.com/";
workerResponse = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: POLL,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(workerResponse, STATUS, "request-0001");
assert.equal(workerResponse.status, "checking_login");

workerResponse = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: POLL,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(workerResponse, STATUS, "request-0001");
assert.equal(workerResponse.status, "waiting_for_login");
assert.equal(cookieReads, 1);

const deadlineBeforeHeartbeat = opened.deadline;
now += 20000;
workerResponse = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: HEARTBEAT,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(workerResponse, ACK, "request-0001");
assert.equal(opened.deadline, deadlineBeforeHeartbeat, "heartbeat must not extend the deadline");
assert.equal(backgroundTimers.size, 1);

availableCookies = [
  { name: "unb", value: "seller-contract", domain: ".goofish.com", path: "/" },
  { name: "_m_h5_tk", value: "token-contract_123", domain: ".goofish.com", path: "/" },
  { name: "cookie2", value: "session-contract", domain: ".goofish.com", path: "/" },
  { name: "outside", value: "must-not-leak", domain: ".evil.example", path: "/" },
];
fetchQueue.push({ ok: true, status: 200, body: { ok: true, connected: true, shop_name: "合同店铺", product_count: 2 } });
workerResponse = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: POLL,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(workerResponse, STATUS, "request-0001");
assert.equal(workerResponse.status, "submitting");
assert.equal(fetchCalls.length, 1);
assert.equal(fetchCalls[0].url, COOKIE_ENDPOINT);
assert.equal(fetchCalls[0].options.method, "POST");
assert.equal(fetchCalls[0].options.credentials, "omit");
assert.equal(fetchCalls[0].options.redirect, "error");
assert.ok(fetchCalls[0].options.signal, "submission must be abortable");
assert.deepEqual(JSON.parse(fetchCalls[0].options.body), {
  handoff_token: HANDOFF_TOKEN,
  cookies: "_m_h5_tk=token-contract_123; cookie2=session-contract; unb=seller-contract",
});
assert.equal(JSON.stringify(pushedMessages).includes("must-not-leak"), false);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(pushedMessages.at(-1).message.response.type, STATUS);
assert.equal(pushedMessages.at(-1).message.response.status, "connected");
assertNoCookieField(pushedMessages.at(-1).message.response);
assert.equal(backgroundTimers.size, 0, "successful submission must clean the session");

const gone = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: HEARTBEAT,
  requestId: "request-0001",
  sessionId: opened.sessionId,
});
assertEnvelope(gone, ERROR, "request-0001");
assert.equal(gone.code, "untrusted_session");

const retryToken = "handoff-retry-token_0123456789";
const retryStart = await sendToWorker({
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: START,
  requestId: "request-0002",
  handoffToken: retryToken,
});
const retryTarget = tabs.get(41);
retryTarget.url = "https://www.goofish.com/";
await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0002", sessionId: retryStart.sessionId });
availableCookies = [
  { name: "unb", value: "seller-contract", domain: ".goofish.com", path: "/" },
  { name: "_m_h5_tk", value: "retry-token-1", domain: ".goofish.com", path: "/" },
];
fetchQueue.push({ ok: false, status: 422, body: { detail: { code: "risk_control", message: "seller-contract token-contract" } } });
await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0002", sessionId: retryStart.sessionId });
await new Promise((resolve) => setImmediate(resolve));
assert.equal(fetchCalls.length, 2);
assert.equal(pushedMessages.at(-1).message.response.type, STATUS);
assert.equal(pushedMessages.at(-1).message.response.status, "verification_required");
assertNoCookieField(pushedMessages.at(-1).message.response);
const pushesBeforeDuplicate = pushedMessages.length;
const readsBeforeDuplicate = cookieReads;
const duplicate = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0002", sessionId: retryStart.sessionId });
assert.equal(duplicate.status, "verification_required");
assert.equal(fetchCalls.length, 2, "same Cookie fingerprint must not be submitted again");
assert.equal(cookieReads, readsBeforeDuplicate + 1);
assert.equal(pushedMessages.length, pushesBeforeDuplicate, "duplicate polling has no sensitive event");

availableCookies[1].value = "retry-token-2";
fetchQueue.push({ ok: true, status: 200, body: { ok: true, connected: true } });
const changed = await sendToWorker({ source: PAGE_SOURCE, type: POLL, protocol: PROTOCOL, requestId: "request-0002", sessionId: retryStart.sessionId });
assert.equal(changed.status, "submitting");
assert.equal(fetchCalls.length, 3, "a changed Cookie fingerprint may be submitted");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(pushedMessages.at(-1).message.response.status, "connected");
assert.equal(backgroundTimers.size, 0);

const wrongFrameStart = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0003", handoffToken: HANDOFF_TOKEN });
const wrongFrame = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0003", sessionId: wrongFrameStart.sessionId }, { ...trustedSender, frameId: 1 });
assert.equal(wrongFrame.code, "untrusted_source");
const wrongDocument = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0003", sessionId: wrongFrameStart.sessionId }, { ...trustedSender, documentId: "doc-b" });
assert.equal(wrongDocument.code, "untrusted_session");
await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: CANCEL, requestId: "request-0003", sessionId: wrongFrameStart.sessionId });

const leftStart = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0004", handoffToken: HANDOFF_TOKEN });
tabs.get(43).url = "https://evil.example/";
const left = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0004", sessionId: leftStart.sessionId });
assert.equal(left.code, "official_page_left");

const closeStart = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0005", handoffToken: HANDOFF_TOKEN });
tabRemoved.dispatch(44);
assert.equal(pushedMessages.at(-1).message.response.code, "official_tab_closed");
assertNoCookieField(pushedMessages.at(-1).message.response);

const timeoutStart = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0006", handoffToken: HANDOFF_TOKEN });
now = timeoutStart.deadline;
const timeout = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: HEARTBEAT, requestId: "request-0006", sessionId: timeoutStart.sessionId });
assert.equal(timeout.code, "connection_timeout");
assert.equal(backgroundTimers.size, 0);

cookieStoresAvailable = false;
const noStore = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0007", handoffToken: HANDOFF_TOKEN });
assertEnvelope(noStore, ERROR, "request-0007");
assert.equal(noStore.code, "cookie_store_unavailable");
assert.equal(backgroundTimers.size, 0, "missing target cookie store must not create a session");
cookieStoresAvailable = true;

const deferredFetch = { deferred: true };
const abortStart = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "request-0008", handoffToken: HANDOFF_TOKEN });
tabs.get(nextTabId - 1).url = "https://www.goofish.com/";
await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0008", sessionId: abortStart.sessionId });
availableCookies = [
  { name: "unb", value: "abort-contract", domain: ".goofish.com", path: "/" },
  { name: "_m_h5_tk", value: "abort-token", domain: ".goofish.com", path: "/" },
];
fetchQueue.push(deferredFetch);
const abortSubmitting = await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: POLL, requestId: "request-0008", sessionId: abortStart.sessionId });
assert.equal(abortSubmitting.status, "submitting");
const abortSignal = fetchCalls.at(-1).options.signal;
assert.equal(abortSignal.aborted, false);
await sendToWorker({ source: PAGE_SOURCE, protocol: PROTOCOL, type: CANCEL, requestId: "request-0008", sessionId: abortStart.sessionId });
assert.equal(abortSignal.aborted, true, "cancelling a session must abort an in-flight handoff");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(backgroundTimers.size, 0);

const contentMessages = eventSlot();
const pageEvents = new Map();
const pagePosts = [];
const runtimeCalls = [];
const contentTimers = new Map();
const contentSessionIds = new Map();
const deferredContentCancels = [];
let contentTimerId = 1;
let contentSessionId = 99;
let deferNextContentCancel = false;
let contentNow = 2_000_000_000_000;
class ContentDate extends Date {
  constructor(...args) {
    super(...(args.length ? args : [contentNow]));
  }

  static now() {
    return contentNow;
  }
}

const pageWindow = {
  top: null,
  addEventListener(type, listener) {
    const listeners = pageEvents.get(type) || [];
    listeners.push(listener);
    pageEvents.set(type, listeners);
  },
  postMessage(message, targetOrigin) { pagePosts.push({ message, targetOrigin }); },
};
pageWindow.top = pageWindow;
const contentChrome = {
  runtime: {
    lastError: null,
    onMessage: contentMessages,
    sendMessage(message, callback) {
      runtimeCalls.push(message);
      assert.equal(message.source, PAGE_SOURCE);
      assert.equal(message.protocol, PROTOCOL);
      if (message.type === START) {
        const sessionId = `00000000-0000-4000-8000-${String(contentSessionId++).padStart(12, "0")}`;
        contentSessionIds.set(message.requestId, sessionId);
        callback({
          source: EXTENSION_SOURCE,
          protocol: PROTOCOL,
          type: ACK,
          requestId: message.requestId,
          sessionId,
          deadline: contentNow + 300000,
        });
      } else if (message.type === POLL) {
        callback(contentPollReplies.shift() || {
          source: EXTENSION_SOURCE,
          protocol: PROTOCOL,
          type: STATUS,
          requestId: message.requestId,
          status: "waiting_for_login",
        });
      } else if (message.type === CANCEL && deferNextContentCancel) {
        deferNextContentCancel = false;
        deferredContentCancels.push({ message, callback });
      } else if (message.type === HEARTBEAT || message.type === CANCEL) {
        callback({ source: EXTENSION_SOURCE, protocol: PROTOCOL, type: ACK, requestId: message.requestId });
      }
    },
  },
};
const contentPollReplies = [];
const contentContext = vm.createContext({
  chrome: contentChrome,
  window: pageWindow,
  location: { origin: "https://deepwhale.chat", pathname: "/xianyu-saas/" },
  Date: ContentDate,
  TextEncoder,
  Set,
  Map,
  Promise,
  Object,
  Array,
  Number,
  String,
  RegExp,
  JSON,
  setTimeout(callback, delay) {
    const id = contentTimerId++;
    contentTimers.set(id, { callback, delay });
    return id;
  },
  clearTimeout(id) { contentTimers.delete(id); },
});
vm.runInContext(contentSource, contentContext, { filename: "content-script.js" });

function dispatchPageMessage(data, origin = "https://deepwhale.chat", source = pageWindow) {
  for (const listener of pageEvents.get("message") || []) listener({ data, origin, source });
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function runContentTimer(delay) {
  const entry = [...contentTimers.entries()].find(([, value]) => value.delay === delay);
  assert.ok(entry, `content timer ${delay} should exist`);
  contentTimers.delete(entry[0]);
  entry[1].callback();
  await flush();
}

dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0001", handoffToken: HANDOFF_TOKEN }, "https://evil.example");
dispatchPageMessage({ source: PAGE_SOURCE, protocol: 2, type: START, requestId: "content-0001", handoffToken: HANDOFF_TOKEN });
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0001", handoffToken: HANDOFF_TOKEN, cookies: "must-ignore" });
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0001", handoffToken: "short" });
assert.equal(runtimeCalls.length, 0, "untrusted or malformed page messages must be ignored");

dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0001", handoffToken: HANDOFF_TOKEN });
await flush();
assert.equal(runtimeCalls.length, 1);
assert.deepEqual(plain(runtimeCalls[0]), {
  source: PAGE_SOURCE,
  protocol: PROTOCOL,
  type: START,
  requestId: "content-0001",
  handoffToken: HANDOFF_TOKEN,
});
assert.deepEqual(pagePosts.map((entry) => entry.message.type), [ACK, STATUS]);
assert.equal(pagePosts[1].message.status, "login_opened");
for (const entry of pagePosts) {
  assert.equal(entry.targetOrigin, "https://deepwhale.chat");
  assertNoCookieField(entry.message);
}
assert.deepEqual([...contentTimers.values()].map((entry) => entry.delay).sort((a, b) => a - b), [1500, 20000, 300000]);

contentPollReplies.push(
  { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "checking_login" },
  { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "waiting_for_login" },
  { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "submitting" },
  { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "verification_required" },
);
await runContentTimer(1500);
await runContentTimer(1500);
await runContentTimer(1500);
assert.equal(pagePosts.at(-1).message.status, "submitting");
await runContentTimer(20000);
assert.equal([...contentTimers.values()].filter((entry) => entry.delay === 300000).length, 1, "heartbeat must not replace the deadline");

const pushedVerification = {
  source: EXTENSION_SOURCE,
  protocol: PROTOCOL,
  type: EVENT,
  requestId: "content-0001",
  sessionId: "00000000-0000-4000-8000-000000000099",
  response: { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "verification_required" },
};
contentMessages.dispatch(pushedVerification);
assert.equal(pagePosts.at(-1).message.type, STATUS);
assert.equal(pagePosts.at(-1).message.status, "verification_required");
assertNoCookieField(pagePosts.at(-1).message);
await runContentTimer(1500);
assert.equal(pagePosts.at(-1).message.status, "verification_required");

contentMessages.dispatch({ ...pushedVerification, sessionId: "00000000-0000-4000-8000-000000000098" });
assert.equal(pagePosts.at(-1).message.status, "verification_required", "wrong session events must be ignored");
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: "DW_XIANYU_CONNECT_RESULT", requestId: "content-0001", result: "success" });
assert.equal(runtimeCalls.some((message) => message.type === "DW_XIANYU_CONNECT_RESULT"), false);

contentMessages.dispatch({
  source: EXTENSION_SOURCE,
  protocol: PROTOCOL,
  type: EVENT,
  requestId: "content-0001",
  sessionId: "00000000-0000-4000-8000-000000000099",
  response: { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0001", status: "connected" },
});
assert.equal(pagePosts.at(-1).message.status, "connected");
assert.equal(contentTimers.size, 0, "connected must clean local timers");

dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0002", handoffToken: HANDOFF_TOKEN });
await flush();
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: CANCEL, requestId: "content-0002" });
await flush();
assert.equal(pagePosts.at(-1).message.type, ACK);
assert.equal(contentTimers.size, 0);

dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0003", handoffToken: HANDOFF_TOKEN });
await flush();
deferNextContentCancel = true;
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: CANCEL, requestId: "content-0003" });
await flush();
assert.equal(deferredContentCancels.length, 1, "the old CANCEL response must remain pending");

dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: START, requestId: "content-0004", handoffToken: HANDOFF_TOKEN });
await flush();
assert.equal(pagePosts.at(-1).message.status, "login_opened");
assert.equal(pagePosts.at(-1).message.requestId, "content-0004");
assert.deepEqual(
  [...contentTimers.values()].map((entry) => entry.delay).sort((a, b) => a - b),
  [1500, 20000, 300000],
  "the replacement session must own a complete timer set",
);

const postsBeforeOldCancel = pagePosts.length;
const delayedCancel = deferredContentCancels.shift();
delayedCancel.callback({
  source: EXTENSION_SOURCE,
  protocol: PROTOCOL,
  type: ACK,
  requestId: delayedCancel.message.requestId,
});
await flush();
assert.equal(pagePosts.length, postsBeforeOldCancel, "a stale CANCEL response must not publish into the new session");
assert.deepEqual(
  [...contentTimers.values()].map((entry) => entry.delay).sort((a, b) => a - b),
  [1500, 20000, 300000],
  "a stale CANCEL response must not clear replacement session timers",
);

contentMessages.dispatch({
  source: EXTENSION_SOURCE,
  protocol: PROTOCOL,
  type: EVENT,
  requestId: "content-0004",
  sessionId: contentSessionIds.get("content-0004"),
  response: { source: EXTENSION_SOURCE, protocol: PROTOCOL, type: STATUS, requestId: "content-0004", status: "checking_login" },
});
assert.equal(pagePosts.at(-1).message.status, "checking_login", "the replacement session must remain active");
dispatchPageMessage({ source: PAGE_SOURCE, protocol: PROTOCOL, type: CANCEL, requestId: "content-0004" });
await flush();
assert.equal(contentTimers.size, 0);

process.stdout.write(JSON.stringify({
  ok: true,
  manifestVersion: manifest.manifest_version,
  permissions: manifest.permissions,
  hostPermissions: manifest.host_permissions,
  cookieReads,
  fetchCalls: fetchCalls.length,
  pageNeverReceivesCookie: true,
  heartbeatIntervalMs: 20000,
}) + "\n");

// Regression test for update readiness copy in frontend/assets/app.js.
//
// Coverage boundary: this test boots the real frontend/assets/app.js inside
// node:vm with a minimal DOM stub and a one-line export hook injected into the
// in-memory copy (the source file itself is not modified). It then drives the
// real render functions. It covers code-to-copy mapping for update readiness,
// the readiness sentence, and the rollback control's disabled explanation. It
// does not exercise a browser, the backend, HTTP/network behavior, or the
// full ui-check.mjs DOM contract.
//
// Run: node tests/update-readiness-copy.test.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
// APP_JS_UNDER_TEST is used for mutation testing; the default is the real source.
const appPath = process.env.APP_JS_UNDER_TEST || path.join(here, "..", "frontend", "assets", "app.js");
const source = readFileSync(appPath, "utf8");

const anchor = 'window.addEventListener("DOMContentLoaded", init);';
assert.equal(source.split(anchor).length - 1, 1, "app.js test anchor must appear exactly once");
const instrumented = source.replace(
  anchor,
  anchor
    + "\n  globalThis.__updateReadinessCopy = { state, UPDATE_UI_COPY, updateErrorMessage, updateActionAllowed,"
    + " ensurePlatformUpdateSession, renderPlatformUpdate, renderVersionBadgeRollback };",
);

function makeElement() {
  const element = {
    textContent: "",
    innerHTML: "",
    title: "",
    href: "",
    src: "",
    hidden: false,
    disabled: false,
    checked: false,
    open: false,
    value: "",
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    removeEventListener() {},
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { if (name === "title") this.title = ""; },
    getAttribute() { return null; },
    hasAttribute() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return element; },
    focus() {},
    reset() {},
    remove() {},
    getClientRects() { return []; },
  };
  return element;
}

const elements = new Map();
function elementFor(selector) {
  if (!elements.has(selector)) elements.set(selector, makeElement());
  return elements.get(selector);
}

const documentStub = {
  hidden: false,
  body: makeElement(),
  documentElement: makeElement(),
  querySelector(selector) { return elementFor(selector); },
  querySelectorAll() { return []; },
  getElementById(id) { return elementFor("#" + id); },
  addEventListener() {},
  removeEventListener() {},
  createElement() { return makeElement(); },
  contains() { return false; },
};

const windowStub = {
  addEventListener() {},
  removeEventListener() {},
  setTimeout() { return 0; },
  clearTimeout() {},
  setInterval() { return 0; },
  clearInterval() {},
  requestAnimationFrame() { return 0; },
  matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
  location: { href: "https://example.test/", search: "", hash: "", reload() {} },
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
    setItem(key, value) { this.store.set(key, String(value)); },
    removeItem(key) { this.store.delete(key); },
  },
  confirm() { return true; },
  prompt() { return null; },
  alert() {},
};

const sandbox = {
  window: windowStub,
  document: documentStub,
  navigator: { userAgent: "node-test", clipboard: {} },
  location: windowStub.location,
  localStorage: windowStub.localStorage,
  fetch: async () => { throw new Error("fetch must not run in this test"); },
  URL,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
  structuredClone,
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(instrumented, sandbox, { filename: appPath });

const hook = sandbox.__updateReadinessCopy;
assert.ok(hook, "frontend/assets/app.js did not load in the test sandbox");
const copy = hook.UPDATE_UI_COPY;
const admin = { id: 1, username: "admin", role: "admin", is_admin: true };

function prepare(caps, { rollbackVersions = [] } = {}) {
  hook.state.me = { ...admin };
  hook.ensurePlatformUpdateSession();
  const update = { update_check: {}, capabilities: caps, rollback_versions: rollbackVersions };
  hook.state.version = { version: "0.4.2", capabilities: caps };
  hook.state.versionUpdate = update;
  hook.state.docs.badgeLoaded = true;
  hook.state.docs.version = hook.state.version;
  hook.state.docs.update = update;
}

function readinessText() {
  hook.renderPlatformUpdate();
  return elementFor("#updateReadinessMessage").textContent;
}

function rollbackTitle() {
  hook.renderVersionBadgeRollback();
  return elementFor("#versionPopoverRollbackBtn").title;
}

const blocked = {
  deployment: "systemd", check: true, download: false, apply: false, rollback: false, reason: "",
};
const rollbackVersions = [{ version: "0.4.1", manifest_sha256: "a".repeat(64) }];

// 1. An installation migration requirement is described accurately, not as a
//    missing updater, in the readiness sentence and the rollback explanation.
prepare({ ...blocked, reason: "update_installation_migration_required" });
const migrationText = readinessText();
assert.equal(migrationText, copy.errors.update_installation_migration_required);
assert.match(migrationText, /安装迁移/);
assert.ok(!migrationText.includes("安装并启动更新组件"), "migration must not claim the updater is missing");
assert.equal(hook.updateActionAllowed("apply"), false, "blocked update must stay disabled");

prepare({ ...blocked, reason: "update_installation_migration_required" }, { rollbackVersions });
assert.equal(rollbackTitle(), copy.errors.update_installation_migration_required);
assert.equal(elementFor("#versionPopoverRollbackBtn").disabled, true);

// 2. Unrecognized readiness codes fall back to neutral copy, never to the
//    "updater missing" claim, and never surface raw server text.
const unknownCaps = { ...blocked, reason: "update_future_unknown_reason" };
prepare(unknownCaps);
const unknownText = readinessText();
assert.equal(unknownText, copy.messages.readiness_unknown);
assert.ok(!unknownText.includes("独立更新器"), "unknown code must not assert a missing updater");

prepare(unknownCaps, { rollbackVersions });
assert.equal(rollbackTitle(), copy.messages.readiness_unknown);

const rawServerText = "RAW-SERVER-TEXT <img src=x onerror=alert(1)>";
const rendered = hook.updateErrorMessage(
  { code: "update_future_unknown_reason", message: rawServerText, detail: rawServerText },
  "readiness_unknown",
);
assert.equal(rendered, copy.messages.readiness_unknown);
assert.ok(!rendered.includes("RAW-SERVER-TEXT") && !rendered.includes("<img"));

// 3. Absent readiness codes behave the same way.
prepare({ ...blocked, reason: "", error_code: "" });
assert.equal(readinessText(), copy.messages.readiness_unknown);
prepare({ ...blocked, reason: "", error_code: "" }, { rollbackVersions });
assert.equal(rollbackTitle(), copy.messages.readiness_unknown);
assert.ok(!String(rollbackTitle()).includes("独立更新器"));

// 4. Explicit known failures keep their specific messages.
assert.equal(hook.updateErrorMessage({ code: "update_updater_not_initialized" }), copy.errors.update_updater_not_initialized);
assert.match(copy.errors.update_updater_not_initialized, /尚未完成可信初始化/);
prepare({ ...blocked, reason: "update_updater_not_initialized" });
assert.equal(readinessText(), copy.errors.update_updater_not_initialized);
assert.equal(hook.updateErrorMessage({ code: "update_signature_invalid" }), copy.errors.update_signature_invalid);
assert.equal(hook.updateErrorMessage({ code: "authentication_failed" }), copy.errors.authentication_failed);
assert.equal(hook.updateErrorMessage({ status: 401 }), copy.errors.authentication_failed);

// 5. Capability and permission checks are preserved: ready capabilities stay
//    enabled, unavailable ones stay disabled, and non-admins never pass.
const readyCaps = { deployment: "systemd", check: true, download: true, apply: true, rollback: true, ready: true, reason: "" };
prepare(readyCaps);
assert.equal(hook.updateActionAllowed("apply"), true);
assert.equal(hook.updateActionAllowed("rollback"), true);
assert.equal(readinessText(), copy.messages.unchecked);

hook.state.me = { id: 2, username: "user", role: "user", is_admin: false };
assert.equal(hook.updateActionAllowed("apply"), false);
assert.equal(hook.updateActionAllowed("rollback"), false);

console.log("update-readiness-copy.test.mjs: all assertions passed");

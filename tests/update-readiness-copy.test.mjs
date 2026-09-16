// Regression test for update readiness copy in frontend/assets/app.js.
//
// Coverage boundary: this test boots the real frontend/assets/app.js inside
// node:vm with a minimal DOM stub and a one-line export hook injected into the
// in-memory copy (the source file itself is not modified). It then drives the
// real render and request functions. It covers update readiness copy, download
// progress, and request/session races through a deterministic fetch stub. It
// does not exercise a browser, the backend, real HTTP/network behavior, or the
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
    + " ensurePlatformUpdateSession, renderPlatformUpdate, renderVersionBadgeRollback,"
    + " checkPlatformUpdate, updateCapabilities, loadVersionInfo, acceptUpdatePreparation,"
    + " renderUpdateDownload, preparePlatformUpdate, pollUpdateOperation, closePlatformUpdate, openPlatformUpdate };",
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
    focusCount: 0,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    removeEventListener() {},
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) {
      if (name === "title") this.title = "";
      else if (name === "open") this.open = false;
      else if (name === "value") delete this.value;
    },
    getAttribute() { return null; },
    hasAttribute() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return element; },
    focus() { this.focusCount += 1; },
    showModal() { this.open = true; },
    close() { this.open = false; },
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
  const session = hook.ensurePlatformUpdateSession();
  if (session) session.probeError = "";
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

// 6. Manual-check and status-read failure paths. The manual check response is
//    release discovery data only, so checkPlatformUpdate must read the
//    authoritative status; no single status failure may drop capabilities, and
//    a real read failure must stay visible and recoverable.
assert.equal(typeof hook.checkPlatformUpdate, "function");
assert.equal(typeof hook.updateCapabilities, "function");
assert.equal(typeof hook.loadVersionInfo, "function");

function jsonResponse(payload, status = 200) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get(name) { return String(name).toLowerCase() === "content-type" ? "application/json" : ""; } },
    async json() { return payload; },
    async text() { return JSON.stringify(payload); },
  };
}

const readyCapabilities = {
  deployment: "systemd", check: true, download: true, apply: true, rollback: true, ready: true, reason: "",
};
const blockedCapabilities = {
  deployment: "systemd", check: true, download: false, apply: false, rollback: false, ready: false,
  reason: "update_launcher_stale",
};
const readyUpdateCheck = { status: "available", available: true, version: "0.4.5-update-test.5", release_notes: "" };
const probePayload = {
  channel: "release", current_version: "0.4.5-update-test.4", status: "available", available: true,
  version: "0.4.5-update-test.5", published_at: "", release_notes: "", error_code: "",
  update_probe: { state: "idle", manual_cooldown_until: 0 },
};
const versionBody = (caps) => ({ version: "0.4.5-update-test.4", capabilities: caps, update_check: readyUpdateCheck });
const updatesBody = (caps) => ({ capabilities: caps, update_check: readyUpdateCheck, rollback_versions: [], operation: null });

let routeHandler = null;
sandbox.fetch = async (input, options = {}) => {
  const url = String(input);
  const method = String(options.method || "GET").toUpperCase();
  if (!routeHandler) throw new Error("no route handler");
  return routeHandler(url, method);
};
function routes(map) {
  return async (url, method) => {
    const entry = map[method + " " + url.slice(url.lastIndexOf("/api"))];
    if (!entry) throw new Error("unexpected " + method + " " + url);
    if (entry.fail) throw new Error(entry.fail);
    return jsonResponse(entry.body, entry.status || 200);
  };
}
function resetScenario() {
  hook.state.me = { ...admin };
  hook.state.platformUpdate = null;
  hook.state.version = { version: "0.4.5-update-test.4" };
  hook.state.versionUpdate = null;
  hook.state.docs.version = null;
  hook.state.docs.update = null;
  hook.state.docs.badgeLoaded = false;
}
function downloadButton() { return elementFor("#updateDownloadButton"); }

// 6a. Fast path: probe + both status reads succeed -> real deployment, enabled.
resetScenario();
routeHandler = routes({
  "POST /api/admin/updates/check": { body: probePayload },
  "GET /api/version": { body: versionBody(readyCapabilities) },
  "GET /api/admin/updates": { body: updatesBody(readyCapabilities) },
});
await hook.checkPlatformUpdate();
assert.equal(hook.updateCapabilities().deployment, "systemd");
assert.equal(hook.updateCapabilities().apply, true);
assert.equal(hook.state.docs.badgeLoaded, true);
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.systemd);
assert.equal(elementFor("#updateTargetVersion").textContent, "0.4.5-update-test.5");
assert.equal(downloadButton().disabled, false);

// 6b. One status read fails: the other's valid capabilities must survive.
resetScenario();
routeHandler = routes({
  "GET /api/version": { body: versionBody(readyCapabilities) },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.loadVersionInfo({ force: true });
assert.equal(hook.state.docs.badgeLoaded, true);
assert.equal(hook.updateCapabilities().deployment, "systemd");
assert.equal(hook.updateCapabilities().apply, true);
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.systemd);
assert.equal(downloadButton().disabled, false);

// 6c. Both status reads fail: a locatable read error, not a marked-loaded
//     unknown snapshot; a later successful read recovers.
resetScenario();
routeHandler = routes({
  "GET /api/version": { fail: "status-read-failed" },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.loadVersionInfo({ force: true });
assert.equal(hook.state.docs.badgeLoaded, false, "failed status read must not mark the snapshot loaded");
assert.equal(readinessText(), copy.messages.probe_failed, "real read error must be shown");
assert.equal(hook.updateActionAllowed("apply"), false);
assert.equal(downloadButton().disabled, true);
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.unknown);
routeHandler = routes({
  "GET /api/version": { body: versionBody(readyCapabilities) },
  "GET /api/admin/updates": { body: updatesBody(readyCapabilities) },
});
await hook.loadVersionInfo({ force: true });
assert.equal(hook.state.docs.badgeLoaded, true, "a later successful read must recover");
assert.equal(hook.updateCapabilities().apply, true);
assert.equal(downloadButton().disabled, false);

// 6d. Incomplete probe (probe ok, both status reads fail) must not mark loaded
//     and must show the real read error, never "reason not identified".
resetScenario();
routeHandler = routes({
  "POST /api/admin/updates/check": { body: probePayload },
  "GET /api/version": { fail: "status-read-failed" },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.checkPlatformUpdate();
assert.equal(hook.state.docs.badgeLoaded, false);
assert.equal(readinessText(), copy.messages.probe_failed);
assert.notEqual(readinessText(), copy.messages.readiness_unknown);
assert.equal(hook.updateActionAllowed("apply"), false);
assert.equal(downloadButton().disabled, true);

// 6e. A slow initial read racing a manual check: the stale initial result must
//     not clobber the manual check's authoritative snapshot.
resetScenario();
let versionCalls = 0;
let releaseInitial;
const initialVersion = new Promise((resolve) => { releaseInitial = resolve; });
routeHandler = async (url) => {
  if (url.endsWith("/api/version")) {
    versionCalls += 1;
    if (versionCalls === 1) {
      return initialVersion.then(() => jsonResponse(versionBody(blockedCapabilities)));
    }
    return jsonResponse(versionBody(readyCapabilities));
  }
  if (url.endsWith("/api/admin/updates/check")) return jsonResponse(probePayload);
  if (url.endsWith("/api/admin/updates")) return jsonResponse(updatesBody(readyCapabilities));
  throw new Error("unexpected " + url);
};
const initialLoad = hook.loadVersionInfo({ force: true });
await hook.checkPlatformUpdate();
assert.equal(hook.updateCapabilities().apply, true);
releaseInitial();
await initialLoad;
assert.equal(hook.updateCapabilities().apply, true, "stale initial snapshot must not clobber the manual result");
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.systemd);

// 6f. Both current reads fail but a history exists: keep the display and the
//     real error, and do NOT refresh the successful-read timestamp.
async function seedCache(caps) {
  resetScenario();
  routeHandler = routes({
    "GET /api/version": { body: versionBody(caps) },
    "GET /api/admin/updates": { body: updatesBody(caps) },
  });
  await hook.loadVersionInfo({ force: true });
  assert.equal(hook.state.docs.badgeLoaded, true, "seed cache must load a full state");
}

await seedCache(readyCapabilities);
hook.state.platformUpdate.cacheAt = 12345;
routeHandler = routes({
  "GET /api/version": { fail: "status-read-failed" },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.loadVersionInfo({ force: true });
assert.equal(readinessText(), copy.messages.probe_failed, "real read error must stay visible with a cache");
assert.equal(hook.state.platformUpdate.cacheAt, 12345, "a failed read must not refresh the success timestamp");
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.systemd, "historical display is preserved");

// 6g. /api/admin/updates fails while /api/version returns a NEW false: the
//     fresh capability must beat the cached true.
await seedCache(readyCapabilities);
routeHandler = routes({
  "GET /api/version": { body: versionBody(blockedCapabilities) },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.loadVersionInfo({ force: true });
assert.equal(hook.updateCapabilities().apply, false, "fresh false must win over cached true");
assert.equal(hook.state.versionUpdate.capabilities.apply, false);
assert.equal(elementFor("#updateDeployment").textContent, copy.deployments.systemd);

// 6h. /api/admin/updates fails while /api/version returns a NEW true: the fresh
//     capability must beat the cached false and re-enable the button.
await seedCache(blockedCapabilities);
routeHandler = routes({
  "GET /api/version": { body: versionBody(readyCapabilities) },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.loadVersionInfo({ force: true });
assert.equal(hook.updateCapabilities().apply, true, "fresh true must win over cached false");
assert.equal(hook.state.versionUpdate.capabilities.apply, true);
assert.equal(downloadButton().disabled, false);

// 6i. Manual check with a cache and both reads failing keeps the real error and
//     must not refresh the success timestamp either.
await seedCache(readyCapabilities);
hook.state.platformUpdate.cacheAt = 4321;
routeHandler = routes({
  "POST /api/admin/updates/check": { body: probePayload },
  "GET /api/version": { fail: "status-read-failed" },
  "GET /api/admin/updates": { fail: "status-read-failed" },
});
await hook.checkPlatformUpdate();
assert.equal(readinessText(), copy.messages.probe_failed);
assert.equal(hook.state.platformUpdate.cacheAt, 4321, "manual check must not refresh the success timestamp on read failure");

// 7. Launcher/store readiness reasons are locatable instead of swallowed as
//    "reason not identified".
assert.match(copy.errors.update_launcher_stale, /启动器/);
assert.notEqual(copy.errors.update_launcher_stale, copy.messages.readiness_unknown);
assert.equal(hook.updateErrorMessage({ code: "update_launcher_stale" }), copy.errors.update_launcher_stale);
prepare({ deployment: "systemd", check: true, download: false, apply: false, rollback: false, reason: "update_launcher_stale" });
assert.equal(readinessText(), copy.errors.update_launcher_stale);

// 8. The progress meter reflects measured bytes only. Unknown lengths and
//    verification are indeterminate, including after a determinate download.
function readySession() {
  resetScenario();
  const session = hook.ensurePlatformUpdateSession();
  hook.state.version = versionBody(readyCapabilities);
  hook.state.versionUpdate = updatesBody(readyCapabilities);
  hook.state.docs.version = hook.state.version;
  hook.state.docs.update = hook.state.versionUpdate;
  hook.state.docs.badgeLoaded = true;
  elementFor("#platformUpdateDialog").showModal();
  return session;
}
const preparedRelease = {
  status: "staged", version: readyUpdateCheck.version,
  operation_id: "a".repeat(32), manifest_sha256: "b".repeat(64), release_notes: "",
};
const downloading = {
  active: true, phase: "downloading", version: readyUpdateCheck.version, action: "apply",
  operation_id: preparedRelease.operation_id, downloaded_bytes: 250000, total_bytes: 1000000,
};
let progressSession = readySession();
hook.acceptUpdatePreparation(progressSession, { preparation: downloading });
hook.renderPlatformUpdate();
assert.equal(elementFor("#updateDownloadProgress").hidden, false);
assert.equal(elementFor("#updateDownloadPercent").textContent, "25%");
assert.equal(elementFor("#updateDownloadMeter").value, 25);
assert.equal(elementFor("#updateDownloadAmount").textContent, "250 KB / 1.0 MB");
assert.equal(downloadButton().disabled, true, "another click cannot start a second active download");

hook.acceptUpdatePreparation(progressSession, { preparation: { ...downloading, total_bytes: null } });
hook.renderPlatformUpdate();
assert.equal(elementFor("#updateDownloadPercent").textContent, "");
assert.equal(Object.hasOwn(elementFor("#updateDownloadMeter"), "value"), false, "unknown totals must remove the old percent");
assert.equal(elementFor("#updateDownloadAmount").textContent, "250 KB 已下载");

hook.acceptUpdatePreparation(progressSession, { preparation: { ...downloading, phase: "verifying" } });
hook.renderPlatformUpdate();
assert.equal(elementFor("#updateDownloadPercent").textContent, "");
assert.equal(Object.hasOwn(elementFor("#updateDownloadMeter"), "value"), false);
assert.ok(!elementFor("#updateDownloadAmount").textContent.includes("下载完成"), "metadata verification can precede the main download");

hook.acceptUpdatePreparation(progressSession, { preparation: { ...downloading, downloaded_bytes: 1100000 } });
hook.renderPlatformUpdate();
assert.equal(elementFor("#updateDownloadMeter").value, 100, "meter must not exceed 100 percent");

// 9. A status read can display a prepared package, but cannot give the current
//    login another administrator's staged confirmation. Matching completion
//    removes that preparation instead of showing a stale download card.
const readyPreparation = { ...downloading, active: false, phase: "ready" };
hook.acceptUpdatePreparation(progressSession, {
  preparation: readyPreparation, operation: preparedRelease,
});
hook.renderPlatformUpdate();
assert.equal(progressSession.stage, null);
assert.equal(elementFor("#updatePasswordForm").hidden, true);
assert.equal(elementFor("#updateDownloadMeter").value, 100);
assert.equal(downloadButton().disabled, false, "the download endpoint must bind the prepared package to this login");
hook.acceptUpdatePreparation(progressSession, {
  preparation: readyPreparation,
  operation: { ...preparedRelease, operation_id: "c".repeat(32), status: "succeeded" },
});
assert.equal(progressSession.preparation.phase, "ready", "unrelated historical completion must not clear preparation");
hook.acceptUpdatePreparation(progressSession, {
  preparation: readyPreparation,
  operation: { ...preparedRelease, status: "succeeded" },
});
hook.renderPlatformUpdate();
assert.equal(progressSession.preparation, null);
assert.equal(elementFor("#updateDownloadProgress").hidden, true);

// 10. Closing while POST is in flight keeps the download and its result. A
//     reopen reuses the prepared stage without another download or focus theft.
progressSession = readySession();
let finishDownload;
let downloadCalls = 0;
const downloadResult = new Promise((resolve) => { finishDownload = resolve; });
routeHandler = async (url, method) => {
  if (method === "POST" && url.endsWith("/api/admin/updates/download")) {
    downloadCalls += 1;
    return downloadResult;
  }
  if (method === "GET" && url.endsWith("/api/admin/updates")) {
    return jsonResponse({ ...updatesBody(readyCapabilities), preparation: downloading });
  }
  throw new Error("unexpected " + method + " " + url);
};
const pendingDownload = hook.preparePlatformUpdate();
await hook.preparePlatformUpdate();
assert.equal(downloadCalls, 1);
const passwordFocusBeforeClose = elementFor("#updateAdminPassword").focusCount;
elementFor("#platformUpdateDialog").close();
hook.closePlatformUpdate({ type: "close" });
assert.equal(progressSession.busy, "prepare");
await hook.pollUpdateOperation(progressSession);
assert.equal(progressSession.preparation.downloaded_bytes, 250000, "progress continues while the dialog is closed");
finishDownload(jsonResponse(preparedRelease));
await pendingDownload;
assert.equal(progressSession.stage.operation_id, preparedRelease.operation_id);
assert.equal(progressSession.busy, "");
assert.equal(progressSession.preparation, null);
assert.equal(elementFor("#updateAdminPassword").focusCount, passwordFocusBeforeClose, "a closed dialog must not steal focus");
progressSession.manual = true; // Isolate reopen rendering from the separately tested cache refresh.
hook.openPlatformUpdate();
progressSession.manual = false;
assert.equal(elementFor("#platformUpdateDialog").open, true);
assert.equal(elementFor("#updatePasswordForm").hidden, false);
assert.equal(elementFor("#updateDownloadProgress").hidden, true);
await hook.preparePlatformUpdate();
assert.equal(downloadCalls, 1, "reopening a prepared update must reuse the stage");
elementFor("#platformUpdateDialog").close();
hook.closePlatformUpdate({ type: "close" });
assert.equal(progressSession.stage.operation_id, preparedRelease.operation_id, "closing the confirmation form preserves its stage");

// 11. An older full-status response must not erase live progress while the
//     download POST is pending; the dedicated poll is authoritative then.
progressSession = readySession();
let releaseOldRead;
let finishRacingDownload;
const oldRead = new Promise((resolve) => { releaseOldRead = resolve; });
const racingDownload = new Promise((resolve) => { finishRacingDownload = resolve; });
let statusCalls = 0;
routeHandler = async (url, method) => {
  if (url.endsWith("/api/version")) return oldRead.then(() => jsonResponse(versionBody(readyCapabilities)));
  if (method === "POST" && url.endsWith("/api/admin/updates/download")) return racingDownload;
  if (url.endsWith("/api/admin/updates")) {
    statusCalls += 1;
    return statusCalls === 1
      ? oldRead.then(() => jsonResponse({ ...updatesBody(readyCapabilities), preparation: null }))
      : jsonResponse({ ...updatesBody(readyCapabilities), preparation: downloading });
  }
  throw new Error("unexpected " + method + " " + url);
};
const oldLoad = hook.loadVersionInfo({ force: true });
const racingPrepare = hook.preparePlatformUpdate();
await hook.pollUpdateOperation(progressSession);
releaseOldRead();
await oldLoad;
assert.equal(progressSession.preparation.phase, "downloading");
assert.equal(progressSession.preparation.downloaded_bytes, 250000);
finishRacingDownload(jsonResponse(preparedRelease));
await racingPrepare;

// 12. A failed transfer displays only mapped error text, releases the button,
//     and allows one fresh attempt. Progress-read failures never claim restart.
progressSession = readySession();
downloadCalls = 0;
const failedPreparation = {
  ...downloading, active: false, phase: "failed", error_code: "update_archive_hash_mismatch",
  message: "PRIVATE-SERVER-DETAIL", detail: "PRIVATE-SERVER-DETAIL",
};
routeHandler = async (url, method) => {
  if (method === "POST" && url.endsWith("/api/admin/updates/download")) {
    downloadCalls += 1;
    return downloadCalls === 1
      ? jsonResponse({ detail: { code: "update_archive_hash_mismatch", message: "PRIVATE-SERVER-DETAIL" } }, 400)
      : jsonResponse(preparedRelease);
  }
  if (url.endsWith("/api/admin/updates")) return jsonResponse({ ...updatesBody(readyCapabilities), preparation: failedPreparation });
  throw new Error("unexpected " + method + " " + url);
};
await hook.preparePlatformUpdate();
assert.equal(progressSession.preparation.active, false, "a rejected request must stop the optimistic download before polling");
assert.equal(progressSession.preparation.phase, "failed");
assert.equal(progressSession.error, copy.errors.update_archive_hash_mismatch);
assert.equal(downloadButton().disabled, false, "a definitive rejection must remain retryable even when status reads fail");
progressSession.manual = true; // Prevent unrelated automatic version refresh after terminal status.
await hook.pollUpdateOperation(progressSession);
progressSession.manual = false;
assert.equal(elementFor("#updateDownloadMeter").hidden, true);
assert.equal(elementFor("#updateDownloadAmount").textContent, copy.errors.update_archive_hash_mismatch);
assert.ok(!elementFor("#updateDownloadAmount").textContent.includes("PRIVATE-SERVER-DETAIL"));
assert.equal(downloadButton().disabled, false);
await hook.preparePlatformUpdate();
assert.equal(downloadCalls, 2);
assert.equal(progressSession.stage.operation_id, preparedRelease.operation_id);

progressSession = readySession();
hook.acceptUpdatePreparation(progressSession, { preparation: downloading });
routeHandler = async () => { throw new Error("offline"); };
await hook.pollUpdateOperation(progressSession);
assert.equal(progressSession.reconnecting, false, "download polling failure must not imply service restart");
assert.match(progressSession.probeError, /下载进度.*重试/);
routeHandler = async () => jsonResponse({ ...updatesBody(readyCapabilities), preparation: downloading });
await hook.pollUpdateOperation(progressSession);
assert.equal(progressSession.probeError, "", "a successful progress read clears the read error");

// 13. A previous login's delayed POST and status poll cannot mutate the new
//     login's state, even when both users are administrators.
progressSession = readySession();
let finishPreviousPost;
let finishPreviousPoll;
routeHandler = async (url, method) => {
  if (method === "POST" && url.endsWith("/api/admin/updates/download")) {
    return new Promise((resolve) => { finishPreviousPost = resolve; });
  }
  if (url.endsWith("/api/admin/updates")) return new Promise((resolve) => { finishPreviousPoll = resolve; });
  throw new Error("unexpected " + method + " " + url);
};
const previousPost = hook.preparePlatformUpdate();
const previousPoll = hook.pollUpdateOperation(progressSession);
hook.state.me = { ...admin, id: 9, username: "other-admin" };
const newSession = hook.ensurePlatformUpdateSession();
finishPreviousPost(jsonResponse(preparedRelease));
finishPreviousPoll(jsonResponse({ ...updatesBody(readyCapabilities), preparation: downloading }));
await Promise.all([previousPost, previousPoll]);
assert.equal(hook.state.platformUpdate, newSession);
assert.equal(newSession.stage, null);
assert.equal(newSession.preparation, null);
assert.equal(newSession.busy, "");
assert.equal(newSession.probeError, "");

console.log("update-readiness-copy.test.mjs: all assertions passed");

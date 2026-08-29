import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontend = path.join(root, "frontend");
const assets = path.join(frontend, "assets");
const port = Number.parseInt(process.env.SAAS_DEV_WEB_PORT || "4173", 10);
const apiOrigin = process.env.SAAS_DEV_API_ORIGIN || "http://127.0.0.1:8096";
const maxBodyBytes = 16 * 1024 * 1024;

const contentTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".woff2", "font/woff2"],
  [".zip", "application/zip"],
]);

function send(response, status, body, headers = {}) {
  response.writeHead(status, {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    ...headers,
  });
  response.end(body);
}

async function requestBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) throw new Error("body_too_large");
    chunks.push(chunk);
  }
  return chunks.length ? Buffer.concat(chunks) : undefined;
}

async function proxyApi(request, response, url) {
  const target = new URL(url.pathname.slice("/xianyu-saas".length) + url.search, apiOrigin);
  const headers = new Headers();
  for (const [name, value] of Object.entries(request.headers)) {
    if (value === undefined || ["connection", "content-length", "host"].includes(name)) continue;
    headers.set(name, Array.isArray(value) ? value.join(", ") : value);
  }
  if (request.headers.host) headers.set("x-forwarded-host", request.headers.host);
  headers.set("x-forwarded-proto", "http");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 75_000);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method || "GET") ? undefined : await requestBody(request),
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = {};
    for (const [name, value] of upstream.headers) {
      if (!["connection", "content-length", "transfer-encoding"].includes(name)) responseHeaders[name] = value;
    }
    if (typeof upstream.headers.getSetCookie === "function") {
      const cookies = upstream.headers.getSetCookie();
      if (cookies.length) responseHeaders["set-cookie"] = cookies;
    }
    send(response, upstream.status, Buffer.from(await upstream.arrayBuffer()), responseHeaders);
  } catch (error) {
    const status = error?.message === "body_too_large" ? 413 : 502;
    send(response, status, JSON.stringify({ detail: "本地 API 暂时不可用" }), {
      "content-type": "application/json; charset=utf-8",
    });
  } finally {
    clearTimeout(timeout);
  }
}

async function serveStatic(response, file) {
  try {
    const body = await fs.readFile(file);
    send(response, 200, body, {
      "content-type": contentTypes.get(path.extname(file)) || "application/octet-stream",
    });
  } catch {
    send(response, 404, "Not found", { "content-type": "text/plain; charset=utf-8" });
  }
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || "/", "http://127.0.0.1");
  if (url.pathname === "/xianyu-saas") {
    response.writeHead(308, { location: "/xianyu-saas/" });
    response.end();
    return;
  }
  if (url.pathname.startsWith("/xianyu-saas/api/")) {
    await proxyApi(request, response, url);
    return;
  }
  if (url.pathname === "/xianyu-saas/") {
    await serveStatic(response, path.join(frontend, "index.html"));
    return;
  }
  if (url.pathname.startsWith("/xianyu-saas/assets/")) {
    const relative = decodeURIComponent(url.pathname.slice("/xianyu-saas/assets/".length));
    const target = path.resolve(assets, relative);
    if (target !== assets && target.startsWith(assets + path.sep)) {
      await serveStatic(response, target);
      return;
    }
  }
  send(response, 404, "Not found", { "content-type": "text/plain; charset=utf-8" });
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`DeepWhale 闲鱼客服本地页面: http://127.0.0.1:${port}/xianyu-saas/\n`);
});

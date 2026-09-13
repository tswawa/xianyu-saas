#!/usr/bin/env python3
"""Static deployment guards for shop login endpoints."""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
LOCATIONS = (ROOT / "deploy/nginx/xianyu-saas.locations.conf").read_text(encoding="utf-8")
ZONES = (ROOT / "deploy/nginx/xianyu-saas-rate-limits.conf").read_text(encoding="utf-8")
SERVICE = (ROOT / "deploy/systemd/xianyu-saas.service").read_text(encoding="utf-8")
CONSUMER_SERVICE = (ROOT / "deploy/systemd/xianyu-saas-consumer.service").read_text(encoding="utf-8")
UPDATER_SERVICE = (ROOT / "deploy/systemd/xianyu-saas-updater.service").read_text(encoding="utf-8")
UPDATER_PATH = (ROOT / "deploy/systemd/xianyu-saas-updater.path").read_text(encoding="utf-8")
BOOTSTRAP_DROPIN = (ROOT / "deploy/systemd/xianyu-saas-bootstrap.conf.example").read_text(encoding="utf-8")
UPDATER_SOURCE = (ROOT / "deploy/updater/updater.py").read_text(encoding="utf-8")
API_SOURCE = (ROOT / "backend/app.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKER_UPDATES = (ROOT / "docker-compose.updates.yml").read_text(encoding="utf-8")
DOCKER_ENGINE = (ROOT / "backend/docker_engine.py").read_text(encoding="utf-8")
DOCKER_UPDATER = (ROOT / "backend/docker_updater.py").read_text(encoding="utf-8")
LOGROTATE = (ROOT / "deploy/xianyu-saas-bot-logrotate.conf").read_text(encoding="utf-8")

BOOTSTRAP = "location = /xianyu-saas/api/auth/bootstrap {"
ADMIN_CONFIRM = "location = /xianyu-saas/api/admin/confirm {"
ADMIN_UPDATES = "location ^~ /xianyu-saas/api/admin/updates/ {"
LOGIN_START = "location = /xianyu-saas/api/bot/login/start {"
LOGIN_COMPLETE = "location = /xianyu-saas/api/bot/login/complete {"
LOGIN_SESSION = "location ^~ /xianyu-saas/api/bot/login/ {"
GENERIC = "location ^~ /xianyu-saas/api/ {"

assert LOCATIONS.count(BOOTSTRAP) == 1
assert LOCATIONS.count(ADMIN_CONFIRM) == 1
assert LOCATIONS.count(ADMIN_UPDATES) == 1
assert LOCATIONS.count(LOGIN_START) == 1
assert LOCATIONS.count(LOGIN_COMPLETE) == 1
assert LOCATIONS.count(LOGIN_SESSION) == 1
assert LOCATIONS.index(BOOTSTRAP) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(ADMIN_CONFIRM) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(ADMIN_UPDATES) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(LOGIN_START) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(LOGIN_COMPLETE) < LOCATIONS.index(LOGIN_SESSION)
assert LOCATIONS.index(LOGIN_SESSION) < LOCATIONS.index(GENERIC)

bootstrap = LOCATIONS.split(BOOTSTRAP, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_auth_bootstrap burst=2 nodelay;",
    "limit_except POST { deny all; }",
    "client_max_body_size 32k;",
    "client_body_timeout 10s;",
    "proxy_pass http://127.0.0.1:8096/api/auth/bootstrap;",
    "proxy_read_timeout 30s;",
):
    assert directive in bootstrap, directive

admin_confirm = LOCATIONS.split(ADMIN_CONFIRM, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_admin_confirm burst=3 nodelay;",
    "limit_except POST { deny all; }",
    "client_max_body_size 8k;",
    "proxy_pass http://127.0.0.1:8096/api/admin/confirm;",
):
    assert directive in admin_confirm, directive

admin_updates = LOCATIONS.split(ADMIN_UPDATES, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_platform_update burst=4 nodelay;",
    "limit_except POST { deny all; }",
    "client_max_body_size 8k;",
    "proxy_pass http://127.0.0.1:8096/api/admin/updates/;",
    "proxy_read_timeout 10m;",
):
    assert directive in admin_updates, directive

assert "zone=xianyu_auth_bootstrap:1m rate=3r/m;" in ZONES
assert "zone=xianyu_admin_confirm:1m rate=6r/m;" in ZONES
assert "zone=xianyu_platform_update:1m rate=12r/m;" in ZONES
assert "zone=xianyu_login_poll:1m rate=60r/m;" in ZONES
assert "zone=xianyu_login_complete:1m rate=6r/m;" in ZONES
assert "zone=xianyu_login_conn:1m;" in ZONES

# 每个被引用的限流 zone 都必须在仓库内定义，否则自建者的 nginx 会因
# unknown limit_req zone 启动失败。
referenced_zones = set(re.findall(r"limit_req zone=([a-z_]+)", LOCATIONS))
referenced_zones |= set(re.findall(r"limit_conn ([a-z_]+) ", LOCATIONS))
defined_zones = set(re.findall(r"zone=([a-z_]+):", ZONES))
missing_zones = referenced_zones - defined_zones
assert not missing_zones, f"locations.conf 引用了未定义的 zone: {sorted(missing_zones)}"

login_start = LOCATIONS.split(LOGIN_START, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_session burst=5 nodelay;",
    "limit_conn xianyu_login_conn 2;",
    "limit_conn_status 429;",
    "limit_except POST { deny all; }",
    "client_max_body_size 1k;",
    "proxy_pass http://127.0.0.1:8096/api/bot/login/start;",
    "proxy_read_timeout 20s;",
):
    assert directive in login_start, directive

login_complete = LOCATIONS.split(LOGIN_COMPLETE, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_login_complete burst=2 nodelay;",
    "limit_conn xianyu_login_conn 2;",
    "limit_conn_status 429;",
    "limit_except POST { deny all; }",
    "client_max_body_size 1k;",
    "proxy_pass http://127.0.0.1:8096/api/bot/login/complete;",
    "proxy_read_timeout 70s;",
):
    assert directive in login_complete, directive

login_session = LOCATIONS.split(LOGIN_SESSION, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_login_poll burst=8 nodelay;",
    "limit_conn xianyu_login_conn 3;",
    "limit_conn_status 429;",
    "limit_except GET POST { deny all; }",
    "client_max_body_size 1k;",
    "proxy_pass http://127.0.0.1:8096/api/bot/login/;",
    "proxy_read_timeout 30s;",
):
    assert directive in login_session, directive

generic = LOCATIONS.split(GENERIC, 1)[1].split("\n}", 1)[0]
assert "limit_except GET POST PUT PATCH DELETE { deny all; }" in generic
assert "client_max_body_size 16m;" in generic
for directive in (
    "proxy_set_header Host $host;",
    "proxy_set_header X-Real-IP $remote_addr;",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "proxy_set_header X-Forwarded-Proto $scheme;",
):
    assert directive in generic, directive
assert "# Reject /xianyu-saas/internal/*" in LOCATIONS
assert "location ^~ /xianyu-saas/ {\n    return 404;\n}" in LOCATIONS
for header in (
    "Content-Security-Policy",
    "Permissions-Policy",
    "Referrer-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
):
    assert header in LOCATIONS, header

api_exec = next(line for line in SERVICE.splitlines() if line.startswith("ExecStart="))
assert "uvicorn app:app" in api_exec
assert "--proxy-headers" in api_exec
assert "--forwarded-allow-ips=127.0.0.1" in api_exec
assert "--limit-concurrency 100" in api_exec
assert "--backlog 128" in api_exec
assert "--workers" not in api_exec
assert "--preload" not in api_exec
for unit in (SERVICE, CONSUMER_SERVICE):
    assert "WorkingDirectory=/opt/xianyu-saas/current/backend" in unit
    assert "Environment=SAAS_CURRENT_ROOT=/opt/xianyu-saas/current" in unit
    assert "Environment=SAAS_RELEASE_KIND=standalone" in unit
    assert "Environment=PYTHONPATH=/opt/xianyu-saas/current/runtime/site/backend" in unit
    assert "ExecStart=/opt/xianyu-saas/current/runtime/python/bin/python3" in unit
    assert "ReadWritePaths=/var/lib/xianyu-saas" in unit
    assert "ProtectSystem=strict" in unit
    assert "UMask=0077" in unit
assert "Environment=SAAS_BOT_PYTHON=/opt/xianyu-saas/current/runtime/python/bin/python3" in SERVICE
assert "Environment=SAAS_BOT_PYTHONPATH=/opt/xianyu-saas/current/runtime/site/worker" in SERVICE
assert "Environment=SAAS_UPDATE_PUBLIC_KEY_FILE=/etc/xianyu-saas/update-signing.pub" in SERVICE

for directive in (
    "Type=oneshot",
    "User=root",
    "Environment=SAAS_CURRENT_LINK=/opt/xianyu-saas/current",
    "Environment=SAAS_RELEASES_DIR=/opt/xianyu-saas/releases",
    "Environment=SAAS_UPDATE_STAGING_DIR=/var/lib/xianyu-saas/update-staging",
    "Environment=SAAS_UPDATE_INTENT_FILE=/var/lib/xianyu-saas-updates/intent.json",
    "Environment=SAAS_UPDATER_STATE_DIR=/var/lib/xianyu-saas-updater",
    "Environment=SAAS_RELEASE_KIND=standalone",
    "Environment=SAAS_UPDATE_PUBLIC_KEY_FILE=/etc/xianyu-saas/update-signing.pub",
    "Environment=SAAS_MANAGER_CURRENT=/opt/xianyu-saas/manager/current",
    "ExecStart=/opt/xianyu-saas/manager/current/xianyu-saas internal consume-intent",
    "ProtectSystem=strict",
    "ReadWritePaths=/opt/xianyu-saas /var/lib/xianyu-saas",
    "UMask=0077",
):
    assert directive in UPDATER_SERVICE, directive
assert "PathExists=/var/lib/xianyu-saas-updates/intent.json" in UPDATER_PATH
assert "PathExists=/var/lib/xianyu-saas-updater/active.json" in UPDATER_PATH
assert "Unit=xianyu-saas-updater.service" in UPDATER_PATH
assert "DirectoryMode=01770" in UPDATER_PATH
assert "Environment=SAAS_UPDATE_STATUS_DIR=/var/lib/xianyu-saas-updates/status" in SERVICE
assert "ReadWritePaths=/var/lib/xianyu-saas -/var/lib/xianyu-saas-updates" in SERVICE
assert "/updater/deploy/updater/updater.py" not in next(line for line in UPDATER_SERVICE.splitlines() if line.startswith("ExecStart="))
assert "StateDirectory=xianyu-saas-updater" in UPDATER_SERVICE
for directive in (
    "LoadCredential=bootstrap-token:/etc/xianyu-saas/bootstrap-token",
    "Environment=SAAS_BOOTSTRAP_ENABLED=1",
    "Environment=SAAS_BOOTSTRAP_TOKEN_FILE=bootstrap-token",
    "Environment=SAAS_BOOTSTRAP_TRUSTED_SOURCES=127.0.0.1,::1",
):
    assert directive in BOOTSTRAP_DROPIN, directive
for implementation_guard in (
    "fcntl.flock",
    "source.backup(destination)",
    "os.replace(temporary, config.current_link)",
    "RELEASE_KEEP = 3",
    "update_downgrade_rejected",
    "check_health",
    "_child_environment",
):
    assert implementation_guard in UPDATER_SOURCE, implementation_guard
assert "git pull" not in UPDATER_SOURCE.lower()
assert "systemctl" not in API_SOURCE.lower()
assert "git pull" not in API_SOURCE.lower()
for application_web_guard in (
    'PUBLIC_WEB_PREFIX = "/xianyu-saas"',
    "def _rewrite_public_api_scope(",
    "def _restore_public_api_redirect(",
    "def _enforce_request_body_limit(",
    "def _browser_host_trusted(",
    'os.environ.get("SAAS_CURRENT_ROOT"',
    "StaticFiles(directory=FRONTEND_ASSETS_ROOT",
    '"public, max-age=604800, immutable"',
    '"Permissions-Policy": "camera=(), geolocation=(), microphone=()"',
):
    assert application_web_guard in API_SOURCE, application_web_guard
assert "upgrade-insecure-requests" not in API_SOURCE

# Docker updater: the official fixed Compose plugin owns only the application
# lifecycle. The web IPC remains path/command-free and the updates overlay is last.
for directive in (
    "FROM docker:28.3.3-cli AS updater-docker-cli",
    "FROM docker/buildx-bin:0.26.1 AS updater-buildx",
    "FROM docker:29.8.0-cli AS updater-compose-cli",
    "COPY --from=updater-compose-cli /usr/local/libexec/docker/cli-plugins/docker-compose",
    'test "$(docker compose version --short)" = "5.5.1"',
):
    assert directive in DOCKERFILE, directive
app_overlay = DOCKER_UPDATES.split("\n  xianyu-saas:\n", 1)[1].split("\n  xianyu-updater:\n", 1)[0]
assert "container_name:" not in app_overlay
assert "必须最后应用" in DOCKER_UPDATES
for target in ("target: /updates", "target: /app/update-signing.pub"):
    assert target in app_overlay, target
for guard in (
    "def register(",
    "def validate_registration(",
    "def compose_up(",
    '"--no-deps", "--no-build"',
    '"--pull", "never"',
    '"stop", "--timeout", "60"',
):
    assert guard in DOCKER_ENGINE, guard
assert "def replace(" not in DOCKER_ENGINE
assert "NetworkingConfig" not in DOCKER_ENGINE
for guard in ("docker-deployment.json", "trusted-update-signing.pub", "def initialize(", "compose_stop_started", "compose_up_started", "compose_restore_started"):
    assert guard in DOCKER_UPDATER, guard
for forbidden in ("down -v", "renew-anon-volumes", "remove-orphans", "prune"):
    assert forbidden not in (DOCKER_ENGINE + DOCKER_UPDATER).lower(), forbidden

tenants_log_root = str(PurePosixPath("/", "var", "lib", "xianyu-saas", "tenants"))
for directive in (
    f"{tenants_log_root}/*/bot.log",
    f"{tenants_log_root}/*/accounts/*/bot.log",
    "su xianyu-saas xianyu-saas",
    "daily",
    "size 10M",
    "rotate 14",
    "compress",
    "copytruncate",
):
    assert directive in LOGROTATE, directive


def _reserve_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _raw_http_request(
    port: int,
    target: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> bytes:
    request_headers = {"Host": "127.0.0.1", "Connection": "close", **(headers or {})}
    if body and not any(name.lower() == "content-length" for name in request_headers):
        request_headers["Content-Length"] = str(len(body))
    encoded_headers = "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(f"{method} {target} HTTP/1.1\r\n{encoded_headers}\r\n".encode("latin-1") + body)
        chunks = []
        while chunk := connection.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


def _raw_http_get(port: int, target: str) -> bytes:
    return _raw_http_request(port, target)


def _parse_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert separator, raw[:120]
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers[name.decode("ascii").lower()] = value.decode("latin-1").strip()
    return status, headers, body


def _assert_frontend_csp_contract() -> None:
    frontend_js = (ROOT / "frontend/assets/app.js").read_text(encoding="utf-8")
    frontend_css = (ROOT / "frontend/assets/app.css").read_text(encoding="utf-8")
    frontend_html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    for forbidden in ('style=', '.style.', 'onerror=', 'onclick='):
        assert forbidden not in frontend_js, f"frontend JavaScript violates style-src/script-src CSP: {forbidden}"
    preload = re.search(r'<link rel="preload" href="([^"]+\.woff2\?v=[^"]+)"', frontend_html)
    font_source = re.search(r'url\("([^"]+\.woff2\?v=[^"]+)"\)', frontend_css)
    assert preload and font_source
    assert preload.group(1) == font_source.group(1), "font preload and CSS source must use the same cache version"


def _assert_application_serving_contract() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory(prefix="xianyu-app-web-contract-") as temporary:
        run = Path(temporary)
        release = run / "release"
        assets = release / "frontend" / "assets"
        assets.mkdir(parents=True)
        index_payload = b"<!doctype html><title>application-serving-contract</title>\n"
        asset_payload = b"window.applicationServingContract = true;\n"
        (release / "frontend" / "index.html").write_bytes(index_payload)
        (assets / "app.js").write_bytes(asset_payload)
        (release / "frontend" / "frontend-secret.txt").write_bytes(b"frontend-secret")
        (release / "release-secret.txt").write_bytes(b"release-secret")

        port = _reserve_loopback_port()
        environment = os.environ.copy()
        state = run / "state"
        environment.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "SAAS_TESTING": "0",
            "SAAS_ENV": "production",
            "SAAS_COOKIE_SECURE": "0",
            "SAAS_CURRENT_ROOT": str(release),
            "SAAS_DB": str(state / "control.db"),
            "SAAS_TENANTS_DIR": str(state / "tenants"),
            "SAAS_UPDATE_INTENT_FILE": str(state / "update-intent.json"),
            "SAAS_UPDATE_MAINTENANCE_FILE": str(state / "maintenance.json"),
            "SAAS_UPDATE_STATUS_DIR": str(state / "update-status"),
        })
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
                "--lifespan",
                "off",
            ],
            cwd=ROOT / "backend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(200):
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(f"application server exited during startup: {stdout}{stderr}")
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.02)
            else:
                raise AssertionError("application server did not start")

            status, headers, body = _parse_http_response(_raw_http_get(port, "/xianyu-saas"))
            assert status == 308 and headers.get("location") == "/xianyu-saas/"

            status, headers, body = _parse_http_response(_raw_http_get(port, "/xianyu-saas/"))
            assert status == 200 and body == index_payload
            expected_security = {
                "content-security-policy": (
                    "default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; "
                    "form-action 'self'; frame-ancestors 'none'; img-src 'self'; object-src 'none'; "
                    "script-src 'self'; style-src 'self'"
                ),
                "permissions-policy": "camera=(), geolocation=(), microphone=()",
                "referrer-policy": "same-origin",
                "x-content-type-options": "nosniff",
                "x-frame-options": "DENY",
            }
            for name, value in expected_security.items():
                assert headers.get(name) == value, (name, headers)
            assert headers.get("cache-control") == "no-cache"
            assert "upgrade-insecure-requests" not in headers["content-security-policy"]

            status, headers, body = _parse_http_response(
                _raw_http_get(port, "/xianyu-saas/assets/app.js?v=contract")
            )
            assert status == 200 and body == asset_payload
            for name, value in expected_security.items():
                assert headers.get(name) == value, (name, headers)
            assert headers.get("cache-control") == "public, max-age=604800, immutable"

            direct_health = _parse_http_response(_raw_http_get(port, "/api/health"))
            public_health = _parse_http_response(_raw_http_get(port, "/xianyu-saas/api/health"))
            root_health = _parse_http_response(_raw_http_get(port, "/health"))
            assert direct_health[0] == public_health[0] == root_health[0] == 200
            assert direct_health[2] == public_health[2] == root_health[2]

            redirect = _parse_http_response(_raw_http_get(port, "/xianyu-saas/api/health/"))
            assert redirect[0] == 307, redirect
            assert redirect[1].get("location", "").endswith("/xianyu-saas/api/health"), redirect[1]

            login_payload = b'{"username":"missing","password":"not-valid-long-enough"}'
            accepted_login = _parse_http_response(_raw_http_request(
                port,
                "/xianyu-saas/api/auth/login",
                method="POST",
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Origin": f"http://127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    "X-SaaS-Browser-Intent": "browser-write",
                },
                body=login_payload,
            ))
            assert accepted_login[0] == 401, accepted_login

            proxied_login = _parse_http_response(_raw_http_request(
                port,
                "/xianyu-saas/api/auth/login",
                method="POST",
                headers={
                    "Host": "127.0.0.1:8096",
                    "X-Forwarded-Host": f"localhost:{port}",
                    "Origin": f"http://localhost:{port}",
                    "Content-Type": "application/json",
                    "X-SaaS-Browser-Intent": "browser-write",
                },
                body=login_payload,
            ))
            assert proxied_login[0] == 401, proxied_login

            direct_ip_login = _parse_http_response(_raw_http_request(
                port,
                "/xianyu-saas/api/auth/login",
                method="POST",
                headers={
                    "Host": "192.0.2.10:8096",
                    "Origin": "http://192.0.2.10:8096",
                    "Content-Type": "application/json",
                    "X-SaaS-Browser-Intent": "browser-write",
                },
                body=login_payload,
            ))
            assert direct_ip_login[0] == 401, direct_ip_login

            for headers in (
                {
                    "Host": "untrusted.example",
                    "Origin": "http://untrusted.example",
                },
                {
                    "Host": "192.0.2.10:8096",
                    "X-Forwarded-Host": "untrusted.example",
                    "Origin": "http://untrusted.example",
                },
            ):
                rejected = _parse_http_response(_raw_http_request(
                    port,
                    "/xianyu-saas/api/auth/login",
                    method="POST",
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "X-SaaS-Browser-Intent": "browser-write",
                    },
                    body=login_payload,
                ))
                assert rejected[0] == 403, (headers, rejected)

            oversized = _parse_http_response(_raw_http_request(
                port,
                "/xianyu-saas/api/auth/login",
                method="POST",
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Origin": f"http://127.0.0.1:{port}",
                    "Content-Length": str(32 * 1024 + 1),
                    "Content-Type": "application/json",
                    "X-SaaS-Browser-Intent": "browser-write",
                },
            ))
            assert oversized[0] == 413, oversized

            oversized_shop_login = _parse_http_response(_raw_http_request(
                port,
                "/xianyu-saas/api/bot/login/start",
                method="POST",
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Origin": f"http://127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    "X-SaaS-Browser-Intent": "browser-write",
                },
                body=b"x" * 1025,
            ))
            assert oversized_shop_login[0] == 413, oversized_shop_login

            for target in (
                "/xianyu-saas/internal/v1/update/drain",
                "/xianyu-saas/private",
                "/xianyu-saas/api/../internal/v1/update/drain",
                "/xianyu-saas/assets/%2e%2e/frontend-secret.txt",
                "/xianyu-saas/assets/%2e%2e/%2e%2e/release-secret.txt",
                "/xianyu-saas/assets/%5c..%5c..%5crelease-secret.txt",
            ):
                status, _headers, body = _parse_http_response(_raw_http_get(port, target))
                assert status == 404, (target, status, body[:120])
                assert b"frontend-secret" not in body and b"release-secret" not in body
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _assert_dev_server_survives_malformed_requests() -> None:
    port = _reserve_loopback_port()
    environment = os.environ.copy()
    environment["SAAS_DEV_WEB_PORT"] = str(port)
    process = subprocess.Popen(
        ["node", str(ROOT / "scripts/dev-server.mjs")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"dev server exited during startup: {stdout}{stderr}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("dev server did not start")

        invalid_target = _raw_http_get(port, "http://[")
        assert invalid_target.startswith(b"HTTP/1.1 400 "), invalid_target[:80]
        assert process.poll() is None, "invalid request targets must not terminate the dev server"

        malformed_asset = _raw_http_get(port, "/xianyu-saas/assets/%E0%A4%A")
        assert malformed_asset.startswith(b"HTTP/1.1 400 "), malformed_asset[:80]
        assert process.poll() is None, "malformed asset paths must not terminate the dev server"

        healthy = _raw_http_get(port, "/xianyu-saas/")
        assert healthy.startswith(b"HTTP/1.1 200 "), healthy[:80]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


_assert_frontend_csp_contract()
_assert_application_serving_contract()
_assert_dev_server_survives_malformed_requests()

print("deploy contract: proxy trust, limits, private paths, static serving and log retention passed")

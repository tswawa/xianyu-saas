#!/usr/bin/env python3
"""Static deployment guards for shop login endpoints."""

import os
import re
import socket
import subprocess
import time
from pathlib import Path


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
assert "--workers" not in api_exec
assert "--preload" not in api_exec
for unit in (SERVICE, CONSUMER_SERVICE):
    assert "WorkingDirectory=/opt/xianyu-saas/current/backend" in unit
    assert "Environment=SAAS_CURRENT_ROOT=/opt/xianyu-saas/current" in unit
    assert "ReadWritePaths=/var/lib/xianyu-saas" in unit
    assert "ProtectSystem=strict" in unit
    assert "UMask=0077" in unit

for directive in (
    "Type=oneshot",
    "User=root",
    "Environment=SAAS_CURRENT_LINK=/opt/xianyu-saas/current",
    "Environment=SAAS_RELEASES_DIR=/opt/xianyu-saas/releases",
    "Environment=SAAS_UPDATE_STAGING_DIR=/var/lib/xianyu-saas/update-staging",
    "Environment=SAAS_UPDATE_INTENT_FILE=/var/lib/xianyu-saas/update-intents/intent.json",
    "ExecStart=/opt/xianyu-saas/runtime/backend-venv/bin/python /opt/xianyu-saas/current/deploy/updater/updater.py",
    "ProtectSystem=strict",
    "ReadWritePaths=/opt/xianyu-saas /var/lib/xianyu-saas",
    "UMask=0077",
):
    assert directive in UPDATER_SERVICE, directive
assert "PathExists=/var/lib/xianyu-saas/update-intents/intent.json" in UPDATER_PATH
assert "Unit=xianyu-saas-updater.service" in UPDATER_PATH
assert "DirectoryMode=0700" in UPDATER_PATH
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

tenants_log_root = str(Path("/", "var", "lib", "xianyu-saas", "tenants"))
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


def _raw_http_get(port: int, target: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(
            f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while chunk := connection.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


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


_assert_dev_server_survives_malformed_requests()

print("deploy contract: proxy trust, limits, private paths, static serving and log retention passed")

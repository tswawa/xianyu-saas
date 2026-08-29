#!/usr/bin/env python3
"""Static deployment guards for shop login and connector endpoints."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCATIONS = (ROOT / "deploy/nginx/xianyu-saas.locations.conf").read_text(encoding="utf-8")
ZONES = (ROOT / "deploy/nginx/xianyu-saas-rate-limits.conf").read_text(encoding="utf-8")
SERVICE = (ROOT / "deploy/systemd/xianyu-saas.service").read_text(encoding="utf-8")
LOGROTATE = (ROOT / "deploy/xianyu-saas-bot-logrotate.conf").read_text(encoding="utf-8")

EXACT = "location = /xianyu-saas/api/bot/connector/cookies {"
LOGIN_START = "location = /xianyu-saas/api/bot/login/start {"
LOGIN_COMPLETE = "location = /xianyu-saas/api/bot/login/complete {"
LOGIN_SESSION = "location ^~ /xianyu-saas/api/bot/login/ {"
GENERIC = "location ^~ /xianyu-saas/api/ {"

assert LOCATIONS.count(EXACT) == 1
assert LOCATIONS.count(LOGIN_START) == 1
assert LOCATIONS.count(LOGIN_COMPLETE) == 1
assert LOCATIONS.count(LOGIN_SESSION) == 1
assert LOCATIONS.index(EXACT) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(LOGIN_START) < LOCATIONS.index(GENERIC)
assert LOCATIONS.index(LOGIN_COMPLETE) < LOCATIONS.index(LOGIN_SESSION)
assert LOCATIONS.index(LOGIN_SESSION) < LOCATIONS.index(GENERIC)

connector = LOCATIONS.split(EXACT, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=xianyu_connector_submit burst=4 nodelay;",
    "limit_conn xianyu_connector_conn 2;",
    "limit_conn_status 429;",
    "limit_except POST { deny all; }",
    "client_max_body_size 40k;",
    "client_body_timeout 10s;",
    "proxy_pass http://127.0.0.1:8096/api/bot/connector/cookies;",
    'proxy_set_header Cookie "";',
    'proxy_set_header Authorization "";',
):
    assert directive in connector, directive

assert "zone=xianyu_connector_submit:1m rate=12r/m;" in ZONES
assert "zone=xianyu_connector_conn:1m;" in ZONES
assert "zone=xianyu_login_poll:1m rate=60r/m;" in ZONES
assert "zone=xianyu_login_complete:1m rate=6r/m;" in ZONES
assert "zone=xianyu_login_conn:1m;" in ZONES

login_start = LOCATIONS.split(LOGIN_START, 1)[1].split("\n}", 1)[0]
for directive in (
    "limit_req zone=deepwhale_session burst=5 nodelay;",
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

print("deploy contract: proxy trust, limits, private paths and log retention passed")

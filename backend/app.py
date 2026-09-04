"""xianyu-saas API service.

Static files are intentionally served by nginx from ``frontend/``.  This
process exposes JSON APIs and a loopback-only platform model proxy.
"""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from access import account_payload, has_permission, is_platform_admin, plan_for
from account_storage import AccountStorage, AccountStorageError, DEFAULT_ACCOUNT_ID, normalize_account_key
from ai_customer_service import AIServiceError, catgirl_preset, service as ai_service
from ai_provider_adapters import provider_catalog
from automation import (
    AutomationValidationError,
    deliveries_from_products,
    merge_material_products,
    merge_material_product_updates,
    material_batch_preview,
    normalise_material_batch,
    normalise_deliveries,
    normalise_rules,
    normalise_settings,
    product_config_revision,
    product_snapshot_revision,
    rules_document,
)
from bot_manager import (
    adopt as bot_adopt,
    auth_status as bot_auth_status,
    clear_auth_status as bot_clear_auth_status,
    logs as bot_logs,
    process_id as bot_process_id,
    read_secret,
    shutdown_all,
    start as bot_start,
    start_watchdog,
    status as bot_status,
    stop as bot_stop,
    terminate_pid as bot_terminate_pid,
    write_secret,
)
from db import (
    DB,
    DB_PATH,
    DUMMY_PASSWORD_HASH,
    BootstrapTokenError,
    BootstrapUnavailableError,
    LastAdminError,
    RegistrationClosedError,
    verify_password,
    verify_password_details,
)
from platform_ai import (
    MAX_REQUEST_BYTES,
    PlatformAIError,
    forward as platform_forward,
    identify_scope,
    is_configured as platform_ai_configured,
)
from platform_update import (
    PlatformUpdateError,
    available_rollback_versions,
    fetch_release,
    release_payload,
    stage_release,
    validate_candidate,
    write_update_intent,
)
import records
from shop_sync import (
    ShopSyncError,
    load_verified_snapshot,
    parse_cookie_header,
    reserve_sync,
    sync_shop,
    SYNC_COOLDOWN_SECONDS,
    SYNC_MAX_SECONDS,
    sync_status_payload,
)
from shop_sync_service import ShopSyncPersistenceError, run_shop_sync_inner
from version import version_payload
from xianyu_login import XianyuLoginError, qr_logins


ADMIN_TOKEN = os.environ.get("SAAS_ADMIN_TOKEN", "")
SESSION_COOKIE = "xianyu_saas_session"
COOKIE_PATH = "/xianyu-saas/"
COOKIE_SECURE = os.environ.get("SAAS_COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"}
PASSWORD_MIN_LENGTH = 12
BROWSER_LOGS_MODE = os.environ.get("SAAS_BROWSER_LOGS_MODE", "off").strip().lower()
BROWSER_WRITE_HEADER = "X-SaaS-Browser-Intent"
BROWSER_WRITE_HEADER_VALUE = "browser-write"
BOOTSTRAP_TOKEN_HEADER = "X-Bootstrap-Token"
PUBLIC_ORIGIN = os.environ.get("SAAS_PUBLIC_ORIGIN", "").strip()
_AUDIT_HMAC_KEY = (
    os.environ.get("SAAS_AUDIT_HMAC_KEY", "").encode("utf-8") or secrets.token_bytes(32)
)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
AUDIT_EVENT_TYPES = frozenset(
    {
        "auth.bootstrap_succeeded",
        "auth.bootstrap_failed",
        "auth.registration_succeeded",
        "auth.registration_failed",
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.logout",
        "auth.password_changed",
        "platform.settings_changed",
        "platform.user_changed",
        "platform.user_unlocked",
        "platform.sessions_revoked",
        "platform.confirmation_issued",
        "platform.update_checked",
        "platform.update_downloaded",
        "platform.update_requested",
        "platform.rollback_requested",
    }
)
AUDIT_METADATA_KEYS = frozenset(
    {
        "code",
        "role",
        "enabled",
        "setting",
        "value",
        "version",
        "channel",
        "status",
        "sessions_revoked",
    }
)
TRUSTED_BROWSER_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("SAAS_TRUSTED_HOSTS", "").split(",")
    if host.strip()
}
TRUSTED_BROWSER_HOSTS.update({"127.0.0.1", "::1", "localhost", "testserver"})
TRUSTED_PROXY_IPS = {
    item.strip()
    for item in os.environ.get("SAAS_TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
    if item.strip()
}


def _acquire_api_process_lock():
    """Enforce one API supervisor process for each shared control DB."""
    database_path = os.path.abspath(os.environ.get("SAAS_DB", DB_PATH))
    lock_path = database_path + ".api.lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    lock_file = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(
            f"another xianyu-saas API supervisor already owns database: {database_path}"
        ) from exc
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()}\ndatabase={database_path}\n")
    lock_file.flush()
    return lock_file


_api_process_lock = _acquire_api_process_lock()
db = DB()
app = FastAPI(title="xianyu-saas-api", docs_url=None, redoc_url=None)


def _assert_not_testing_in_production() -> None:
    if os.environ.get("SAAS_TESTING") != "1":
        return
    env = os.environ.get("SAAS_ENV", "").strip().lower()
    node_env = os.environ.get("NODE_ENV", "").strip().lower()
    if env == "production" or node_env == "production":
        raise RuntimeError("SAAS_TESTING=1 cannot be enabled in production")


_assert_not_testing_in_production()


def _env_true(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _registration_env_allowed() -> bool:
    """Read the deployment ceiling at request time so operators can close it."""
    return _env_true("SAAS_ALLOW_REGISTRATION", "0")


def _client_address(request: Request) -> str:
    peer = str(request.client.host if request.client else "unknown").strip() or "unknown"
    candidate = peer
    if peer in TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("x-real-ip", "").strip()
        if not forwarded:
            forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            candidate = forwarded
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return candidate[:120]


def _source_matches(value: str, configured: str) -> bool:
    value = str(value or "").strip()
    configured = str(configured or "").strip()
    if not value or not configured:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return secrets.compare_digest(value.lower(), configured.lower())
    try:
        return address in ipaddress.ip_network(configured, strict=False)
    except ValueError:
        try:
            return address == ipaddress.ip_address(configured)
        except ValueError:
            return False


def _bootstrap_source_trusted(request: Request) -> bool:
    sources = [
        item.strip()
        for item in os.environ.get("SAAS_BOOTSTRAP_TRUSTED_SOURCES", "").split(",")
        if item.strip()
    ]
    address = _client_address(request)
    return bool(sources and any(_source_matches(address, item) for item in sources))


def _audit_hash(namespace: str, value: str) -> str:
    message = f"{namespace}:{value}".encode("utf-8", "replace")
    return hmac.new(_AUDIT_HMAC_KEY, message, hashlib.sha256).hexdigest()[:32]


def _username_hash(username: str) -> str:
    """Stable, non-reversible login-throttle key that survives restarts."""
    normalized = str(username).strip().casefold().encode("utf-8", "replace")
    return hashlib.sha256(b"username:" + normalized).hexdigest()


def _login_client_hash(request: Request) -> str:
    address = _client_address(request).encode("utf-8", "replace")
    return hashlib.sha256(b"client:" + address).hexdigest()


def _client_hash(request: Request) -> str:
    return _audit_hash("client", _client_address(request))


def _audit(
    event_type: str,
    request: Request | None = None,
    *,
    actor_user_id=None,
    target_type="",
    target_id="",
    outcome="success",
    metadata=None,
) -> None:
    if event_type not in AUDIT_EVENT_TYPES:
        raise ValueError("unsupported audit event")
    safe_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) in AUDIT_METADATA_KEYS
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    try:
        db.append_audit(
            event_type,
            actor_user_id=actor_user_id,
            target_type=target_type,
            target_id=str(target_id)[:120],
            outcome=outcome,
            ip_hash=_client_hash(request) if request is not None else "",
            metadata=safe_metadata,
        )
    except (sqlite3.Error, ValueError):
        # Audit failure must not leak a sensitive exception to the browser.
        return


def _bootstrap_token_file() -> Path | None:
    raw = os.environ.get("SAAS_BOOTSTRAP_TOKEN_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
        if not credentials_dir or path.name != raw:
            return None
        path = Path(credentials_dir) / path
    return path


def _bootstrap_token_digest() -> str:
    """Read only a secure one-time credential and persist only its digest."""
    if not _env_true("SAAS_BOOTSTRAP_ENABLED", "0"):
        return ""
    path = _bootstrap_token_file()
    if path is None:
        return ""
    descriptor = None
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return ""
        if metadata.st_uid not in {0, os.geteuid()} or metadata.st_mode & 0o077:
            return ""
        if metadata.st_size <= 0 or metadata.st_size > 512:
            return ""
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077 or opened.st_size > 512:
            return ""
        value = os.read(descriptor, 513).decode("utf-8", "strict").strip()
        if not (32 <= len(value) <= 256) or any(character.isspace() for character in value):
            return ""
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return digest if db.configure_bootstrap(digest) else ""
    except (OSError, UnicodeError):
        return ""
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _normalize_origin(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _request_host_candidates(request: Request) -> list[str]:
    hosts: list[str] = []
    for raw in (
        request.headers.get("x-forwarded-host"),
        request.headers.get("host"),
        request.url.netloc,
    ):
        if not raw:
            continue
        candidate = raw.split(",", 1)[0].strip().lower()
        if candidate and candidate not in hosts:
            hosts.append(candidate)
    return hosts


def _request_origin_candidates(request: Request) -> list[str]:
    origins: list[str] = []
    for raw in (request.headers.get("origin"), request.headers.get("referer")):
        origin = _normalize_origin(raw)
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _configured_public_origin(request: Request) -> str | None:
    configured = _normalize_origin(PUBLIC_ORIGIN)
    if configured is not None:
        return configured
    hosts = _request_host_candidates(request)
    if not hosts:
        return None
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",", 1)[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    return f"{scheme}://{hosts[0]}"


_BROWSER_LOG_REDACTIONS = (
    (re.compile(r"(?i)\b(bearer\s+)[^\s,;]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(cookie|authorization|token|password|secret)=([^&\s]+)"), r"\1=[redacted]"),
    (re.compile(r"(?i)\b(x-admin-token|x-bootstrap-token):\s*[^\s,;]+"), r"\1: [redacted]"),
)


def _redact_browser_logs(value):
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _BROWSER_LOG_REDACTIONS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(value, list):
        return [_redact_browser_logs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_browser_logs(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_browser_logs(item) for key, item in value.items()}
    return value


def _browser_logs_payload(user_id: int, lines: int, account_key: str):
    if BROWSER_LOGS_MODE != "redacted":
        return []
    return _redact_browser_logs(bot_logs(user_id, lines, account_key))


def _require_browser_write_origin(request: Request) -> None:
    intent = request.headers.get(BROWSER_WRITE_HEADER, "").strip().lower()
    if intent != BROWSER_WRITE_HEADER_VALUE:
        raise HTTPException(
            403,
            detail={"code": "browser_write_header_required", "message": "浏览器写入请求缺少校验头"},
        )
    configured_origin = _configured_public_origin(request)
    supplied_origins = _request_origin_candidates(request)
    if supplied_origins:
        if configured_origin is not None and supplied_origins[0] != configured_origin:
            raise HTTPException(
                403,
                detail={"code": "browser_origin_mismatch", "message": "浏览器写入来源不匹配"},
            )
        return
    if configured_origin is None:
        raise HTTPException(
            403,
            detail={"code": "browser_origin_untrusted", "message": "无法确认浏览器写入来源"},
        )
    configured_host = urlsplit(configured_origin).netloc.lower()
    if not any(host == configured_host or host in TRUSTED_BROWSER_HOSTS for host in _request_host_candidates(request)):
        raise HTTPException(
            403,
            detail={"code": "browser_origin_untrusted", "message": "浏览器写入来源不匹配"},
        )


def _require_public_write_origin(request: Request) -> None:
    if os.environ.get("SAAS_TESTING") != "1":
        _require_browser_write_origin(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    method = request.method.upper()
    response = None
    if (
        method not in {"GET", "HEAD", "OPTIONS"}
        and request.url.path.startswith("/api/")
        and request.cookies.get(SESSION_COOKIE)
        and os.environ.get("SAAS_TESTING") != "1"
    ):
        try:
            _require_browser_write_origin(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
    if response is None:
        response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/", include_in_schema=False)
@app.get("/api/health")
@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "service": "xianyu-saas-api"}


@app.get("/api/ready")
def ready():
    try:
        database_ready = db.is_ready()
    except sqlite3.Error as exc:
        raise HTTPException(
            503,
            detail={"code": "database_unavailable", "message": "控制面数据库不可用"},
        ) from exc
    if not database_ready:
        raise HTTPException(
            503,
            detail={"code": "database_unavailable", "message": "控制面数据库不可用"},
        )
    return {"ok": True, "service": "xianyu-saas-api", "database": "ready"}


def _local_release_notes() -> str:
    try:
        return (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )[:16_000]
    except (OSError, UnicodeError):
        return ""


def _platform_update_payload(row) -> dict | None:
    if row is None:
        return None
    return {
        "version": str(row["version"]),
        "channel": str(row["channel"]),
        "status": str(row["status"]),
        "release_notes": str(row["release_notes"] or ""),
        "error_code": str(row["error_code"] or ""),
        "updated_at": float(row["updated_at"]),
    }


@app.get("/api/version/public")
def public_version():
    payload = version_payload("stable")
    return {
        "version": payload["version"],
        "asset_version": payload["asset_version"],
    }


@app.get("/api/auth/capabilities")
def auth_capabilities(request: Request):
    env_allowed = _registration_env_allowed()
    registration_open = db.get_platform_setting("registration_open", "0") == "1"
    users_exist = db.user_count() > 0
    bootstrap_trusted = _bootstrap_source_trusted(request)
    bootstrap_digest = _bootstrap_token_digest() if bootstrap_trusted else ""
    bootstrap_available = bool(
        bootstrap_digest and db.bootstrap_available(bootstrap_digest)
    )
    return {
        "registration_enabled": bool(env_allowed and registration_open and users_exist),
        "bootstrap_available": bootstrap_available,
        "password_min_length": PASSWORD_MIN_LENGTH,
    }


def _request_token(request: Request, authorization: str = "") -> str:
    cookie_token = request.cookies.get(SESSION_COOKIE, "").strip()
    if cookie_token:
        return cookie_token
    if authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return ""


class Auth:
    @staticmethod
    def current_user(request: Request, authorization: str = Header(default="")):
        token = _request_token(request, authorization)
        user_id = db.get_token_user(token)
        if user_id is None:
            raise HTTPException(401, "未登录或登录已过期")
        user = db.get_user_by_id(user_id)
        if user is None or user["disabled_at"] is not None:
            db.delete_token(token)
            raise HTTPException(401, "未登录或登录已过期")
        return user

    @staticmethod
    def current_token(request: Request, authorization: str = Header(default="")) -> str:
        return _request_token(request, authorization)


@app.get("/api/version")
def get_version(user=Depends(Auth.current_user)):
    channel = db.get_platform_setting("update_channel", "stable")
    return {
        **version_payload(channel),
        "release_notes": _local_release_notes(),
        "latest_update": _platform_update_payload(db.latest_platform_update(channel)),
    }


def _loopback_request(request: Request) -> bool:
    try:
        return ipaddress.ip_address(_client_address(request)).is_loopback
    except ValueError:
        return os.environ.get("SAAS_TESTING") == "1"


def require_platform_admin(
    request: Request,
    authorization: str = Header(default=""),
    x_admin_token: str = Header(default=""),
):
    token = _request_token(request, authorization)
    user_id = db.get_token_user(token) if token else None
    if user_id is not None:
        user = db.get_user_by_id(user_id)
        if user is not None and user["disabled_at"] is None and is_platform_admin(user):
            return user
        raise HTTPException(
            403,
            detail={"code": "admin_required", "message": "需要管理员权限"},
        )
    if (
        ADMIN_TOKEN
        and x_admin_token
        and _loopback_request(request)
        and secrets.compare_digest(x_admin_token, ADMIN_TOKEN)
    ):
        return {"id": 0, "username": "emergency-admin", "role": "admin", "disabled_at": None}
    raise HTTPException(
        403,
        detail={"code": "admin_required", "message": "需要管理员权限"},
    )


def current_shop_account(
    request: Request,
    user=Depends(Auth.current_user),
):
    """Resolve one tenant-owned shop without exposing storage details.

    The browser keeps the simple default flow. Operators and future UI code
    can select another account with ``X-Shop-Account``; every data path then
    receives the validated account row instead of trusting a raw directory.
    """
    requested = request.headers.get("X-Shop-Account", "default").strip() or "default"
    if len(requested) > 80 or not requested.isascii():
        raise HTTPException(400, "店铺账号标识无效")
    account = db.get_shop_account(user["id"], account_key=requested)
    if account is None or not account["enabled"]:
        raise HTTPException(404, "店铺账号不存在或已停用")
    return account


class _AccountLease:
    def __init__(self, key: str, owner: str, lease_seconds: float):
        self.key = key
        self.owner = owner
        self.lease_seconds = max(float(lease_seconds), 1.0)
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.last_error = ""
        self.thread = threading.Thread(
            target=self._renew_loop,
            daemon=True,
            name=f"lease-renew:{key}",
        )
        self.thread.start()

    def _renew_loop(self):
        interval = max(0.2, min(self.lease_seconds / 3.0, 10.0))
        while not self.stop_event.wait(interval):
            try:
                renewed = db.renew_control_lease(
                    self.key,
                    self.owner,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                self.last_error = str(exc)[:240]
                self.lost.set()
                return
            if not renewed:
                self.last_error = "control lease owner lost or lease expired"
                self.lost.set()
                return

    def ensure_owned(self) -> None:
        if self.lost.is_set():
            raise RuntimeError(self.last_error or "control lease lost")


def _acquire_account_lease(
    scope: str,
    user,
    account,
    *,
    lease_seconds: float,
    busy_message: str,
    unavailable_message: str,
):
    lease_key = f"{scope}:{int(user['id'])}:{int(account['id'])}"
    lease_owner = f"api:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    try:
        lease_result = db.acquire_control_lease(
            lease_key,
            lease_owner,
            lease_seconds=lease_seconds,
            cooldown_seconds=0,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise HTTPException(503, unavailable_message) from exc
    if lease_result != "acquired":
        raise HTTPException(409, busy_message)
    return _AccountLease(lease_key, lease_owner, lease_seconds), lease_owner


def _ensure_account_lease(lease, unavailable_message: str = "操作租约已失效，请重试") -> None:
    if isinstance(lease, _AccountLease):
        try:
            lease.ensure_owned()
        except RuntimeError as exc:
            raise HTTPException(503, unavailable_message) from exc


def _release_account_lease(lease, lease_owner: str) -> None:
    lease_key = lease.key if isinstance(lease, _AccountLease) else str(lease)
    if isinstance(lease, _AccountLease):
        lease.stop_event.set()
        lease.thread.join(timeout=1)
    try:
        db.release_control_lease(lease_key, lease_owner)
    except sqlite3.Error:
        pass


class RegisterIn(BaseModel):
    username: str
    password: str

    class Config:
        extra = "forbid"


class LoginIn(BaseModel):
    username: str
    password: str

    class Config:
        extra = "forbid"


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str

    class Config:
        extra = "forbid"


class PlatformSettingsIn(BaseModel):
    registration_open: bool | None = None
    update_channel: str | None = None

    class Config:
        extra = "forbid"


class PlatformUserUpdateIn(BaseModel):
    role: str | None = None
    enabled: bool | None = None

    class Config:
        extra = "forbid"


class AdminConfirmationIn(BaseModel):
    password: str
    action: str

    class Config:
        extra = "forbid"


class PlatformUpdateDownloadIn(BaseModel):
    version: str = ""

    class Config:
        extra = "forbid"


class PlatformUpdateApplyIn(BaseModel):
    version: str
    confirmation_token: str

    class Config:
        extra = "forbid"


class ConfigIn(BaseModel):
    keywords_json: str | None = None

    class Config:
        extra = "forbid"


class AutomationIn(BaseModel):
    rules: list | None = None
    deliveries: list | None = None
    strategy: str | None = None
    enabled: bool | None = None
    first_reply: str | None = None
    fallback_reply: str | None = None
    delay_min_seconds: int | None = None
    delay_max_seconds: int | None = None
    trigger_cooldown_seconds: int | None = None
    manual_takeover_cooldown_seconds: int | None = None
    business_hours_enabled: bool | None = None
    business_start: str | None = None
    business_end: str | None = None

    class Config:
        extra = "forbid"


class BotStartIn(BaseModel):
    mode: str | None = None

    class Config:
        extra = "forbid"


class AIConnectionTestIn(BaseModel):
    provider: str = "openai_chat_completions"
    base_url: str
    model: str
    api_key: str = ""
    expected_revision: int

    class Config:
        extra = "forbid"


class AIConnectionSaveIn(AIConnectionTestIn):
    verification_token: str


class AIConnectionDeleteIn(BaseModel):
    confirm: bool
    expected_revision: int

    class Config:
        extra = "forbid"


class AIConfigIn(BaseModel):
    config: dict | None = None
    store_content: str | None = None
    persona_preset: str | None = None
    persona_name: str | None = None
    tone: str | None = None
    buyer_address: str | None = None
    reply_length: str | None = None
    emoji_level: str | None = None
    forbidden_claims: str | list[str] | None = None
    handoff_rules: str | list[str] | None = None
    enabled: bool | None = None
    expected_revision: int
    action: str = "draft"

    class Config:
        extra = "forbid"


class AITemplateIn(BaseModel):
    name: str
    config: dict
    template_id: str | None = None

    class Config:
        extra = "forbid"


class AIKnowledgeIn(BaseModel):
    knowledge: dict | None = None
    content: str | None = None
    expected_revision: int

    class Config:
        extra = "forbid"


class AIKnowledgeActionIn(BaseModel):
    confirm: bool
    expected_revision: int

    class Config:
        extra = "forbid"


class AIExtractIn(BaseModel):
    source_text: str | None = None
    content: str | None = None

    class Config:
        extra = "forbid"


class AIPreviewIn(BaseModel):
    buyer_message: str
    item_id: str | None = None
    store_config: dict | None = None
    knowledge: dict | None = None
    history: list = []
    recent_assistant_replies: list = []

    class Config:
        extra = "forbid"


class AIReplyIn(BaseModel):
    message: str
    history: list = []
    item_id: str | None = None
    item_context: dict | None = None
    recent_assistant_replies: list = []

    class Config:
        extra = "forbid"


class AIReadyIn(BaseModel):
    expected_config_revision: int

    class Config:
        extra = "forbid"


class CookiesIn(BaseModel):
    cookies: str


class XianyuLoginCompleteIn(BaseModel):
    login_id: str

    class Config:
        extra = "forbid"


class CodesIn(BaseModel):
    codes: list


class ShopAccountIn(BaseModel):
    # ``key`` remains accepted for API/ops callers, while the UI can submit
    # only a friendly name and let the server create the opaque storage key.
    key: str = ""
    name: str = ""

    class Config:
        extra = "forbid"


class ShopAccountRenameIn(BaseModel):
    name: str = ""

    class Config:
        extra = "forbid"


class ProductsIn(BaseModel):
    products: dict


class ProductBatchIn(BaseModel):
    item_ids: list
    material: str = ""
    enabled: bool = True

    class Config:
        extra = "forbid"


class ProductBatchCommitIn(ProductBatchIn):
    preview_token: str


class TemplateItemIn(BaseModel):
    id: str | None = None
    name: str = ""
    description: str | None = None
    price: str | None = None
    delivery: str = ""
    item_ids: list | None = None
    resource_match: list | None = None
    enabled: bool | None = None

    class Config:
        extra = "forbid"


class TemplateIn(BaseModel):
    template: TemplateItemIn


class CardsIn(BaseModel):
    name: str = ""
    note: str = ""
    codes: list = []

    class Config:
        extra = "forbid"


class QuickRepliesIn(BaseModel):
    quick_replies: list = []

    class Config:
        extra = "forbid"


class ManualReplyIn(BaseModel):
    content: str = ""
    chat_id: str
    media: list = []

    class Config:
        extra = "forbid"


class ManualImageDeleteIn(BaseModel):
    path: str

    class Config:
        extra = "forbid"


class ConversationReadIn(BaseModel):
    read: bool = True

    class Config:
        extra = "forbid"


class ConversationTakeoverIn(BaseModel):
    enabled: bool = True

    class Config:
        extra = "forbid"


class AttentionResolutionIn(BaseModel):
    resolved: bool = True

    class Config:
        extra = "forbid"


def _require_permission(user, permission: str):
    if not has_permission(user, permission):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "permission_required",
                "permission": permission,
                "plan": plan_for(user["expires_at"]),
                "message": "当前账号无此功能权限",
            },
            headers={"X-SaaS-Permission": permission},
        )
    return user


def _ai_scope(user, account) -> tuple[int, int, str]:
    return int(user["id"]), int(account["id"]), str(account["account_key"])


def _public_ai_content_status(value: object) -> str:
    status = str(value or "unconfigured")
    return {
        "published": "saved",
        "draft": "unsaved",
        "needs_content": "needs_content",
        "stale": "needs_confirmation",
    }.get(status, status)


def _public_ai_config(payload: dict) -> dict:
    published = payload.get("published") if isinstance(payload, dict) else None
    draft = payload.get("draft") if isinstance(payload, dict) else None
    config = published.get("config") if isinstance(published, dict) else draft
    config = config if isinstance(config, dict) else {}
    status = _public_ai_content_status(payload.get("status") if isinstance(payload, dict) else None)
    return {
        "version": 2,
        "revision": int(payload.get("revision") or 0),
        "status": status,
        "content_status": status,
        "content_valid": payload.get("content_valid") is True,
        "updated_at": str(payload.get("updated_at") or "")[:40],
        **{
            key: config.get(key)
            for key in (
                "store_content", "persona_preset", "persona_name", "persona_instruction",
                "tone", "buyer_address", "reply_length", "emoji_level",
                "forbidden_claims", "handoff_rules", "enabled",
            )
        },
    }


def _public_ai_knowledge(payload: dict) -> dict:
    published = payload.get("published") if isinstance(payload, dict) else None
    draft = payload.get("draft") if isinstance(payload, dict) else None
    knowledge = published.get("knowledge") if isinstance(published, dict) else draft
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    status = _public_ai_content_status(payload.get("status") if isinstance(payload, dict) else None)
    return {
        "version": 2,
        "item_id": str(payload.get("item_id") or ""),
        "revision": int(payload.get("revision") or 0),
        "status": status,
        "content_status": status,
        "content": str(knowledge.get("content") or ""),
        "disabled": payload.get("disabled") is True,
        "stale": payload.get("stale") is True,
        "needs_confirmation": payload.get("needs_confirmation") is True,
        "review_recommended": payload.get("review_recommended") is True,
        "facts_changed": payload.get("facts_changed") is True,
        "facts": payload.get("facts") if isinstance(payload.get("facts"), dict) else {},
        "updated_at": str(payload.get("updated_at") or "")[:40],
    }


def _raise_ai_error(error: AIServiceError):
    raise HTTPException(
        error.status_code,
        detail={"code": error.code, "message": str(error)},
    ) from error


def _require_ai_reply_ready(user, account) -> None:
    scope = _ai_scope(user, account)
    connection = ai_service.get_connection(*scope)
    if connection.get("connection_status") != "verified" or not ai_service.is_configured(*scope):
        raise HTTPException(
            409,
            detail={
                "code": "ai_connection_unavailable",
                "message": "请先为当前店铺测试并保存可用的 AI 连接",
            },
        )
    config = ai_service.get_config(*scope)
    published = config.get("published")
    if not (
        isinstance(published, dict)
        and isinstance(published.get("config"), dict)
        and published.get("content_valid") is True
        and published["config"].get("enabled") is True
    ):
        raise HTTPException(
            409,
            detail={
                "code": "ai_store_content_invalid",
                "message": "请先保存有实质内容并启用的店铺客服说明",
            },
        )


def _shop_account_payload(row, user_id: int | None = None):
    """Expose account health metadata without credentials or platform payloads."""
    if row is None:
        return None
    status = str(row["status"] or "unconfigured")
    last_error_code = str(row["last_error_code"] or "")
    if user_id is not None:
        auth_state = _read_auth_status(user_id, str(row["account_key"]))
        if auth_state["needs_human"]:
            status = "degraded" if auth_state["code"] == "risk_control" else "expired"
            last_error_code = auth_state["code"]
    return {
        "id": int(row["id"]),
        "key": str(row["account_key"]),
        "platform": str(row["platform"]),
        "name": str(row["display_name"] or ""),
        "status": status,
        "enabled": bool(row["enabled"]),
        "last_error_code": last_error_code,
        "last_verified_at": row["last_verified_at"],
        "last_sync_at": row["last_sync_at"],
    }


def _sync_account_state(
    user_id: int,
    code: str,
    snapshot: dict | None = None,
    account=None,
):
    """Persist bounded connection health beside legacy tenant files."""
    account = account or db.ensure_default_shop_account(user_id)
    if account is None:
        return
    try:
        generation = int(account["generation"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        generation = 0
    if snapshot is not None:
        now = time.time()
        fields = {
            "account_ref": str(snapshot.get("account_ref") or "")[:64],
            "status": "ready",
            "enabled": True,
            "last_error_code": "",
            "last_verified_at": now,
            "last_sync_at": now,
        }
        # A user-entered shop name is authoritative.  Only seed the display
        # name from Xianyu the first time a connection is verified.
        if not str(account["display_name"] or "").strip():
            fields["display_name"] = str(snapshot.get("nickname") or "")[:160]
        db.update_shop_account_if_current(
            user_id,
            account["id"],
            generation,
            display_name_if_empty=("display_name" in fields),
            **fields,
        )
        return
    if code in {"cookie_expired", "cookie_invalid", "cookie_incomplete"}:
        status = "expired"
    elif code == "account_restricted":
        status = "restricted"
    else:
        status = "degraded"
    db.update_shop_account_if_current(
        user_id,
        account["id"],
        generation,
        status=status,
        last_error_code=str(code or "sync_error")[:80],
    )


AUTH_STATUS_CODES = {
    "ok",
    "session_expired",
    "risk_control",
    "token_unavailable",
    "network_error",
    "platform_busy",
    "response_invalid",
}
AUTH_REAUTHORIZE_CODES = {"session_expired", "risk_control"}


def _read_auth_status(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> dict:
    """Read only the bounded, non-secret worker authentication state."""
    return bot_auth_status(user_id, account_key)


def _clear_auth_status(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> None:
    bot_clear_auth_status(user_id, account_key)


def _persist_worker_observation(
    user_id: int,
    account,
    *,
    desired_state: str,
    state: str,
    pid: int | None,
    last_error: str = "",
    mode: str | None = None,
):
    previous = db.get_worker_runtime(user_id, account["id"])
    generation = int(previous["generation"] or 0) if previous else 0
    persisted_mode = mode or (str(previous["mode"] or "rules") if previous else "rules")
    row = db.persist_worker_runtime(
        user_id,
        account["id"],
        desired_state=desired_state,
        mode=persisted_mode,
        state=state,
        pid=pid,
        generation=generation,
        started_at=(previous["started_at"] if previous else None),
        heartbeat_at=time.time(),
        exit_code=(previous["exit_code"] if previous else None),
        last_error=str(last_error or "")[:240],
        expected_generation=generation,
    )
    if row is None:
        raise RuntimeError("worker runtime generation changed")
    return row


def _persist_worker_started(
    user_id: int,
    mode: str,
    already_running: bool = False,
    account=None,
):
    account = account or db.ensure_default_shop_account(user_id)
    if account is None:
        raise RuntimeError("default shop account unavailable")
    previous = db.get_worker_runtime(user_id, account["id"])
    previous_generation = int(previous["generation"] or 0) if previous else 0
    generation = previous_generation if already_running else previous_generation + 1
    row = db.persist_worker_runtime(
        user_id,
        account["id"],
        desired_state="running",
        mode=mode,
        state="running",
        pid=bot_process_id(user_id, account["account_key"]),
        generation=generation,
        started_at=(previous["started_at"] if already_running and previous else time.time()),
        heartbeat_at=time.time(),
        exit_code=None,
        last_error="",
        expected_generation=previous_generation,
    )
    if row is None:
        raise RuntimeError("worker runtime generation changed")
    return row


def _persist_worker_stopped(
    user_id: int,
    reason: str = "",
    account=None,
    state: str = "stopped",
    pid: int | None = None,
):
    account = account or db.ensure_default_shop_account(user_id)
    if account is None:
        return None
    previous = db.get_worker_runtime(user_id, account["id"])
    generation = int(previous["generation"] or 0) if previous else 0
    mode = str(previous["mode"] or "rules") if previous else "rules"
    row = db.persist_worker_runtime(
        user_id,
        account["id"],
        desired_state="stopped",
        mode=mode,
        state=state,
        pid=pid,
        generation=generation,
        started_at=(previous["started_at"] if previous else None),
        heartbeat_at=time.time(),
        exit_code=(previous["exit_code"] if previous else None),
        last_error=str(reason or "")[:240],
        expected_generation=generation,
    )
    if row is None:
        raise RuntimeError("worker runtime generation changed")
    return row


def _stop_confirmed(ok: bool, reason: str) -> bool:
    return bool(ok or reason in {"not_running", "already_dead", "stopped"})


def _stop_account_worker_locked(
    user_id: int,
    account,
    reason_prefix: str = "",
    lease=None,
    *,
    preserve_desired_running: bool = False,
):
    _ensure_account_lease(lease)
    account_key = str(_runtime_value(account, "account_key", "default"))
    runtime = db.get_worker_runtime(user_id, account["id"])
    desired_running = bool(runtime and runtime["desired_state"] == "running")
    durable_pid = int(runtime["pid"] or 0) if runtime and runtime["pid"] else None
    local_pid = bot_process_id(user_id, account_key)
    local_ok, local_reason = bot_stop(user_id, account_key)
    confirmed = _stop_confirmed(local_ok, local_reason)
    reason = local_reason
    surviving_pid = None if confirmed else (bot_process_id(user_id, account_key) or local_pid)
    if durable_pid is not None and durable_pid != local_pid:
        orphan_ok, orphan_reason = bot_terminate_pid(user_id, durable_pid, account_key)
        orphan_confirmed = _stop_confirmed(orphan_ok, orphan_reason)
        confirmed = confirmed and orphan_confirmed
        if not orphan_confirmed:
            surviving_pid = durable_pid
            reason = orphan_reason
        elif local_reason == "not_running":
            reason = orphan_reason
    persisted_reason = f"{reason_prefix}:{reason}" if reason_prefix else reason
    _ensure_account_lease(lease)
    if preserve_desired_running and desired_running:
        _persist_worker_observation(
            user_id,
            account,
            desired_state="running",
            state="waiting_login" if confirmed else "degraded",
            pid=None if confirmed else surviving_pid,
            last_error=persisted_reason,
        )
    else:
        _persist_worker_stopped(
            user_id,
            persisted_reason,
            account=account,
            state="stopped" if confirmed else "degraded",
            pid=None if confirmed else surviving_pid,
        )
    return confirmed, reason


def _pause_account_worker_for_login_replace(user_id: int, account) -> None:
    """Pause a live worker only after a replacement login is verified."""
    lease, owner = _acquire_account_lease(
        "worker-control",
        {"id": user_id},
        account,
        lease_seconds=45,
        busy_message="自动客服正在切换店铺登录，请稍后重试",
        unavailable_message="自动客服登录切换暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        current = db.get_shop_account(user_id, account_id=account["id"])
        if current is None or not current["enabled"]:
            raise HTTPException(404, "店铺账号不存在或已停用")
        confirmed, reason = _stop_account_worker_locked(
            user_id,
            current,
            "login_replace",
            lease,
            preserve_desired_running=True,
        )
        if not confirmed:
            raise HTTPException(
                503,
                detail={"code": reason, "message": "自动客服登录切换尚未完成，请稍后重试"},
            )
    finally:
        _release_account_lease(lease, owner)


def _prepare_account_worker_start_locked(
    user_id: int,
    account,
    mode: str,
    lease=None,
    *,
    preserve_desired_running: bool = False,
) -> None:
    _ensure_account_lease(lease)
    account_key = str(_runtime_value(account, "account_key", "default"))
    runtime = db.get_worker_runtime(user_id, account["id"])
    if runtime is None or not runtime["pid"]:
        return
    durable_pid = int(runtime["pid"])
    if bot_process_id(user_id, account_key) == durable_pid:
        return
    durable_mode = str(runtime["mode"] or "rules")
    if durable_mode == mode == "rules":
        adopted, adopt_reason = bot_adopt(user_id, durable_pid, "rules", account_key)
        if adopted:
            return
        if adopt_reason in {"pid_dead", "pid_invalid"}:
            return
        if adopt_reason == "pid_mismatch":
            raise HTTPException(409, "已记录的机器人进程身份不匹配，请先停止后重试")
    stopped, stop_reason = bot_terminate_pid(user_id, durable_pid, account_key)
    _ensure_account_lease(lease)
    desired_state = "running" if preserve_desired_running else "stopped"
    if not _stop_confirmed(stopped, stop_reason):
        _persist_worker_observation(
            user_id,
            account,
            desired_state=desired_state,
            state="degraded",
            pid=durable_pid,
            last_error=f"start_orphan_{stop_reason}",
            mode=mode,
        )
        raise HTTPException(503, "旧机器人进程尚未确认停止，请稍后重试")
    _persist_worker_observation(
        user_id,
        account,
        desired_state=desired_state,
        state="degraded",
        pid=None,
        last_error="start_replacing_old_worker",
        mode=mode,
    )


def _safe_worker_error_code(reason: str, default: str = "worker_start_failed") -> str:
    value = str(reason or "").strip().lower()
    if value and len(value) <= 80 and all(char.isalnum() or char in {"_", "-"} for char in value):
        return f"worker_start_{value}"[:120]
    return default


def _worker_configuration_error_code(error: HTTPException) -> str:
    detail = error.detail
    value = str(detail.get("code") or "") if isinstance(detail, dict) else ""
    if value and len(value) <= 80 and all(char.isalnum() or char in {"_", "-"} for char in value):
        return value
    return "worker_configuration_unavailable"


def _require_account_worker_configuration(
    user,
    account,
    mode: str,
    *,
    require_rules_content: bool,
) -> None:
    """Validate every control document before a worker can start or be adopted."""
    user_id = int(user["id"])
    account_key = str(account["account_key"])
    document = _read_rules_document(
        user_id, user, account_key, persist_legacy=False
    )
    settings = _read_automation_settings(user_id, account_key)
    if settings.get("enabled") is False:
        raise HTTPException(
            409,
            detail={"code": "automation_disabled", "message": "请先开启自动处理"},
        )
    if require_rules_content and mode == "rules":
        snapshot = load_verified_snapshot(user_id, account_key)
        products = _read_products_document(user_id, account_key)
        rules_set = any(
            rule.get("enabled") and rule.get("reply")
            for rule in document["rules"]
        )
        deliveries_set = bool(deliveries_from_products(products, snapshot))
        configured = (
            rules_set
            or deliveries_set
            or str(settings.get("first_reply") or "").strip()
            or str(settings.get("fallback_reply") or "").strip()
        )
    else:
        configured = True
    if not configured:
        raise HTTPException(
            409,
            detail={
                "code": "automation_rules_unconfigured",
                "message": "请先设置关键词回复、默认回复或订单自动发资料",
            },
        )
    if mode == "rules_ai" and not ai_service.is_reply_ready(
        int(user["id"]), int(account["id"]), str(account["account_key"])
    ):
        raise HTTPException(
            409,
            detail={"code": "ai_reply_not_ready", "message": "AI 客服配置尚未就绪"},
        )


def _degrade_invalid_worker_configuration(
    user_id: int,
    account,
    mode: str,
    code: str,
) -> None:
    """Stop any local or durable worker before recording a fail-closed block."""
    runtime = db.get_worker_runtime(user_id, account["id"])
    durable_pid = int(runtime["pid"]) if runtime is not None and runtime["pid"] else None
    local_pid = bot_process_id(user_id, str(account["account_key"]))
    remaining_pid = None
    stop_reason = "not_running"
    if local_pid:
        stopped, stop_reason = bot_stop(user_id, str(account["account_key"]))
        if not _stop_confirmed(stopped, stop_reason):
            remaining_pid = int(local_pid)
    if durable_pid and durable_pid != local_pid:
        stopped, stop_reason = bot_terminate_pid(
            user_id, durable_pid, str(account["account_key"])
        )
        if not _stop_confirmed(stopped, stop_reason):
            remaining_pid = durable_pid
    _persist_worker_observation(
        user_id,
        account,
        desired_state="running",
        state="degraded",
        pid=remaining_pid,
        last_error=code if remaining_pid is None else f"{code}_{stop_reason}"[:240],
        mode=mode,
    )


def _worker_transition_payload(user_id: int, account, code: str = "") -> dict:
    runtime = db.get_worker_runtime(user_id, account["id"])
    try:
        running = bool(
            bot_status(user_id, str(account["account_key"])).get("running")
        )
    except (OSError, RuntimeError, ValueError):
        running = False
    return {
        "desired_running": bool(runtime and runtime["desired_state"] == "running"),
        "state": str(runtime["state"] or "stopped") if runtime else "stopped",
        "running": running,
        "code": str(code or "")[:120],
    }


def _start_account_worker_locked(
    user,
    account,
    mode: str,
    lease,
    *,
    validate_configuration: bool,
    automatic: bool,
) -> str:
    """Start one account while its worker-control lease is held."""
    user_id = int(user["id"])
    account_key = str(account["account_key"])
    _ensure_account_lease(lease)
    try:
        _require_account_worker_configuration(
            user,
            account,
            mode,
            require_rules_content=validate_configuration,
        )
    except HTTPException as exc:
        if automatic:
            code = _worker_configuration_error_code(exc)
            try:
                _degrade_invalid_worker_configuration(user_id, account, mode, code)
            except (OSError, RuntimeError, sqlite3.Error):
                pass
        raise
    if not bot_status(user_id, account_key).get("connected"):
        if automatic:
            _persist_worker_observation(
                user_id,
                account,
                desired_state="running",
                state="waiting_login",
                pid=None,
                last_error="shop_not_connected",
                mode=mode,
            )
        raise HTTPException(409, "请先连接并验证闲鱼店铺")

    _prepare_account_worker_start_locked(
        user_id,
        account,
        mode,
        lease,
        preserve_desired_running=automatic,
    )
    _ensure_account_lease(lease)
    try:
        ok, reason = (
            bot_start(user_id, mode)
            if account_key == "default"
            else bot_start(user_id, mode, account_key)
        )
    except OSError as exc:
        if automatic:
            _persist_worker_observation(
                user_id,
                account,
                desired_state="running",
                state="degraded",
                pid=bot_process_id(user_id, account_key),
                last_error="worker_start_os_error",
                mode=mode,
            )
        raise HTTPException(503, "机器人进程启动失败") from exc
    if not ok:
        if automatic:
            _persist_worker_observation(
                user_id,
                account,
                desired_state="running",
                state="degraded",
                pid=bot_process_id(user_id, account_key),
                last_error=_safe_worker_error_code(reason),
                mode=mode,
            )
        raise HTTPException(409, reason)
    try:
        _ensure_account_lease(lease)
        _persist_worker_started(user_id, mode, reason == "already_running", account)
        _ensure_account_lease(lease)
    except (OSError, RuntimeError, sqlite3.Error, HTTPException) as exc:
        stop_ok, stop_reason = bot_stop(user_id, account_key)
        stop_confirmed = _stop_confirmed(stop_ok, stop_reason)
        try:
            if automatic:
                _persist_worker_observation(
                    user_id,
                    account,
                    desired_state="running",
                    state="degraded",
                    pid=None if stop_confirmed else bot_process_id(user_id, account_key),
                    last_error="worker_state_persist_failed",
                    mode=mode,
                )
            else:
                _persist_worker_stopped(
                    user_id,
                    f"persist_start_failed:{stop_reason}",
                    account,
                    state="degraded",
                    pid=None if stop_confirmed else bot_process_id(user_id, account_key),
                )
        except (OSError, RuntimeError, sqlite3.Error):
            pass
        raise HTTPException(503, "机器人状态保存失败，请稍后重试") from exc
    return reason


def _autostart_account_worker(user_id: int, account) -> dict:
    """Clear verified auth state and resume durable running intent idempotently."""
    try:
        lease, owner = _acquire_account_lease(
            "worker-control",
            {"id": user_id},
            account,
            lease_seconds=45,
            busy_message="自动客服状态正在变更，请稍后重试",
            unavailable_message="自动客服状态暂时不可用，请稍后重试",
        )
    except Exception as exc:
        code = (
            "worker_control_busy"
            if isinstance(exc, HTTPException) and exc.status_code == 409
            else "worker_control_unavailable"
        )
        try:
            return _worker_transition_payload(user_id, account, code)
        except Exception:
            return {"desired_running": False, "state": "degraded", "running": False, "code": code}
    try:
        _ensure_account_lease(lease)
        account = db.get_shop_account(user_id, account_id=account["id"])
        if account is None or not account["enabled"]:
            return {"desired_running": False, "state": "stopped", "running": False, "code": "account_unavailable"}
        account_key = str(account["account_key"])
        runtime = db.get_worker_runtime(user_id, account["id"])
        try:
            _clear_auth_status(user_id, account_key)
        except OSError:
            if runtime is not None and runtime["desired_state"] == "running":
                _persist_worker_observation(
                    user_id,
                    account,
                    desired_state="running",
                    state="degraded",
                    pid=bot_process_id(user_id, account_key),
                    last_error="auth_status_clear_failed",
                )
            return _worker_transition_payload(user_id, account, "auth_status_clear_failed")
        if runtime is None or runtime["desired_state"] != "running":
            return _worker_transition_payload(user_id, account)
        user = db.get_user_by_id(user_id)
        if user is None:
            _persist_worker_observation(
                user_id,
                account,
                desired_state="running",
                state="degraded",
                pid=None,
                last_error="user_unavailable",
            )
            return _worker_transition_payload(user_id, account, "user_unavailable")
        persisted_mode = str(runtime["mode"] or "rules")
        mode = "rules_ai" if persisted_mode == "rules_ai" and has_permission(user, "automation.ai") else "rules"
        try:
            reason = _start_account_worker_locked(
                user,
                account,
                mode,
                lease,
                validate_configuration=False,
                automatic=True,
            )
        except HTTPException as exc:
            runtime = db.get_worker_runtime(user_id, account["id"])
            code = str(runtime["last_error"] or "worker_start_failed") if runtime else "worker_start_failed"
            if runtime is None or runtime["desired_state"] != "running" or runtime["state"] != "degraded":
                _persist_worker_observation(
                    user_id,
                    account,
                    desired_state="running",
                    state="degraded",
                    pid=bot_process_id(user_id, account_key),
                    last_error=code,
                    mode=mode,
                )
            del exc
            return _worker_transition_payload(user_id, account, code)
        return _worker_transition_payload(user_id, account, reason)
    except Exception:
        try:
            if account is not None:
                runtime = db.get_worker_runtime(user_id, account["id"])
                if runtime is not None and runtime["desired_state"] == "running":
                    _persist_worker_observation(
                        user_id,
                        account,
                        desired_state="running",
                        state="degraded",
                        pid=bot_process_id(user_id, str(account["account_key"])),
                        last_error="worker_autostart_failed",
                    )
        except Exception:
            pass
        try:
            return _worker_transition_payload(user_id, account, "worker_autostart_failed")
        except Exception:
            return {
                "desired_running": True,
                "state": "degraded",
                "running": False,
                "code": "worker_autostart_failed",
            }
    finally:
        _release_account_lease(lease, owner)


def _reserve_watchdog_transition(
    user_id: int,
    account_key: str,
    expected_pid: int,
    expected_mode: str,
    target_mode: str | None,
):
    del target_mode
    account = db.get_shop_account(user_id, account_key=account_key)
    if account is None or not account["enabled"]:
        return None
    try:
        lease, owner = _acquire_account_lease(
            "worker-control",
            {"id": user_id},
            account,
            lease_seconds=45,
            busy_message="watchdog worker lease busy",
            unavailable_message="watchdog worker lease unavailable",
        )
        _ensure_account_lease(lease)
        runtime = db.get_worker_runtime(user_id, account["id"])
        if (
            runtime is None
            or runtime["desired_state"] != "running"
            or int(runtime["pid"] or 0) != int(expected_pid)
            or str(runtime["mode"] or "rules") != str(expected_mode or "rules")
        ):
            _release_account_lease(lease, owner)
            return None
        return lease, owner, account
    except Exception:
        try:
            if "lease" in locals():
                _release_account_lease(lease, owner)
        except Exception:
            pass
        return None


def _persist_watchdog_transition(
    reservation,
    user_id: int,
    account_key: str,
    mode: str | None,
    pid: int | None,
    supervisor_generation: int,
    expected_pid: int,
) -> bool:
    del account_key, supervisor_generation
    lease, _owner, account = reservation
    try:
        _ensure_account_lease(lease)
        runtime = db.get_worker_runtime(user_id, account["id"])
        if (
            runtime is None
            or runtime["desired_state"] != "running"
            or int(runtime["pid"] or 0) != int(expected_pid)
        ):
            return False
        previous_generation = int(runtime["generation"] or 0)
        if mode is None:
            desired_state = "stopped"
            persisted_mode = str(runtime["mode"] or "rules")
            state = "stopped"
            next_pid = None
            generation = previous_generation
        else:
            desired_state = "running"
            persisted_mode = mode
            state = "running"
            next_pid = int(pid)
            generation = previous_generation + 1
        row = db.persist_worker_runtime(
            user_id,
            account["id"],
            desired_state=desired_state,
            mode=persisted_mode,
            state=state,
            pid=next_pid,
            generation=generation,
            started_at=time.time() if next_pid else runtime["started_at"],
            heartbeat_at=time.time(),
            exit_code=None,
            last_error="",
            expected_generation=previous_generation,
        )
        _ensure_account_lease(lease)
        return row is not None
    except Exception:
        return False


def _release_watchdog_transition(reservation) -> None:
    lease, owner, _account = reservation
    _release_account_lease(lease, owner)


def _shop_sync_http_error(error: ShopSyncError) -> HTTPException:
    status = {
        "cookie_invalid": 400,
        "cookie_incomplete": 400,
        "account_restricted": 422,
        "sync_cooldown": 429,
        "risk_cooldown": 429,
        "platform_busy": 429,
        "risk_control": 422,
        "cookie_expired": 422,
        "profile_missing": 422,
        "sync_busy": 429,
        "sync_timeout": 504,
        "platform_error": 503,
        "network_error": 503,
    }.get(error.code, 503)
    detail = sync_status_payload(error.code, str(error))
    detail["retryable"] = error.code not in {
        "cookie_invalid",
        "cookie_incomplete",
        "cookie_expired",
        "account_restricted",
    }
    return HTTPException(status, detail=detail)


def _cookie_http_error(code: str, message: str, status: int) -> HTTPException:
    detail = sync_status_payload(code, message)
    detail["retryable"] = code not in {"cookie_invalid", "cookie_incomplete", "cookie_expired"}
    return HTTPException(status, detail=detail)


def _same_saved_cookie(user_id: int, account_key: str, candidate: str) -> bool:
    saved = read_secret(user_id, "cookies.txt", account_key)
    if not saved:
        return False
    try:
        saved_normalized, _ = parse_cookie_header(saved)
        candidate_normalized, _ = parse_cookie_header(candidate)
    except ShopSyncError:
        return False
    return hashlib.sha256(saved_normalized.encode("utf-8")).digest() == hashlib.sha256(
        candidate_normalized.encode("utf-8")
    ).digest()


def _xianyu_login_http_error(error: XianyuLoginError) -> HTTPException:
    statuses = {
        "invalid_request": 400,
        "login_not_found": 404,
        "login_exists": 409,
        "login_busy": 409,
        "login_not_confirmed": 409,
        "login_consumed": 409,
        "login_expired": 410,
        "login_cooldown": 429,
        "login_capacity": 503,
        "qr_query_failed": 502,
        "login_confirm_failed": 502,
        "mtop_context_failed": 502,
        "qr_cookie_incomplete": 422,
        "network_error": 503,
        "platform_error": 502,
    }
    code = error.code
    return HTTPException(
        statuses.get(code, 502),
        detail={
            "code": code,
            "message": error.message,
            "retryable": code not in {"invalid_request"},
        },
    )


def _run_shop_sync_inner(
    user_id: int,
    cookie_header: str,
    replace_cookie: bool,
    account=None,
    before_replace_persist=None,
) -> dict:
    try:
        return run_shop_sync_inner(
            db=db,
            read_secret=read_secret,
            write_secret=write_secret,
            load_verified_snapshot=load_verified_snapshot,
            sync_account_state=_sync_account_state,
            user_id=user_id,
            cookie_header=cookie_header,
            replace_cookie=replace_cookie,
            account=account,
            # Keep API contract tests' injected functions effective while the
            # consumer uses the module defaults in its own process.
            sync_func=sync_shop,
            reserve_sync_func=reserve_sync,
            lease_owner_prefix="api",
            before_replace_persist=before_replace_persist,
        )
    except ShopSyncError as error:
        raise _shop_sync_http_error(error) from None
    except ShopSyncPersistenceError as error:
        raise HTTPException(503, str(error)) from None


def _run_shop_sync(
    user_id: int,
    cookie_header: str,
    replace_cookie: bool,
    account=None,
    before_replace_persist=None,
) -> dict:
    """Run one sync through a durable, account-scoped idempotent job."""
    try:
        normalized, _ = parse_cookie_header(cookie_header)
    except ShopSyncError as error:
        raise _shop_sync_http_error(error) from None

    account = account or db.ensure_default_shop_account(user_id)
    if account is None:
        raise HTTPException(503, "店铺账号状态不可用，请稍后重试")
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    if replace_cookie:
        idempotency_key = f"replace:{fingerprint}"
    else:
        bucket = int(time.time() // max(SYNC_COOLDOWN_SECONDS, 1))
        # Include the verified Cookie fingerprint so a replacement created in
        # the same cooldown bucket cannot be mistaken for the old refresh.
        idempotency_key = f"refresh:{bucket}:{fingerprint}"
    job_kind = "shop_sync_replace" if replace_cookie else "shop_sync"
    job = db.enqueue_job(
        user_id,
        job_kind,
        idempotency_key,
        account_id=account["id"],
        payload={"replace_cookie": bool(replace_cookie), "cookie_fingerprint": fingerprint},
        max_attempts=3,
    )
    if job["status"] == "dead_letter":
        # A terminal failure belongs to the previous attempt. A deliberate
        # user retry must receive a fresh idempotency key, otherwise one burst
        # of platform errors permanently suppresses all later recovery tries.
        idempotency_key = f"{idempotency_key}:retry:{time.time_ns()}"
        job = db.enqueue_job(
            user_id,
            job_kind,
            idempotency_key,
            account_id=account["id"],
            payload={"replace_cookie": bool(replace_cookie), "cookie_fingerprint": fingerprint},
            max_attempts=3,
        )
    if job["status"] == "completed":
        snapshot = load_verified_snapshot(user_id, str(account["account_key"]))
        if snapshot is not None:
            result = dict(snapshot)
            result["_worker_transition"] = _autostart_account_worker(user_id, account)
            return result
        # A manually removed snapshot must not make a completed job suppress
        # all future repairs; create a fresh, unique repair key.
        idempotency_key = f"{idempotency_key}:repair:{time.time_ns()}"
        job = db.enqueue_job(
            user_id,
            job_kind,
            idempotency_key,
            account_id=account["id"],
            payload={"replace_cookie": bool(replace_cookie), "cookie_fingerprint": fingerprint},
            max_attempts=3,
        )

    owner = f"sync-job:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    claim_now = time.time()
    if os.environ.get("SAAS_TESTING") == "1" and job["status"] == "retry":
        # Contract tests intentionally invoke consecutive synthetic failures;
        # keep production backoff while allowing them to exercise each error
        # classifier in one process.
        claim_now = max(claim_now, float(job["available_at"] or claim_now))
    claimed = db.claim_job(job["id"], owner, lease_seconds=SYNC_MAX_SECONDS + 90, now=claim_now)
    if claimed is None:
        current = db.get_job(job["id"])
        if current is not None and current["status"] == "retry":
            raise _shop_sync_http_error(ShopSyncError("sync_cooldown", "操作太频繁，请稍后再试"))
        raise _shop_sync_http_error(ShopSyncError("sync_busy", "已有店铺同步正在进行，请稍后再试"))
    try:
        snapshot = _run_shop_sync_inner(
            user_id,
            normalized,
            replace_cookie,
            account,
            before_replace_persist=before_replace_persist,
        )
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        db.fail_job(job["id"], owner, detail.get("code", "sync_error"))
        raise
    except Exception:
        db.fail_job(job["id"], owner, "sync_error")
        raise
    else:
        try:
            account_generation = int(account["generation"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            account_generation = 0
        completed = db.complete_job_for_account(
            job["id"],
            owner,
            user_id,
            int(account["id"]),
            account_generation,
            str(account["account_key"]),
        )
        if not completed and not db.account_is_current(
            user_id, int(account["id"]), account_generation
        ):
            db.fail_job(job["id"], owner, "account_unavailable", "店铺账号已停用")
            raise HTTPException(503, "店铺账号已停用，同步结果已丢弃")
        result = dict(snapshot)
        result["_worker_transition"] = _autostart_account_worker(user_id, account)
        return result


def _shop_sync_payload(snapshot: dict) -> dict:
    checked_at = str(snapshot.get("synced_at") or "")
    payload = {
        "ok": True,
        "connected": True,
        "shop_name": snapshot.get("nickname", ""),
        "platform_name": snapshot.get("nickname", ""),
        "product_count": int(snapshot.get("product_count") or 0),
        "synced_at": checked_at,
        "truncated": bool(snapshot.get("truncated")),
        "cookie_status": sync_status_payload("verified", checked_at=checked_at),
    }
    transition = snapshot.get("_worker_transition")
    if isinstance(transition, dict):
        payload["worker"] = {
            "desired_running": bool(transition.get("desired_running")),
            "state": str(transition.get("state") or "stopped")[:40],
            "running": bool(transition.get("running")),
            "code": str(transition.get("code") or "")[:120],
        }
    return payload


def _job_summary(row, account=None) -> dict:
    """Return a browser-safe job shape without payloads or error text."""
    if row is None:
        return None
    raw_account_id = int(row["account_id"] or 0)
    account_id = int(account["id"]) if account is not None and raw_account_id == 0 else raw_account_id
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "account_id": account_id,
        "status": str(row["status"]),
        "attempts": int(row["attempts"] or 0),
        "max_attempts": int(row["max_attempts"] or 0),
        "available_at": row["available_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "error_code": str(row["last_error_code"] or ""),
    }


def _job_matches_account(row, account) -> bool:
    if row is None or account is None:
        return False
    raw_account_id = int(row["account_id"] or 0)
    return raw_account_id == int(account["id"]) or (
        raw_account_id == 0 and str(account["account_key"]) == "default"
    )


def _enqueue_shop_refresh(user_id: int, account):
    """Queue a refresh using only a hash of the saved Cookie in the payload."""
    account_key = str(account["account_key"])
    cookie_header = read_secret(user_id, "cookies.txt", account_key)
    if not cookie_header:
        raise _cookie_http_error("unconfigured", "请先连接闲鱼店铺", 400)
    try:
        normalized, _ = parse_cookie_header(cookie_header)
    except ShopSyncError as error:
        raise _shop_sync_http_error(error) from None
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    bucket = int(time.time() // max(SYNC_COOLDOWN_SECONDS, 1))
    job = db.enqueue_job(
        user_id,
        "shop_sync",
        f"refresh:{bucket}:{fingerprint}",
        account_id=account["id"],
        payload={"replace_cookie": False, "cookie_fingerprint": fingerprint},
        max_attempts=3,
    )
    if job["status"] == "dead_letter":
        job = db.enqueue_job(
            user_id,
            "shop_sync",
            f"refresh:{bucket}:{fingerprint}:retry:{time.time_ns()}",
            account_id=account["id"],
            payload={"replace_cookie": False, "cookie_fingerprint": fingerprint},
            max_attempts=3,
        )
    return job


def _async_job_response(job, account):
    summary = _job_summary(job, account)
    if job["status"] == "completed":
        user_id = int(job["user_id"])
        snapshot = load_verified_snapshot(user_id, str(account["account_key"]))
        if snapshot is not None:
            result = dict(snapshot)
            # The consumer cannot control API-owned workers. Finalizing the
            # completed refresh here clears stale NEEDS_HUMAN auth state and
            # resumes the account's durable running intent before the browser
            # refreshes its status cards.
            result["_worker_transition"] = _autostart_account_worker(user_id, account)
            return _shop_sync_payload(result)
        detail = sync_status_payload("sync_error", "同步任务已结束，但店铺快照暂时不可用")
        detail["retryable"] = True
        raise HTTPException(503, detail=detail)
    if job["status"] == "dead_letter":
        detail = sync_status_payload(job["last_error_code"] or "sync_error")
        detail["retryable"] = True
        raise HTTPException(503, detail=detail)
    return JSONResponse(status_code=202, content={"ok": True, "job": summary})


def _read_products_document(user_id: int, account_key: str = "default") -> dict:
    raw = read_secret(user_id, "products_config.json", account_key)
    if not raw:
        return {"version": 1, "types": []}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {"version": 1, "types": []}
    return payload if isinstance(payload, dict) else {"version": 1, "types": []}


def _material_batch_token(account, snapshot: dict, products: dict, updates: list[dict]) -> str:
    """Bind a preview to one account, snapshot, config and exact request."""
    payload = {
        "account_id": int(account["id"]),
        "account": str(account["account_key"]),
        "snapshot": product_snapshot_revision(snapshot),
        "config": product_config_revision(products),
        "updates": updates,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _material_batch_candidate(user, account, body: ProductBatchIn):
    account_key = str(account["account_key"])
    snapshot = load_verified_snapshot(user["id"], account_key)
    if snapshot is None:
        raise HTTPException(409, "请先连接并验证闲鱼店铺")
    try:
        updates = normalise_material_batch(
            body.item_ids, body.material, body.enabled, snapshot
        )
    except AutomationValidationError as error:
        raise HTTPException(400, str(error)) from error
    products = _read_products_document(user["id"], account_key)
    summary = material_batch_preview(products, updates, snapshot)
    token = _material_batch_token(account, snapshot, products, updates)
    preview = {"preview_token": token, **summary}
    return snapshot, products, updates, preview


def _read_account_control_file(
    user_id: int,
    account_key: str,
    name: str,
    *,
    maximum_bytes: int = 256 * 1024,
) -> str | None:
    """Read one account control file without conflating absence and corruption."""
    storage = AccountStorage()
    try:
        path = storage.account_dir(user_id, account_key) / name
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, AccountStorageError) as exc:
        raise HTTPException(
            503,
            detail={"code": "automation_config_unavailable", "message": "自动化配置暂时不可用，请检查后重试"},
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
        raise HTTPException(
            503,
            detail={"code": "automation_config_unavailable", "message": "自动化配置暂时不可用，请检查后重试"},
        )
    try:
        return storage.read_text(user_id, account_key, name).strip()
    except (OSError, AccountStorageError) as exc:
        raise HTTPException(
            503,
            detail={"code": "automation_config_unavailable", "message": "自动化配置暂时不可用，请检查后重试"},
        ) from exc


def _read_rules_document(
    user_id: int,
    user=None,
    account_key: str = "default",
    persist_legacy: bool = True,
) -> dict:
    del user, persist_legacy
    raw = _read_account_control_file(user_id, account_key, "reply_rules.json")
    if raw is None:
        raise HTTPException(
            503,
            detail={"code": "reply_rules_unavailable", "message": "回复规则文件缺失，全部自动回复已暂停，请重新保存规则"},
        )
    try:
        return rules_document(json.loads(raw))
    except (TypeError, ValueError, AutomationValidationError) as exc:
        raise HTTPException(
            503,
            detail={"code": "reply_rules_unavailable", "message": "回复规则文件无效，全部自动回复已暂停，请重新保存规则"},
        ) from exc


def _read_automation_settings(user_id: int, account_key: str = "default") -> dict:
    """Read the account strategy document and fail closed on loss or corruption."""
    raw = _read_account_control_file(user_id, account_key, "automation_settings.json")
    if raw is None:
        raise HTTPException(
            503,
            detail={"code": "automation_settings_unavailable", "message": "自动化设置文件缺失，自动回复已暂停，请重新保存设置"},
        )
    try:
        return normalise_settings(json.loads(raw))
    except (TypeError, ValueError, AutomationValidationError) as exc:
        raise HTTPException(
            503,
            detail={"code": "automation_settings_unavailable", "message": "自动化设置文件无效，自动回复已暂停，请重新保存设置"},
        ) from exc


def _automation_payload(user, account=None, persist_legacy: bool = True) -> dict:
    account_key = str(account["account_key"]) if account else "default"
    document = _read_rules_document(user["id"], user, account_key, persist_legacy=persist_legacy)
    snapshot = load_verified_snapshot(user["id"], account_key)
    products = _read_products_document(user["id"], account_key)
    settings = _read_automation_settings(user["id"], account_key)
    return {
        "version": 1,
        "rules": document["rules"],
        "deliveries": deliveries_from_products(products, snapshot),
        "strategy": settings["strategy"],
        "enabled": settings["enabled"],
        "first_reply": settings.get("first_reply", ""),
        "fallback_reply": settings.get("fallback_reply", ""),
        "delay_min_seconds": settings.get("delay_min_seconds", 0),
        "delay_max_seconds": settings.get("delay_max_seconds", 0),
        "trigger_cooldown_seconds": settings.get("trigger_cooldown_seconds", 0),
        "manual_takeover_cooldown_seconds": settings.get("manual_takeover_cooldown_seconds", 0),
        "business_hours_enabled": settings.get("business_hours_enabled", False),
        "business_start": settings.get("business_start", "09:00"),
        "business_end": settings.get("business_end", "23:30"),
        "running": bool(bot_status(user["id"], account_key).get("running")),
        "rules_set": bool(
            any(rule.get("enabled") and rule.get("reply") for rule in document["rules"])
        ),
        "deliveries_set": bool(deliveries_from_products(products, snapshot)),
    }


def _config_payload(user, account=None):
    account_key = str(account["account_key"]) if account else "default"
    row = db.get_config(user["id"]) if account_key == "default" else None
    payload = {
        "bot_running": bool(row["bot_running"]) if row else False,
        "platform_ai": {
            "managed": False,
            "available": bool(
                account
                and platform_ai_configured(
                    user["id"], account["id"], str(account["account_key"])
                )
            ),
        },
    }
    document = None
    if has_permission(user, "automation.rules"):
        document = _read_rules_document(user["id"], user, account_key)
        payload["reply_rules"] = document["rules"]
    if has_permission(user, "automation.ai"):
        if row is not None and row["keywords_json"]:
            payload["keywords_json"] = row["keywords_json"]
        else:
            document = document or _read_rules_document(user["id"], user, account_key)
            payload["keywords_json"] = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return payload


def _discard_new_account_storage(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID) -> bool:
    """Best-effort removal limited to a just-initialized private account directory."""
    try:
        from bot_manager import discard_initialized_dir

        return discard_initialized_dir(user_id, account_key)
    except (OSError, ValueError):
        return False


def _validate_password(password: str) -> None:
    password = str(password or "")
    if not (PASSWORD_MIN_LENGTH <= len(password) <= 1024):
        raise HTTPException(
            400,
            detail={
                "code": "password_invalid",
                "message": f"密码长度需为 {PASSWORD_MIN_LENGTH}-1024 位",
            },
        )


def _validate_new_credentials(username: str, password: str) -> str:
    username = str(username or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            400,
            detail={
                "code": "username_invalid",
                "message": "账号需为 3-32 位字母、数字、点、下划线或短横线",
            },
        )
    _validate_password(password)
    return username


def _new_user_initializer(holder: dict):
    def initialize(user_id: int) -> None:
        from bot_manager import ensure_dir as ensure_bot_dir

        holder["user_id"] = int(user_id)
        ensure_bot_dir(user_id, DEFAULT_ACCOUNT_ID, initialize=True)

    return initialize


def _registration_disabled() -> HTTPException:
    return HTTPException(
        403,
        detail={"code": "registration_disabled", "message": "注册当前未开放"},
    )


@app.post("/api/auth/bootstrap")
def bootstrap_admin(
    body: RegisterIn,
    request: Request,
    x_bootstrap_token: str = Header(default="", alias=BOOTSTRAP_TOKEN_HEADER),
):
    _require_public_write_origin(request)
    if not _env_true("SAAS_BOOTSTRAP_ENABLED", "0") or not _bootstrap_source_trusted(request):
        _audit("auth.bootstrap_failed", request, outcome="denied", metadata={"code": "unavailable"})
        raise HTTPException(
            403,
            detail={"code": "bootstrap_unavailable", "message": "初始化入口不可用"},
        )
    bootstrap_state = db.get_bootstrap_state()
    if (
        bootstrap_state is None
        or str(bootstrap_state["state"]) != "pending"
        or db.user_count() != 0
    ):
        _audit("auth.bootstrap_failed", request, outcome="conflict", metadata={"code": "consumed"})
        raise HTTPException(
            409,
            detail={"code": "bootstrap_consumed", "message": "初始化已完成"},
        )
    configured_digest = _bootstrap_token_digest()
    supplied = str(x_bootstrap_token or "").strip()
    supplied_digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    if (
        not configured_digest
        or not (32 <= len(supplied) <= 256)
        or not secrets.compare_digest(configured_digest, supplied_digest)
    ):
        _audit("auth.bootstrap_failed", request, outcome="denied", metadata={"code": "invalid"})
        raise HTTPException(
            403,
            detail={"code": "bootstrap_denied", "message": "无法完成初始化"},
        )
    username = _validate_new_credentials(body.username, body.password)
    holder: dict[str, int] = {}
    try:
        user_id = db.bootstrap_user(
            username,
            body.password,
            supplied_digest,
            initializer=_new_user_initializer(holder),
        )
    except BootstrapTokenError as exc:
        _audit("auth.bootstrap_failed", request, outcome="denied", metadata={"code": "invalid"})
        raise HTTPException(
            403,
            detail={"code": "bootstrap_denied", "message": "无法完成初始化"},
        ) from exc
    except BootstrapUnavailableError as exc:
        _audit("auth.bootstrap_failed", request, outcome="conflict", metadata={"code": "consumed"})
        raise HTTPException(
            409,
            detail={"code": "bootstrap_consumed", "message": "初始化已完成"},
        ) from exc
    except sqlite3.IntegrityError as exc:
        _audit("auth.bootstrap_failed", request, outcome="conflict", metadata={"code": "conflict"})
        raise HTTPException(
            409,
            detail={"code": "bootstrap_consumed", "message": "初始化已完成"},
        ) from exc
    except (OSError, sqlite3.Error) as exc:
        if holder.get("user_id") is not None:
            _discard_new_account_storage(holder["user_id"], DEFAULT_ACCOUNT_ID)
        _audit("auth.bootstrap_failed", request, outcome="failed", metadata={"code": "storage"})
        raise HTTPException(
            503,
            detail={"code": "account_initialization_failed", "message": "账号初始化失败，请稍后重试"},
        ) from exc
    _audit(
        "auth.bootstrap_succeeded",
        request,
        actor_user_id=user_id,
        target_type="user",
        target_id=user_id,
        metadata={"role": "admin"},
    )
    return {"ok": True}


@app.post("/api/auth/register")
def register(body: RegisterIn, request: Request):
    _require_public_write_origin(request)
    if not _registration_env_allowed():
        raise _registration_disabled()
    username = _validate_new_credentials(body.username, body.password)
    holder: dict[str, int] = {}
    try:
        user_id = db.register_user(
            username,
            body.password,
            initializer=_new_user_initializer(holder),
        )
    except RegistrationClosedError as exc:
        raise _registration_disabled() from exc
    except sqlite3.IntegrityError as exc:
        _audit("auth.registration_failed", request, outcome="conflict", metadata={"code": "username_conflict"})
        raise HTTPException(
            409,
            detail={"code": "username_unavailable", "message": "账号名称不可用"},
        ) from exc
    except (OSError, sqlite3.Error) as exc:
        if holder.get("user_id") is not None:
            _discard_new_account_storage(holder["user_id"], DEFAULT_ACCOUNT_ID)
        _audit("auth.registration_failed", request, outcome="failed", metadata={"code": "storage"})
        raise HTTPException(
            503,
            detail={"code": "account_initialization_failed", "message": "账号初始化失败，请稍后重试"},
        ) from exc
    _audit(
        "auth.registration_succeeded",
        request,
        actor_user_id=user_id,
        target_type="user",
        target_id=user_id,
        metadata={"role": "owner"},
    )
    return {"ok": True}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    _require_public_write_origin(request)
    username = str(body.username or "").strip()
    username_digest = _username_hash(username)
    client_digest = _login_client_hash(request)
    rate = db.login_rate_status(username_digest, client_digest)
    user = db.get_user(username) if len(username) <= 128 else None
    stored_hash = str(user["password_hash"]) if user is not None else DUMMY_PASSWORD_HASH
    password = str(body.password or "")
    valid, needs_upgrade = verify_password_details(
        password if len(password) <= 1024 else "invalid-password-length",
        stored_hash,
    )
    valid = bool(valid and user is not None and user["disabled_at"] is None and not rate["locked"])
    if not valid:
        retry_after = db.record_login_failure(username_digest, client_digest)
        _audit(
            "auth.login_failed",
            request,
            target_type="account",
            target_id=username_digest,
            outcome="denied",
            metadata={"code": "login_failed"},
        )
        detail = {"code": "login_failed", "message": "账号或密码错误，请稍后重试"}
        if rate["locked"] or retry_after:
            raise HTTPException(
                429,
                detail=detail,
                headers={"Retry-After": str(max(rate["retry_after"], retry_after, 1))},
            )
        raise HTTPException(401, detail=detail)
    db.clear_login_failures(username_digest, client_digest)
    if needs_upgrade:
        db.upgrade_password_hash(user["id"], stored_hash, password)
    token = db.create_token(user["id"])
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=30 * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path=COOKIE_PATH,
    )
    _audit(
        "auth.login_succeeded",
        request,
        actor_user_id=user["id"],
        target_type="user",
        target_id=user["id"],
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(
    request: Request,
    response: Response,
    user=Depends(Auth.current_user),
    token=Depends(Auth.current_token),
):
    clear_all_qr = getattr(qr_logins, "clear_user_all", None)
    if clear_all_qr is not None:
        clear_all_qr(user["id"])
    else:
        # Compatibility with the tiny QR fake used by older contract tests.
        qr_logins.clear_user(user["id"])
    db.delete_token(token)
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=COOKIE_SECURE, samesite="strict")
    _audit(
        "auth.logout",
        request,
        actor_user_id=user["id"],
        target_type="user",
        target_id=user["id"],
    )
    return {"ok": True}


@app.post("/api/auth/password")
def change_current_password(
    body: PasswordChangeIn,
    request: Request,
    user=Depends(Auth.current_user),
    token=Depends(Auth.current_token),
):
    _validate_password(body.new_password)
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(
            400,
            detail={"code": "password_change_denied", "message": "当前密码不正确"},
        )
    changed = db.change_password(
        user["id"], user["password_hash"], body.new_password, keep_token=token
    )
    if not changed:
        raise HTTPException(
            409,
            detail={"code": "password_changed_elsewhere", "message": "密码已发生变化，请重新登录"},
        )
    _audit(
        "auth.password_changed",
        request,
        actor_user_id=user["id"],
        target_type="user",
        target_id=user["id"],
    )
    return {"ok": True, "other_sessions_revoked": True}


@app.get("/api/me")
def me(user=Depends(Auth.current_user)):
    return account_payload(user)


@app.get("/api/config")
def get_config(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    return _config_payload(user, account)


@app.put("/api/config")
def save_config(
    body: ConfigIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    account_key = str(account["account_key"])
    if body.keywords_json is not None:
        _require_permission(user, "automation.ai")
        if len(body.keywords_json) > 64 * 1024:
            raise HTTPException(400, "模板规则过长")
        try:
            document = rules_document(json.loads(body.keywords_json))
        except (TypeError, ValueError, AutomationValidationError) as exc:
            raise HTTPException(400, "回复规则格式无效") from exc
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        # Keep the old user-level column as a compatibility mirror only for
        # the legacy default account. Other accounts are fully file-scoped.
        if account_key == "default":
            db.save_config(user["id"], {"keywords_json": encoded})
        write_secret(user["id"], "reply_rules.json", encoded, account_key)
    return {"ok": True, "config": _config_payload(user, account)}


@app.get("/api/automation")
def get_automation(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.rules")
    return _automation_payload(user, account)


@app.put("/api/automation")
def save_automation(
    body: AutomationIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.rules")
    values = (
        body.rules, body.deliveries, body.strategy, body.enabled,
        body.first_reply, body.fallback_reply, body.delay_min_seconds,
        body.delay_max_seconds, body.trigger_cooldown_seconds,
        body.manual_takeover_cooldown_seconds, body.business_hours_enabled,
        body.business_start, body.business_end,
    )
    if all(value is None for value in values):
        raise HTTPException(400, "至少提交一项自动化设置")
    leases = []
    try:
        leases.append(_acquire_account_lease(
            "automation-save", user, account, lease_seconds=45,
            busy_message="另一项自动化设置正在保存，请稍后重试",
            unavailable_message="自动化设置暂时不可用，请稍后重试",
        ))
        if body.deliveries is not None:
            leases.append(_acquire_account_lease(
                "products-config", user, account, lease_seconds=45,
                busy_message="商品履约配置正在更新，请稍后重试",
                unavailable_message="商品履约配置暂时不可用，请稍后重试",
            ))
        if body.enabled is not None:
            leases.append(_acquire_account_lease(
                "worker-control", user, account, lease_seconds=45,
                busy_message="自动客服状态正在变更，请稍后重试",
                unavailable_message="自动客服状态暂时不可用，请稍后重试",
            ))
        return _save_automation_locked(body, user, account, leases)
    finally:
        for lease, owner in reversed(leases):
            _release_account_lease(lease, owner)


def _save_automation_locked(body: AutomationIn, user, account, leases=()):
    account_key = str(account["account_key"])
    rules = None
    deliveries = None
    if body.rules is not None:
        try:
            rules = normalise_rules(body.rules)
        except AutomationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
    settings = None
    setting_fields = (
        "strategy", "enabled", "first_reply", "fallback_reply",
        "delay_min_seconds", "delay_max_seconds", "trigger_cooldown_seconds",
        "manual_takeover_cooldown_seconds", "business_hours_enabled",
        "business_start", "business_end",
    )
    if any(getattr(body, name) is not None for name in setting_fields):
        try:
            candidate = dict(_read_automation_settings(user["id"], account_key))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("code") != "automation_settings_unavailable":
                raise
            # An explicit owner save is the only path allowed to repair a lost
            # settings file; passive reads and worker starts remain fail-closed.
            candidate = dict(normalise_settings(None))
        for name in setting_fields:
            value = getattr(body, name)
            if value is not None:
                candidate[name] = value
        try:
            settings = normalise_settings(candidate)
        except AutomationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.deliveries is not None:
        _require_permission(user, "fulfillment.basic")
        snapshot = load_verified_snapshot(user["id"], account_key)
        if body.deliveries and snapshot is None:
            raise HTTPException(409, "请先连接并验证闲鱼店铺")
        try:
            deliveries = normalise_deliveries(body.deliveries, snapshot)
        except AutomationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        if rules is not None:
            encoded = json.dumps({"version": 1, "rules": rules}, ensure_ascii=False, separators=(",", ":"))
            write_secret(user["id"], "reply_rules.json", encoded, account_key)
            if account_key == "default":
                db.save_config(user["id"], {"keywords_json": encoded})
        if deliveries is not None:
            snapshot = load_verified_snapshot(user["id"], account_key)
            merged = merge_material_products(_read_products_document(user["id"], account_key), deliveries, snapshot)
            write_secret(user["id"], "products_config.json", json.dumps(merged, ensure_ascii=False, separators=(",", ":")), account_key)
        if settings is not None:
            write_secret(user["id"], "automation_settings.json", json.dumps(settings, ensure_ascii=False, separators=(",", ":")), account_key)
            if body.enabled is False:
                worker_lease = next((lease for lease, _owner in leases if getattr(lease, "key", "").startswith("worker-control:")), None)
                confirmed, reason = _stop_account_worker_locked(user["id"], account, "automation_disabled", worker_lease)
                if not confirmed:
                    raise OSError(reason)
    except OSError as exc:
        raise HTTPException(503, "自动化设置保存失败，请稍后重试") from exc
    return {"ok": True, "automation": _automation_payload(user, account)}


@app.get("/api/bot/status")
def get_bot_status(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    account_key = str(account["account_key"])
    result = bot_status(user["id"], account_key)
    runtime = db.get_worker_runtime(user["id"], account["id"])
    result["account_id"] = int(account["id"]) if account else None
    result["account"] = {
        "id": int(account["id"]),
        "key": str(account["account_key"]),
        "name": str(account["display_name"] or result.get("shop_name") or ""),
        "status": str(account["status"] or "unconfigured"),
        "enabled": bool(account["enabled"]),
        "last_error_code": str(account["last_error_code"] or ""),
        "last_verified_at": account["last_verified_at"],
        "last_sync_at": account["last_sync_at"],
    }
    result["desired_running"] = bool(runtime and runtime["desired_state"] == "running")
    result["runtime_state"] = str(runtime["state"]) if runtime else "stopped"
    auth_state = _read_auth_status(user["id"], account_key)
    result["auth_code"] = auth_state["code"]
    result["auth_phase"] = auth_state["phase"]
    result["auth_failure_class"] = auth_state["failure_class"]
    result["auth_layers"] = {
        "session": auth_state["session"],
        "mtop_token": auth_state["mtop_token"],
        "websocket": auth_state["websocket"],
    }
    result["auth_next_retry_at"] = auth_state["next_retry_at"]
    result["auth_failure_count"] = auth_state["failure_count"]
    result["needs_human"] = auth_state["needs_human"]
    result["reauthorization_required"] = auth_state["reauthorization_required"]
    result["auth_updated_at"] = auth_state["updated_at"]
    if auth_state["reauthorization_required"]:
        if result["desired_running"]:
            result["runtime_state"] = "waiting_login"
        # A security challenge is not the same as a platform capability ban.
        # Only an explicit account_restricted result may use "restricted".
        result["account"]["status"] = (
            "degraded" if auth_state["code"] == "risk_control" else "expired"
        )
        result["account"]["last_error_code"] = auth_state["code"]
    if not has_permission(user, "fulfillment.manage"):
        result.update(
            {
                "codes_set": None,
                "codes_total": None,
                "codes_available": None,
                "codes_locked": True,
            }
        )
    else:
        result["codes_locked"] = False
    result["ai_locked"] = not has_permission(user, "automation.ai")
    result["rules_locked"] = not has_permission(user, "automation.rules")
    result["basic_fulfillment_locked"] = not has_permission(user, "fulfillment.basic")
    return result


@app.get("/api/bot/accounts")
def get_shop_accounts(user=Depends(Auth.current_user)):
    _require_permission(user, "shop.configure")
    return {
        "accounts": [
            _shop_account_payload(row, user["id"])
            for row in db.list_shop_accounts(user["id"], include_disabled=True)
        ]
    }


@app.post("/api/bot/accounts")
def create_shop_account(body: ShopAccountIn, user=Depends(Auth.current_user)):
    _require_permission(user, "shop.configure")
    key = body.key.strip()
    name = body.name.strip()
    if len(name) > 160:
        raise HTTPException(400, "店铺备注不能超过 160 个字")
    existing = db.list_shop_accounts(user["id"], include_disabled=True)
    if len(existing) >= 20:
        raise HTTPException(409, "最多添加 20 个店铺账号")
    if key:
        if len(key) > 80 or not key.isascii():
            raise HTTPException(400, "店铺账号标识无效")
        if key == "default":
            raise HTTPException(409, "默认店铺账号已存在")
    else:
        # The key is an internal path/tenant identifier.  Do not make the
        # owner invent one (Chinese names cannot be used as-is); a random
        # suffix also avoids revealing account names in URLs or directories.
        key = "shop-" + uuid.uuid4().hex[:16]
    row = None
    try:
        normalize_account_key(key)
        row = db.create_shop_account(user["id"], key, name)
        # Create the private directory now so a later request cannot fall back
        # to the legacy tenant path by accident.
        from bot_manager import ensure_dir as ensure_bot_dir

        ensure_bot_dir(user["id"], key, initialize=True)
        runtime = db.persist_worker_runtime(
            user["id"],
            row["id"],
            desired_state="running",
            mode="rules",
            state="waiting_login",
            pid=None,
            generation=0,
            started_at=None,
            heartbeat_at=time.time(),
            exit_code=None,
            last_error="",
            expected_generation=0,
        )
        if runtime is None:
            raise RuntimeError("initial worker runtime generation changed")
    except sqlite3.IntegrityError as exc:
        if row is not None:
            removed = db.remove_unconfigured_shop_account(user["id"], row["id"])
            if removed:
                _discard_new_account_storage(user["id"], key)
            raise HTTPException(400, "店铺账号创建失败") from exc
        raise HTTPException(409, "店铺账号标识已存在，请重试") from exc
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        # Account creation is durable only after its private directory and
        # waiting-login runtime intent both exist. Compensate either failure so
        # GET /accounts cannot expose a broken ghost account.
        if row is not None:
            removed = db.remove_unconfigured_shop_account(user["id"], row["id"])
            if removed:
                _discard_new_account_storage(user["id"], key)
        raise HTTPException(400, "店铺账号创建失败") from exc
    return {"ok": True, "account": _shop_account_payload(row, user["id"])}


@app.patch("/api/bot/accounts/{account_key}")
def rename_shop_account(
    account_key: str,
    body: ShopAccountRenameIn,
    user=Depends(Auth.current_user),
):
    """Change only the owner-facing label; platform identity stays intact."""
    _require_permission(user, "shop.configure")
    key = str(account_key or "").strip()
    try:
        normalize_account_key(key)
    except ValueError:
        raise HTTPException(400, "店铺账号标识无效") from None
    name = str(body.name or "").strip()
    if len(name) > 160:
        raise HTTPException(400, "店铺名称不能超过 160 个字")
    row = db.get_shop_account(user["id"], account_key=key)
    if row is None or not row["enabled"]:
        raise HTTPException(404, "店铺账号不存在或已停用")
    updated = db.update_shop_account(user["id"], account_id=row["id"], display_name=name)
    return {"ok": True, "account": _shop_account_payload(updated, user["id"])}


@app.delete("/api/bot/accounts/{account_key}")
def delete_shop_account(account_key: str, user=Depends(Auth.current_user)):
    """Soft-delete an isolated shop account and stop all of its activity."""
    _require_permission(user, "shop.configure")
    key = str(account_key or "").strip()
    try:
        normalize_account_key(key)
    except ValueError:
        raise HTTPException(400, "店铺账号标识无效") from None
    if key == DEFAULT_ACCOUNT_ID:
        raise HTTPException(409, "默认店铺不能删除，请清除连接后重新绑定")
    row = db.get_shop_account(user["id"], account_key=key)
    if row is None or not row["enabled"]:
        raise HTTPException(404, "店铺账号不存在或已停用")
    lease, owner = _acquire_account_lease(
        "worker-control",
        user,
        row,
        lease_seconds=45,
        busy_message="自动客服状态正在变更，请稍后重试",
        unavailable_message="店铺状态暂时不可用，请稍后重试",
    )
    try:
        try:
            confirmed, reason = _stop_account_worker_locked(
                user["id"], row, "account_deleted", lease
            )
            if not confirmed:
                raise RuntimeError(reason)
        except Exception as exc:
            raise HTTPException(503, "店铺仍在停止，请稍后重试") from exc
        try:
            qr_logins.clear_user(user["id"], preserve_cooldown=False, account_key=key)
        except Exception:
            pass
        _ensure_account_lease(lease, "店铺状态租约已失效，请重试")
        updated = db.disable_shop_account(user["id"], row["id"])
        return {"ok": True, "account": _shop_account_payload(updated, user["id"])}
    finally:
        _release_account_lease(lease, owner)


def _attention_display(item: dict) -> dict:
    kind = str(item.get("kind") or "shop_account")
    code = str(item.get("code") or "degraded")
    error_code = str(item.get("error_code") or "")
    effective_code = error_code if kind == "shop_account" and error_code else code
    count = max(int(item.get("count") or 1), 1)
    job_kind = str(item.get("job_kind") or "")
    severity = str(item.get("severity") or "").lower()
    title = str(item.get("title") or "").strip()
    message = str(item.get("message") or "").strip()
    action_view = "shops"
    action_label = "查看店铺"

    if kind == "job":
        job_label = "店铺同步任务" if job_kind == "shop_sync" else "后台任务"
        if code == "dead_letter":
            title = title or "任务需要人工处理"
            message = message or f"有 {count} 个{job_label}已停止自动重试，需要检查店铺状态。"
            severity = severity or "error"
        else:
            title = title or "任务等待重试"
            message = message or f"有 {count} 个{job_label}等待系统自动重试。"
            severity = severity or "warning"
    elif kind == "worker":
        runtime_labels = {
            "degraded": "运行异常",
            "stopped": "已停止",
            "starting": "正在启动",
            "stopping": "正在停止",
        }
        runtime_label = runtime_labels.get(code, code or "状态异常")
        title = title or "自动客服需要检查"
        message = message or f"自动客服当前状态：{runtime_label}。"
        action_view = "auto-reply"
        action_label = "查看自动规则"
        severity = severity or "error"
    elif kind == "manual_reply":
        action_view = "chat"
        action_label = "查看智能客服"
        if code == "manual_reply_review":
            title = title or "人工回复需要重新处理"
            message = message or f"有 {count} 条人工回复需要重新处理。"
            severity = severity or "error"
        else:
            title = title or "人工回复等待重试"
            message = message or f"有 {count} 条人工回复等待重试。"
            severity = severity or "warning"
    else:
        custom = {
            "degraded": ("店铺需要检测", "最近一次店铺检测没有完成，已有商品仍会保留。", "warning"),
            "worker_unhealthy": ("自动客服需要检查", "自动客服没有按预期运行。", "error"),
        }.get(effective_code)
        if custom:
            fallback_title, fallback_message, fallback_severity = custom
        else:
            status = sync_status_payload(effective_code)
            fallback_title = status["label"]
            fallback_message = status["message"]
            fallback_severity = "error" if effective_code == "account_restricted" else "warning"
        title = title or fallback_title
        message = message or fallback_message
        severity = severity or fallback_severity
        kind = "shop_account"

    safe_severity = "error" if severity == "error" else "warning"
    return {
        "kind": kind,
        "code": effective_code,
        "error_code": error_code,
        "count": count,
        "title": title[:120],
        "message": message[:240],
        "severity": safe_severity,
        "action_view": action_view,
        "action_label": action_label,
        "desired_state": str(item.get("desired_state") or ""),
        "_source_key": job_kind,
    }


def _attention_payload(user, account) -> dict:
    user_id = int(user["id"])
    account_id = int(account["id"])
    account_key = str(account["account_key"])
    include_jobs = has_permission(user, "records.read") or has_permission(user, "runtime.logs")
    raw_items = []
    status_attention = bot_status(user_id, account_key).get("attention")
    if isinstance(status_attention, list):
        raw_items.extend(status_attention)
    raw_items.extend(
        db.attention_items(user_id, include_jobs=include_jobs, account_id=account_id)
    )
    raw_items.extend(records.manual_reply_attention(user_id, account_key))

    items = []
    active_fingerprints = {}
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _attention_display(raw)
        identity = {
            "kind": item["kind"],
            "code": item["code"],
            "error_code": item["error_code"],
            "source_key": item["_source_key"],
        }
        identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        attention_id = "att_" + hashlib.sha256(
            f"{user_id}:{account_id}:{identity_json}".encode()
        ).hexdigest()[:24]
        if attention_id in seen:
            continue
        seen.add(attention_id)
        fingerprint_fields = {
            **identity,
            "count": item["count"],
            "desired_state": item["desired_state"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        active_fingerprints[attention_id] = fingerprint
        item.update(
            {
                "id": attention_id,
                "account_id": account_id,
                "resolved": False,
                "resolved_at": None,
                "_fingerprint": fingerprint,
            }
        )
        item.pop("desired_state", None)
        items.append(item)

    resolved = db.reconcile_attention_acknowledgements(
        user_id, account_id, active_fingerprints
    )
    for item in items:
        resolved_at = resolved.get(item["id"])
        item["resolved"] = resolved_at is not None
        item["resolved_at"] = resolved_at
    items.sort(
        key=lambda item: (
            bool(item["resolved"]),
            0 if item["severity"] == "error" else 1,
            item["title"],
        )
    )
    pending_total = sum(1 for item in items if not item["resolved"])
    public_items = []
    for item in items:
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        public_items.append(public)
    return {
        "items": public_items,
        "total": len(public_items),
        "pending_total": pending_total,
        "resolved_total": len(public_items) - pending_total,
        "_items": items,
    }


@app.get("/api/bot/attention")
def get_attention(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    payload = _attention_payload(user, account)
    payload.pop("_items", None)
    return payload


@app.put("/api/bot/attention/{attention_id}")
def update_attention_resolution(
    attention_id: str,
    body: AttentionResolutionIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    key = str(attention_id or "").strip()
    if not key.startswith("att_") or len(key) != 28 or any(
        character not in "0123456789abcdef" for character in key[4:]
    ):
        raise HTTPException(404, "预警事项不存在")
    payload = _attention_payload(user, account)
    selected = next((item for item in payload["_items"] if item["id"] == key), None)
    if selected is None:
        raise HTTPException(404, "预警事项不存在或状态已恢复")
    db.set_attention_resolved(
        user["id"],
        account["id"],
        key,
        selected["_fingerprint"],
        body.resolved,
    )
    updated = _attention_payload(user, account)
    updated.pop("_items", None)
    return {"ok": True, **updated}


@app.get("/api/bot/jobs/{job_id}")
def get_job_status(
    job_id: int,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    """Read one account-scoped job without exposing its payload."""
    if job_id <= 0:
        raise HTTPException(400, "任务标识无效")
    row = db.get_job(job_id)
    if row is None or int(row["user_id"]) != int(user["id"]) or not _job_matches_account(row, account):
        raise HTTPException(404, "任务不存在")
    if str(row["kind"]) != "shop_sync":
        raise HTTPException(404, "任务不存在")
    response = _async_job_response(row, account)
    if isinstance(response, JSONResponse):
        # The polling endpoint returns the summary nested under ``job`` while
        # preserving the same 202 semantics as the enqueue endpoint.
        return response
    return {"ok": True, "job": _job_summary(row, account), "result": response}


@app.post("/api/bot/products/batch/preview")
def preview_product_batch(
    body: ProductBatchIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    """Validate a selected material change without writing tenant state."""
    _require_permission(user, "fulfillment.basic")
    _snapshot, _products, _updates, preview = _material_batch_candidate(user, account, body)
    return {"ok": True, "preview": preview}


@app.post("/api/bot/products/batch/commit")
def commit_product_batch(
    body: ProductBatchCommitIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    """Revalidate and atomically apply one account-local material change."""
    _require_permission(user, "fulfillment.basic")
    if not body.preview_token or len(body.preview_token) > 128:
        raise HTTPException(400, "预览凭证无效")
    lease_key, lease_owner = _acquire_account_lease(
        "products-config", user, account, lease_seconds=45,
        busy_message="另一项商品履约配置正在保存，请稍后重试",
        unavailable_message="商品资料状态暂时不可用，请稍后重试",
    )
    try:
        snapshot, products, updates, preview = _material_batch_candidate(user, account, body)
        if body.preview_token != preview["preview_token"] and preview["change_count"]:
            raise HTTPException(409, "商品或资料配置已经变化，请重新检查")
        merged = merge_material_product_updates(products, updates, snapshot)
        try:
            if merged != products:
                write_secret(
                    user["id"],
                    "products_config.json",
                    json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                    str(account["account_key"]),
                )
        except OSError as error:
            raise HTTPException(503, "商品资料保存失败，请稍后重试") from error
        automation = _automation_payload(user, account, persist_legacy=False)
        automation["deliveries"] = [
            {
                "item_id": item["item_id"],
                "enabled": item["enabled"],
                "delivery": item["delivery"],
            }
            for item in automation.get("deliveries", [])
            if isinstance(item, dict)
        ]
        return {"ok": True, "preview": preview, "automation": automation}
    finally:
        _release_account_lease(lease_key, lease_owner)


@app.get("/api/bot/products")
def get_products(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
    limit: int = 500,
):
    _require_permission(user, "products.manage")
    return {
        "products": records.products(
            user["id"], max(1, min(int(limit), 500)), str(account["account_key"])
        )
    }


@app.get("/api/bot/ai/status")
def get_ai_status(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    scope = _ai_scope(user, account)
    connection = ai_service.get_connection(*scope)
    try:
        config = ai_service.get_config(*scope)
    except AIServiceError as error:
        _raise_ai_error(error)
    published = config.get("published") if isinstance(config, dict) else None
    enabled = bool(
        isinstance(published, dict)
        and isinstance(published.get("config"), dict)
        and published["config"].get("enabled") is True
        and published.get("content_valid") is True
    )
    runtime = bot_status(user["id"], str(account["account_key"]))
    available = platform_ai_configured(*scope)
    error_code = str(connection.get("last_error_code") or "")
    if enabled and not available:
        error_code = error_code or "ai_connection_unavailable"
    return {
        "enabled": enabled,
        "running": bool(
            runtime.get("running")
            and runtime.get("automation_mode") == "rules_ai"
            and enabled
            and available
        ),
        "effective_mode": runtime.get("automation_mode") or "rules",
        "connection_available": available,
        "connection_status": connection.get("connection_status", "unconfigured"),
        "connection_revision": connection.get("revision", 0),
        "config_revision": config.get("revision", 0),
        "active_config_revision": int(published.get("revision") or 0) if published else 0,
        "current_error_code": error_code,
    }


@app.get("/api/bot/ai/connection")
def get_ai_connection(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    return {**ai_service.get_connection(*_ai_scope(user, account)), "providers": provider_catalog()}


@app.post("/api/bot/ai/connection/test")
def test_ai_connection(
    body: AIConnectionTestIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return ai_service.test_connection(
            *_ai_scope(user, account),
            provider=body.provider,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
            expected_revision=body.expected_revision,
        )
    except AIServiceError as error:
        _raise_ai_error(error)


@app.put("/api/bot/ai/connection")
def save_ai_connection(
    body: AIConnectionSaveIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        saved = ai_service.save_connection(
            *_ai_scope(user, account),
            provider=body.provider,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
            verification_token=body.verification_token,
            expected_revision=body.expected_revision,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "connection": saved}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.delete("/api/bot/ai/connection/key")
def delete_ai_connection_key(
    body: AIConnectionDeleteIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    if body.confirm is not True:
        raise HTTPException(400, detail={"code": "confirmation_required", "message": "请确认删除当前店铺 AI 密钥"})
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        saved = ai_service.delete_key(
            *_ai_scope(user, account), expected_revision=body.expected_revision
        )
        _ensure_account_lease(lease)
        return {"ok": True, "connection": saved}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.get("/api/bot/ai/config")
def get_ai_config(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return {
            "config": _public_ai_config(ai_service.get_config(*_ai_scope(user, account))),
            "presets": {"catgirl": catgirl_preset()},
        }
    except AIServiceError as error:
        _raise_ai_error(error)


@app.put("/api/bot/ai/config")
def save_ai_config(
    body: AIConfigIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        direct_config = body.dict(
            exclude={"config", "expected_revision", "action"},
            exclude_none=True,
        )
        if body.config is not None and direct_config:
            raise HTTPException(400, detail={"code": "invalid_payload", "message": "店铺客服内容格式无效"})
        config = body.config
        action = body.action
        if config is None:
            config = direct_config
            config.setdefault("enabled", True)
            action = "save"
        saved = ai_service.save_config(
            *_ai_scope(user, account),
            config=config,
            expected_revision=body.expected_revision,
            action=action,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "config": _public_ai_config(saved)}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.get("/api/bot/ai/templates")
def get_ai_templates(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return {"templates": ai_service.get_templates(*_ai_scope(user, account))}
    except AIServiceError as error:
        _raise_ai_error(error)


@app.post("/api/bot/ai/templates")
def save_ai_template(
    body: AITemplateIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        saved = ai_service.save_template(
            *_ai_scope(user, account),
            name=body.name,
            config=body.config,
            template_id=body.template_id,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "template": saved}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.delete("/api/bot/ai/templates/{template_id}")
def delete_ai_template(
    template_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        ai_service.delete_template(*_ai_scope(user, account), template_id)
        _ensure_account_lease(lease)
        return {"ok": True}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.get("/api/bot/ai/products")
def get_ai_products(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return {"products": ai_service.list_products(*_ai_scope(user, account))}
    except AIServiceError as error:
        _raise_ai_error(error)


@app.get("/api/bot/ai/products/{item_id}/knowledge")
def get_ai_product_knowledge(
    item_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return {"knowledge": _public_ai_knowledge(ai_service.get_knowledge(*_ai_scope(user, account), item_id))}
    except AIServiceError as error:
        _raise_ai_error(error)


@app.put("/api/bot/ai/products/{item_id}/knowledge")
def save_ai_product_knowledge(
    item_id: str,
    body: AIKnowledgeIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        if body.knowledge is not None and body.content is not None:
            raise HTTPException(400, detail={"code": "invalid_payload", "message": "商品客服内容格式无效"})
        knowledge = body.knowledge if body.knowledge is not None else {"content": body.content or ""}
        saved = ai_service.save_knowledge(
            *_ai_scope(user, account), item_id,
            knowledge=knowledge,
            expected_revision=body.expected_revision,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "knowledge": _public_ai_knowledge(saved)}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/bot/ai/products/{item_id}/publish")
def publish_ai_product_knowledge(
    item_id: str,
    body: AIKnowledgeActionIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    if body.confirm is not True:
        raise HTTPException(400, detail={"code": "confirmation_required", "message": "请确认发布当前商品知识"})
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        saved = ai_service.publish_knowledge(
            *_ai_scope(user, account), item_id,
            expected_revision=body.expected_revision,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "knowledge": _public_ai_knowledge(saved)}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/bot/ai/products/{item_id}/disable")
def disable_ai_product_knowledge(
    item_id: str,
    body: AIKnowledgeActionIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    if body.confirm is not True:
        raise HTTPException(400, detail={"code": "confirmation_required", "message": "请确认停用当前商品知识"})
    lease, owner = _acquire_account_lease(
        "ai-config", user, account, lease_seconds=45,
        busy_message="AI 配置正在更新，请稍后重试",
        unavailable_message="AI 配置暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        saved = ai_service.disable_knowledge(
            *_ai_scope(user, account), item_id,
            expected_revision=body.expected_revision,
        )
        _ensure_account_lease(lease)
        return {"ok": True, "knowledge": _public_ai_knowledge(saved)}
    except AIServiceError as error:
        _raise_ai_error(error)
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/bot/ai/products/{item_id}/extract")
def extract_ai_product_knowledge(
    item_id: str,
    body: AIExtractIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        if body.source_text is not None and body.content is not None:
            raise HTTPException(400, detail={"code": "invalid_payload", "message": "商品资料格式无效"})
        source_text = body.content if body.content is not None else body.source_text
        result = ai_service.extract_knowledge(
            *_ai_scope(user, account), item_id, source_text or ""
        )
        return {
            "content": str(result.get("content") or "") if isinstance(result, dict) else "",
            "saved": False,
        }
    except AIServiceError as error:
        _raise_ai_error(error)


@app.post("/api/bot/ai/preview")
def preview_ai_reply(
    body: AIPreviewIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "automation.ai")
    try:
        return ai_service.preview(
            *_ai_scope(user, account),
            buyer_message=body.buyer_message,
            item_id=body.item_id,
            store_config_override=body.store_config,
            knowledge_override=body.knowledge,
            history=body.history,
            recent_assistant_replies=body.recent_assistant_replies,
        )
    except AIServiceError as error:
        _raise_ai_error(error)


@app.post("/api/bot/start")
def start_bot(
    body: BotStartIn | None = None,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    requested_mode = str(body.mode or "").strip().lower() if body else ""
    if requested_mode not in {"", "rules", "rules_ai"}:
        raise HTTPException(400, "自动处理模式无效")
    if requested_mode == "rules":
        _require_permission(user, "automation.rules")
        mode = "rules"
    elif requested_mode == "rules_ai":
        _require_permission(user, "automation.ai")
        _require_ai_reply_ready(user, account)
        mode = "rules_ai"
    else:
        if not has_permission(user, "automation.rules"):
            _require_permission(user, "automation.ai")
        # Compatibility default remains conservative: implicit starts use rules.
        # AI requires an explicit user action after connection and content validation.
        mode = "rules"
    lease, owner = _acquire_account_lease(
        "worker-control", user, account, lease_seconds=45,
        busy_message="自动客服状态正在变更，请稍后重试",
        unavailable_message="自动客服状态暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        account = db.get_shop_account(user["id"], account_id=account["id"])
        if account is None or not account["enabled"]:
            raise HTTPException(404, "店铺账号不存在或已停用")
        reason = _start_account_worker_locked(
            user,
            account,
            mode,
            lease,
            validate_configuration=True,
            automatic=False,
        )
        return {"ok": True, "reason": reason}
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/bot/stop")
def stop_bot(user=Depends(Auth.current_user), account=Depends(current_shop_account)):
    lease, owner = _acquire_account_lease(
        "worker-control", user, account, lease_seconds=45,
        busy_message="自动客服状态正在变更，请稍后重试",
        unavailable_message="自动客服状态暂时不可用，请稍后重试",
    )
    try:
        _ensure_account_lease(lease)
        account = db.get_shop_account(user["id"], account_id=account["id"])
        if account is None:
            raise HTTPException(404, "店铺账号不存在")
        try:
            confirmed, reason = _stop_account_worker_locked(user["id"], account, lease=lease)
        except (OSError, RuntimeError, sqlite3.Error, HTTPException) as exc:
            raise HTTPException(503, "机器人状态保存失败，请稍后重试") from exc
        if not confirmed:
            raise HTTPException(503, detail={"code": reason, "message": "机器人进程尚未确认停止，请稍后重试"})
        return {"ok": reason != "not_running", "reason": reason}
    finally:
        _release_account_lease(lease, owner)


@app.get("/api/bot/logs")
def get_bot_logs(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
    lines: int = 200,
):
    _require_permission(user, "runtime.logs")
    safe_lines = max(1, min(int(lines), 2000))
    return {
        "logs": _browser_logs_payload(
            user["id"], safe_lines, str(account["account_key"])
        )
    }


@app.post("/api/bot/login/start")
def start_xianyu_login(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    try:
        # Starting from a reloaded tab replaces only this user's abandoned QR.
        if account_key == "default":
            qr_logins.clear_user(user["id"], preserve_cooldown=True)
            return qr_logins.start(user["id"])
        qr_logins.clear_user(user["id"], preserve_cooldown=True, account_key=account_key)
        return qr_logins.start(user["id"], account_key=account_key)
    except XianyuLoginError as error:
        raise _xianyu_login_http_error(error) from None


@app.get("/api/bot/login/{login_id}/qr.svg")
def xianyu_login_qr(
    login_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    try:
        svg = (
            qr_logins.qr_svg(user["id"], login_id)
            if account_key == "default"
            else qr_logins.qr_svg(user["id"], login_id, account_key)
        )
    except XianyuLoginError as error:
        raise _xianyu_login_http_error(error) from None
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="xianyu-login.svg"',
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@app.get("/api/bot/login/{login_id}/status")
def xianyu_login_status(
    login_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    try:
        result = (
            qr_logins.poll(user["id"], login_id)
            if account_key == "default"
            else qr_logins.poll(user["id"], login_id, account_key)
        )
        if result.get("status") == "error":
            raise XianyuLoginError(str(result.get("code") or "platform_error"))
        return result
    except XianyuLoginError as error:
        raise _xianyu_login_http_error(error) from None


@app.post("/api/bot/login/complete")
def complete_xianyu_login(
    body: XianyuLoginCompleteIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    worker_paused = False

    def pause_worker_after_verification():
        nonlocal worker_paused
        _pause_account_worker_for_login_replace(user["id"], account)
        worker_paused = True

    try:
        cookie_header = (
            qr_logins.begin_consume(user["id"], body.login_id)
            if account_key == "default"
            else qr_logins.begin_consume(user["id"], body.login_id, account_key)
        )
    except XianyuLoginError as error:
        raise _xianyu_login_http_error(error) from None

    try:
        snapshot = _run_shop_sync(
            user["id"],
            cookie_header,
            replace_cookie=True,
            account=account,
            before_replace_persist=pause_worker_after_verification,
        )
    except Exception:
        if worker_paused:
            _autostart_account_worker(user["id"], account)
        try:
            if account_key == "default":
                qr_logins.finish_consume(user["id"], body.login_id, False)
            else:
                qr_logins.finish_consume(user["id"], body.login_id, False, account_key)
        except XianyuLoginError:
            pass
        raise

    try:
        if account_key == "default":
            qr_logins.finish_consume(user["id"], body.login_id, True)
        else:
            qr_logins.finish_consume(user["id"], body.login_id, True, account_key)
    except XianyuLoginError:
        # The verified Cookie and snapshot are already durable. Ensure any
        # remaining in-memory login material is removed without failing the
        # user's completed connection.
        if account_key == "default":
            qr_logins.clear_user(user["id"], preserve_cooldown=True)
        else:
            qr_logins.clear_user(user["id"], preserve_cooldown=True, account_key=account_key)
    payload = _shop_sync_payload(snapshot)
    payload.update({
        "login_id": body.login_id,
        "status": "connected",
        "account": _shop_account_payload(
            db.get_shop_account(user["id"], account_id=account["id"]), user["id"]
        ),
    })
    return payload


@app.post("/api/bot/login/{login_id}/cancel")
def cancel_xianyu_login(
    login_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    try:
        if account_key == "default":
            qr_logins.cancel(user["id"], login_id)
        else:
            qr_logins.cancel(user["id"], login_id, account_key)
    except XianyuLoginError as error:
        if error.code not in {"login_not_found", "login_expired"}:
            raise _xianyu_login_http_error(error) from None
    return {"ok": True}


@app.put("/api/bot/cookies")
def save_cookies(
    body: CookiesIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    cookies = body.cookies.strip()
    if not cookies or len(cookies) > 32768:
        raise _cookie_http_error("cookie_invalid", "Cookie 内容无效或过长，请重新复制完整 Cookie", 400)
    if bot_status(user["id"], account_key).get("running") and not _same_saved_cookie(
        user["id"], account_key, cookies
    ):
        raise _cookie_http_error("bot_running", "请先暂停自动客服，再更新店铺 Cookie", 409)
    return _shop_sync_payload(_run_shop_sync(user["id"], cookies, replace_cookie=True, account=account))


@app.post("/api/bot/shop/sync")
def sync_saved_shop(
    request: Request,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "shop.configure")
    account_key = str(account["account_key"])
    if "respond-async" in request.headers.get("Prefer", "").lower():
        return _async_job_response(_enqueue_shop_refresh(user["id"], account), account)
    cookies = read_secret(user["id"], "cookies.txt", account_key)
    if not cookies:
        raise _cookie_http_error("unconfigured", "请先粘贴闲鱼 Cookie", 400)
    return _shop_sync_payload(_run_shop_sync(user["id"], cookies, replace_cookie=False, account=account))


@app.put("/api/bot/codes")
def save_codes(
    body: CodesIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    lease_key, lease_owner = _acquire_account_lease(
        "worker-control",
        user,
        account,
        lease_seconds=45,
        busy_message="自动客服状态正在变更，请稍后重试",
        unavailable_message="卡密状态暂时不可用，请稍后重试",
    )
    try:
        account_key = str(account["account_key"])
        if bot_status(user["id"], account_key).get("running"):
            raise HTTPException(409, "请先暂停自动客服，再修改卡密库存")
        return _save_codes_locked(body, user, account)
    finally:
        _release_account_lease(lease_key, lease_owner)


def _save_codes_locked(body: CodesIn, user, account):
    if not isinstance(body.codes, list) or not body.codes or len(body.codes) > 20000:
        raise HTTPException(400, "码列表格式错误")
    cleaned = []
    seen = set()
    for item in body.codes:
        code = str(item.get("code", "")).strip() if isinstance(item, dict) else str(item).strip()
        if not code or len(code) > 512 or code in seen:
            continue
        seen.add(code)
        cleaned.append(
            {
                "code": code,
                "value": 5,
                "used": bool(item.get("used", False)) if isinstance(item, dict) else False,
            }
        )
    if not cleaned:
        raise HTTPException(400, "没有有效码")
    write_secret(
        user["id"],
        "redeem_codes.json",
        json.dumps(cleaned, ensure_ascii=False),
        str(account["account_key"]),
    )
    return {"ok": True, "codes": len(cleaned)}


@app.put("/api/bot/products")
def save_products(
    body: ProductsIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    lease_key, lease_owner = _acquire_account_lease(
        "products-config",
        user,
        account,
        lease_seconds=45,
        busy_message="商品履约配置正在更新，请稍后重试",
        unavailable_message="商品履约配置暂时不可用，请稍后重试",
    )
    try:
        return _save_products_locked(body, user, account)
    finally:
        _release_account_lease(lease_key, lease_owner)


def _save_products_locked(body: ProductsIn, user, account):
    account_key = str(account["account_key"])
    products = body.products
    types = products.get("types") if isinstance(products, dict) else None
    if not isinstance(types, list):
        raise HTTPException(400, "types 必须是列表")
    if len(types) > 500:
        raise HTTPException(400, "商品类型过多")
    normalized_types = []
    seen_item_ids = set()
    snapshot = load_verified_snapshot(user["id"], account_key)
    snapshot_ids = {
        str(item.get("id"))
        for item in (snapshot or {}).get("products", [])
        if isinstance(item, dict) and str(item.get("id") or "").isdigit()
    }
    for raw_item in types:
        if not isinstance(raw_item, dict) or raw_item.get("delivery") not in {"redeem", "pan"}:
            raise HTTPException(400, "type 配置无效(delivery 只能是 redeem/pan)")
        item = dict(raw_item)
        item_ids = item.get("item_ids")
        if not isinstance(item_ids, list):
            single_item_id = item.get("item_id")
            item_ids = [single_item_id] if single_item_id is not None else []
        clean_ids = []
        for item_id in item_ids:
            if isinstance(item_id, bool):
                item_key = ""
            else:
                item_key = str(item_id).strip()
            if not item_key.isdigit() or len(item_key) > 64:
                raise HTTPException(400, "商品配置必须使用有效的数字商品 ID")
            if item_key not in snapshot_ids:
                raise HTTPException(400, "商品配置只能绑定当前店铺已识别的商品")
            if item_key in seen_item_ids:
                raise HTTPException(400, "同一个商品不能重复配置")
            seen_item_ids.add(item_key)
            clean_ids.append(item_key)
        if not clean_ids:
            raise HTTPException(400, "每个商品类型至少需要一个商品 ID")
        item["item_ids"] = clean_ids
        item.pop("item_id", None)
        if item["delivery"] == "pan":
            tags = item.get("resource_match")
            if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) and tag.strip() for tag in tags):
                raise HTTPException(400, "网盘商品必须配置资源匹配标签")
            item["resource_match"] = [tag.strip()[:120] for tag in tags[:32]]
        normalized_types.append(item)
    normalized_products = dict(products)
    normalized_products["types"] = normalized_types
    write_secret(
        user["id"],
        "products_config.json",
        json.dumps(normalized_products, ensure_ascii=False),
        account_key,
    )
    return {"ok": True}


def _delivery_template_targets(products, types):
    """Return only the redeem/pan delivery templates (material is separate)."""
    if not isinstance(products, dict) or not isinstance(types, list):
        return []
    return [item for item in types if isinstance(item, dict) and item.get("delivery") in {"redeem", "pan"}]


def _normalise_template_item_ids(raw, snapshot, preserved_item_ids=None):
    """Validate item IDs, preserving only existing bindings under a partial snapshot."""
    item_ids = raw.get("item_ids")
    if item_ids is None:
        legacy = raw.get("item_id")
        item_ids = [legacy] if legacy is not None else []
    if not isinstance(item_ids, list):
        raise HTTPException(400, "item_ids 必须是列表")
    if len(item_ids) > 500:
        raise HTTPException(400, "绑定商品数量过多")
    snapshot_ids = {
        str(item.get("id")).strip()
        for item in (snapshot or {}).get("products", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip().isdigit()
    }
    preserved_ids = {
        str(value).strip()
        for value in (preserved_item_ids or [])
        if not isinstance(value, bool) and str(value).strip().isdigit()
    }
    snapshot_truncated = bool((snapshot or {}).get("truncated"))
    clean = []
    seen: set[str] = set()
    for value in item_ids:
        if isinstance(value, bool):
            item_key = ""
        else:
            item_key = str(value).strip()
        if not item_key.isdigit() or len(item_key) > 64:
            raise HTTPException(400, "商品配置必须使用有效的数字商品 ID")
        if item_key not in snapshot_ids and not (
            snapshot_truncated and item_key in preserved_ids
        ):
            raise HTTPException(400, "商品配置只能绑定当前店铺已识别的商品")
        if item_key in seen:
            raise HTTPException(400, "同一个商品不能重复配置")
        seen.add(item_key)
        clean.append(item_key)
    return clean


def _normalise_template_input(raw, snapshot, preserved_item_ids=None):
    """Validate one redeem/pan delivery template and return a clean dict."""
    if not isinstance(raw, dict):
        raise HTTPException(400, "template 格式无效")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "模板名称必填")
    if len(name) > 120:
        raise HTTPException(400, "模板名称最多 120 字")
    delivery = str(raw.get("delivery") or "").strip()
    if delivery not in {"redeem", "pan"}:
        raise HTTPException(400, "delivery 只能是 redeem/pan")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise HTTPException(400, "description 格式无效")
    if description is not None and len(description.strip()) > 500:
        raise HTTPException(400, "模板说明最多 500 字")
    price = raw.get("price")
    if price is not None and not isinstance(price, str):
        raise HTTPException(400, "price 必须是字符串")
    if price is not None and len(price.strip()) > 120:
        raise HTTPException(400, "price 过长")
    enabled = True if raw.get("enabled") is None else raw.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled 必须是布尔值")
    if delivery == "pan":
        tags = raw.get("resource_match")
        if not isinstance(tags, list) or not tags or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise HTTPException(400, "网盘模板必须配置资源匹配标签")
        resource_match = [tag.strip()[:120] for tag in tags[:32]]
    else:
        resource_match = None
    item_ids = _normalise_template_item_ids(raw, snapshot, preserved_item_ids)
    template = {
        "name": name,
        "description": description.strip() if description is not None else None,
        "price": price.strip() if price is not None else None,
        "delivery": delivery,
        "item_ids": item_ids,
        "enabled": enabled,
    }
    if delivery == "pan":
        template["resource_match"] = resource_match
    return {key: value for key, value in template.items() if value is not None}


def _template_public(item):
    """Stable UI-safe delivery template; never expose payload text."""
    out = {}
    if item.get("id") is not None:
        out["id"] = str(item["id"])
    if item.get("name") is not None:
        out["name"] = str(item["name"])
    if item.get("description") is not None:
        out["description"] = str(item["description"])
    if item.get("price") is not None:
        out["price"] = str(item["price"])
    if item.get("delivery") is not None:
        out["delivery"] = str(item["delivery"])
    raw_ids = item.get("item_ids")
    if not isinstance(raw_ids, list):
        raw_ids = [item["item_id"]] if item.get("item_id") is not None else []
    out["item_ids"] = [str(value).strip() for value in raw_ids if not isinstance(value, bool)]
    if item.get("resource_match") is not None:
        out["resource_match"] = [
            str(tag) for tag in item["resource_match"] if isinstance(tag, str)
        ]
    if "enabled" in item:
        out["enabled"] = item["enabled"] is True
    out["item_count"] = len(out["item_ids"])
    if item.get("payload") or item.get("material"):
        out["payload_set"] = True
    return out


@app.get("/api/bot/templates")
def get_templates(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    account_key = str(account["account_key"])
    document = _read_products_document(user["id"], account_key)
    templates = [
        _template_public(item)
        for item in _delivery_template_targets(document, document.get("types", []))
    ]
    return {"templates": templates}


@app.put("/api/bot/templates")
def save_template(
    body: TemplateIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    lease_key, lease_owner = _acquire_account_lease(
        "products-config",
        user,
        account,
        lease_seconds=45,
        busy_message="商品履约配置正在更新，请稍后重试",
        unavailable_message="商品履约配置暂时不可用，请稍后重试",
    )
    try:
        return _save_template_locked(body, user, account)
    finally:
        _release_account_lease(lease_key, lease_owner)


def _save_template_locked(body: TemplateIn, user, account):
    account_key = str(account["account_key"])
    snapshot = load_verified_snapshot(user["id"], account_key)
    document = _read_products_document(user["id"], account_key)
    types = document.get("types", [])
    if not isinstance(types, list):
        types = []
    template_id = body.template.id
    template_id = str(template_id).strip() if template_id is not None else ""
    index = None
    for pos, item in enumerate(types):
        if isinstance(item, dict) and item.get("delivery") in {"redeem", "pan"} and str(item.get("id") or "") == template_id:
            index = pos
            break
    existing_item_ids = types[index].get("item_ids", []) if index is not None else []
    template = _normalise_template_input(
        body.template.model_dump(), snapshot, existing_item_ids
    )
    if index is not None:
        updated = dict(template)
        updated["id"] = template_id
        types[index] = updated
        persisted = updated
    else:
        new_id = f"template-{uuid.uuid4().hex[:8]}"
        created = dict(template)
        created["id"] = new_id
        types.append(created)
        persisted = created
    normalized_document = dict(document)
    normalized_document["types"] = types
    try:
        write_secret(
            user["id"],
            "products_config.json",
            json.dumps(normalized_document, ensure_ascii=False),
            account_key,
        )
    except OSError as error:
        raise HTTPException(503, "发货模板保存失败，请稍后重试") from error
    return {"ok": True, "template": _template_public(persisted)}


@app.delete("/api/bot/templates/{template_id}")
def delete_template(
    template_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    lease_key, lease_owner = _acquire_account_lease(
        "products-config",
        user,
        account,
        lease_seconds=45,
        busy_message="商品履约配置正在更新，请稍后重试",
        unavailable_message="商品履约配置暂时不可用，请稍后重试",
    )
    try:
        return _delete_template_locked(template_id, user, account)
    finally:
        _release_account_lease(lease_key, lease_owner)


def _delete_template_locked(template_id: str, user, account):
    account_key = str(account["account_key"])
    template_id = str(template_id).strip()
    if not template_id or len(template_id) > 120:
        raise HTTPException(400, "模板标识无效")
    document = _read_products_document(user["id"], account_key)
    types = document.get("types", [])
    if not isinstance(types, list):
        types = []
    remaining = [
        item
        for item in types
        if not (
            isinstance(item, dict)
            and item.get("delivery") in {"redeem", "pan"}
            and str(item.get("id") or "") == template_id
        )
    ]
    if len(remaining) == len(types):
        raise HTTPException(404, "发货模板不存在")
    normalized_document = dict(document)
    normalized_document["types"] = remaining
    try:
        write_secret(
            user["id"],
            "products_config.json",
            json.dumps(normalized_document, ensure_ascii=False),
            account_key,
        )
    except OSError as error:
        raise HTTPException(503, "发货模板删除失败，请稍后重试") from error
    return {"ok": True}


def _read_codes(user_id: int, account_key: str) -> list:
    raw = read_secret(user_id, "redeem_codes.json", account_key)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return payload


def _read_card_pool_meta(user_id: int, account_key: str) -> dict:
    raw = read_secret(user_id, "card_pool.json", account_key)
    if not raw:
        return {"name": "兑换码池", "note": ""}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {"name": "兑换码池", "note": ""}
    if not isinstance(payload, dict):
        return {"name": "兑换码池", "note": ""}
    name = str(payload.get("name") or "").strip() or "兑换码池"
    note = str(payload.get("note") or "").strip()
    return {"name": name[:120], "note": note[:500]}


def _cards_payload(
    codes: list,
    name: str = "兑换码池",
    note: str = "",
    runtime_counts: dict | None = None,
) -> dict:
    valid = [item for item in codes if isinstance(item, dict) and item.get("code")]
    if runtime_counts is None:
        total = len(valid)
        available = sum(1 for item in valid if not item.get("used"))
        reserved = 0
        used = total - available
    else:
        available = int(runtime_counts.get("available", 0))
        reserved = int(runtime_counts.get("reserved", 0))
        used = int(runtime_counts.get("delivered", 0)) + int(runtime_counts.get("legacy_used", 0))
        total = available + reserved + used
    pool = {
        "id": "redeem",
        "name": name,
        "note": note,
        "total": total,
        "available": available,
        "used": used,
        "enabled": True,
    }
    stats = {
        "pools": 1,
        "total": total,
        "available": available,
        "reserved": reserved,
        "used": used,
    }
    return {"pool": pool, "stats": stats}


@app.get("/api/bot/cards")
def get_cards(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    account_key = str(account["account_key"])
    codes = _read_codes(user["id"], account_key)
    meta = _read_card_pool_meta(user["id"], account_key)
    runtime_counts = (
        records.inventory_counts(user["id"], "redeem", account_key)
        if bot_status(user["id"], account_key).get("running")
        else None
    )
    return _cards_payload(
        codes,
        name=meta["name"],
        note=meta["note"],
        runtime_counts=runtime_counts,
    )


@app.put("/api/bot/cards")
def save_cards(
    body: CardsIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "fulfillment.manage")
    lease_key, lease_owner = _acquire_account_lease(
        "worker-control",
        user,
        account,
        lease_seconds=45,
        busy_message="自动客服状态正在变更，请稍后重试",
        unavailable_message="卡密状态暂时不可用，请稍后重试",
    )
    try:
        account_key = str(account["account_key"])
        if bot_status(user["id"], account_key).get("running"):
            raise HTTPException(409, "请先暂停自动客服，再修改卡密库存")
        return _save_cards_locked(body, user, account)
    finally:
        _release_account_lease(lease_key, lease_owner)


def _save_cards_locked(body: CardsIn, user, account):
    account_key = str(account["account_key"])
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(400, "卡池名称必填")
    if len(name) > 120:
        raise HTTPException(400, "卡池名称最多 120 字")
    note = str(body.note or "").strip()
    if len(note) > 500:
        raise HTTPException(400, "卡池备注最多 500 字")
    raw_codes = body.codes
    if not isinstance(raw_codes, list) or len(raw_codes) > 20000:
        raise HTTPException(400, "码列表数量无效（最多 20000）")
    cleaned = {}
    for item in _read_codes(user["id"], account_key):
        if not isinstance(item, dict):
            continue
        existing_code = str(item.get("code") or "").strip()
        if existing_code:
            cleaned[existing_code] = {
                "code": existing_code,
                "value": 5,
                "used": bool(item.get("used", False)),
            }
    for item in raw_codes:
        if isinstance(item, dict):
            code = str(item.get("code") or "").strip()
            used = bool(item.get("used", False))
        else:
            code = str(item).strip()
            used = False
        if not code:
            continue
        if len(code) > 512:
            raise HTTPException(400, "单个兑换码最多 512 字符")
        cleaned[code] = {"code": code, "value": 5, "used": used}
    codes = list(cleaned.values())
    # An empty code list means "rename/metadata only": keep the existing
    # inventory. Creating a new empty pool is allowed and matches the demo.
    if not codes:
        codes = _read_codes(user["id"], account_key)
    try:
        write_secret(
            user["id"],
            "redeem_codes.json",
            json.dumps(codes, ensure_ascii=False),
            account_key,
        )
        write_secret(
            user["id"],
            "card_pool.json",
            json.dumps({"name": name, "note": note}, ensure_ascii=False),
            account_key,
        )
    except OSError as error:
        raise HTTPException(503, "卡密保存失败，请稍后重试") from error
    payload = _cards_payload(codes, name=name, note=note)
    return {"ok": True, **payload}


_DEFAULT_QUICK_REPLIES = [
    {"id": "welcome", "title": "在的", "content": "你好，在的，请问需要了解什么？"},
    {"id": "delivery", "title": "发货说明", "content": "付款后系统会按当前商品配置自动处理，请留意聊天消息。"},
    {"id": "manual", "title": "人工处理", "content": "这个问题我帮您核实一下，请稍候。"},
]


def _normalise_quick_replies(raw: list) -> list:
    if not isinstance(raw, list) or len(raw) > 20:
        raise HTTPException(400, "快捷短语最多保存 20 条")
    replies = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(400, "快捷短语格式无效")
        reply_id = str(item.get("id") or "").strip()
        if not reply_id:
            reply_id = "quick-" + uuid.uuid4().hex[:12]
        if (
            len(reply_id) > 64
            or not reply_id.isascii()
            or any(not (character.isalnum() or character in "-_.") for character in reply_id)
        ):
            raise HTTPException(400, "快捷短语标识无效")
        if reply_id in seen:
            raise HTTPException(400, "快捷短语标识重复")
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title or len(title) > 10 or any(ord(character) < 32 for character in title):
            raise HTTPException(400, "快捷短语标题需为 1 至 10 个可见字符")
        if not content or len(content) > 1000 or any(ord(character) < 32 and character not in "\n\t" for character in content):
            raise HTTPException(400, "快捷短语内容需为 1 至 1000 个有效字符")
        seen.add(reply_id)
        replies.append({"id": reply_id, "title": title, "content": content})
    return replies


def _read_quick_replies(user_id: int, account_key: str) -> list:
    raw = read_secret(user_id, "quick_replies.json", account_key)
    if not raw:
        return [dict(item) for item in _DEFAULT_QUICK_REPLIES]
    try:
        payload = json.loads(raw)
        source = payload.get("quick_replies", []) if isinstance(payload, dict) else payload
        return _normalise_quick_replies(source)
    except (TypeError, ValueError, HTTPException):
        return [dict(item) for item in _DEFAULT_QUICK_REPLIES]


@app.get("/api/bot/quick-replies")
def get_quick_replies(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    return {"quick_replies": _read_quick_replies(user["id"], str(account["account_key"]))}


@app.put("/api/bot/quick-replies")
def save_quick_replies(
    body: QuickRepliesIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    replies = _normalise_quick_replies(body.quick_replies)
    lease_key, lease_owner = _acquire_account_lease(
        "quick-replies",
        user,
        account,
        lease_seconds=30,
        busy_message="快捷短语正在保存，请稍后重试",
        unavailable_message="快捷短语暂时无法保存，请稍后重试",
    )
    try:
        write_secret(
            user["id"],
            "quick_replies.json",
            json.dumps({"version": 1, "quick_replies": replies}, ensure_ascii=False),
            str(account["account_key"]),
        )
    except OSError as error:
        raise HTTPException(503, "快捷短语保存失败，请稍后重试") from error
    finally:
        _release_account_lease(lease_key, lease_owner)
    return {"ok": True, "quick_replies": replies}


@app.get("/api/bot/conversations")
def get_conversations(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
    limit: int = 50,
    search: str = "",
    unread_only: bool = False,
):
    _require_permission(user, "records.read")
    search = str(search or "").strip()
    if len(search) > 120 or any(ord(char) < 32 for char in search):
        raise HTTPException(400, "搜索内容无效")
    rows = records.conversations(
        user["id"],
        max(1, min(int(limit), 200)),
        str(account["account_key"]),
        search=search,
        unread_only=bool(unread_only),
    )
    totals = records.conversation_unread_totals(
        user["id"], str(account["account_key"]), search=search
    )
    return {
        "conversations": rows,
        "unread_total": int(totals["conversations"]),
        "unread_messages_total": int(totals["messages"]),
        "filters": {"search": search, "unread_only": bool(unread_only)},
    }


def _conversation_path_id(chat_id: str) -> str:
    selected = str(chat_id or "").strip()
    if not selected or len(selected) > 256 or any(ord(char) < 32 for char in selected):
        raise HTTPException(
            400,
            detail={"code": "conversation_invalid", "message": "会话标识无效"},
        )
    return selected


@app.post("/api/bot/conversations/{chat_id}/read")
def mark_conversation_read(
    chat_id: str,
    body: ConversationReadIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    selected = _conversation_path_id(chat_id)
    conversation = records.mark_conversation_read(
        user["id"], selected, bool(body.read), str(account["account_key"])
    )
    if conversation is None:
        raise HTTPException(
            404,
            detail={"code": "conversation_not_found", "message": "找不到当前店铺的这个对话"},
        )
    return {"ok": True, "conversation": conversation}


@app.post("/api/bot/conversations/{chat_id}/takeover")
def set_conversation_takeover(
    chat_id: str,
    body: ConversationTakeoverIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    selected = _conversation_path_id(chat_id)
    conversation = records.set_conversation_takeover(
        user["id"], selected, bool(body.enabled), str(account["account_key"])
    )
    if conversation is None:
        raise HTTPException(
            404,
            detail={"code": "conversation_not_found", "message": "找不到当前店铺的这个对话"},
        )
    return {
        "ok": True,
        "enabled": bool(conversation.get("takeover")),
        "conversation": conversation,
    }


@app.post("/api/bot/messages/image")
async def upload_manual_image(
    request: Request,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    chat_id = str(request.query_params.get("chat_id") or "").strip()
    if not chat_id or len(chat_id) > 120:
        raise HTTPException(400, "请选择一个对话后再上传图片")
    if not records.conversation_exists(user["id"], chat_id, str(account["account_key"])):
        raise HTTPException(404, "找不到当前店铺的这个对话")
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > 8 * 1024 * 1024:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(413, "图片不能超过 8 MB") from None
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 8 * 1024 * 1024:
            raise HTTPException(413, "图片不能超过 8 MB")
        chunks.append(chunk)
    payload = b"".join(chunks)
    try:
        media = records.save_manual_image(
            user["id"],
            payload,
            request.headers.get("x-file-name", ""),
            request.headers.get("content-type", ""),
            str(account["account_key"]),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(503, "图片暂时无法保存，请稍后重试") from error
    return {"ok": True, "media": media}


@app.delete("/api/bot/messages/image")
def delete_manual_image(
    body: ManualImageDeleteIn,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    status = records.manual_image_delete_status(
        user["id"], body.path, str(account["account_key"])
    )
    if status == "active":
        raise HTTPException(
            409,
            detail={"code": "image_in_use", "message": "图片已进入发送队列，不能删除"},
        )
    if status == "unavailable":
        raise HTTPException(
            503,
            detail={"code": "image_delete_unavailable", "message": "图片暂时无法删除，请稍后重试"},
        )
    if status == "invalid":
        raise HTTPException(
            404,
            detail={"code": "image_not_found", "message": "图片不存在或已失效"},
        )
    return {"ok": True, "deleted": status == "deleted"}


@app.get("/api/bot/messages")
def get_messages(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
    limit: int = 50,
    chat_id: str = "",
    search: str = "",
):
    _require_permission(user, "records.read")
    selected = chat_id.strip()
    if len(selected) > 120:
        raise HTTPException(400, "会话标识无效")
    keyword = str(search or "").strip()
    if len(keyword) > 120 or any(ord(character) < 32 for character in keyword):
        raise HTTPException(400, "搜索内容无效")
    messages = records.messages(
        user["id"],
        max(1, min(int(limit), 200)),
        selected,
        str(account["account_key"]),
        search=keyword,
    )
    return {
        "messages": messages,
        "match_count": records.message_match_count(
            user["id"], selected, keyword, str(account["account_key"])
        ) if keyword else 0,
        "search": keyword,
    }


@app.post("/api/bot/messages/reply")
def post_manual_reply(
    body: ManualReplyIn,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    content = body.content.strip()
    if len(content) > 4000:
        raise HTTPException(400, "回复内容不能超过 4000 字")
    chat_id = body.chat_id.strip()
    if not chat_id or len(chat_id) > 120:
        raise HTTPException(400, "请选择一个对话后再发送")
    request_id = idempotency_key.strip() or uuid.uuid4().hex
    if (
        len(request_id) < 8
        or len(request_id) > 120
        or not request_id.isascii()
        or any(not (character.isalnum() or character in "-_.:") for character in request_id)
    ):
        raise HTTPException(
            400,
            detail={"code": "invalid_idempotency_key", "message": "发送标识无效，请重新发送"},
        )
    try:
        message = records.enqueue_manual_reply(
            user["id"], content, chat_id, request_id, str(account["account_key"]), body.media
        )
    except records.ManualReplyQueueError as error:
        status_code = 404 if error.code == "conversation_not_found" else 400 if error.code in {"invalid_media", "invalid_payload"} else 503 if error.code == "reply_queue_unavailable" else 409
        raise HTTPException(
            status_code,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return {
        "ok": True,
        "accepted": True,
        "saved": True,
        "delivered": False,
        "platform_acknowledged": bool(message.get("platform_acknowledged")),
        "reply": {
            "reply_id": message.get("reply_id", ""),
            "status": message.get("status", "queued"),
            "attempts": message.get("attempts", 0),
            "platform_acknowledged": bool(message.get("platform_acknowledged")),
            "current_part": message.get("current_part"),
            "parts": message.get("parts", []),
        },
        "message": message,
    }


@app.get("/api/bot/messages/reply/{request_id}")
def get_manual_reply_status(
    request_id: str,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    _require_permission(user, "records.read")
    key = request_id.strip()
    if (
        len(key) < 8
        or len(key) > 120
        or not key.isascii()
        or any(not (character.isalnum() or character in "-_.:") for character in key)
    ):
        raise HTTPException(404, "找不到这条回复")
    reply = records.manual_reply_status(user["id"], key, str(account["account_key"]))
    if reply is None:
        raise HTTPException(404, "找不到这条回复")
    return {"reply": reply}


@app.get("/api/bot/orders")
def get_orders(
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
    limit: int = 50,
):
    _require_permission(user, "records.read")
    return {
        "orders": records.orders(
            user["id"], max(1, min(int(limit), 200)), str(account["account_key"])
        )
    }


@app.get("/api/bot/summary")
def get_summary(user=Depends(Auth.current_user), account=Depends(current_shop_account)):
    _require_permission(user, "analytics.read")
    return records.summary(user["id"], str(account["account_key"]))


@app.get("/api/bot/analytics")
def get_analytics(
    period: int = 1,
    user=Depends(Auth.current_user),
    account=Depends(current_shop_account),
):
    """Return a bounded, account-scoped operations aggregate.

    ``summary`` remains the stable workbench contract.  This endpoint adds
    optional 1/7/30-day buckets without returning message/order payloads,
    inventory, credentials, or business identifiers.
    """
    _require_permission(user, "analytics.read")
    try:
        days = int(period)
    except (TypeError, ValueError):
        raise HTTPException(400, "统计周期无效")
    if days not in {1, 7, 30}:
        raise HTTPException(400, "统计周期仅支持 1、7 或 30 天")
    try:
        return records.analytics_summary(user["id"], str(account["account_key"]), days)
    except ValueError as error:
        raise HTTPException(400, "统计周期无效") from error


def _internal_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not (1 <= len(messages) <= 64):
        raise HTTPException(400, "messages 数量无效")
    total_chars = 0
    clean_messages = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise HTTPException(400, "消息角色无效")
        content = message.get("content")
        if not isinstance(content, str) or len(content) > 16000:
            raise HTTPException(400, "消息内容无效")
        total_chars += len(content)
        clean_messages.append({"role": message["role"], "content": content})
    if total_chars > 64000:
        raise HTTPException(413, "消息内容过大")

    clean: dict = {"messages": clean_messages, "stream": False}
    allowed = {"temperature", "top_p", "max_tokens", "reasoning_effort", "frequency_penalty", "presence_penalty"}
    for key in allowed:
        if key not in payload:
            continue
        value = payload[key]
        if key == "reasoning_effort":
            if isinstance(value, str) and value in {"low", "medium", "high"}:
                clean[key] = value
            continue
        if key == "max_tokens":
            if isinstance(value, int) and 1 <= value <= 2048:
                clean[key] = value
            continue
        if isinstance(value, (int, float)) and -2 <= float(value) <= 2:
            clean[key] = value
    return clean


def _internal_ai_account(request: Request):
    client_host = request.client.host if request.client else ""
    if client_host and client_host not in {"127.0.0.1", "::1"} and os.environ.get("SAAS_TESTING") != "1":
        raise HTTPException(404, "not found")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "unauthorized")
    token_scope = identify_scope(authorization[7:].strip())
    if token_scope is None:
        raise HTTPException(401, "unauthorized")
    user_id, token_account_key = token_scope
    raw_account_key = request.headers.get("X-Shop-Account")
    if raw_account_key is None:
        raise HTTPException(403, "unauthorized scope")
    try:
        requested_account_key = normalize_account_key(raw_account_key.strip())
    except AccountStorageError:
        raise HTTPException(403, "unauthorized scope") from None
    if requested_account_key != token_account_key:
        raise HTTPException(403, "unauthorized scope")
    user = db.get_user_by_id(user_id)
    if user is None or not has_permission(user, "automation.ai"):
        raise HTTPException(403, "当前账号无 AI 服务权限")
    account = db.get_shop_account(user_id, account_key=token_account_key)
    if account is None or not account["enabled"]:
        raise HTTPException(403, "店铺账号不可用")
    return user, account, token_account_key


def _ensure_internal_ai_runtime_ready(
    user,
    account,
    *,
    expected_config_revision: int | None = None,
) -> dict:
    account_key = str(account["account_key"])
    settings = _read_automation_settings(user["id"], account_key)
    if settings.get("enabled") is not True:
        raise HTTPException(409, detail={"code": "automation_disabled", "message": "自动回复已停用"})
    _read_rules_document(user["id"], user, account_key, persist_legacy=False)
    try:
        return ai_service.ensure_reply_ready(
            *_ai_scope(user, account),
            expected_config_revision=expected_config_revision,
        )
    except AIServiceError as error:
        _raise_ai_error(error)


@app.post("/internal/v1/ai/ready")
async def internal_ai_ready(request: Request):
    """Revalidate the exact generated AI configuration immediately before a send."""
    user, account, _account_key = _internal_ai_account(request)
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_REQUEST_BYTES:
        raise HTTPException(413, "请求内容过大")
    try:
        body = AIReadyIn.parse_obj(await request.json())
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "请求体无效") from exc
    if body.expected_config_revision < 0:
        raise HTTPException(400, "请求体无效")
    return {
        "ok": True,
        **_ensure_internal_ai_runtime_ready(
            user,
            account,
            expected_config_revision=body.expected_config_revision,
        ),
    }


@app.post("/internal/v1/ai/reply")
async def internal_ai_reply(request: Request):
    """Generate one bounded, side-effect-free customer-service decision."""
    user, account, token_account_key = _internal_ai_account(request)
    user_id = int(user["id"])
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_REQUEST_BYTES:
        raise HTTPException(413, "请求内容过大")
    try:
        raw_payload = await request.json()
        body = AIReplyIn.parse_obj(raw_payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "请求体无效") from exc
    try:
        return ai_service.reply(
            user_id,
            int(account["id"]),
            token_account_key,
            message=body.message,
            history=body.history,
            item_id=body.item_id,
            item_context=body.item_context,
            recent_assistant_replies=body.recent_assistant_replies,
        )
    except AIServiceError as error:
        _raise_ai_error(error)


@app.post("/internal/v1/chat/completions")
async def internal_chat(request: Request):
    # This endpoint is deliberately not reachable through the public nginx
    # locations.  Keep a loopback check here as a second independent boundary.
    client_host = request.client.host if request.client else ""
    if client_host and client_host not in {"127.0.0.1", "::1"} and os.environ.get("SAAS_TESTING") != "1":
        raise HTTPException(404, "not found")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "unauthorized")
    token_scope = identify_scope(authorization[7:].strip())
    if token_scope is None:
        raise HTTPException(401, "unauthorized")
    user_id, token_account_key = token_scope
    requested_account_key = request.headers.get("X-Shop-Account", DEFAULT_ACCOUNT_ID).strip()
    try:
        requested_account_key = normalize_account_key(requested_account_key)
    except AccountStorageError:
        raise HTTPException(403, "unauthorized scope") from None
    if requested_account_key != token_account_key:
        raise HTTPException(403, "unauthorized scope")
    user = db.get_user_by_id(user_id)
    if user is None or not has_permission(user, "automation.ai"):
        raise HTTPException(403, "当前账号无 AI 服务权限")
    account = db.get_shop_account(user_id, account_key=token_account_key)
    if account is None or not account["enabled"]:
        raise HTTPException(403, "店铺账号不可用")
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_REQUEST_BYTES:
        raise HTTPException(413, "请求内容过大")
    try:
        payload = _internal_payload(await request.json())
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "请求体不是有效 JSON") from exc
    try:
        status_code, body = platform_forward(
            payload,
            user_id=user_id,
            shop_account_id=account["id"],
            account_key=token_account_key,
        )
    except PlatformAIError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": exc.code}},
            status_code=exc.status_code,
        )
    return Response(content=body, status_code=status_code, media_type="application/json")


# ---------------- Administrator APIs ----------------


def _platform_settings_payload() -> dict:
    environment_allowed = _registration_env_allowed()
    database_open = db.get_platform_setting("registration_open", "0") == "1"
    users_exist = db.user_count() > 0
    return {
        "registration": {
            "environment_allowed": environment_allowed,
            "database_open": database_open,
            "users_exist": users_exist,
            "effective": bool(environment_allowed and database_open and users_exist),
        },
        "update_channel": db.get_platform_setting("update_channel", "stable"),
    }


def _platform_user_payload(row) -> dict:
    username = str(row["username"])
    lock = db.username_lock_status(_username_hash(username))
    try:
        session_count = int(row["session_count"] or 0)
    except (KeyError, TypeError, IndexError):
        session_count = 0
    return {
        "id": int(row["id"]),
        "username": username,
        "role": str(row["role"]),
        "role_label": "管理员" if str(row["role"]) == "admin" else "店主",
        "enabled": row["disabled_at"] is None,
        "locked": bool(lock["locked"]),
        "session_count": session_count,
        "created_at": float(row["created_at"]),
        "password_changed_at": (
            float(row["password_changed_at"])
            if row["password_changed_at"] is not None
            else None
        ),
    }


@app.get("/api/admin/settings")
def admin_get_settings(_=Depends(require_platform_admin)):
    return _platform_settings_payload()


@app.put("/api/admin/settings")
def admin_save_settings(
    body: PlatformSettingsIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    if body.registration_open is None and body.update_channel is None:
        raise HTTPException(
            400,
            detail={"code": "settings_empty", "message": "没有可保存的平台设置"},
        )
    if body.update_channel is not None and body.update_channel not in {"stable", "beta"}:
        raise HTTPException(
            400,
            detail={"code": "update_channel_invalid", "message": "更新通道无效"},
        )
    changed = []
    if body.registration_open is not None:
        db.set_platform_setting(
            "registration_open", "1" if body.registration_open else "0", admin["id"]
        )
        changed.append("registration_open")
    if body.update_channel is not None:
        db.set_platform_setting("update_channel", body.update_channel, admin["id"])
        changed.append("update_channel")
    for setting in changed:
        _audit(
            "platform.settings_changed",
            request,
            actor_user_id=admin["id"],
            target_type="setting",
            target_id=setting,
            metadata={"setting": setting, "value": db.get_platform_setting(setting)},
        )
    return _platform_settings_payload()


@app.get("/api/admin/users")
def admin_list_users(
    cursor: int | None = None,
    limit: int = 50,
    _=Depends(require_platform_admin),
):
    rows, next_cursor = db.list_platform_users(cursor=cursor, limit=limit)
    return {
        "users": [_platform_user_payload(row) for row in rows],
        "next_cursor": next_cursor,
    }


@app.patch("/api/admin/users/{target_user_id}")
def admin_update_user(
    target_user_id: int,
    body: PlatformUserUpdateIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    if body.role is None and body.enabled is None:
        raise HTTPException(
            400,
            detail={"code": "user_change_empty", "message": "没有可保存的账号变更"},
        )
    if body.role is not None and body.role not in {"admin", "owner"}:
        raise HTTPException(
            400,
            detail={"code": "role_invalid", "message": "角色无效"},
        )
    if int(admin["id"]) == int(target_user_id):
        raise HTTPException(
            400,
            detail={"code": "self_change_forbidden", "message": "不能修改自己的角色或状态"},
        )
    try:
        row = db.update_platform_user(
            target_user_id,
            role=body.role,
            enabled=body.enabled,
        )
    except LastAdminError as exc:
        raise HTTPException(
            409,
            detail={"code": "last_admin_protected", "message": "必须保留至少一个启用的管理员"},
        ) from exc
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "user_not_found", "message": "账号不存在"},
        )
    _audit(
        "platform.user_changed",
        request,
        actor_user_id=admin["id"],
        target_type="user",
        target_id=target_user_id,
        metadata={"role": str(row["role"]), "enabled": row["disabled_at"] is None},
    )
    return {"user": _platform_user_payload(row)}


@app.post("/api/admin/users/{target_user_id}/unlock")
def admin_unlock_user(
    target_user_id: int,
    request: Request,
    admin=Depends(require_platform_admin),
):
    row = db.get_user_by_id(target_user_id)
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "user_not_found", "message": "账号不存在"},
        )
    cleared = db.clear_login_failures(_username_hash(str(row["username"])))
    _audit(
        "platform.user_unlocked",
        request,
        actor_user_id=admin["id"],
        target_type="user",
        target_id=target_user_id,
        metadata={"sessions_revoked": cleared},
    )
    return {"ok": True}


@app.post("/api/admin/users/{target_user_id}/sessions/revoke")
def admin_revoke_user_sessions(
    target_user_id: int,
    request: Request,
    admin=Depends(require_platform_admin),
):
    row = db.get_user_by_id(target_user_id)
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "user_not_found", "message": "账号不存在"},
        )
    keep_token = _request_token(request) if int(admin["id"]) == int(target_user_id) else ""
    revoked = db.revoke_user_sessions(target_user_id, keep_token=keep_token)
    _audit(
        "platform.sessions_revoked",
        request,
        actor_user_id=admin["id"],
        target_type="user",
        target_id=target_user_id,
        metadata={"sessions_revoked": revoked},
    )
    return {"ok": True, "sessions_revoked": revoked}


@app.post("/api/admin/confirm")
def admin_create_confirmation(
    body: AdminConfirmationIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    if body.action not in {"update.apply", "update.rollback"}:
        raise HTTPException(
            400,
            detail={"code": "confirmation_action_invalid", "message": "确认操作无效"},
        )
    if int(admin["id"]) <= 0 or not verify_password(body.password, admin["password_hash"]):
        raise HTTPException(
            403,
            detail={"code": "reauthentication_failed", "message": "管理员密码校验失败"},
        )
    confirmation = db.create_admin_confirmation(admin["id"], body.action, ttl_seconds=180)
    _audit(
        "platform.confirmation_issued",
        request,
        actor_user_id=admin["id"],
        target_type="operation",
        target_id=body.action,
    )
    return {"confirmation_token": confirmation, "expires_in": 180}


@app.get("/api/admin/audit")
def admin_list_audit(
    cursor: int | None = None,
    limit: int = 50,
    event_type: str = "",
    _=Depends(require_platform_admin),
):
    if event_type and event_type not in AUDIT_EVENT_TYPES:
        raise HTTPException(
            400,
            detail={"code": "audit_event_invalid", "message": "审计事件类型无效"},
        )
    rows, next_cursor = db.list_audit(cursor=cursor, limit=limit, event_type=event_type)
    events = []
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        events.append(
            {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "actor_user_id": (
                    int(row["actor_user_id"]) if row["actor_user_id"] is not None else None
                ),
                "target_type": str(row["target_type"]),
                "target_id": str(row["target_id"]),
                "outcome": str(row["outcome"]),
                "source_hash": str(row["ip_hash"]),
                "metadata": metadata if isinstance(metadata, dict) else {},
                "created_at": float(row["created_at"]),
            }
        )
    return {"events": events, "next_cursor": next_cursor}


_UPDATE_ERROR_MESSAGES = {
    "update_channel_invalid": "更新通道无效",
    "release_version_invalid": "发布版本无效",
    "update_downgrade_rejected": "不能安装当前版本或更旧版本",
    "update_dependency_change_rejected": "发布制品包含未审批的依赖变化",
    "update_signature_invalid": "发布签名校验失败",
    "update_artifact_hash_mismatch": "发布制品完整性校验失败",
    "update_archive_hash_mismatch": "发布文件完整性校验失败",
    "update_intent_pending": "已有更新操作等待执行",
    "update_candidate_invalid": "候选版本已失效，请重新下载",
    "update_release_not_found": "没有找到可用发布版本",
}


def _raise_update_error(error: PlatformUpdateError):
    code = str(error.code)
    if code in {
        "update_downgrade_rejected",
        "update_intent_pending",
        "update_release_exists",
        "update_version_already_current",
    }:
        status = 409
    elif code.startswith("release_") or code.startswith("update_archive_") or code in {
        "update_channel_invalid",
        "update_dependency_change_rejected",
        "update_signature_invalid",
        "update_artifact_hash_mismatch",
        "update_candidate_invalid",
    }:
        status = 400
    elif code.startswith("update_source_") or code.startswith("update_download_"):
        status = 502
    else:
        status = 503
    raise HTTPException(
        status,
        detail={
            "code": code,
            "message": _UPDATE_ERROR_MESSAGES.get(code, "更新操作暂时不可用"),
        },
    ) from error


def _begin_platform_update_lease(seconds: float = 600) -> tuple[_AccountLease, str]:
    owner = f"api-update:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    try:
        result = db.acquire_control_lease(
            "platform-update",
            owner,
            lease_seconds=max(float(seconds), 30.0),
            cooldown_seconds=0,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise HTTPException(
            503,
            detail={"code": "update_lock_unavailable", "message": "更新锁暂时不可用"},
        ) from exc
    if result != "acquired":
        raise HTTPException(
            409,
            detail={"code": "update_busy", "message": "已有更新操作正在进行"},
        )
    return _AccountLease("platform-update", owner, max(float(seconds), 30.0)), owner


def _admin_update_status_payload() -> dict:
    channel = db.get_platform_setting("update_channel", "stable")
    current = version_payload(channel)
    return {
        "current": current,
        "latest_update": _platform_update_payload(db.latest_platform_update(channel)),
        "rollback_versions": available_rollback_versions(current["version"]),
    }


@app.get("/api/admin/updates")
def admin_update_status(_=Depends(require_platform_admin)):
    return _admin_update_status_payload()


@app.post("/api/admin/updates/check")
def admin_check_update(
    request: Request,
    admin=Depends(require_platform_admin),
):
    channel = db.get_platform_setting("update_channel", "stable")
    current = version_payload(channel)
    lease, owner = _begin_platform_update_lease(120)
    try:
        release = fetch_release(channel, current["version"])
        payload = release_payload(release, channel, current["version"])
        if release is not None:
            db.upsert_platform_update(
                release.version,
                channel,
                "available",
                release_id=release.release_id,
                release_notes=release.notes,
                requested_by=admin["id"],
            )
        _audit(
            "platform.update_checked",
            request,
            actor_user_id=admin["id"],
            target_type="version",
            target_id=release.version if release is not None else current["version"],
            metadata={
                "version": release.version if release is not None else current["version"],
                "channel": channel,
                "status": "available" if release is not None else "current",
            },
        )
        return payload
    except PlatformUpdateError as exc:
        _raise_update_error(exc)
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/admin/updates/download")
def admin_download_update(
    body: PlatformUpdateDownloadIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    channel = db.get_platform_setting("update_channel", "stable")
    current = version_payload(channel)
    lease, owner = _begin_platform_update_lease(900)
    try:
        release = fetch_release(channel, current["version"])
        if release is None:
            raise HTTPException(
                409,
                detail={"code": "update_not_available", "message": "当前已是最新版本"},
            )
        requested_version = str(body.version or "").strip()
        if requested_version and requested_version != release.version:
            raise HTTPException(
                409,
                detail={"code": "update_version_changed", "message": "发布版本已变化，请重新检查"},
            )
        staged = stage_release(release, channel, current["version"])
        db.upsert_platform_update(
            release.version,
            channel,
            "staged",
            release_id=release.release_id,
            manifest_sha256=staged["manifest_sha256"],
            candidate_path=staged["candidate_path"],
            release_notes=release.notes,
            requested_by=admin["id"],
        )
        _audit(
            "platform.update_downloaded",
            request,
            actor_user_id=admin["id"],
            target_type="version",
            target_id=release.version,
            metadata={"version": release.version, "channel": channel, "status": "staged"},
        )
        return {
            "version": release.version,
            "channel": channel,
            "status": "staged",
            "release_notes": release.notes,
        }
    except PlatformUpdateError as exc:
        _raise_update_error(exc)
    finally:
        _release_account_lease(lease, owner)


@app.post("/api/admin/updates/apply", status_code=202)
def admin_apply_update(
    body: PlatformUpdateApplyIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    channel = db.get_platform_setting("update_channel", "stable")
    row = db.get_platform_update(body.version, channel)
    if row is None or str(row["status"]) != "staged":
        raise HTTPException(
            409,
            detail={"code": "update_not_staged", "message": "请先下载并校验该版本"},
        )
    if not db.consume_admin_confirmation(
        body.confirmation_token, admin["id"], "update.apply"
    ):
        raise HTTPException(
            403,
            detail={"code": "confirmation_invalid", "message": "二次确认已失效，请重新确认"},
        )
    try:
        validate_candidate(
            str(row["candidate_path"]), body.version, str(row["manifest_sha256"])
        )
        queued = write_update_intent(
            "apply",
            body.version,
            channel=channel,
            requested_by=admin["id"],
            candidate_path=str(row["candidate_path"]),
            manifest_sha256=str(row["manifest_sha256"]),
        )
    except PlatformUpdateError as exc:
        _raise_update_error(exc)
    db.upsert_platform_update(
        body.version,
        channel,
        "apply_requested",
        release_id=str(row["release_id"]),
        manifest_sha256=str(row["manifest_sha256"]),
        candidate_path=str(row["candidate_path"]),
        release_notes=str(row["release_notes"]),
        requested_by=admin["id"],
    )
    _audit(
        "platform.update_requested",
        request,
        actor_user_id=admin["id"],
        target_type="version",
        target_id=body.version,
        metadata={"version": body.version, "channel": channel, "status": "queued"},
    )
    return queued


@app.post("/api/admin/updates/rollback", status_code=202)
def admin_rollback_update(
    body: PlatformUpdateApplyIn,
    request: Request,
    admin=Depends(require_platform_admin),
):
    channel = db.get_platform_setting("update_channel", "stable")
    current = version_payload(channel)
    if body.version not in available_rollback_versions(current["version"]):
        raise HTTPException(
            404,
            detail={"code": "rollback_version_unavailable", "message": "该回滚版本不可用"},
        )
    if not db.consume_admin_confirmation(
        body.confirmation_token, admin["id"], "update.rollback"
    ):
        raise HTTPException(
            403,
            detail={"code": "confirmation_invalid", "message": "二次确认已失效，请重新确认"},
        )
    try:
        queued = write_update_intent(
            "rollback",
            body.version,
            channel=channel,
            requested_by=admin["id"],
        )
    except PlatformUpdateError as exc:
        _raise_update_error(exc)
    db.upsert_platform_update(
        body.version,
        channel,
        "rollback_requested",
        requested_by=admin["id"],
    )
    _audit(
        "platform.rollback_requested",
        request,
        actor_user_id=admin["id"],
        target_type="version",
        target_id=body.version,
        metadata={"version": body.version, "channel": channel, "status": "queued"},
    )
    return queued


def _runtime_value(runtime, name: str, default=None):
    try:
        value = runtime[name]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value


def _persist_restore_runtime(
    runtime,
    *,
    desired_state=None,
    mode=None,
    state,
    pid,
    last_error,
):
    generation = int(_runtime_value(runtime, "generation", 0))
    return db.persist_worker_runtime(
        int(runtime["user_id"]),
        int(runtime["account_id"]),
        desired_state=desired_state or str(_runtime_value(runtime, "desired_state", "stopped")),
        mode=mode or str(_runtime_value(runtime, "mode", "rules")),
        state=state,
        pid=pid,
        generation=generation,
        started_at=_runtime_value(runtime, "started_at"),
        heartbeat_at=time.time(),
        exit_code=_runtime_value(runtime, "exit_code"),
        last_error=last_error,
        expected_generation=generation,
    )


def restore_desired_workers():
    """Reconcile one durable worker intent for every enabled shop account."""
    if os.environ.get("SAAS_RESTORE_WORKERS", "1").strip().lower() in {"0", "false", "no"}:
        return
    for runtime in db.list_worker_runtimes():
        if runtime["desired_state"] != "running" and not runtime["pid"]:
            continue
        user_id = int(runtime["user_id"])
        account_id = int(runtime["account_id"])
        lease_key = f"worker-control:{user_id}:{account_id}"
        lease_owner = f"api:{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
        account_key = "default"
        account = None
        try:
            lease_result = db.acquire_control_lease(
                lease_key,
                lease_owner,
                lease_seconds=45,
                cooldown_seconds=0,
            )
        except (OSError, sqlite3.Error, ValueError):
            continue
        if lease_result != "acquired":
            continue
        try:
            # The list entry predates the account lease.  Re-read all mutable
            # control state after acquisition so a concurrent stop/disable
            # cannot be resurrected from the stale iterator snapshot.
            runtime = db.get_worker_runtime(user_id, account_id)
            if runtime is None:
                continue
            user = db.get_user_by_id(user_id)
            account = db.get_shop_account(user_id, account_id=account_id)
            if account is None:
                _persist_restore_runtime(
                    runtime,
                    desired_state="stopped",
                    state="degraded" if runtime["pid"] else "stopped",
                    pid=runtime["pid"],
                    last_error="account_unavailable",
                )
                continue
            account_key = str(_runtime_value(account, "account_key", "default"))
            if runtime["desired_state"] != "running":
                _stop_account_worker_locked(user_id, account, "restore_stopped")
                continue
            if user is None or not account["enabled"]:
                _stop_account_worker_locked(user_id, account, "account_unavailable")
                continue

            persisted_mode = str(runtime["mode"] or "rules")
            mode = (
                "rules_ai"
                if persisted_mode == "rules_ai" and has_permission(user, "automation.ai")
                else "rules"
            )
            # Recovery is also used by older supervisors and small test
            # doubles that only expose the account id.  A real DB row always
            # has ``account_key``; keep the legacy default when it is absent.
            account_key = "default"
            has_account_key = False
            try:
                keys = account.keys()
                has_account_key = "account_key" in keys
            except AttributeError:
                has_account_key = isinstance(account, dict) and "account_key" in account
            if has_account_key:
                account_key = str(account["account_key"])
                try:
                    _require_account_worker_configuration(
                        user,
                        account,
                        mode,
                        require_rules_content=False,
                    )
                except HTTPException as exc:
                    code = _worker_configuration_error_code(exc)
                    try:
                        _degrade_invalid_worker_configuration(
                            user_id, account, mode, code
                        )
                    except (OSError, RuntimeError, sqlite3.Error):
                        _persist_restore_runtime(
                            runtime,
                            mode=mode,
                            state="degraded",
                            pid=runtime["pid"],
                            last_error=code,
                        )
                    continue

            auth_state = _read_auth_status(user_id, account_key)
            if auth_state["reauthorization_required"]:
                waiting_pid = runtime["pid"]
                if waiting_pid:
                    stopped, stop_reason = bot_terminate_pid(
                        user_id, int(waiting_pid), account_key
                    )
                    if _stop_confirmed(stopped, stop_reason):
                        waiting_pid = None
                    else:
                        _persist_restore_runtime(
                            runtime,
                            state="degraded",
                            pid=waiting_pid,
                            last_error=f"reauthorization_{stop_reason}",
                        )
                        continue
                _persist_restore_runtime(
                    runtime,
                    state="waiting_login",
                    pid=None,
                    last_error=str(auth_state["code"]),
                )
                continue

            persisted_pid = runtime["pid"]
            if persisted_pid:
                adopted, adopt_reason = bot_adopt(
                    user_id, persisted_pid, persisted_mode, account_key
                )
                if adopted:
                    if persisted_mode != mode:
                        # This should only be reachable for an obsolete
                        # supervisor implementation; never preserve an AI
                        # process after the subscription no longer authorizes it.
                        stopped, stop_reason = bot_terminate_pid(
                            user_id, persisted_pid, account_key
                        )
                        if not stopped and stop_reason not in {"already_dead"}:
                            _persist_restore_runtime(
                                runtime,
                                state="degraded",
                                pid=persisted_pid,
                                last_error=f"orphan_{stop_reason}",
                            )
                            continue
                        adopt_reason = "pid_dead"
                    else:
                        persist_kwargs = {"account": account} if has_account_key else {}
                        _persist_worker_started(user_id, mode, already_running=True, **persist_kwargs)
                        continue
                if adopt_reason == "token_unavailable":
                    # The old AI worker's loopback token lived in the old API
                    # process.  Revoke the orphan only after the PID identity
                    # check, then launch a fresh worker with a new token.
                    stopped, stop_reason = bot_terminate_pid(
                        user_id, persisted_pid, account_key
                    )
                    if not stopped and stop_reason not in {"already_dead"}:
                        _persist_restore_runtime(
                            runtime,
                            state="degraded",
                            pid=persisted_pid,
                            last_error=f"orphan_{stop_reason}",
                        )
                        continue
                elif adopt_reason not in {"pid_dead", "pid_invalid"}:
                    # A live PID that is not our worker must never be killed or
                    # shadowed by a duplicate automatic process.
                    _persist_restore_runtime(
                        runtime,
                        state="degraded",
                        pid=persisted_pid,
                        last_error=f"worker_{adopt_reason}",
                    )
                    continue

            current = bot_status(user_id, account_key)
            if not current.get("connected"):
                _persist_restore_runtime(
                    runtime,
                    state="waiting_login",
                    pid=None,
                    last_error="shop_not_connected",
                )
                continue
            ok, reason = bot_start(user_id, mode, account_key)
            if not ok:
                raise RuntimeError(reason)
            persist_kwargs = {"account": account} if has_account_key else {}
            _persist_worker_started(
                user_id,
                mode,
                already_running=reason == "already_running",
                **persist_kwargs,
            )
        except Exception as exc:
            stop_reason = "not_running"
            try:
                _, stop_reason = bot_stop(user_id, account_key)
            except Exception as stop_exc:
                stop_reason = f"stop_failed:{stop_exc}"
            try:
                latest_runtime = db.get_worker_runtime(user_id, account_id) or runtime
                _persist_restore_runtime(
                    latest_runtime,
                    desired_state="stopped",
                    state="degraded",
                    pid=bot_process_id(user_id, account_key),
                    last_error=f"{exc}; {stop_reason}"[:240],
                )
            except (OSError, sqlite3.Error, RuntimeError, ValueError):
                pass
        finally:
            try:
                db.release_control_lease(lease_key, lease_owner)
            except (OSError, sqlite3.Error):
                pass


def shutdown_services():
    qr_logins.shutdown()
    shutdown_all()


restore_desired_workers()
start_watchdog(
    lambda uid: db.get_user_by_id(uid)["expires_at"] if db.get_user_by_id(uid) else None,
    _reserve_watchdog_transition,
    _persist_watchdog_transition,
    _release_watchdog_transition,
)
atexit.register(shutdown_services)

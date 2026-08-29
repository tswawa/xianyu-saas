"""xianyu-saas 控制面数据库。

The original release keyed runtime files by ``user_id``.  The durable control
plane now also has account, job, and worker-runtime records so the service can
grow to multiple shops without making the browser understand orchestration.
Secrets and platform payloads remain outside this database.
"""
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

from account_storage import AccountStorageError, normalize_account_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SAAS_DB", os.path.join(BASE_DIR, "saas.db"))
TOKEN_TTL_SECONDS = 30 * 86400
COMPLETED_JOB_RETENTION_SECONDS = 7 * 86400
DEAD_LETTER_JOB_RETENTION_SECONDS = 30 * 86400
RETENTION_CLEANUP_INTERVAL_SECONDS = 3600
RETENTION_CLEANUP_BATCH_SIZE = 500
logger = logging.getLogger(__name__)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt).split("$", 1)[1], digest)


class DB:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self._next_retention_cleanup_at = 0.0
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=30000")
        self._init()

    def _init(self):
        with self._lock:
            self.con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_created_at
                    ON tokens(created_at, token);
                CREATE TABLE IF NOT EXISTS activation_codes (
                    code TEXT PRIMARY KEY,
                    days INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unused',
                    redeemed_by INTEGER,
                    redeemed_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenant_configs (
                    user_id INTEGER PRIMARY KEY,
                    bot_running INTEGER NOT NULL DEFAULT 0,
                    llm_base_url TEXT NOT NULL DEFAULT '',
                    llm_model TEXT NOT NULL DEFAULT '',
                    llm_api_key TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shop_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_key TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'xianyu',
                    display_name TEXT NOT NULL DEFAULT '',
                    account_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unconfigured',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    generation INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_verified_at REAL,
                    last_sync_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, account_key)
                );
                CREATE INDEX IF NOT EXISTS idx_shop_accounts_user
                    ON shop_accounts(user_id, enabled, id);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    available_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    UNIQUE(user_id, account_id, kind, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON jobs(status, available_at, lease_until, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_account
                    ON jobs(user_id, account_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_terminal_retention
                    ON jobs(status, completed_at, id);
                CREATE TABLE IF NOT EXISTS worker_runtimes (
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    desired_state TEXT NOT NULL DEFAULT 'stopped',
                    mode TEXT NOT NULL DEFAULT 'rules',
                    state TEXT NOT NULL DEFAULT 'stopped',
                    pid INTEGER,
                    generation INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    heartbeat_at REAL,
                    exit_code INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_runtimes_desired
                    ON worker_runtimes(desired_state, state, updated_at);
                CREATE TABLE IF NOT EXISTS control_leases (
                    resource_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attention_acknowledgements (
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    attention_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    resolved_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, account_id, attention_key)
                );
                CREATE INDEX IF NOT EXISTS idx_attention_acknowledgements_updated
                    ON attention_acknowledgements(updated_at);
                """
            )
            self._migrate_shop_accounts_locked()
            # Existing installations have users but no account rows.  The
            # default account is intentionally metadata-only and keeps all
            # legacy tenant files usable until their callers opt into
            # account-scoped paths.
            self._backfill_default_shop_accounts_locked()
            self._backfill_worker_runtimes_locked()
            self._prune_retention_locked(time.time(), RETENTION_CLEANUP_BATCH_SIZE)
            self.con.commit()

    def _migrate_shop_accounts_locked(self):
        """Add account fencing metadata to databases created before it existed."""
        columns = {
            str(row["name"])
            for row in self.con.execute("PRAGMA table_info(shop_accounts)").fetchall()
        }
        if "generation" not in columns:
            try:
                self.con.execute(
                    "ALTER TABLE shop_accounts ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError as error:
                # API and consumer can initialize against the same legacy DB
                # at once.  One process may win the ALTER between our PRAGMA
                # read and statement; accept only that benign duplicate race.
                if "duplicate column name" not in str(error).lower():
                    raise

    @staticmethod
    def _cleanup_batch_size(value):
        try:
            return min(max(int(value), 1), 5000)
        except (TypeError, ValueError):
            return RETENTION_CLEANUP_BATCH_SIZE

    def _prune_retention_locked(self, now, batch_size):
        """Delete only expired sessions and terminal jobs in bounded batches."""
        now = float(now)
        batch_size = self._cleanup_batch_size(batch_size)
        token_cutoff = now - TOKEN_TTL_SECONDS
        completed_cutoff = now - COMPLETED_JOB_RETENTION_SECONDS
        dead_letter_cutoff = now - DEAD_LETTER_JOB_RETENTION_SECONDS
        token_cur = self.con.execute(
            """
            DELETE FROM tokens WHERE token IN (
                SELECT token FROM tokens
                WHERE created_at <= ?
                ORDER BY created_at, token
                LIMIT ?
            )
            """,
            (token_cutoff, batch_size),
        )
        counts = {"tokens": max(token_cur.rowcount, 0)}
        for status, cutoff, key in (
            ("completed", completed_cutoff, "completed_jobs"),
            ("dead_letter", dead_letter_cutoff, "dead_letter_jobs"),
        ):
            cur = self.con.execute(
                """
                DELETE FROM jobs WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status = ?
                      AND (
                          (completed_at IS NOT NULL AND completed_at <= ?)
                          OR (completed_at IS NULL AND updated_at <= ?)
                      )
                    ORDER BY COALESCE(completed_at, updated_at), id
                    LIMIT ?
                )
                """,
                (status, cutoff, cutoff, batch_size),
            )
            counts[key] = max(cur.rowcount, 0)
        self._next_retention_cleanup_at = now + RETENTION_CLEANUP_INTERVAL_SECONDS
        total = sum(counts.values())
        if total:
            logger.info(
                "control_plane_retention_pruned tokens=%d completed_jobs=%d dead_letter_jobs=%d",
                counts["tokens"],
                counts["completed_jobs"],
                counts["dead_letter_jobs"],
            )
        return counts

    def _maybe_prune_retention_locked(self, now):
        if float(now) < self._next_retention_cleanup_at:
            return None
        return self._prune_retention_locked(now, RETENTION_CLEANUP_BATCH_SIZE)

    def prune_retention(self, now=None, batch_size=RETENTION_CLEANUP_BATCH_SIZE):
        """Run one bounded retention pass and return non-sensitive delete counts."""
        now = time.time() if now is None else float(now)
        with self._lock:
            counts = self._prune_retention_locked(now, batch_size)
            self.con.commit()
            return counts

    def is_ready(self):
        """Return whether the control database can execute a minimal read."""
        with self._lock:
            row = self.con.execute("SELECT 1").fetchone()
            return row is not None and int(row[0]) == 1

    # ---- durable account/control-plane state ----
    def _backfill_default_shop_accounts_locked(self):
        now = time.time()
        self.con.execute(
            """
            INSERT OR IGNORE INTO shop_accounts(
                user_id, account_key, platform, status, enabled, created_at, updated_at
            )
            SELECT id, 'default', 'xianyu', 'unconfigured', 1, ?, ?
            FROM users
            """,
            (now, now),
        )

    def _backfill_worker_runtimes_locked(self):
        """Give every enabled shop a durable default running intent."""
        now = time.time()
        self.con.execute(
            """
            INSERT OR IGNORE INTO worker_runtimes(
                user_id, account_id, desired_state, mode, state, pid,
                generation, started_at, heartbeat_at, exit_code, last_error, updated_at
            )
            SELECT user_id, id, 'running', 'rules', 'waiting_login', NULL,
                   0, NULL, ?, NULL, '', ?
            FROM shop_accounts
            WHERE enabled = 1
            """,
            (now, now),
        )

    def _ensure_worker_runtime_locked(self, user_id, account_id):
        """Create one missing account runtime without changing existing intent."""
        now = time.time()
        self.con.execute(
            """
            INSERT OR IGNORE INTO worker_runtimes(
                user_id, account_id, desired_state, mode, state, pid,
                generation, started_at, heartbeat_at, exit_code, last_error, updated_at
            ) VALUES (?, ?, 'running', 'rules', 'waiting_login', NULL,
                      0, NULL, ?, NULL, '', ?)
            """,
            (int(user_id), int(account_id), now, now),
        )

    def ensure_default_shop_account(self, user_id):
        """Return the compatibility account for a legacy user tenant."""
        user_id = int(user_id)
        now = time.time()
        with self._lock:
            self.con.execute(
                """
                INSERT OR IGNORE INTO shop_accounts(
                    user_id, account_key, platform, status, enabled, created_at, updated_at
                ) VALUES (?, 'default', 'xianyu', 'unconfigured', 1, ?, ?)
                """,
                (user_id, now, now),
            )
            account = self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND account_key = 'default'",
                (user_id,),
            ).fetchone()
            if account is not None and account["enabled"]:
                self._ensure_worker_runtime_locked(user_id, account["id"])
            self.con.commit()
            return account

    def list_shop_accounts(self, user_id, include_disabled=False):
        user_id = int(user_id)
        self.ensure_default_shop_account(user_id)
        query = "SELECT * FROM shop_accounts WHERE user_id = ?"
        params = [user_id]
        if not include_disabled:
            query += " AND enabled = 1"
        query += " ORDER BY id"
        with self._lock:
            return self.con.execute(query, params).fetchall()

    def create_shop_account(self, user_id, account_key, display_name=""):
        """Create a non-secret account record owned by ``user_id``."""
        user_id = int(user_id)
        try:
            account_key = normalize_account_key(account_key)
        except AccountStorageError:
            raise ValueError("invalid account key") from None
        display_name = str(display_name or "").strip()[:160]
        if not account_key or len(account_key) > 80 or not account_key.isascii():
            raise ValueError("invalid account key")
        now = time.time()
        with self._lock:
            cur = self.con.execute(
                """
                INSERT INTO shop_accounts(
                    user_id, account_key, platform, display_name, status, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, 'xianyu', ?, 'unconfigured', 1, ?, ?)
                """,
                (user_id, account_key, display_name, now, now),
            )
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                (user_id, cur.lastrowid),
            ).fetchone()

    def get_shop_account(self, user_id, account_id=None, account_key=None):
        user_id = int(user_id)
        if account_id is None and account_key is None:
            account_key = "default"
        with self._lock:
            if account_id is not None:
                return self.con.execute(
                    "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                    (user_id, int(account_id)),
                ).fetchone()
            return self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND account_key = ?",
                (user_id, str(account_key)),
            ).fetchone()

    def update_shop_account(self, user_id, account_id=None, **fields):
        """Update non-secret account metadata, enforcing tenant ownership."""
        allowed = {
            "display_name", "account_ref", "status", "enabled", "last_error_code",
            "last_verified_at", "last_sync_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_shop_account(user_id, account_id=account_id)
        user_id = int(user_id)
        now = time.time()
        with self._lock:
            row = self.get_shop_account(user_id, account_id=account_id)
            if row is None:
                return None
            if "enabled" in updates:
                updates["enabled"] = 1 if updates["enabled"] else 0
            sets = ", ".join(f"{key} = ?" for key in updates)
            values = list(updates.values()) + [now, user_id, row["id"]]
            self.con.execute(
                f"UPDATE shop_accounts SET {sets}, updated_at = ? WHERE user_id = ? AND id = ?",
                values,
            )
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                (user_id, row["id"]),
            ).fetchone()

    def update_shop_account_if_current(
        self, user_id, account_id, generation, *, display_name_if_empty=False, **fields
    ):
        """Update active account metadata only for the captured generation.

        Sync workers keep an account row from before a platform request.  A
        delete/reconnect increments ``generation``; this conditional update
        makes a stale worker unable to revive or mutate that account.
        """
        allowed = {
            "display_name", "account_ref", "status", "enabled", "last_error_code",
            "last_verified_at", "last_sync_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_shop_account(user_id, account_id=account_id)
        user_id = int(user_id)
        account_id = int(account_id)
        generation = int(generation)
        now = time.time()
        with self._lock:
            if "enabled" in updates:
                updates["enabled"] = 1 if updates["enabled"] else 0
            sets = ", ".join(f"{key} = ?" for key in updates)
            values = list(updates.values()) + [now, user_id, account_id, generation]
            name_guard = ""
            if display_name_if_empty:
                # The nickname is only a first-connect default.  A concurrent
                # PATCH that supplied a custom name must win.
                name_guard = " AND display_name = ''"
            cur = self.con.execute(
                f"""UPDATE shop_accounts SET {sets}, updated_at = ?
                    WHERE user_id = ? AND id = ? AND enabled = 1 AND generation = ?{name_guard}""",
                values,
            )
            self.con.commit()
            if cur.rowcount != 1:
                return None
            return self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                (user_id, account_id),
            ).fetchone()

    def account_is_current(self, user_id, account_id, generation):
        """Return whether an account is still enabled at ``generation``."""
        with self._lock:
            row = self.con.execute(
                """SELECT 1 FROM shop_accounts
                   WHERE user_id = ? AND id = ? AND enabled = 1 AND generation = ?""",
                (int(user_id), int(account_id), int(generation)),
            ).fetchone()
            return row is not None

    def disable_shop_account(self, user_id, account_id):
        """Atomically fence and soft-delete one tenant-owned shop account."""
        user_id = int(user_id)
        account_id = int(account_id)
        now = time.time()
        with self._lock:
            cur = self.con.execute(
                """UPDATE shop_accounts
                   SET enabled = 0, status = 'deleted', last_error_code = '',
                       generation = generation + 1, updated_at = ?
                   WHERE user_id = ? AND id = ? AND enabled = 1""",
                (now, user_id, account_id),
            )
            self.con.commit()
            if cur.rowcount != 1:
                return self.con.execute(
                    "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                    (user_id, account_id),
                ).fetchone()
            return self.con.execute(
                "SELECT * FROM shop_accounts WHERE user_id = ? AND id = ?",
                (user_id, account_id),
            ).fetchone()

    def remove_unconfigured_shop_account(self, user_id, account_id):
        """Remove a just-created account and its initial runtime intent.

        This narrow compensating delete cannot remove a connected or active
        account, so it is safe to use only during POST account setup.
        """
        user_id = int(user_id)
        account_id = int(account_id)
        with self._lock:
            try:
                row = self.con.execute(
                    """SELECT 1 FROM shop_accounts
                       WHERE user_id = ? AND id = ? AND enabled = 1
                         AND status = 'unconfigured' AND generation = 0""",
                    (user_id, account_id),
                ).fetchone()
                if row is None:
                    return False
                self.con.execute(
                    "DELETE FROM worker_runtimes WHERE user_id = ? AND account_id = ?",
                    (user_id, account_id),
                )
                cur = self.con.execute(
                    """DELETE FROM shop_accounts
                       WHERE user_id = ? AND id = ? AND enabled = 1
                         AND status = 'unconfigured' AND generation = 0""",
                    (user_id, account_id),
                )
                self.con.commit()
                return cur.rowcount == 1
            except BaseException:
                if self.con.in_transaction:
                    self.con.rollback()
                raise

    @staticmethod
    def _job_payload(payload):
        if payload is None:
            return "{}"
        if not isinstance(payload, dict):
            raise ValueError("job payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 128 * 1024:
            raise ValueError("job payload is too large")
        return encoded

    def enqueue_job(
        self,
        user_id,
        kind,
        idempotency_key,
        payload=None,
        account_id=None,
        max_attempts=5,
        delay_seconds=0,
    ):
        """Insert or return one durable job; duplicate keys are idempotent."""
        user_id = int(user_id)
        kind = str(kind).strip()
        idempotency_key = str(idempotency_key).strip()
        if not kind or len(kind) > 80 or not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("invalid job identity")
        max_attempts = min(max(int(max_attempts), 1), 20)
        encoded = self._job_payload(payload)
        if account_id is None:
            account_id = 0
        else:
            account_id = int(account_id)
            if account_id <= 0 or self.get_shop_account(user_id, account_id=account_id) is None:
                raise ValueError("account does not belong to user")
        now = time.time()
        available_at = now + max(float(delay_seconds), 0.0)
        with self._lock:
            self._maybe_prune_retention_locked(now)
            self.con.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    user_id, account_id, kind, idempotency_key, payload_json,
                    status, attempts, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (user_id, account_id, kind, idempotency_key, encoded,
                 max_attempts, available_at, now, now),
            )
            self.con.commit()
            return self.con.execute(
                """SELECT * FROM jobs
                   WHERE user_id = ? AND account_id = ? AND kind = ? AND idempotency_key = ?""",
                (user_id, account_id, kind, idempotency_key),
            ).fetchone()

    def get_job(self, job_id):
        with self._lock:
            return self.con.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()

    def _recover_expired_jobs_locked(self, now):
        rows = self.con.execute(
            """
            SELECT id, attempts, max_attempts FROM jobs
            WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            if row["attempts"] >= row["max_attempts"]:
                status = "dead_letter"
                completed_at = now
            else:
                status = "retry"
                completed_at = None
            self.con.execute(
                """
                UPDATE jobs SET status = ?, lease_owner = NULL, lease_until = NULL,
                    available_at = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, now, completed_at, now, row["id"]),
            )
        return len(rows)

    def acquire_control_lease(
        self,
        resource_key,
        owner,
        lease_seconds=60,
        cooldown_seconds=10,
        now=None,
    ):
        """Acquire a restart-safe lease, returning ``acquired``, ``busy`` or ``cooldown``."""
        resource_key = str(resource_key).strip()
        owner = str(owner).strip()
        if not resource_key or len(resource_key) > 200 or not owner or len(owner) > 128:
            raise ValueError("invalid control lease identity")
        now = time.time() if now is None else float(now)
        lease_until = now + min(max(float(lease_seconds), 1.0), 3600.0)
        cooldown_until = now + min(max(float(cooldown_seconds), 0.0), 86400.0)
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                row = self.con.execute(
                    "SELECT owner, lease_until, cooldown_until FROM control_leases WHERE resource_key = ?",
                    (resource_key,),
                ).fetchone()
                if row and row["owner"] != owner and row["lease_until"] > now:
                    self.con.rollback()
                    return "busy"
                if row and row["owner"] != owner and row["cooldown_until"] > now:
                    self.con.rollback()
                    return "cooldown"
                self.con.execute(
                    """
                    INSERT INTO control_leases(resource_key, owner, lease_until, cooldown_until, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        owner = excluded.owner,
                        lease_until = excluded.lease_until,
                        cooldown_until = excluded.cooldown_until,
                        updated_at = excluded.updated_at
                    """,
                    (resource_key, owner, lease_until, cooldown_until, now),
                )
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return "acquired"

    def renew_control_lease(self, resource_key, owner, lease_seconds=60, now=None):
        """Atomically extend an unexpired lease still owned by ``owner``."""
        now = time.time() if now is None else float(now)
        lease_until = now + min(max(float(lease_seconds), 1.0), 3600.0)
        with self._lock:
            cur = self.con.execute(
                """
                UPDATE control_leases SET lease_until = ?, updated_at = ?
                WHERE resource_key = ? AND owner = ? AND lease_until > ?
                """,
                (lease_until, now, str(resource_key), str(owner), now),
            )
            self.con.commit()
            return cur.rowcount == 1

    def release_control_lease(self, resource_key, owner, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self.con.execute(
                """
                UPDATE control_leases SET owner = '', lease_until = 0, updated_at = ?
                WHERE resource_key = ? AND owner = ?
                """,
                (now, str(resource_key), str(owner)),
            )
            self.con.commit()
            return cur.rowcount == 1

    def get_control_lease(self, resource_key):
        with self._lock:
            return self.con.execute(
                "SELECT * FROM control_leases WHERE resource_key = ?",
                (str(resource_key),),
            ).fetchone()

    def claim_jobs(self, lease_owner, limit=1, lease_seconds=60, now=None, kinds=None):
        """Atomically claim queued jobs and recover expired leases.

        ``kinds`` lets a dedicated consumer claim only the work it owns.  An
        omitted value preserves the original all-kinds maintenance behavior.
        """
        owner = str(lease_owner).strip()
        if not owner or len(owner) > 128:
            raise ValueError("invalid lease owner")
        now = time.time() if now is None else float(now)
        limit = min(max(int(limit), 1), 100)
        lease_until = now + min(max(float(lease_seconds), 1.0), 3600.0)
        normalized_kinds = None
        if kinds is not None:
            normalized_kinds = tuple(
                sorted({str(kind).strip() for kind in kinds if str(kind).strip()})
            )
            if not normalized_kinds:
                return []
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self._maybe_prune_retention_locked(now)
                self._recover_expired_jobs_locked(now)
                kind_filter = ""
                kind_params = []
                if normalized_kinds is not None:
                    placeholders = ",".join("?" for _ in normalized_kinds)
                    kind_filter = f" AND kind IN ({placeholders})"
                    kind_params.extend(normalized_kinds)
                rows = self.con.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE status IN ('queued', 'retry') AND available_at <= ?
                      {kind_filter}
                    ORDER BY id LIMIT ?
                    """,
                    (now, *kind_params, limit),
                ).fetchall()
                ids = [row["id"] for row in rows]
                for job_id in ids:
                    self.con.execute(
                        """
                        UPDATE jobs SET status = 'running', attempts = attempts + 1,
                            lease_owner = ?, lease_until = ?, updated_at = ?
                        WHERE id = ? AND status IN ('queued', 'retry')
                        """,
                        (owner, lease_until, now, job_id),
                    )
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            return self.con.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY id", ids
            ).fetchall()

    def claim_job(self, job_id, lease_owner, lease_seconds=60, now=None):
        """Claim one known job without accidentally consuming another tenant's work."""
        owner = str(lease_owner).strip()
        if not owner or len(owner) > 128:
            raise ValueError("invalid lease owner")
        now = time.time() if now is None else float(now)
        lease_until = now + min(max(float(lease_seconds), 1.0), 3600.0)
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self._maybe_prune_retention_locked(now)
                self._recover_expired_jobs_locked(now)
                cur = self.con.execute(
                    """
                    UPDATE jobs SET status = 'running', attempts = attempts + 1,
                        lease_owner = ?, lease_until = ?, updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'retry') AND available_at <= ?
                    """,
                    (owner, lease_until, now, int(job_id), now),
                )
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
            if cur.rowcount != 1:
                return None
            return self.con.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()

    def renew_job(self, job_id, lease_owner, lease_seconds=60, now=None):
        """Extend one running job lease when the owner is still current."""
        now = time.time() if now is None else float(now)
        lease_until = now + min(max(float(lease_seconds), 1.0), 3600.0)
        with self._lock:
            cur = self.con.execute(
                """
                UPDATE jobs SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (lease_until, now, int(job_id), str(lease_owner), now),
            )
            self.con.commit()
            return cur.rowcount == 1

    def complete_job(self, job_id, lease_owner, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self.con.execute(
                """
                UPDATE jobs SET status = 'completed', lease_owner = NULL,
                    lease_until = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (now, now, int(job_id), str(lease_owner)),
            )
            self.con.commit()
            return cur.rowcount == 1

    def complete_job_for_account(
        self,
        job_id,
        lease_owner,
        user_id,
        account_id,
        generation,
        account_key=None,
        now=None,
    ):
        """Complete a job only while its fenced shop account is still active."""
        now = time.time() if now is None else float(now)
        with self._lock:
            legacy_default = 1 if str(account_key or "") == "default" else 0
            cur = self.con.execute(
                """UPDATE jobs SET status = 'completed', lease_owner = NULL,
                       lease_until = NULL, completed_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'running' AND lease_owner = ?
                     AND user_id = ?
                     AND (account_id = ? OR (account_id = 0 AND ? = 1))
                     AND EXISTS (
                         SELECT 1 FROM shop_accounts
                         WHERE shop_accounts.user_id = jobs.user_id
                           AND shop_accounts.id = ?
                           AND shop_accounts.enabled = 1
                           AND shop_accounts.generation = ?
                     )""",
                (
                    now,
                    now,
                    int(job_id),
                    str(lease_owner),
                    int(user_id),
                    int(account_id),
                    legacy_default,
                    int(account_id),
                    int(generation),
                ),
            )
            self.con.commit()
            return cur.rowcount == 1

    def fail_job(
        self,
        job_id,
        lease_owner,
        error_code,
        error_message="",
        now=None,
        retry_delay_seconds=None,
    ):
        """Release a lease and either retry or dead-letter the job."""
        now = time.time() if now is None else float(now)
        code = str(error_code or "temporary")[:80]
        message = str(error_message or "")[:240]
        with self._lock:
            row = self.con.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (int(job_id), str(lease_owner)),
            ).fetchone()
            if row is None:
                return False
            terminal = row["attempts"] >= row["max_attempts"]
            status = "dead_letter" if terminal else "retry"
            if terminal:
                retry_at = now
            elif retry_delay_seconds is None:
                retry_at = now + min(2 ** min(row["attempts"], 8), 900)
            else:
                retry_at = now + min(max(float(retry_delay_seconds), 0.0), 900.0)
            cur = self.con.execute(
                """
                UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_until = NULL, last_error_code = ?, last_error = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (status, retry_at, code, message, now if terminal else None, now,
                 int(job_id), str(lease_owner)),
            )
            self.con.commit()
            return cur.rowcount == 1

    def get_worker_runtime(self, user_id, account_id=None):
        user_id = int(user_id)
        with self._lock:
            if account_id is None:
                account = self.ensure_default_shop_account(user_id)
                account_id = account["id"] if account else None
            return self.con.execute(
                "SELECT * FROM worker_runtimes WHERE user_id = ? AND account_id = ?",
                (user_id, int(account_id)),
            ).fetchone()

    def list_worker_runtimes(self, desired_state=None):
        with self._lock:
            if desired_state is None:
                return self.con.execute(
                    "SELECT * FROM worker_runtimes ORDER BY user_id, account_id"
                ).fetchall()
            return self.con.execute(
                "SELECT * FROM worker_runtimes WHERE desired_state = ? ORDER BY user_id, account_id",
                (str(desired_state),),
            ).fetchall()

    def persist_worker_runtime(
        self,
        user_id,
        account_id,
        *,
        desired_state,
        mode="rules",
        state="stopped",
        pid=None,
        generation=None,
        started_at=None,
        heartbeat_at=None,
        exit_code=None,
        last_error="",
        expected_generation=None,
    ):
        """Atomically write desired intent and the matching observed runtime."""
        user_id = int(user_id)
        account_id = int(account_id)
        desired = "running" if desired_state in {True, 1, "running"} else "stopped"
        mode = "rules_ai" if mode == "rules_ai" else "rules"
        state = str(state or "stopped")[:40]
        now = time.time()
        expected = None if expected_generation is None else int(expected_generation)
        next_generation = 0 if generation is None else int(generation)
        with self._lock:
            account = self.con.execute(
                "SELECT 1 FROM shop_accounts WHERE user_id = ? AND id = ?",
                (user_id, account_id),
            ).fetchone()
            if account is None:
                raise ValueError("account does not belong to user")
            try:
                # One conditional UPSERT is the CAS boundary. SQLite serializes
                # the statement across connections/processes, and the conflict
                # WHERE observes the generation after acquiring the write lock.
                cur = self.con.execute(
                    """
                    INSERT INTO worker_runtimes(
                        user_id, account_id, desired_state, mode, state, pid,
                        generation, started_at, heartbeat_at, exit_code,
                        last_error, updated_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE ? IS NULL OR ? = 0 OR EXISTS (
                        SELECT 1 FROM worker_runtimes
                        WHERE user_id = ? AND account_id = ?
                    )
                    ON CONFLICT(user_id, account_id) DO UPDATE SET
                        desired_state = excluded.desired_state,
                        mode = excluded.mode,
                        state = excluded.state,
                        pid = excluded.pid,
                        generation = CASE
                            WHEN ? THEN worker_runtimes.generation
                            ELSE excluded.generation
                        END,
                        started_at = excluded.started_at,
                        heartbeat_at = excluded.heartbeat_at,
                        exit_code = excluded.exit_code,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    WHERE ? IS NULL OR worker_runtimes.generation = ?
                    """,
                    (
                        user_id,
                        account_id,
                        desired,
                        mode,
                        state,
                        pid,
                        next_generation,
                        started_at,
                        heartbeat_at,
                        exit_code,
                        str(last_error or "")[:240],
                        now,
                        expected,
                        expected,
                        user_id,
                        account_id,
                        generation is None,
                        expected,
                        expected,
                    ),
                )
                if cur.rowcount != 1:
                    self.con.rollback()
                    return None
                row = self.con.execute(
                    "SELECT * FROM worker_runtimes WHERE user_id = ? AND account_id = ?",
                    (user_id, account_id),
                ).fetchone()
                self.con.commit()
                return row
            except BaseException:
                if self.con.in_transaction:
                    self.con.rollback()
                raise

    def set_worker_desired(self, user_id, desired_state, mode="rules", account_id=None):
        user_id = int(user_id)
        desired = "running" if desired_state in {True, 1, "running"} else "stopped"
        mode = "rules_ai" if mode == "rules_ai" else "rules"
        account = self.ensure_default_shop_account(user_id) if account_id is None else None
        if account_id is not None and self.get_shop_account(user_id, account_id=account_id) is None:
            raise ValueError("account does not belong to user")
        account_id = int(account["id"] if account else account_id)
        now = time.time()
        with self._lock:
            self.con.execute(
                """
                INSERT INTO worker_runtimes(
                    user_id, account_id, desired_state, mode, state, updated_at
                ) VALUES (?, ?, ?, ?, 'stopped', ?)
                ON CONFLICT(user_id, account_id) DO UPDATE SET
                    desired_state = excluded.desired_state,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (user_id, account_id, desired, mode, now),
            )
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM worker_runtimes WHERE user_id = ? AND account_id = ?",
                (user_id, account_id),
            ).fetchone()

    def update_worker_runtime(self, user_id, account_id=None, **fields):
        allowed = {"state", "pid", "generation", "started_at", "heartbeat_at", "exit_code", "last_error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_worker_runtime(user_id, account_id)
        row = self.get_worker_runtime(user_id, account_id)
        if row is None:
            self.set_worker_desired(user_id, False, account_id=account_id)
            row = self.get_worker_runtime(user_id, account_id)
        now = time.time()
        with self._lock:
            sets = ", ".join(f"{key} = ?" for key in updates)
            self.con.execute(
                f"UPDATE worker_runtimes SET {sets}, updated_at = ? WHERE user_id = ? AND account_id = ?",
                (*updates.values(), now, int(user_id), int(row["account_id"])),
            )
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM worker_runtimes WHERE user_id = ? AND account_id = ?",
                (int(user_id), int(row["account_id"])),
            ).fetchone()

    def reconcile_attention_acknowledgements(self, user_id, account_id, active_items):
        """Return matching resolved timestamps and discard stale acknowledgements.

        ``active_items`` maps opaque attention keys to fingerprints derived only
        from bounded operational metadata.  A changed fingerprint represents a
        new occurrence and must become pending again.
        """
        user_id = int(user_id)
        account_id = int(account_id)
        active = {
            str(key): str(fingerprint)
            for key, fingerprint in dict(active_items or {}).items()
            if str(key) and str(fingerprint)
        }
        now = time.time()
        with self._lock:
            rows = self.con.execute(
                """SELECT attention_key, fingerprint, resolved_at
                   FROM attention_acknowledgements
                   WHERE user_id = ? AND account_id = ?""",
                (user_id, account_id),
            ).fetchall()
            resolved = {}
            stale = []
            for row in rows:
                key = str(row["attention_key"] or "")
                if active.get(key) != str(row["fingerprint"] or ""):
                    stale.append(key)
                    continue
                resolved[key] = float(row["resolved_at"] or 0)
            if stale:
                self.con.executemany(
                    """DELETE FROM attention_acknowledgements
                       WHERE user_id = ? AND account_id = ? AND attention_key = ?""",
                    [(user_id, account_id, key) for key in stale],
                )
            self.con.execute(
                "DELETE FROM attention_acknowledgements WHERE updated_at < ?",
                (now - 180 * 86400,),
            )
            self.con.commit()
            return resolved

    def set_attention_resolved(self, user_id, account_id, attention_key, fingerprint, resolved):
        """Persist or clear one account-scoped acknowledgement idempotently."""
        user_id = int(user_id)
        account_id = int(account_id)
        key = str(attention_key or "").strip()
        signature = str(fingerprint or "").strip()
        if not key or len(key) > 80 or not signature or len(signature) > 80:
            raise ValueError("invalid attention acknowledgement")
        now = time.time()
        with self._lock:
            if resolved:
                self.con.execute(
                    """INSERT INTO attention_acknowledgements(
                           user_id, account_id, attention_key, fingerprint, resolved_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, account_id, attention_key) DO UPDATE SET
                           fingerprint = excluded.fingerprint,
                           resolved_at = excluded.resolved_at,
                           updated_at = excluded.updated_at""",
                    (user_id, account_id, key, signature, now, now),
                )
                resolved_at = now
            else:
                self.con.execute(
                    """DELETE FROM attention_acknowledgements
                       WHERE user_id = ? AND account_id = ? AND attention_key = ?""",
                    (user_id, account_id, key),
                )
                resolved_at = None
            self.con.commit()
            return resolved_at

    def attention_items(self, user_id, include_jobs=False, limit=50, account_id=None):
        """Return bounded operational summaries without payloads or secrets.

        ``account_id`` is optional for compatibility with the original
        tenant-wide summary. API callers should pass the selected account so
        a warning from another shop cannot appear in the current workspace.
        """
        user_id = int(user_id)
        limit = min(max(int(limit), 1), 200)
        selected_account_id = None if account_id is None else int(account_id)
        selected_account_key = ""
        items = []
        with self._lock:
            if selected_account_id is not None:
                selected = self.con.execute(
                    "SELECT account_key FROM shop_accounts WHERE user_id = ? AND id = ?",
                    (user_id, selected_account_id),
                ).fetchone()
                if selected is None:
                    return []
                selected_account_key = str(selected["account_key"] or "")

            account_filter = ""
            account_params = [user_id]
            if selected_account_id is not None:
                account_filter = " AND id = ?"
                account_params.append(selected_account_id)
            account_params.append(limit)
            accounts = self.con.execute(
                f"""
                SELECT id, account_key, status, last_error_code
                FROM shop_accounts
                WHERE user_id = ? AND enabled = 1
                  AND status NOT IN ('ready', 'unconfigured')
                  {account_filter}
                ORDER BY id LIMIT ?
                """,
                account_params,
            ).fetchall()
            for row in accounts:
                items.append(
                    {
                        "kind": "shop_account",
                        "code": str(row["status"] or "degraded"),
                        "account_id": int(row["id"]),
                        "account_key": str(row["account_key"]),
                        "error_code": str(row["last_error_code"] or ""),
                    }
                )
            runtime_filter = ""
            runtime_params = [user_id]
            if selected_account_id is not None:
                runtime_filter = " AND account_id = ?"
                runtime_params.append(selected_account_id)
            runtime_params.append(limit)
            runtimes = self.con.execute(
                f"""
                SELECT account_id, desired_state, state, last_error
                FROM worker_runtimes
                WHERE user_id = ?
                  AND (
                        state = 'degraded'
                        OR desired_state = 'running'
                           AND state NOT IN ('running', 'waiting_login')
                  )
                  {runtime_filter}
                ORDER BY account_id LIMIT ?
                """,
                runtime_params,
            ).fetchall()
            for row in runtimes:
                items.append(
                    {
                        "kind": "worker",
                        "code": str(row["state"] or "degraded"),
                        "account_id": int(row["account_id"]),
                        "desired_state": str(row["desired_state"]),
                        "error_code": "worker_unhealthy",
                    }
                )
            if include_jobs:
                job_filter = ""
                job_params = [user_id]
                if selected_account_id is not None:
                    # Jobs created before account scoping used account_id=0;
                    # expose those legacy jobs only on the default account.
                    if selected_account_key == "default":
                        job_filter = " AND account_id IN (?, 0)"
                        job_params.append(selected_account_id)
                    else:
                        job_filter = " AND account_id = ?"
                        job_params.append(selected_account_id)
                job_params.append(limit)
                jobs = self.con.execute(
                    f"""
                    SELECT account_id, kind, status, last_error_code, COUNT(*) AS total
                    FROM jobs
                    WHERE user_id = ? AND status IN ('retry', 'dead_letter')
                      {job_filter}
                    GROUP BY account_id, kind, status, last_error_code
                    ORDER BY total DESC LIMIT ?
                    """,
                    job_params,
                ).fetchall()
                for row in jobs:
                    raw_account_id = int(row["account_id"])
                    items.append(
                        {
                            "kind": "job",
                            "job_kind": str(row["kind"] or ""),
                            "code": str(row["status"]),
                            "account_id": (
                                selected_account_id
                                if selected_account_id is not None
                                and selected_account_key == "default"
                                and raw_account_id == 0
                                else raw_account_id
                            ),
                            "error_code": str(row["last_error_code"] or ""),
                            "count": int(row["total"]),
                        }
                    )
        return items[:limit]

    # ---- users ----
    def create_user(self, username, password):
        """Atomically create the user, default shop and initial worker intent."""
        now = time.time()
        with self._lock:
            try:
                cur = self.con.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), now),
                )
                user_id = int(cur.lastrowid)
                account_cur = self.con.execute(
                    """
                    INSERT INTO shop_accounts(
                        user_id, account_key, platform, status, enabled, created_at, updated_at
                    ) VALUES (?, 'default', 'xianyu', 'unconfigured', 1, ?, ?)
                    """,
                    (user_id, now, now),
                )
                self._ensure_worker_runtime_locked(user_id, int(account_cur.lastrowid))
                self.con.commit()
                return user_id
            except BaseException:
                if self.con.in_transaction:
                    self.con.rollback()
                raise

    def remove_unconfigured_user(self, user_id):
        """Compensate a failed registration before the account becomes usable."""
        user_id = int(user_id)
        with self._lock:
            try:
                accounts = self.con.execute(
                    "SELECT id, account_key, status, enabled, generation FROM shop_accounts WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                if len(accounts) != 1:
                    return False
                account = accounts[0]
                if not (
                    account["account_key"] == "default"
                    and account["status"] == "unconfigured"
                    and int(account["enabled"] or 0) == 1
                    and int(account["generation"] or 0) == 0
                ):
                    return False
                if self.con.execute("SELECT 1 FROM tokens WHERE user_id = ? LIMIT 1", (user_id,)).fetchone():
                    return False
                if self.con.execute("SELECT 1 FROM jobs WHERE user_id = ? LIMIT 1", (user_id,)).fetchone():
                    return False
                self.con.execute("DELETE FROM worker_runtimes WHERE user_id = ?", (user_id,))
                self.con.execute("DELETE FROM shop_accounts WHERE user_id = ?", (user_id,))
                self.con.execute("DELETE FROM tenant_configs WHERE user_id = ?", (user_id,))
                self.con.execute("DELETE FROM attention_acknowledgements WHERE user_id = ?", (user_id,))
                cur = self.con.execute("DELETE FROM users WHERE id = ?", (user_id,))
                self.con.commit()
                return cur.rowcount == 1
            except BaseException:
                if self.con.in_transaction:
                    self.con.rollback()
                raise

    def get_user(self, username):
        with self._lock:
            return self.con.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def get_user_by_id(self, user_id):
        with self._lock:
            return self.con.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()

    # ---- tokens ----
    def create_token(self, user_id):
        token = secrets.token_hex(32)
        now = time.time()
        with self._lock:
            self._maybe_prune_retention_locked(now)
            self.con.execute(
                "INSERT INTO tokens(token, user_id, created_at) VALUES (?, ?, ?)",
                (token, user_id, now),
            )
            self.con.commit()
        return token

    def get_token_user(self, token, max_age=TOKEN_TTL_SECONDS):
        with self._lock:
            row = self.con.execute(
                "SELECT user_id, created_at FROM tokens WHERE token = ?", (token,)
            ).fetchone()
            if row and row["created_at"] + max_age <= time.time():
                self.con.execute("DELETE FROM tokens WHERE token = ?", (token,))
                self.con.commit()
                return None
        return row["user_id"] if row else None

    def delete_token(self, token):
        if not token:
            return
        with self._lock:
            self.con.execute("DELETE FROM tokens WHERE token = ?", (token,))
            self.con.commit()

    # ---- activation ----
    def generate_codes(self, count, days):
        now = time.time()
        with self._lock:
            for _ in range(count):
                code = secrets.token_hex(16)
                self.con.execute(
                    "INSERT INTO activation_codes(code, days, created_at) VALUES (?, ?, ?)",
                    (code, days, now),
                )
            self.con.commit()

    def redeem(self, user_id, code):
        now = time.time()
        with self._lock:
            row = self.con.execute(
                "SELECT * FROM activation_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None or row["status"] != "unused":
                return None
            self.con.execute(
                """UPDATE activation_codes SET status = 'used', redeemed_by = ?, redeemed_at = ?
                   WHERE code = ? AND status = 'unused'""",
                (user_id, now, code),
            )
            user = self.con.execute(
                "SELECT expires_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            base = max(user["expires_at"], now)
            expires = base + row["days"] * 86400
            self.con.execute(
                "UPDATE users SET expires_at = ? WHERE id = ?", (expires, user_id)
            )
            self.con.commit()
            return expires

    # ---- tenant config ----
    def get_config(self, user_id):
        with self._lock:
            return self.con.execute(
                "SELECT * FROM tenant_configs WHERE user_id = ?", (user_id,)
            ).fetchone()

    def save_config(self, user_id, fields):
        now = time.time()
        # Legacy llm_* columns remain only for backward-compatible database
        # reads.  New requests can never write tenant model endpoints or keys.
        allowed = {"keywords_json", "bot_running"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        with self._lock:
            row = self.con.execute(
                "SELECT 1 FROM tenant_configs WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                defaults = {
                    "llm_base_url": "", "llm_model": "", "llm_api_key": "",
                    "keywords_json": "{}", "bot_running": 0,
                }
                values = {**defaults, **updates}
                self.con.execute(
                    """INSERT INTO tenant_configs(user_id, bot_running, llm_base_url, llm_model,
                       llm_api_key, keywords_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, int(values["bot_running"]), values["llm_base_url"],
                        values["llm_model"], values["llm_api_key"],
                        values["keywords_json"], now,
                    ),
                )
            else:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self.con.execute(
                    f"UPDATE tenant_configs SET {sets}, updated_at = ? WHERE user_id = ?",
                    (*updates.values(), now, user_id),
                )
            self.con.commit()

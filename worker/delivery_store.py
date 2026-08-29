import json
import hashlib
import math
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


class DeliveryStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryReservation:
    key: str
    status: str
    delivery_type: str
    chat_id: str
    buyer_id: str
    item_id: str
    resources: tuple[str, ...]
    quantity: int = 1
    payload: Optional[str] = None
    reason: Optional[str] = None
    platform_order_id: Optional[str] = None
    verified_at: Optional[float] = None
    platform_status: Optional[str] = None
    paid_amount: Optional[str] = None


@dataclass(frozen=True)
class PaymentEvent:
    key: str
    chat_id: str
    status: str
    event_at: float
    expires_at: float


@dataclass(frozen=True)
class InboundEvent:
    key: str
    chat_id: str
    payload: str
    status: str


@dataclass(frozen=True)
class ManualReview:
    key: str
    chat_id: str
    reason: str
    event_at: float
    created_at: float
    updated_at: float


class DeliveryStore:
    """SQLite-backed inventory, payment, idempotency, and manual-mode state."""

    MAX_INBOUND_EVENT_BYTES = 1024 * 1024
    MAX_INBOUND_EVENT_KEY_LENGTH = 512
    MAX_CHAT_ID_LENGTH = 256
    MAX_INBOUND_QUEUE_EVENTS = 2048
    MAX_INBOUND_QUEUE_BYTES = 64 * 1024 * 1024
    MAX_INBOUND_EVENTS_PER_CHAT = 256
    MAX_PENDING_SCAN = 1024
    INBOUND_COMPLETED_RETENTION = 30 * 86400
    MAX_INBOUND_ATTEMPTS = 5
    MAX_DELIVERY_PAYLOAD_CHARS = 16 * 1024
    INBOUND_RETRY_BASE_SECONDS = 5
    IDEMPOTENCY_RETENTION_SECONDS = 90 * 86400
    LLM_WINDOW_SECONDS = 3600
    MAX_LLM_CALLS_PER_CHAT = 30
    MAX_LLM_INPUT_CHARS_PER_CHAT = 120_000
    MAX_LLM_CALLS_GLOBAL = 500
    MAX_LLM_INPUT_CHARS_GLOBAL = 2_000_000
    SAFE_ERROR_TYPES = frozenset(
        {
            "stored_error",
            "expired_message",
            "worker_cancelled",
            "interrupted_send",
            "platform_order_identity_unavailable",
            "chat_binding_unavailable",
            "unsupported_item",
            "headinfo_invalid",
            "headinfo_item_mismatch",
            "order_detail_invalid",
            "order_identity_mismatch",
            "order_item_mismatch",
            "order_buyer_mismatch",
            "seller_identity_mismatch",
            "order_not_awaiting_shipment",
            "unsupported_quantity",
            "order_reverification_failed",
            "pan_resource_unavailable",
            "material_payload_unavailable",
            "inventory_empty",
            "inventory_marked_used",
            "inventory_removed_from_manifest",
            "inventory_state_changed",
            "cancelled_during_send",
            "cancelled_after_send_attempt",
            "manual_takeover_before_send",
            "trial_state_commit_failed",
            "ack_timeout",
            "untrusted_order",
            "duplicate_replay",
            "temporary",
            "permanent",
            "invalid_event",
            "duplicate_event",
            "not_actionable",
            "operator_cancelled",
            "ConnectionError",
            "TimeoutError",
            "CancelledError",
            "ValueError",
            "RuntimeError",
            "DeliveryStoreError",
            "LLMServiceError",
            "LLMEmptyResponseError",
            "ManualTakeoverError",
        }
    )

    def __init__(
        self,
        db_path: str,
        redeem_pool_path: Optional[str] = None,
        trial_pool_path: Optional[str] = None,
        trial_sent_path: Optional[str] = None,
        now_fn=time.time,
    ):
        self.db_path = os.path.abspath(db_path)
        self.now_fn = now_fn
        state_dir = os.path.dirname(self.db_path)
        if state_dir:
            os.makedirs(state_dir, mode=0o700, exist_ok=True)
            os.chmod(state_dir, 0o700)
        self._init_db()
        if redeem_pool_path:
            self.import_inventory(redeem_pool_path, "redeem")
        if trial_pool_path:
            self.import_inventory(trial_pool_path, "trial")
        if trial_sent_path:
            self.import_trial_claims(trial_sent_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reservation_key TEXT,
                    buyer_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(kind, secret)
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_available
                    ON inventory(kind, status, id);
                CREATE INDEX IF NOT EXISTS idx_inventory_reservation
                    ON inventory(reservation_key);

                CREATE TABLE IF NOT EXISTS delivery_events (
                    order_key TEXT PRIMARY KEY,
                    platform_order_id TEXT,
                    chat_id TEXT NOT NULL,
                    buyer_id TEXT,
                    item_id TEXT,
                    delivery_type TEXT,
                    delivery_payload TEXT,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    inventory_ids TEXT NOT NULL DEFAULT '[]',
                    event_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    send_started_at REAL,
                    delivered_at REAL,
                    verified_at REAL,
                    platform_status TEXT,
                    paid_amount TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_chat_status
                    ON delivery_events(chat_id, status, expires_at);

                CREATE TABLE IF NOT EXISTS chat_bindings (
                    chat_id TEXT PRIMARY KEY,
                    buyer_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    conflicted INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_pending
                    ON inbound_events(status, created_at);

                CREATE TABLE IF NOT EXISTS llm_usage (
                    scope TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL,
                    call_count INTEGER NOT NULL,
                    input_chars INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_usage_window
                    ON llm_usage(window_start);

                CREATE TABLE IF NOT EXISTS trial_claims (
                    buyer_id TEXT PRIMARY KEY,
                    inventory_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    delivered_at REAL,
                    FOREIGN KEY(inventory_id) REFERENCES inventory(id)
                );

                CREATE TABLE IF NOT EXISTS manual_modes (
                    chat_id TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_control_events (
                    control_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_reply_state (
                    chat_id TEXT PRIMARY KEY,
                    last_reply_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_reply_state_updated
                    ON automation_reply_state(updated_at);

                CREATE TABLE IF NOT EXISTS manual_reviews (
                    order_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    resolution TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL,
                    FOREIGN KEY(order_key) REFERENCES delivery_events(order_key)
                );
                CREATE INDEX IF NOT EXISTS idx_manual_reviews_open
                    ON manual_reviews(status, created_at);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(delivery_events)")
            }
            if "send_started_at" not in columns:
                conn.execute("ALTER TABLE delivery_events ADD COLUMN send_started_at REAL")
            if "delivery_payload" not in columns:
                conn.execute("ALTER TABLE delivery_events ADD COLUMN delivery_payload TEXT")
            for column, declaration in (
                ("platform_order_id", "TEXT"),
                ("verified_at", "REAL"),
                ("platform_status", "TEXT"),
                ("paid_amount", "TEXT"),
                ("platform_shipped_at", "REAL"),
                ("platform_ship_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE delivery_events ADD COLUMN {column} {declaration}"
                    )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_platform_order
                ON delivery_events(platform_order_id)
                WHERE platform_order_id IS NOT NULL
                """
            )
            binding_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(chat_bindings)")
            }
            if "conflicted" not in binding_columns:
                conn.execute(
                    "ALTER TABLE chat_bindings ADD COLUMN conflicted INTEGER NOT NULL DEFAULT 0"
                )
            inbound_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(inbound_events)")
            }
            if "payload_hash" not in inbound_columns:
                conn.execute("ALTER TABLE inbound_events ADD COLUMN payload_hash TEXT")
                rows = conn.execute(
                    "SELECT event_key, payload FROM inbound_events"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE inbound_events SET payload_hash = ? WHERE event_key = ?",
                        (
                            hashlib.sha256(row["payload"].encode("utf-8")).hexdigest(),
                            row["event_key"],
                        ),
                    )
            if "attempt_count" not in inbound_columns:
                conn.execute(
                    "ALTER TABLE inbound_events ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_at" not in inbound_columns:
                conn.execute(
                    "ALTER TABLE inbound_events ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO manual_reviews(
                    order_key, status, reason, created_at, updated_at
                )
                SELECT order_key, 'open',
                       COALESCE(last_error, 'manual_review'),
                       created_at, updated_at
                FROM delivery_events
                WHERE status = 'manual_review'
                """
            )
            conn.execute(
                "DELETE FROM manual_control_events WHERE created_at < ?",
                (self.now_fn() - self.IDEMPOTENCY_RETENTION_SECONDS,),
            )
            # A process may have stopped after sending but before recording the ACK.
            # Reusing the deterministic message UUID makes the retry idempotent upstream.
            conn.execute(
                "UPDATE trial_claims SET status = 'retry' WHERE status = 'sending'"
            )
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', next_attempt_at = 0
                WHERE status = 'processing'
                """
            )
            conn.execute(
                """
                UPDATE delivery_events
                SET status = 'retry', last_error = 'interrupted_send', send_started_at = NULL
                WHERE status = 'sending'
                """
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _load_json(path: str):
        try:
            file_stat = os.lstat(path)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 16 * 1024 * 1024:
                raise DeliveryStoreError(f"invalid state file: {Path(path).name}")
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except DeliveryStoreError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryStoreError(f"invalid state file: {Path(path).name}") from exc

    def import_inventory(self, path: str, kind: str) -> int:
        if kind not in {"redeem", "trial"}:
            raise ValueError("unsupported inventory kind")
        payload = self._load_json(path)
        if payload is None:
            raise DeliveryStoreError(
                f"required inventory file is missing: {Path(path).name}"
            )
        if not isinstance(payload, list):
            raise DeliveryStoreError(f"inventory file must contain a list: {Path(path).name}")

        rows = []
        seen = set()
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not item["code"].strip():
                raise DeliveryStoreError(f"inventory item is invalid: {Path(path).name}")
            if "used" in item and not isinstance(item["used"], bool):
                raise DeliveryStoreError(f"inventory used flag is invalid: {Path(path).name}")
            secret = item["code"].strip()
            if secret in seen:
                raise DeliveryStoreError(f"inventory contains duplicate entries: {Path(path).name}")
            seen.add(secret)
            rows.append((secret, "legacy_used" if item.get("used", False) else "available"))

        now = self.now_fn()
        with self._transaction() as conn:
            quarantined_inventory = {}
            existing = {
                row["secret"]: row
                for row in conn.execute(
                    "SELECT id, secret, status FROM inventory WHERE kind = ?",
                    (kind,),
                ).fetchall()
            }
            for secret, status in rows:
                current = existing.get(secret)
                if current is None:
                    conn.execute(
                        """
                        INSERT INTO inventory(kind, secret, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (kind, secret, status, now, now),
                    )
                    continue
                if status == "legacy_used" and current["status"] in {
                    "available",
                    "reserved",
                }:
                    replacement = (
                        "legacy_used"
                        if current["status"] == "available"
                        else "quarantined"
                    )
                    conn.execute(
                        """
                        UPDATE inventory
                        SET status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (replacement, now, current["id"]),
                    )
                    self._quarantine_trial_claim(
                        conn, current["id"], "inventory_marked_used", now
                    )
                    quarantined_inventory[current["id"]] = "inventory_marked_used"

            incoming = {secret for secret, _status in rows}
            for secret, current in existing.items():
                if secret in incoming or current["status"] not in {
                    "available",
                    "reserved",
                }:
                    continue
                conn.execute(
                    """
                    UPDATE inventory
                    SET status = 'quarantined', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, current["id"]),
                )
                self._quarantine_trial_claim(
                    conn, current["id"], "inventory_removed_from_manifest", now
                )
                quarantined_inventory[current["id"]] = "inventory_removed_from_manifest"
            self._quarantine_delivery_claims(conn, quarantined_inventory, now)
        return len(rows)

    @staticmethod
    def _quarantine_trial_claim(
        conn: sqlite3.Connection, inventory_id: int, reason: str, now: float
    ) -> None:
        conn.execute(
            """
            UPDATE trial_claims
            SET status = 'manual_review', last_error = ?, updated_at = ?
            WHERE inventory_id = ? AND status IN ('reserved', 'retry', 'sending')
            """,
            (reason, now, inventory_id),
        )

    def _quarantine_delivery_claims(
        self,
        conn: sqlite3.Connection,
        inventory_reasons: dict[int, str],
        now: float,
    ) -> None:
        """Move orders tied to revoked inventory into durable manual review."""
        if not inventory_reasons:
            return
        rows = conn.execute(
            """
            SELECT order_key, inventory_ids
            FROM delivery_events
            WHERE delivery_type = 'redeem'
              AND status IN ('reserved', 'retry', 'sending')
            """
        ).fetchall()
        revoked_ids = set(inventory_reasons)
        for row in rows:
            order_inventory_ids = self._decode_ids(row["inventory_ids"])
            affected = [item_id for item_id in order_inventory_ids if item_id in revoked_ids]
            if not affected:
                continue
            reason = inventory_reasons[affected[0]]
            conn.execute(
                """
                UPDATE delivery_events
                SET status = 'manual_review', last_error = ?, send_started_at = NULL,
                    updated_at = ?
                WHERE order_key = ? AND status IN ('reserved', 'retry', 'sending')
                """,
                (reason, now, row["order_key"]),
            )
            if order_inventory_ids:
                placeholders = ",".join("?" for _ in order_inventory_ids)
                conn.execute(
                    f"""
                    UPDATE inventory
                    SET status = 'quarantined', updated_at = ?
                    WHERE id IN ({placeholders})
                      AND status = 'reserved' AND reservation_key = ?
                    """,
                    (now, *order_inventory_ids, row["order_key"]),
                )
            self._upsert_manual_review(conn, row["order_key"], reason, now)

    def import_trial_claims(self, path: str) -> int:
        payload = self._load_json(path)
        if payload is None:
            return 0
        if not isinstance(payload, dict):
            raise DeliveryStoreError(f"trial claims file must contain an object: {Path(path).name}")

        imported = 0
        claimed_inventory = set()
        now = self.now_fn()
        with self._transaction() as conn:
            for buyer_id, record in payload.items():
                if not str(buyer_id).strip() or not isinstance(record, dict):
                    raise DeliveryStoreError(f"trial claim is invalid: {Path(path).name}")
                if not isinstance(record.get("code"), str) or not record["code"].strip():
                    raise DeliveryStoreError(f"trial claim code is invalid: {Path(path).name}")
                inventory = conn.execute(
                    "SELECT id FROM inventory WHERE kind = 'trial' AND secret = ?",
                    (record["code"].strip(),),
                ).fetchone()
                if inventory is None:
                    raise DeliveryStoreError(f"trial claim references unknown inventory: {Path(path).name}")
                if inventory["id"] in claimed_inventory:
                    raise DeliveryStoreError(f"trial inventory has multiple claimants: {Path(path).name}")
                claimed_inventory.add(inventory["id"])
                existing_claim = conn.execute(
                    "SELECT inventory_id FROM trial_claims WHERE buyer_id = ?",
                    (str(buyer_id),),
                ).fetchone()
                if (
                    existing_claim is not None
                    and existing_claim["inventory_id"] != inventory["id"]
                ):
                    raise DeliveryStoreError(
                        f"trial buyer references different inventory: {Path(path).name}"
                    )
                other_claim = conn.execute(
                    """
                    SELECT buyer_id FROM trial_claims
                    WHERE inventory_id = ? AND buyer_id != ?
                    LIMIT 1
                    """,
                    (inventory["id"], str(buyer_id)),
                ).fetchone()
                if other_claim is not None:
                    raise DeliveryStoreError(
                        f"trial inventory has multiple claimants: {Path(path).name}"
                    )
                conn.execute(
                    """
                    UPDATE inventory
                    SET status = 'delivered', buyer_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(buyer_id), now, inventory["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO trial_claims(
                        buyer_id, inventory_id, status, created_at, updated_at, delivered_at
                    ) VALUES (?, ?, 'delivered', ?, ?, ?)
                    ON CONFLICT(buyer_id) DO UPDATE SET
                        status = 'delivered', last_error = NULL,
                        updated_at = excluded.updated_at,
                        delivered_at = excluded.delivered_at
                    """,
                    (str(buyer_id), inventory["id"], now, now, now),
                )
                imported += 1
        return imported

    def inventory_counts(self) -> dict[str, dict[str, int]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT kind, status, COUNT(*) AS count FROM inventory GROUP BY kind, status"
            ).fetchall()
        finally:
            conn.close()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["kind"], {})[row["status"]] = row["count"]
        return result

    @classmethod
    def _encode_inbound_payload(cls, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise ValueError("inbound event payload must be an object")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("inbound event payload is not JSON serializable") from exc
        if len(encoded.encode("utf-8")) > cls.MAX_INBOUND_EVENT_BYTES:
            raise ValueError("inbound event payload is too large")
        return encoded

    def record_inbound_event(
        self, event_key: str, chat_id: str, payload: dict
    ) -> InboundEvent:
        event_key = str(event_key)
        chat_id = str(chat_id)
        if not event_key or len(event_key) > self.MAX_INBOUND_EVENT_KEY_LENGTH:
            raise ValueError("inbound event key is invalid")
        if not chat_id or len(chat_id) > self.MAX_CHAT_ID_LENGTH:
            raise ValueError("inbound event chat ID is invalid")
        encoded = self._encode_inbound_payload(payload)
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                DELETE FROM inbound_events
                WHERE status IN ('completed', 'ignored', 'dead_letter') AND completed_at < ?
                """,
                (now - self.INBOUND_COMPLETED_RETENTION,),
            )
            existing = conn.execute(
                """
                SELECT event_key, chat_id, payload, payload_hash, status
                FROM inbound_events WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
            if existing is None:
                active_count, active_bytes = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(length(CAST(payload AS BLOB))), 0)
                    FROM inbound_events
                    WHERE status IN ('pending', 'processing', 'dead_letter')
                    """
                ).fetchone()
                chat_count = conn.execute(
                    """
                    SELECT COUNT(*) FROM inbound_events
                    WHERE chat_id = ?
                      AND status IN ('pending', 'processing', 'dead_letter')
                    """,
                    (chat_id,),
                ).fetchone()[0]
                if active_count >= self.MAX_INBOUND_QUEUE_EVENTS:
                    raise DeliveryStoreError("inbound queue event capacity reached")
                if active_bytes + len(encoded.encode("utf-8")) > self.MAX_INBOUND_QUEUE_BYTES:
                    raise DeliveryStoreError("inbound queue byte capacity reached")
                if chat_count >= self.MAX_INBOUND_EVENTS_PER_CHAT:
                    raise DeliveryStoreError("inbound chat rate capacity reached")
                conn.execute(
                    """
                    INSERT INTO inbound_events(
                        event_key, chat_id, payload, payload_hash,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (event_key, chat_id, encoded, payload_hash, now, now),
                )
            row = conn.execute(
                """
                SELECT event_key, chat_id, payload, payload_hash, status
                FROM inbound_events WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
        if row["chat_id"] != chat_id or row["payload_hash"] != payload_hash:
            raise DeliveryStoreError("inbound event key collision")
        return InboundEvent(row["event_key"], row["chat_id"], row["payload"], row["status"])

    def claim_inbound_event(self, event_key: str) -> Optional[InboundEvent]:
        """Claim an event only when every earlier event for its chat is complete."""
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT rowid AS event_sequence, *
                FROM inbound_events WHERE event_key = ?
                """,
                (str(event_key),),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown inbound event")
            if row["status"] != "pending" or row["next_attempt_at"] > now:
                return None
            earlier = conn.execute(
                """
                SELECT 1 FROM inbound_events
                WHERE chat_id = ? AND status IN ('pending', 'processing', 'dead_letter')
                  AND (
                    created_at < ? OR
                    (created_at = ? AND rowid < ?)
                  )
                LIMIT 1
                """,
                (
                    row["chat_id"],
                    row["created_at"],
                    row["created_at"],
                    row["event_sequence"],
                ),
            ).fetchone()
            if earlier is not None:
                return None
            cursor = conn.execute(
                """
                UPDATE inbound_events
                SET status = 'processing', attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE event_key = ? AND status = 'pending'
                """,
                (now, str(event_key)),
            )
            if cursor.rowcount != 1:
                return None
            return InboundEvent(
                row["event_key"], row["chat_id"], row["payload"], "processing"
            )

    def complete_inbound_event(self, event_key: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM inbound_events WHERE event_key = ?",
                (str(event_key),),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown inbound event")
            if row["status"] in {"completed", "ignored"}:
                return
            if row["status"] != "processing":
                raise DeliveryStoreError("inbound event is not owned by a worker")
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'completed', payload = '{}', last_error = NULL,
                    updated_at = ?, completed_at = ?
                WHERE event_key = ?
                """,
                (now, now, str(event_key)),
            )

    def ignore_inbound_event(self, event_key: str, reason: str) -> None:
        """Close an intentionally ignored event while retaining an audit reason."""
        clean_reason = self._clean_error(reason).strip()
        if not clean_reason:
            raise ValueError("ignored inbound event reason is required")
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM inbound_events WHERE event_key = ?",
                (str(event_key),),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown inbound event")
            if row["status"] == "ignored":
                return
            if row["status"] == "completed":
                raise DeliveryStoreError("completed inbound event cannot be ignored")
            if row["status"] != "processing":
                raise DeliveryStoreError("inbound event is not owned by a worker")
            conn.execute(
                """
                UPDATE inbound_events
                SET status = 'ignored', payload = '{}', last_error = ?,
                    updated_at = ?, completed_at = ?
                WHERE event_key = ? AND status = 'processing'
                """,
                (clean_reason, now, now, str(event_key)),
            )

    def requeue_inbound_event(self, event_key: str, error: str) -> str:
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, attempt_count FROM inbound_events WHERE event_key = ?",
                (str(event_key),),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown inbound event")
            if row["status"] == "pending":
                return "pending"
            if row["status"] != "processing":
                raise DeliveryStoreError("inbound event is not owned by a worker")
            if row["attempt_count"] >= self.MAX_INBOUND_ATTEMPTS:
                conn.execute(
                    """
                    UPDATE inbound_events
                    SET status = 'dead_letter', last_error = ?,
                        updated_at = ?, completed_at = ?
                    WHERE event_key = ? AND status = 'processing'
                    """,
                    (self._clean_error(error), now, now, str(event_key)),
                )
                return "dead_letter"
            delay = min(
                self.INBOUND_RETRY_BASE_SECONDS * (2 ** (row["attempt_count"] - 1)),
                300,
            )
            cursor = conn.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', last_error = ?,
                    next_attempt_at = ?, updated_at = ?
                WHERE event_key = ? AND status = 'processing'
                """,
                (
                    self._clean_error(error),
                    now + delay,
                    now,
                    str(event_key),
                ),
            )
            if cursor.rowcount != 1:
                raise DeliveryStoreError("inbound event could not be requeued")
            return "pending"

    def pending_inbound_events(self, ready_only: bool = False) -> list[InboundEvent]:
        conn = self._connect()
        try:
            if ready_only:
                rows = conn.execute(
                    """
                    SELECT candidate.event_key, candidate.chat_id,
                           candidate.payload, candidate.status
                    FROM inbound_events AS candidate
                    WHERE candidate.status = 'pending'
                      AND candidate.next_attempt_at <= ?
                      AND NOT EXISTS (
                        SELECT 1 FROM inbound_events AS earlier
                        WHERE earlier.chat_id = candidate.chat_id
                          AND earlier.status IN ('pending', 'processing', 'dead_letter')
                          AND (
                            earlier.created_at < candidate.created_at OR
                            (earlier.created_at = candidate.created_at
                             AND earlier.rowid < candidate.rowid)
                          )
                      )
                    ORDER BY candidate.created_at, candidate.rowid
                    LIMIT ?
                    """,
                    (self.now_fn(), self.MAX_PENDING_SCAN),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_key, chat_id, payload, status
                    FROM inbound_events
                    WHERE status = 'pending'
                    ORDER BY created_at, rowid
                    LIMIT ?
                    """,
                    (self.MAX_PENDING_SCAN,),
                ).fetchall()
        finally:
            conn.close()
        return [
            InboundEvent(row["event_key"], row["chat_id"], row["payload"], row["status"])
            for row in rows
        ]

    def reserve_llm_budget(self, chat_id: str, input_chars: int) -> bool:
        """Atomically reserve one bounded model call for a chat and globally."""
        if not isinstance(input_chars, int) or isinstance(input_chars, bool):
            raise ValueError("LLM input size is invalid")
        if input_chars < 0 or input_chars > self.MAX_LLM_INPUT_CHARS_GLOBAL:
            raise ValueError("LLM input size is out of range")
        scope_chat = f"chat:{str(chat_id)}"
        now = self.now_fn()
        window_start = int(now // self.LLM_WINDOW_SECONDS) * self.LLM_WINDOW_SECONDS
        limits = (
            (scope_chat, self.MAX_LLM_CALLS_PER_CHAT, self.MAX_LLM_INPUT_CHARS_PER_CHAT),
            ("global", self.MAX_LLM_CALLS_GLOBAL, self.MAX_LLM_INPUT_CHARS_GLOBAL),
        )
        with self._transaction() as conn:
            # Keep one bounded row per active chat window. Without this cleanup,
            # a long-lived service would accumulate an unbounded row per chat.
            conn.execute(
                "DELETE FROM llm_usage WHERE window_start < ?",
                (window_start,),
            )
            current = {}
            for scope, _call_limit, _char_limit in limits:
                row = conn.execute(
                    "SELECT window_start, call_count, input_chars FROM llm_usage WHERE scope = ?",
                    (scope,),
                ).fetchone()
                if row is None or row["window_start"] != window_start:
                    current[scope] = [window_start, 0, 0]
                else:
                    current[scope] = [
                        row["window_start"], row["call_count"], row["input_chars"]
                    ]
            for scope, call_limit, char_limit in limits:
                _window, calls, chars = current[scope]
                if calls + 1 > call_limit or chars + input_chars > char_limit:
                    return False
            for scope, _call_limit, _char_limit in limits:
                _window, calls, chars = current[scope]
                conn.execute(
                    """
                    INSERT INTO llm_usage(scope, window_start, call_count, input_chars, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET
                        window_start = excluded.window_start,
                        call_count = excluded.call_count,
                        input_chars = excluded.input_chars,
                        updated_at = excluded.updated_at
                    """,
                    (scope, window_start, calls + 1, chars + input_chars, now),
                )
        return True

    def dead_letter_inbound_events(self, limit: int = 100) -> list[dict]:
        """Return dead-letter metadata without exposing persisted buyer payloads."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("dead-letter limit is invalid")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT event_key, chat_id, attempt_count, last_error,
                       created_at, updated_at, completed_at
                FROM inbound_events
                WHERE status = 'dead_letter'
                ORDER BY created_at, rowid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        records = []
        for row in rows:
            record = dict(row)
            record["chat_ref"] = hashlib.sha256(
                str(record.pop("chat_id")).encode("utf-8")
            ).hexdigest()[:10]
            error = record.get("last_error")
            record["last_error"] = (
                error
                if error in self.SAFE_ERROR_TYPES
                else "stored_error"
            )
            records.append(record)
        return records

    def dead_letter_inbound_count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE status = 'dead_letter'"
            ).fetchone()[0]
        finally:
            conn.close()

    def requeue_dead_letter_event(self, event_key: str) -> bool:
        """Reset one retained dead letter so the ordered worker can retry it."""
        now = self.now_fn()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', attempt_count = 0,
                    next_attempt_at = 0, last_error = NULL,
                    updated_at = ?, completed_at = NULL
                WHERE event_key = ? AND status = 'dead_letter'
                """,
                (now, str(event_key)),
            )
        return cursor.rowcount == 1

    def discard_dead_letter_event(self, event_key: str, reason: str) -> bool:
        """Resolve one dead letter after an operator intentionally discards it."""
        clean_reason = self._clean_error(reason).strip()
        if not clean_reason:
            raise ValueError("dead-letter resolution is required")
        now = self.now_fn()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE inbound_events
                SET status = 'completed', payload = '{}',
                    last_error = ?, updated_at = ?, completed_at = ?
                WHERE event_key = ? AND status = 'dead_letter'
                """,
                (f"discarded:{clean_reason}"[:240], now, now, str(event_key)),
            )
        return cursor.rowcount == 1

    def record_chat_binding(
        self,
        chat_id: str,
        buyer_id: str,
        item_id: str,
        observed_at: Optional[float] = None,
    ) -> bool:
        if not chat_id or not buyer_id or not item_id:
            raise ValueError("chat binding requires chat, buyer, and item IDs")
        observed_at = self.now_fn() if observed_at is None else observed_at
        try:
            observed_at = float(observed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("chat binding timestamp is invalid") from exc
        if not math.isfinite(observed_at):
            raise ValueError("chat binding timestamp is invalid")
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT buyer_id, item_id, conflicted FROM chat_bindings WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if existing is not None and (
                existing["buyer_id"] != str(buyer_id)
                or existing["item_id"] != str(item_id)
                or existing["conflicted"]
            ):
                conn.execute(
                    "UPDATE chat_bindings SET conflicted = 1, observed_at = ? WHERE chat_id = ?",
                    (observed_at, str(chat_id)),
                )
                return False
            conn.execute(
                """
                INSERT INTO chat_bindings(chat_id, buyer_id, item_id, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    buyer_id = excluded.buyer_id,
                    item_id = excluded.item_id,
                    observed_at = excluded.observed_at,
                    conflicted = 0
                """,
                (str(chat_id), str(buyer_id), str(item_id), observed_at),
            )
        return True

    def get_chat_binding(
        self,
        chat_id: str,
        max_age: float = 30 * 86400,
        event_at: Optional[float] = None,
        max_event_delta: Optional[float] = None,
    ) -> Optional[dict]:
        if max_age <= 0:
            raise ValueError("chat binding max age must be positive")
        if max_event_delta is not None and max_event_delta <= 0:
            raise ValueError("chat binding event delta must be positive")
        now = self.now_fn()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT buyer_id, item_id, observed_at FROM chat_bindings
                WHERE chat_id = ? AND conflicted = 0
                """,
                (str(chat_id),),
            ).fetchone()
        finally:
            conn.close()
        if row is None or now - row["observed_at"] > max_age:
            return None
        if (
            event_at is not None
            and max_event_delta is not None
            and abs(row["observed_at"] - event_at) > max_event_delta
        ):
            return None
        return dict(row)

    def record_payment_event(
        self,
        order_key: str,
        chat_id: str,
        event_at: float,
        ttl_seconds: float,
    ) -> PaymentEvent:
        if not order_key or not chat_id:
            raise ValueError("payment event requires a key and chat ID")
        if ttl_seconds <= 0:
            raise ValueError("payment event TTL must be positive")
        now = self.now_fn()
        try:
            event_at = float(event_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("payment event timestamp is invalid") from exc
        if not math.isfinite(event_at):
            raise ValueError("payment event timestamp is invalid")
        if (
            event_at < 0
            or event_at > now + 86_400
            or now - event_at > 10 * 365 * 86_400
        ):
            raise ValueError("payment event timestamp is outside the accepted range")
        # A delayed or replayed platform event must not receive a fresh TTL.
        expires_at = min(now + ttl_seconds, event_at + ttl_seconds)
        initial_status = "awaiting_binding" if expires_at >= now else "expired"
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events(
                    order_key, chat_id, status, event_at, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_key) DO NOTHING
                """,
                (order_key, str(chat_id), initial_status, event_at, expires_at, now, now),
            )
            row = conn.execute(
                "SELECT order_key, chat_id, status, event_at, expires_at FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()
        if row["chat_id"] != str(chat_id):
            raise DeliveryStoreError("payment event key collision")
        return PaymentEvent(
            row["order_key"], row["chat_id"], row["status"], row["event_at"], row["expires_at"]
        )

    def record_verified_payment_event(
        self,
        order_key: str,
        chat_id: str,
        event_at: float,
        ttl_seconds: float,
        *,
        platform_order_id: str,
        platform_status: str,
        paid_amount: str,
        quantity: int = 1,
    ) -> PaymentEvent:
        """Persist platform proof before inventory can be reserved."""
        platform_order_id = str(platform_order_id)
        if (
            not platform_order_id
            or len(platform_order_id) > 64
            or not platform_order_id.isascii()
            or not platform_order_id.isdigit()
        ):
            raise ValueError("platform order ID is invalid")
        platform_status = str(platform_status).strip()
        paid_amount = str(paid_amount).strip()
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("order quantity is invalid")
        # 范围校验(1..50)由核验与发货准备阶段强制执行;
        # 这里仅持久化平台事实,便于异常数量订单保留审计证据。
        if not platform_status or len(platform_status) > 64:
            raise ValueError("platform order status is invalid")
        if not paid_amount or len(paid_amount) > 64:
            raise ValueError("paid amount is invalid")

        self.record_payment_event(order_key, chat_id, event_at, ttl_seconds)
        now = self.now_fn()
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
                ).fetchone()
                if row is None:
                    raise DeliveryStoreError("payment event disappeared")
                if row["chat_id"] != str(chat_id):
                    raise DeliveryStoreError("payment event chat mismatch")
                if row["platform_order_id"] not in (None, platform_order_id):
                    raise DeliveryStoreError("payment event order identity mismatch")
                if row["platform_status"] not in (None, platform_status):
                    raise DeliveryStoreError("payment event platform status mismatch")
                if row["paid_amount"] not in (None, paid_amount):
                    raise DeliveryStoreError("payment event paid amount mismatch")
                if row["quantity"] not in (None, quantity, 1) and row["status"] != "awaiting_binding":
                    raise DeliveryStoreError("payment event quantity mismatch")
                next_status = (
                    "verified" if row["status"] == "awaiting_binding" else row["status"]
                )
                conn.execute(
                    """
                    UPDATE delivery_events
                    SET platform_order_id = ?, verified_at = COALESCE(verified_at, ?),
                        platform_status = ?, paid_amount = ?, quantity = ?,
                        status = ?, updated_at = ?
                    WHERE order_key = ?
                    """,
                    (
                        platform_order_id,
                        now,
                        platform_status,
                        paid_amount,
                        quantity,
                        next_status,
                        now,
                        order_key,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT order_key, chat_id, status, event_at, expires_at
                    FROM delivery_events WHERE order_key = ?
                    """,
                    (order_key,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DeliveryStoreError("platform order is already bound") from exc
        return PaymentEvent(
            row["order_key"], row["chat_id"], row["status"], row["event_at"], row["expires_at"]
        )

    def awaiting_events_for_chat(self, chat_id: str) -> list[PaymentEvent]:
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE delivery_events
                SET status = 'expired', updated_at = ?
                WHERE status = 'awaiting_binding' AND expires_at < ?
                """,
                (now, now),
            )
            rows = conn.execute(
                """
                SELECT order_key, chat_id, status, event_at, expires_at
                FROM delivery_events
                WHERE chat_id = ? AND status = 'awaiting_binding' AND expires_at >= ?
                ORDER BY created_at
                """,
                (str(chat_id), now),
            ).fetchall()
        return [
            PaymentEvent(
                row["order_key"], row["chat_id"], row["status"], row["event_at"], row["expires_at"]
            )
            for row in rows
        ]

    def awaiting_events(self) -> list[PaymentEvent]:
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE delivery_events SET status = 'expired', updated_at = ?
                WHERE status = 'awaiting_binding' AND expires_at < ?
                """,
                (now, now),
            )
            rows = conn.execute(
                """
                SELECT order_key, chat_id, status, event_at, expires_at
                FROM delivery_events
                WHERE status = 'awaiting_binding' AND expires_at >= ?
                ORDER BY created_at
                """,
                (now,),
            ).fetchall()
        return [
            PaymentEvent(
                row["order_key"], row["chat_id"], row["status"], row["event_at"], row["expires_at"]
            )
            for row in rows
        ]

    def mark_order_manual_review(self, order_key: str, reason: str) -> None:
        now = self.now_fn()
        clean_reason = self._clean_error(reason)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, inventory_ids FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()
            if row is None or row["status"] == "delivered":
                return
            inventory_status = (
                "quarantined" if row["status"] in {"retry", "sending"} else "available"
            )
            for inventory_id in self._decode_ids(row["inventory_ids"]):
                if inventory_status == "available":
                    conn.execute(
                        """
                        UPDATE inventory
                        SET status = 'available', reservation_key = NULL,
                            buyer_id = NULL, updated_at = ?
                        WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                        """,
                        (now, inventory_id, order_key),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE inventory SET status = 'quarantined', updated_at = ?
                        WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                        """,
                        (now, inventory_id, order_key),
                    )
            cursor = conn.execute(
                """
                UPDATE delivery_events
                SET status = 'manual_review', last_error = ?,
                    send_started_at = NULL, updated_at = ?
                WHERE order_key = ? AND status != 'delivered'
                """,
                (clean_reason, now, order_key),
            )
            if cursor.rowcount:
                self._upsert_manual_review(conn, order_key, clean_reason, now)

    @staticmethod
    def _upsert_manual_review(
        conn: sqlite3.Connection, order_key: str, reason: str, now: float
    ) -> None:
        conn.execute(
            """
            INSERT INTO manual_reviews(
                order_key, status, reason, created_at, updated_at
            ) VALUES (?, 'open', ?, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
                reason = excluded.reason, updated_at = excluded.updated_at
            WHERE manual_reviews.status = 'open'
            """,
            (str(order_key), reason, now, now),
        )

    def pending_manual_reviews(self, limit: int = 100) -> list[ManualReview]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("manual review limit is invalid")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT reviews.order_key, events.chat_id, reviews.reason,
                       events.event_at, reviews.created_at, reviews.updated_at
                FROM manual_reviews AS reviews
                JOIN delivery_events AS events
                  ON events.order_key = reviews.order_key
                WHERE reviews.status = 'open'
                ORDER BY reviews.created_at, reviews.order_key
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            ManualReview(
                row["order_key"],
                row["chat_id"],
                row["reason"],
                row["event_at"],
                row["created_at"],
                row["updated_at"],
            )
            for row in rows
        ]

    def manual_review_count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM manual_reviews WHERE status = 'open'"
            ).fetchone()[0]
        finally:
            conn.close()

    def resolve_manual_review(self, order_key: str, resolution: str) -> bool:
        clean_resolution = self._clean_error(resolution).strip()
        if not clean_resolution:
            raise ValueError("manual review resolution is required")
        now = self.now_fn()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE manual_reviews
                SET status = 'resolved', resolution = ?,
                    updated_at = ?, resolved_at = ?
                WHERE order_key = ? AND status = 'open'
                """,
                (clean_resolution, now, now, str(order_key)),
            )
        return cursor.rowcount == 1

    def quarantine_automatic_orders(self, reason: str) -> int:
        """Fail closed for records created without a verified platform order identity."""
        now = self.now_fn()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT order_key, inventory_ids FROM delivery_events
                WHERE status IN ('awaiting_binding', 'verified', 'reserved', 'retry', 'sending')
                  AND (
                    platform_order_id IS NULL OR verified_at IS NULL
                    OR platform_status IS NULL OR platform_status != '2'
                  )
                """
            ).fetchall()
            for row in rows:
                for inventory_id in self._decode_ids(row["inventory_ids"]):
                    conn.execute(
                        """
                        UPDATE inventory
                        SET status = 'quarantined', updated_at = ?
                        WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                        """,
                        (now, inventory_id, row["order_key"]),
                    )
            cursor = conn.execute(
                """
                UPDATE delivery_events
                SET status = 'manual_review', last_error = ?,
                    send_started_at = NULL, updated_at = ?
                WHERE status IN ('awaiting_binding', 'verified', 'reserved', 'retry', 'sending')
                  AND (
                    platform_order_id IS NULL OR verified_at IS NULL
                    OR platform_status IS NULL OR platform_status != '2'
                  )
                """,
                (self._clean_error(reason), now),
            )
            for row in rows:
                self._upsert_manual_review(
                    conn, row["order_key"], self._clean_error(reason), now
                )
        return cursor.rowcount

    def cancel_awaiting_for_chat(self, chat_id: str) -> int:
        now = self.now_fn()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT order_key, inventory_ids FROM delivery_events
                WHERE chat_id = ? AND status IN ('awaiting_binding', 'verified', 'reserved')
                """,
                (str(chat_id),),
            ).fetchall()
            for row in rows:
                for inventory_id in self._decode_ids(row["inventory_ids"]):
                    conn.execute(
                        """
                        UPDATE inventory
                        SET status = 'available', reservation_key = NULL, buyer_id = NULL, updated_at = ?
                        WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                        """,
                        (now, inventory_id, row["order_key"]),
                    )
            cursor = conn.execute(
                """
                UPDATE delivery_events
                SET status = 'cancelled', updated_at = ?
                WHERE chat_id = ? AND status IN ('awaiting_binding', 'verified', 'reserved')
                """,
                (now, str(chat_id)),
            )
            cancelled = cursor.rowcount
            uncertain_rows = conn.execute(
                """
                SELECT order_key, inventory_ids, status FROM delivery_events
                WHERE chat_id = ? AND status IN ('retry', 'sending')
                """,
                (str(chat_id),),
            ).fetchall()
            for row in uncertain_rows:
                for inventory_id in self._decode_ids(row["inventory_ids"]):
                    conn.execute(
                        """
                        UPDATE inventory
                        SET status = 'quarantined', updated_at = ?
                        WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                        """,
                        (now, inventory_id, row["order_key"]),
                    )
            uncertain = conn.execute(
                """
                UPDATE delivery_events
                SET status = 'manual_review',
                    last_error = CASE
                        WHEN status = 'sending' THEN 'cancelled_during_send'
                        ELSE 'cancelled_after_send_attempt'
                    END,
                    send_started_at = NULL, updated_at = ?
                WHERE chat_id = ? AND status IN ('retry', 'sending')
                """,
                (now, str(chat_id)),
            ).rowcount
            for row in uncertain_rows:
                review_reason = (
                    "cancelled_during_send"
                    if row["status"] == "sending"
                    else "cancelled_after_send_attempt"
                )
                self._upsert_manual_review(
                    conn, row["order_key"], review_reason, now
                )
        return cancelled + uncertain

    def prepare_order(
        self,
        order_key: str,
        chat_id: str,
        buyer_id: str,
        item_id: str,
        delivery_type: str,
        quantity: int = 1,
        delivery_payload: Optional[str] = None,
    ) -> DeliveryReservation:
        if delivery_type not in {"redeem", "pan", "material"}:
            raise ValueError("unsupported delivery type")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("order quantity is invalid")
        if quantity < 1 or quantity > 50:
            raise ValueError("automatic delivery supports 1..50 units per verified payment event")
        if delivery_type == "material" and quantity != 1:
            raise ValueError("material delivery supports exactly one unit")
        if delivery_type in {"pan", "material"}:
            if (
                not isinstance(delivery_payload, str)
                or not delivery_payload.strip()
                or len(delivery_payload.strip()) > self.MAX_DELIVERY_PAYLOAD_CHARS
                or "\x00" in delivery_payload
            ):
                raise ValueError(f"{delivery_type} delivery requires a valid persisted payload")
            delivery_payload = delivery_payload.strip()
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("payment event was not recorded")
            if row["chat_id"] != str(chat_id):
                raise DeliveryStoreError("payment event chat mismatch")
            if (
                row["platform_order_id"] is None
                or row["verified_at"] is None
                or row["platform_status"] != "2"
                or row["paid_amount"] is None
            ):
                raise DeliveryStoreError("payment event has no valid platform proof")
            if row["status"] in {"cancelled", "expired", "manual_review", "delivered"}:
                return self._reservation_from_row(conn, row)
            for field, value in (("buyer_id", buyer_id), ("item_id", item_id), ("delivery_type", delivery_type)):
                if row[field] not in (None, str(value)):
                    raise DeliveryStoreError(f"payment event {field} mismatch")
            if row["status"] in {"reserved", "retry", "sending"}:
                if (
                    delivery_type in {"pan", "material"}
                    and row["delivery_payload"] is None
                    and row["status"] in {"reserved", "retry"}
                ):
                    conn.execute(
                        "UPDATE delivery_events SET delivery_payload = ?, updated_at = ? WHERE order_key = ?",
                        (delivery_payload, now, order_key),
                    )
                    row = conn.execute(
                        "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
                    ).fetchone()
                return self._reservation_from_row(conn, row)
            if row["status"] != "verified":
                raise DeliveryStoreError("payment event has an invalid state")

            inventory_ids = self._decode_ids(row["inventory_ids"])
            if delivery_type == "redeem" and not inventory_ids:
                inventory_rows = conn.execute(
                    """
                    SELECT id FROM inventory
                    WHERE kind = 'redeem' AND status = 'available'
                    ORDER BY id LIMIT ?
                    """,
                    (quantity,),
                ).fetchall()
                if len(inventory_rows) < quantity:
                    # 库存不足整单转人工,绝不部分发送
                    conn.execute(
                        """
                        UPDATE delivery_events
                        SET buyer_id = ?, item_id = ?, delivery_type = ?,
                            status = 'manual_review', last_error = 'inventory_empty', updated_at = ?
                        WHERE order_key = ?
                        """,
                        (str(buyer_id), str(item_id), delivery_type, now, order_key),
                    )
                    self._upsert_manual_review(
                        conn, order_key, "inventory_empty", now
                    )
                    row = conn.execute(
                        "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
                    ).fetchone()
                    return self._reservation_from_row(conn, row)
                inventory_ids = [row_inv["id"] for row_inv in inventory_rows]
                for inv_id in inventory_ids:
                    cursor = conn.execute(
                        """
                        UPDATE inventory
                        SET status = 'reserved', reservation_key = ?, buyer_id = ?, updated_at = ?
                        WHERE id = ? AND status = 'available'
                        """,
                        (order_key, str(buyer_id), now, inv_id),
                    )
                    if cursor.rowcount != 1:
                        raise DeliveryStoreError("failed to reserve inventory")

            conn.execute(
                """
                UPDATE delivery_events
                SET buyer_id = ?, item_id = ?, delivery_type = ?, delivery_payload = ?, quantity = ?,
                    status = 'reserved', inventory_ids = ?, last_error = NULL, updated_at = ?
                WHERE order_key = ?
                """,
                (
                    str(buyer_id),
                    str(item_id),
                    delivery_type,
                    delivery_payload,
                    quantity,
                    json.dumps(inventory_ids),
                    now,
                    order_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
            ).fetchone()
            return self._reservation_from_row(conn, row)

    def claim_order_for_send(self, order_key: str) -> Optional[DeliveryReservation]:
        """Atomically grant one sender ownership of a prepared delivery."""
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown payment event")
            if row["status"] not in {"reserved", "retry"}:
                return None
            cursor = conn.execute(
                """
                UPDATE delivery_events
                SET status = 'sending', send_started_at = ?, updated_at = ?
                WHERE order_key = ? AND status IN ('reserved', 'retry')
                """,
                (now, now, order_key),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
            ).fetchone()
            return self._reservation_from_row(conn, row)

    def get_order(self, order_key: str) -> Optional[DeliveryReservation]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE order_key = ?", (order_key,)
            ).fetchone()
            return self._reservation_from_row(conn, row) if row else None
        finally:
            conn.close()

    def order_inventory_is_sendable(self, order_key: str) -> bool:
        """Recheck the live reservation immediately before a redeem send."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status, delivery_type, quantity, inventory_ids FROM delivery_events WHERE order_key = ?",
                (str(order_key),),
            ).fetchone()
            if row is None or row["status"] != "sending":
                return False
            if row["delivery_type"] != "redeem":
                return True
            inventory_ids = self._decode_ids(row["inventory_ids"])
            if len(inventory_ids) != int(row["quantity"] or 1):
                return False
            if not inventory_ids:
                return False
            placeholders = ",".join("?" for _ in inventory_ids)
            count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM inventory
                WHERE id IN ({placeholders})
                  AND status = 'reserved' AND reservation_key = ?
                """,
                (*inventory_ids, str(order_key)),
            ).fetchone()["count"]
            return int(count) == len(inventory_ids)
        finally:
            conn.close()

    def mark_platform_shipped(self, order_key: str) -> None:
        """记录平台已发货(无需邮寄),订单状态保持不变。"""
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown payment event")
            if row["status"] != "delivered":
                raise DeliveryStoreError("only delivered orders can be marked platform shipped")
            conn.execute(
                """
                UPDATE delivery_events
                SET platform_shipped_at = ?, last_error = NULL, updated_at = ?
                WHERE order_key = ? AND platform_shipped_at IS NULL
                """,
                (now, now, order_key),
            )

    def record_platform_ship_attempt(self, order_key: str, error: str) -> None:
        """记录一次平台发货失败;已达上限后不再重试。"""
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE delivery_events
                SET platform_ship_attempts = platform_ship_attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE order_key = ?
                """,
                (self._clean_error(f"platform_ship:{error}"), now, order_key),
            )

    def pending_platform_shipments(
        self, max_attempts: int = 5
    ) -> list[DeliveryReservation]:
        """已发码但平台尚未发货、且未超重试上限的订单。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status = 'delivered'
                    AND platform_order_id IS NOT NULL
                    AND platform_shipped_at IS NULL
                    AND platform_ship_attempts < ?
                ORDER BY delivered_at
                """,
                (max_attempts,),
            ).fetchall()
            return [self._reservation_from_row(conn, row) for row in rows]
        finally:
            conn.close()

    def revive_takeover_blocked_orders(self) -> list[DeliveryReservation]:
        """恢复因人工接管被搁置的已核验订单,使其回到可自动发货状态。"""
        now = self.now_fn()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT order_key FROM delivery_events
                WHERE status = 'manual_review'
                    AND last_error = 'manual_takeover_before_send'
                    AND platform_status = '2'
                    AND verified_at IS NOT NULL
                """
            ).fetchall()
            keys = []
            for row in rows:
                conn.execute(
                    """
                    UPDATE delivery_events
                    SET status = 'verified', last_error = NULL, updated_at = ?
                    WHERE order_key = ?
                    """,
                    (now, row["order_key"]),
                )
                conn.execute(
                    """
                    UPDATE manual_reviews
                    SET status = 'resolved', resolution = 'auto_revived_fulfill',
                        resolved_at = ?, updated_at = ?
                    WHERE order_key = ? AND status = 'open'
                    """,
                    (now, now, row["order_key"]),
                )
                keys.append(row["order_key"])
        return [order for order in (self.get_order(key) for key in keys) if order is not None]

    def retryable_orders(self) -> list[DeliveryReservation]:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('reserved', 'retry')
                    AND buyer_id IS NOT NULL AND item_id IS NOT NULL AND delivery_type IS NOT NULL
                    AND platform_order_id IS NOT NULL AND verified_at IS NOT NULL
                    AND platform_status = '2' AND paid_amount IS NOT NULL
                ORDER BY created_at
                """
            ).fetchall()
            return [self._reservation_from_row(conn, row) for row in rows]

    def mark_order_retry(self, order_key: str, error: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE delivery_events
                SET status = 'retry', last_error = ?, send_started_at = NULL, updated_at = ?
                WHERE order_key = ? AND status = 'sending'
                """,
                (self._clean_error(error), now, order_key),
            )

    def mark_order_delivered(self, order_key: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, inventory_ids FROM delivery_events WHERE order_key = ?",
                (order_key,),
            ).fetchone()
            if row is None:
                raise DeliveryStoreError("unknown payment event")
            if row["status"] == "delivered":
                return
            if row["status"] != "sending":
                raise DeliveryStoreError("payment event is not owned by a sender")
            inventory_ids = self._decode_ids(row["inventory_ids"])
            for inventory_id in inventory_ids:
                cursor = conn.execute(
                    """
                    UPDATE inventory
                    SET status = 'delivered', updated_at = ?
                    WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                    """,
                    (now, inventory_id, order_key),
                )
                if cursor.rowcount != 1:
                    raise DeliveryStoreError("reserved inventory state is inconsistent")
            conn.execute(
                """
                UPDATE delivery_events
                SET status = 'delivered', last_error = NULL, send_started_at = NULL,
                    updated_at = ?, delivered_at = ?
                WHERE order_key = ?
                """,
                (now, now, order_key),
            )

    def prepare_trial(self, buyer_id: str) -> DeliveryReservation:
        buyer_id = str(buyer_id)
        now = self.now_fn()
        with self._transaction() as conn:
            claim = conn.execute(
                "SELECT * FROM trial_claims WHERE buyer_id = ?", (buyer_id,)
            ).fetchone()
            if claim is None:
                inventory = conn.execute(
                    """
                    SELECT id FROM inventory
                    WHERE kind = 'trial' AND status = 'available'
                    ORDER BY id LIMIT 1
                    """
                ).fetchone()
                if inventory is None:
                    return DeliveryReservation(
                        key=f"trial:{buyer_id}",
                        status="manual_review",
                        delivery_type="trial",
                        chat_id="",
                        buyer_id=buyer_id,
                        item_id="",
                resources=(),
                payload=None,
                reason="inventory_empty",
                    )
                conn.execute(
                    """
                    UPDATE inventory
                    SET status = 'reserved', reservation_key = ?, buyer_id = ?, updated_at = ?
                    WHERE id = ? AND status = 'available'
                    """,
                    (f"trial:{buyer_id}", buyer_id, now, inventory["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO trial_claims(buyer_id, inventory_id, status, created_at, updated_at)
                    VALUES (?, ?, 'reserved', ?, ?)
                    """,
                    (buyer_id, inventory["id"], now, now),
                )
                claim = conn.execute(
                    "SELECT * FROM trial_claims WHERE buyer_id = ?", (buyer_id,)
                ).fetchone()
            resource = conn.execute(
                "SELECT secret FROM inventory WHERE id = ?", (claim["inventory_id"],)
            ).fetchone()
            return DeliveryReservation(
                key=f"trial:{buyer_id}",
                status=claim["status"],
                delivery_type="trial",
                chat_id="",
                buyer_id=buyer_id,
                item_id="",
                resources=(resource["secret"],) if resource else (),
                payload=None,
                reason=claim["last_error"],
            )

    def claim_trial_for_send(self, buyer_id: str) -> Optional[DeliveryReservation]:
        buyer_id = str(buyer_id)
        now = self.now_fn()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE trial_claims
                SET status = 'sending', updated_at = ?
                WHERE buyer_id = ? AND status IN ('reserved', 'retry')
                """,
                (now, buyer_id),
            )
            if cursor.rowcount != 1:
                return None
            claim = conn.execute(
                "SELECT * FROM trial_claims WHERE buyer_id = ?", (buyer_id,)
            ).fetchone()
            resource = conn.execute(
                "SELECT secret FROM inventory WHERE id = ?", (claim["inventory_id"],)
            ).fetchone()
            return DeliveryReservation(
                key=f"trial:{buyer_id}",
                status=claim["status"],
                delivery_type="trial",
                chat_id="",
                buyer_id=buyer_id,
                item_id="",
                resources=(resource["secret"],) if resource else (),
                payload=None,
                reason=claim["last_error"],
            )

    def mark_trial_retry(self, buyer_id: str, error: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE trial_claims
                SET status = 'retry', last_error = ?, updated_at = ?
                WHERE buyer_id = ? AND status = 'sending'
                """,
                (self._clean_error(error), now, str(buyer_id)),
            )

    def mark_trial_manual_review(self, buyer_id: str, reason: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            claim = conn.execute(
                """
                SELECT inventory_id, status FROM trial_claims
                WHERE buyer_id = ?
                """,
                (str(buyer_id),),
            ).fetchone()
            if claim is None or claim["status"] in {"delivered", "manual_review"}:
                return
            if claim["status"] not in {"reserved", "retry", "sending"}:
                raise DeliveryStoreError("trial claim has an invalid state")
            conn.execute(
                """
                UPDATE inventory
                SET status = 'quarantined', updated_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (now, claim["inventory_id"]),
            )
            conn.execute(
                """
                UPDATE trial_claims
                SET status = 'manual_review', last_error = ?, updated_at = ?
                WHERE buyer_id = ?
                """,
                (self._clean_error(reason), now, str(buyer_id)),
            )

    def mark_trial_delivered(self, buyer_id: str) -> None:
        now = self.now_fn()
        with self._transaction() as conn:
            claim = conn.execute(
                "SELECT inventory_id, status FROM trial_claims WHERE buyer_id = ?",
                (str(buyer_id),),
            ).fetchone()
            if claim is None:
                raise DeliveryStoreError("unknown trial claim")
            if claim["status"] == "delivered":
                return
            if claim["status"] != "sending":
                raise DeliveryStoreError("trial claim is not owned by a sender")
            cursor = conn.execute(
                """
                UPDATE inventory SET status = 'delivered', updated_at = ?
                WHERE id = ? AND status = 'reserved' AND reservation_key = ?
                """,
                (now, claim["inventory_id"], f"trial:{buyer_id}"),
            )
            if cursor.rowcount != 1:
                raise DeliveryStoreError("reserved trial inventory state is inconsistent")
            conn.execute(
                """
                UPDATE trial_claims
                SET status = 'delivered', last_error = NULL, updated_at = ?, delivered_at = ?
                WHERE buyer_id = ?
                """,
                (now, now, str(buyer_id)),
            )

    def mark_automation_reply_sent(self, chat_id: str, sent_at: Optional[float] = None) -> float:
        timestamp = self.now_fn() if sent_at is None else float(sent_at)
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM automation_reply_state WHERE updated_at < ?",
                (timestamp - 7 * 86400,),
            )
            conn.execute(
                """INSERT INTO automation_reply_state(chat_id, last_reply_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       last_reply_at = excluded.last_reply_at,
                       updated_at = excluded.updated_at""",
                (str(chat_id), timestamp, timestamp),
            )
        return timestamp

    def automation_last_reply_at(self, chat_id: str) -> Optional[float]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_reply_at FROM automation_reply_state WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else float(row["last_reply_at"])

    @staticmethod
    def _manual_exit_key(chat_id: str, exited_at: float, source: str) -> str:
        digest = hashlib.sha256(f"{chat_id}:{exited_at:.6f}".encode("utf-8")).hexdigest()[:24]
        return f"{source}:{digest}"

    def set_manual_mode(self, chat_id: str, enabled: bool, timeout_seconds: float) -> None:
        if enabled and timeout_seconds <= 0:
            raise ValueError("manual mode timeout must be positive")
        chat_id = str(chat_id)
        now = self.now_fn()
        with self._transaction() as conn:
            if enabled:
                conn.execute(
                    """
                    INSERT INTO manual_modes(chat_id, expires_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, now + timeout_seconds, now),
                )
            else:
                existed = conn.execute(
                    "SELECT 1 FROM manual_modes WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
                conn.execute("DELETE FROM manual_modes WHERE chat_id = ?", (chat_id,))
                if existed is not None:
                    conn.execute(
                        """INSERT OR IGNORE INTO manual_control_events(
                               control_key, chat_id, mode, created_at
                           ) VALUES (?, ?, 'auto', ?)""",
                        (self._manual_exit_key(chat_id, now, "direct-auto"), chat_id, now),
                    )

    def toggle_manual_mode_once(
        self, control_key: str, chat_id: str, timeout_seconds: float
    ) -> tuple[str, bool]:
        if not control_key or not chat_id:
            raise ValueError("manual control key and chat ID are required")
        if timeout_seconds <= 0:
            raise ValueError("manual mode timeout must be positive")
        now = self.now_fn()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM manual_control_events WHERE created_at < ?",
                (now - self.IDEMPOTENCY_RETENTION_SECONDS,),
            )
            previous = conn.execute(
                """
                SELECT chat_id, mode FROM manual_control_events
                WHERE control_key = ?
                """,
                (str(control_key),),
            ).fetchone()
            if previous is not None:
                if previous["chat_id"] != str(chat_id):
                    raise DeliveryStoreError("manual control key collision")
                return previous["mode"], False
            conn.execute("DELETE FROM manual_modes WHERE expires_at <= ?", (now,))
            enabled = conn.execute(
                "SELECT 1 FROM manual_modes WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
            if enabled is None:
                mode = "manual"
                conn.execute(
                    """
                    INSERT INTO manual_modes(chat_id, expires_at, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (str(chat_id), now + timeout_seconds, now),
                )
            else:
                mode = "auto"
                conn.execute(
                    "DELETE FROM manual_modes WHERE chat_id = ?", (str(chat_id),)
                )
            conn.execute(
                """
                INSERT INTO manual_control_events(
                    control_key, chat_id, mode, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (str(control_key), str(chat_id), mode, now),
            )
        return mode, True

    def is_manual_mode(self, chat_id: str) -> bool:
        now = self.now_fn()
        with self._transaction() as conn:
            expired = conn.execute(
                "SELECT chat_id, expires_at FROM manual_modes WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            for row in expired:
                expired_chat_id = str(row["chat_id"])
                exited_at = float(row["expires_at"])
                conn.execute(
                    """INSERT OR IGNORE INTO manual_control_events(
                           control_key, chat_id, mode, created_at
                       ) VALUES (?, ?, 'auto', ?)""",
                    (
                        self._manual_exit_key(expired_chat_id, exited_at, "expiry-auto"),
                        expired_chat_id,
                        exited_at,
                    ),
                )
            conn.execute("DELETE FROM manual_modes WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT 1 FROM manual_modes WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        return row is not None

    def manual_exit_at(self, chat_id: str) -> Optional[float]:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT MAX(created_at) AS exited_at
                   FROM manual_control_events
                   WHERE chat_id = ? AND mode = 'auto'""",
                (str(chat_id),),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["exited_at"] is None:
            return None
        return float(row["exited_at"])

    @staticmethod
    def _decode_ids(payload: str) -> list[int]:
        try:
            values = json.loads(payload or "[]")
        except json.JSONDecodeError as exc:
            raise DeliveryStoreError("delivery inventory state is corrupt") from exc
        if not isinstance(values, list) or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in values
        ):
            raise DeliveryStoreError("delivery inventory state is invalid")
        return values

    @staticmethod
    def _clean_error(error: str) -> str:
        value = str(error).replace("\n", " ").strip()
        if value in DeliveryStore.SAFE_ERROR_TYPES:
            return value
        if value.startswith("discarded:") and value.partition(":")[2] in {
            "invalid_event",
            "duplicate_event",
            "not_actionable",
            "operator_cancelled",
        }:
            return value
        return "stored_error"

    def _reservation_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> DeliveryReservation:
        inventory_ids = self._decode_ids(row["inventory_ids"])
        resources: Sequence[str] = ()
        if inventory_ids:
            placeholders = ",".join("?" for _ in inventory_ids)
            records = conn.execute(
                f"SELECT id, secret FROM inventory WHERE id IN ({placeholders}) ORDER BY id",
                tuple(inventory_ids),
            ).fetchall()
            resources = tuple(record["secret"] for record in records)
        return DeliveryReservation(
            key=row["order_key"],
            status=row["status"],
            delivery_type=row["delivery_type"] or "",
            chat_id=row["chat_id"],
            buyer_id=row["buyer_id"] or "",
            item_id=row["item_id"] or "",
            resources=tuple(resources),
            quantity=row["quantity"],
            payload=row["delivery_payload"],
            reason=row["last_error"],
            platform_order_id=row["platform_order_id"],
            verified_at=row["verified_at"],
            platform_status=row["platform_status"],
            paid_amount=row["paid_amount"],
        )

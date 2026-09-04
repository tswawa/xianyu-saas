"""租户展示数据读取:商品、聊天与发货记录。"""
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from datetime import date, datetime, timedelta

from account_storage import AccountStorage, AccountStorageError, DEFAULT_ACCOUNT_ID
from shop_sync import load_verified_snapshot

TENANTS_ROOT = os.environ.get("SAAS_TENANTS_DIR", "/var/lib/xianyu-saas/tenants")
_MEDIA_TYPES = frozenset({"image", "emoji", "audio", "video", "file", "link", "unknown"})
_MANUAL_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_MANUAL_IMAGE_MIME = {
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/gif": ("gif", (b"GIF87a", b"GIF89a")),
    "image/webp": ("webp", None),
}
_MANUAL_IMAGE_NAME = re.compile(
    r"^manual_reply_[0-9a-f]{32}\.(?:jpg|png|gif|webp)$"
)
_MANUAL_IMAGE_TTL_SECONDS = 24 * 60 * 60
_MANUAL_IMAGE_CLEANUP_SCAN_LIMIT = 256
_MANUAL_IMAGE_CLEANUP_DELETE_LIMIT = 64
_MANUAL_REPLY_ACTIVE_STATUSES = frozenset({"queued", "retry", "sending", "manual_review"})


def _normalise_media(value, *, include_private=False):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(value, list):
        return []

    def bounded_int(raw, maximum):
        try:
            number = int(raw)
        except (TypeError, ValueError, OverflowError):
            return 0
        return number if 0 <= number <= maximum else 0

    out = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "unknown").strip().lower()
        if kind not in _MEDIA_TYPES:
            kind = "unknown"
        url = str(raw.get("url") or "").strip()
        if url and (not url.startswith("https://") or len(url) > 2048):
            url = ""
        path = str(raw.get("path") or "").strip()
        if path and (
            len(path) > 255 or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            path = ""
        item = {
            "type": kind,
            "url": url,
            "alt": str(raw.get("alt") or "")[:160],
            "width": bounded_int(raw.get("width"), 10000),
            "height": bounded_int(raw.get("height"), 10000),
            "duration_ms": bounded_int(raw.get("duration_ms"), 86_400_000),
            "label": str(raw.get("label") or "")[:160],
        }
        if include_private and path:
            item["path"] = path
        if raw.get("mime"):
            item["mime"] = str(raw.get("mime"))[:80]
        if raw.get("name"):
            item["name"] = str(raw.get("name"))[:160]
        out.append(item)
    return out


def _media_json(value):
    return json.dumps(_normalise_media(value, include_private=True), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_manual_image(user_id: int, data: bytes, filename: str = "", mime: str = "", account_key: str = DEFAULT_ACCOUNT_ID):
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > _MANUAL_IMAGE_MAX_BYTES:
        raise ValueError("图片为空或超过 8 MB")
    payload = bytes(data)
    supplied_mime = str(mime or "").split(";", 1)[0].strip().lower()
    detected_mime = ""
    extension = ""
    for candidate, (suffix, signature) in _MANUAL_IMAGE_MIME.items():
        if candidate == "image/webp":
            if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
                detected_mime, extension = candidate, suffix
        elif isinstance(signature, tuple):
            if any(payload.startswith(item) for item in signature):
                detected_mime, extension = candidate, suffix
        elif payload.startswith(signature):
            detected_mime, extension = candidate, suffix
        if detected_mime:
            break
    if not detected_mime or (supplied_mime and supplied_mime != detected_mime):
        raise ValueError("仅支持有效的 JPG、PNG、GIF 或 WebP 图片")
    root = _account_root(user_id, account_key, create=True)
    # Sweep old orphans before creating the new file so even a zero/clock-skewed
    # cutoff can never remove the upload that this call is about to return.
    cleanup_stale_manual_images(user_id, account_key)
    stored_name = f"manual_reply_{uuid.uuid4().hex}.{extension}"
    target = os.path.join(root, stored_name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    display_name = os.path.basename(str(filename or "").replace("\\", "/"))[:160]
    return {
        "type": "image",
        "url": "",
        "path": stored_name,
        "alt": display_name or "图片",
        "label": display_name or "图片",
        "mime": detected_mime,
        "name": display_name or "图片",
    }


def _account_root(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID, *, create: bool = False) -> str:
    storage = AccountStorage(TENANTS_ROOT)
    try:
        path = (
            storage.ensure_account_dir(user_id, account_key)
            if create
            else storage.account_dir(user_id, account_key)
        )
    except AccountStorageError as error:
        raise OSError("invalid account storage path") from error
    return str(path)


def _connect(user_id: int, name: str, account_key: str = DEFAULT_ACCOUNT_ID):
    try:
        path = os.path.join(_account_root(user_id, account_key), name)
    except OSError:
        return None
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _connect_writable(user_id: int, name: str, account_key: str = DEFAULT_ACCOUNT_ID, *, create=False):
    """Open one account-local state database for a short write transaction.

    The normal readers intentionally use SQLite's read-only URI.  Inbox state
    is the small exception: read cursors and takeover commands live beside the
    worker's account files, so they survive an API restart without entering
    the control-plane database or crossing shop boundaries.
    """
    try:
        root = _account_root(user_id, account_key, create=create)
        path = os.path.join(root, name)
    except OSError:
        return None
    if not create and not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 10000")
        return con
    except sqlite3.Error:
        return None


def _manual_image_filename(value) -> str:
    name = str(value or "").strip()
    if not _MANUAL_IMAGE_NAME.fullmatch(name):
        return ""
    return name


def _manual_reply_media_names(value):
    """Return ordered private image names, or ``None`` for an unknown shape."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(value, list) or len(value) > 8:
        return None
    names = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        if str(raw.get("type") or "").strip().lower() != "image":
            return None
        raw_path = raw.get("path")
        if raw_path in (None, ""):
            continue
        name = _manual_image_filename(raw_path)
        if not name:
            return None
        names.append(name)
    return names


def _manual_image_is_active(con, name: str) -> bool:
    if con is None:
        return False
    name = _manual_image_filename(name)
    if not name:
        return True
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manual_reply_drafts'"
    ).fetchone()
    if exists is None:
        return False
    columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(manual_reply_drafts)").fetchall()
    }
    if "status" not in columns or "media_json" not in columns:
        # An unknown legacy shape cannot prove that the file is unreferenced.
        return True
    parts_available = _table_exists(con, "manual_reply_parts")
    rows = con.execute(
        """SELECT id, media_json FROM manual_reply_drafts
           WHERE status IN ('queued', 'retry', 'sending', 'manual_review')"""
    ).fetchall()
    for row in rows:
        names = _manual_reply_media_names(row["media_json"])
        if names is None:
            return True
        matching_indexes = [index for index, candidate in enumerate(names) if candidate == name]
        if not matching_indexes:
            continue
        if not parts_available:
            return True
        for media_index in matching_indexes:
            part = con.execute(
                """SELECT acknowledged_at FROM manual_reply_parts
                   WHERE outbox_id = ? AND kind = 'image' AND media_index = ?""",
                (int(row["id"]), media_index),
            ).fetchone()
            if part is None or part["acknowledged_at"] is None:
                return True
    return False


def _unlink_manual_image_at(root: str, name: str) -> bool:
    name = _manual_image_filename(name)
    if not name:
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        try:
            file_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(file_stat.st_mode):
            return False
        os.unlink(name, dir_fd=root_fd)
        return True
    finally:
        os.close(root_fd)


def manual_image_delete_status(
    user_id: int,
    path: str,
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> str:
    """Return deleted/not_found/active/invalid/unavailable for one private image."""
    name = _manual_image_filename(path)
    if not name:
        return "invalid"
    try:
        root = os.path.realpath(_account_root(user_id, account_key))
    except OSError:
        return "unavailable"
    if not os.path.isdir(root):
        return "not_found"
    database_path = os.path.join(root, "chat_history.db")
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None and os.path.exists(database_path):
        # Never delete without checking references when the account database
        # exists but cannot be opened or locked safely.
        return "unavailable"
    try:
        if con is not None:
            con.execute("BEGIN IMMEDIATE")
            if _manual_image_is_active(con, name):
                con.rollback()
                return "active"
        deleted = _unlink_manual_image_at(root, name)
        if con is not None:
            con.commit()
        return "deleted" if deleted else "not_found"
    except (OSError, sqlite3.Error):
        if con is not None:
            con.rollback()
        return "unavailable"
    finally:
        if con is not None:
            con.close()


def delete_manual_image(
    user_id: int,
    path: str,
    account_key: str = DEFAULT_ACCOUNT_ID,
) -> bool:
    """Delete one unreferenced account-local temporary image."""
    return manual_image_delete_status(user_id, path, account_key) == "deleted"


def cleanup_stale_manual_images(
    user_id: int,
    account_key: str = DEFAULT_ACCOUNT_ID,
    *,
    ttl_seconds: float = _MANUAL_IMAGE_TTL_SECONDS,
) -> int:
    """Boundedly remove expired, unreferenced account-local manual reply images."""
    try:
        ttl_seconds = max(float(ttl_seconds), 0.0)
        root = os.path.realpath(_account_root(user_id, account_key))
        cutoff = time.time() - ttl_seconds
        candidates = []
        with os.scandir(root) as entries:
            for inspected, entry in enumerate(entries, start=1):
                if inspected > _MANUAL_IMAGE_CLEANUP_SCAN_LIMIT:
                    break
                name = _manual_image_filename(entry.name)
                if not name:
                    continue
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(file_stat.st_mode) and file_stat.st_mtime <= cutoff:
                    candidates.append((file_stat.st_mtime, name))
        deleted = 0
        for _mtime, name in sorted(candidates)[:_MANUAL_IMAGE_CLEANUP_DELETE_LIMIT]:
            if delete_manual_image(user_id, name, account_key):
                deleted += 1
        return deleted
    except (OSError, TypeError, ValueError):
        return 0


def _table_exists(con, table_name: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def inventory_counts(user_id: int, kind: str, account_key: str = DEFAULT_ACCOUNT_ID):
    """Return worker-authoritative inventory counts when its DB exists."""
    if kind != "redeem":
        raise ValueError("unsupported inventory kind")
    con = _connect(user_id, "delivery_state.db", account_key)
    if con is None:
        return None
    try:
        if not _table_exists(con, "inventory"):
            return None
        rows = con.execute(
            "SELECT status, COUNT(*) AS count FROM inventory WHERE kind = ? GROUP BY status",
            (kind,),
        ).fetchall()
        return {str(row["status"]): int(row["count"] or 0) for row in rows}
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _ensure_conversation_state(con):
    """Create only bounded control metadata; never copy message contents."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS conversation_state (
               chat_id TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               last_read_message_id INTEGER NOT NULL DEFAULT 0,
               takeover_enabled INTEGER NOT NULL DEFAULT 0,
               takeover_expires_at REAL,
               updated_at REAL NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS conversation_controls (
               event_id TEXT PRIMARY KEY,
               chat_id TEXT NOT NULL,
               action TEXT NOT NULL,
               enabled INTEGER,
               created_at REAL NOT NULL,
               applied_at REAL,
               status TEXT NOT NULL DEFAULT 'applied'
           )"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_conversation_controls_chat
           ON conversation_controls(chat_id, created_at)"""
    )


_MANUAL_REPLY_EXTRA_COLUMNS = {
    "request_id": "TEXT",
    "payload_digest": "TEXT",
    "recipient_id": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'draft'",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 10",
    "available_at": "REAL NOT NULL DEFAULT 0",
    "lease_owner": "TEXT",
    "lease_until": "REAL",
    "last_error_code": "TEXT",
    "media_json": "TEXT NOT NULL DEFAULT '[]'",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "acknowledged_at": "REAL",
}

_MANUAL_REPLY_FULL_RETENTION = 2000
_MANUAL_REPLY_TOMBSTONE_RETENTION = 50_000


class ManualReplyQueueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _normalise_manual_reply_media_input(media):
    if media is None:
        return []
    if not isinstance(media, list) or len(media) > 8:
        raise ManualReplyQueueError("invalid_media", "人工回复最多支持 8 张图片")
    clean_media = []
    seen_paths = set()
    for raw in media:
        if not isinstance(raw, dict) or str(raw.get("type") or "").strip().lower() != "image":
            raise ManualReplyQueueError("invalid_media", "人工回复只支持 JPG、PNG、GIF 或 WebP 图片")
        name = _manual_image_filename(raw.get("path"))
        if not name or name in seen_paths:
            raise ManualReplyQueueError("invalid_media", "图片已失效，请重新选择")
        normalized = _normalise_media([raw], include_private=True)
        if len(normalized) != 1 or normalized[0].get("path") != name:
            raise ManualReplyQueueError("invalid_media", "图片已失效，请重新选择")
        seen_paths.add(name)
        normalized[0]["type"] = "image"
        # Manual uploads are always sent from the account-local temporary file;
        # a caller-provided remote URL is neither trusted nor part of delivery.
        normalized[0]["url"] = ""
        clean_media.append(normalized[0])
    return clean_media


def _normalise_manual_reply_private_media(media):
    """Strictly parse every private image without the public 8-item truncation."""
    if media is None:
        raw_items = []
    elif isinstance(media, str):
        try:
            raw_items = json.loads(media)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise sqlite3.IntegrityError("manual reply media is invalid") from error
    else:
        raw_items = media
    if not isinstance(raw_items, list) or len(raw_items) > 8:
        raise sqlite3.IntegrityError("manual reply media is invalid")
    normalized = _normalise_media(raw_items, include_private=True)
    if len(normalized) != len(raw_items):
        raise sqlite3.IntegrityError("manual reply media is invalid")
    seen_paths = set()
    for raw, item in zip(raw_items, normalized):
        if (
            not isinstance(raw, dict)
            or str(raw.get("type") or "").strip().lower() != "image"
        ):
            raise sqlite3.IntegrityError("manual reply media is invalid")
        path = _manual_image_filename(raw.get("path"))
        if not path or item.get("path") != path or path in seen_paths:
            raise sqlite3.IntegrityError("manual reply media is invalid")
        seen_paths.add(path)
    return normalized


def _validate_manual_reply_media(user_id: int, account_key: str, media):
    clean_media = _normalise_manual_reply_media_input(media)
    if not clean_media:
        return []
    try:
        root = os.path.realpath(_account_root(user_id, account_key))
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise ManualReplyQueueError("invalid_media", "图片已失效，请重新选择") from error
    try:
        for item in clean_media:
            name = item["path"]
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=root_fd)
                try:
                    file_stat = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(file_stat.st_mode)
                        or file_stat.st_size < 1
                        or file_stat.st_size > _MANUAL_IMAGE_MAX_BYTES
                    ):
                        raise OSError("invalid media file")
                    header = os.read(descriptor, 16)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise ManualReplyQueueError("invalid_media", "图片已失效，请重新选择") from error
            detected_mime = ""
            extension = name.rsplit(".", 1)[-1]
            for candidate, (suffix, signature) in _MANUAL_IMAGE_MIME.items():
                if candidate == "image/webp":
                    matched = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
                elif isinstance(signature, tuple):
                    matched = any(header.startswith(item) for item in signature)
                else:
                    matched = header.startswith(signature)
                if matched and suffix == extension:
                    detected_mime = candidate
                    break
            supplied_mime = str(item.get("mime") or "").split(";", 1)[0].strip().lower()
            if not detected_mime or (supplied_mime and supplied_mime != detected_mime):
                raise ManualReplyQueueError("invalid_media", "图片已失效，请重新选择")
            item["mime"] = detected_mime
    finally:
        os.close(root_fd)
    return clean_media


def _ensure_manual_reply_outbox(con):
    """Migrate the old draft table into a durable account-local outbox."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS manual_reply_drafts (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               item_id TEXT NOT NULL,
               content TEXT NOT NULL,
               created_at DATETIME NOT NULL,
               chat_id TEXT NOT NULL,
               request_id TEXT,
               payload_digest TEXT,
               recipient_id TEXT,
               status TEXT NOT NULL DEFAULT 'draft',
               attempts INTEGER NOT NULL DEFAULT 0,
               max_attempts INTEGER NOT NULL DEFAULT 10,
               available_at REAL NOT NULL DEFAULT 0,
               lease_owner TEXT,
               lease_until REAL,
               last_error_code TEXT,
               media_json TEXT NOT NULL DEFAULT '[]',
               updated_at REAL NOT NULL DEFAULT 0,
               acknowledged_at REAL
           )"""
    )
    columns = {
        str(row[1]) for row in con.execute("PRAGMA table_info(manual_reply_drafts)").fetchall()
    }
    for name, definition in _MANUAL_REPLY_EXTRA_COLUMNS.items():
        if name not in columns:
            con.execute(f"ALTER TABLE manual_reply_drafts ADD COLUMN {name} {definition}")
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_manual_reply_drafts_chat
           ON manual_reply_drafts(chat_id, created_at)"""
    )
    con.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_reply_request
           ON manual_reply_drafts(request_id)
           WHERE request_id IS NOT NULL AND request_id != ''"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_manual_reply_available
           ON manual_reply_drafts(status, available_at, id)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS manual_reply_parts (
               outbox_id INTEGER NOT NULL,
               part_index INTEGER NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('image', 'text')),
               media_index INTEGER,
               acknowledged_at REAL,
               sent_media_json TEXT NOT NULL DEFAULT '[]',
               PRIMARY KEY (outbox_id, part_index)
           )"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS idx_manual_reply_parts_pending
           ON manual_reply_parts(outbox_id, acknowledged_at, part_index)"""
    )


def _manual_reply_part_specs(content, media):
    images = _normalise_manual_reply_private_media(media)
    parts = [(index, "image", index) for index, _item in enumerate(images)]
    if str(content or "").strip():
        parts.append((len(parts), "text", None))
    return parts


def _manual_reply_part_rows(con, outbox_id: int):
    return con.execute(
        """SELECT outbox_id, part_index, kind, media_index,
                  acknowledged_at, sent_media_json
           FROM manual_reply_parts
           WHERE outbox_id = ? ORDER BY part_index""",
        (int(outbox_id),),
    ).fetchall()


def _ensure_manual_reply_parts(con, row):
    if row is None:
        return []
    outbox_id = int(row["id"])
    parts = _manual_reply_part_rows(con, outbox_id)
    status = str(row["status"] or "draft")
    if _manual_reply_is_tombstone(row):
        return parts
    specs = _manual_reply_part_specs(row["content"], row["media_json"])
    if not parts and status in _MANUAL_REPLY_ACTIVE_STATUSES:
        if not specs:
            raise sqlite3.IntegrityError("manual reply has no deliverable parts")
        con.executemany(
            """INSERT OR IGNORE INTO manual_reply_parts(
                   outbox_id, part_index, kind, media_index,
                   acknowledged_at, sent_media_json
               ) VALUES (?, ?, ?, ?, NULL, '[]')""",
            [(outbox_id, part_index, kind, media_index) for part_index, kind, media_index in specs],
        )
        parts = _manual_reply_part_rows(con, outbox_id)
    if not parts:
        return parts
    actual = [
        (
            int(part["part_index"]),
            str(part["kind"]),
            None if part["media_index"] is None else int(part["media_index"]),
        )
        for part in parts
    ]
    if actual != specs:
        raise sqlite3.IntegrityError("manual reply parts do not match the parent payload")
    pending_seen = False
    for part in parts:
        if part["acknowledged_at"] is None:
            pending_seen = True
        elif pending_seen:
            raise sqlite3.IntegrityError("manual reply acknowledgements are out of order")
    if status == "acknowledged" and pending_seen:
        raise sqlite3.IntegrityError("acknowledged manual reply has a pending part")
    return parts


def _manual_reply_parts_payload(status: str, parts):
    parts = list(parts or [])
    raw_status = str(status or "draft")
    current_part = None
    for part in parts:
        if part["acknowledged_at"] is None:
            current_part = int(part["part_index"])
            break
    # Legacy acknowledged rows deliberately have no parts.  Modern rows derive
    # completion from their parts, while an inconsistent acknowledged parent is
    # exposed fail-closed until its unfinished part is handled.
    effective_status = raw_status
    if parts and current_part is None:
        effective_status = "acknowledged"
    elif parts and raw_status == "acknowledged":
        effective_status = "manual_review"
    payload = []
    for part in parts:
        part_index = int(part["part_index"])
        if part["acknowledged_at"] is not None:
            part_status = "acknowledged"
        elif part_index == current_part:
            part_status = effective_status
        else:
            part_status = "waiting"
        payload.append(
            {
                "index": part_index,
                "kind": str(part["kind"]),
                "status": part_status,
            }
        )
    return effective_status, current_part, payload


def _manual_reply_digest(chat_id: str, content: str, media=None) -> str:
    clean_media = _normalise_media(media, include_private=True)
    digest_payload = [str(chat_id or ""), str(content or "")]
    if clean_media:
        digest_payload.append(clean_media)
    payload = json.dumps(
        digest_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manual_reply_is_tombstone(row) -> bool:
    if row is None or str(row["status"] or "") != "acknowledged":
        return False
    if (
        str(row["chat_id"] or "")
        or str(row["item_id"] or "")
        or str(row["content"] or "")
    ):
        return False
    try:
        return not _normalise_manual_reply_private_media(row["media_json"])
    except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
        return False


def _compact_manual_reply_outbox(con) -> None:
    """Bound full acknowledged payloads while retaining digest tombstones."""
    rows = con.execute(
        """SELECT drafts.id, drafts.chat_id, drafts.content, drafts.media_json
           FROM manual_reply_drafts AS drafts
           WHERE drafts.status = 'acknowledged'
             AND COALESCE(drafts.request_id, '') != ''
             AND COALESCE(drafts.chat_id, '') != ''
             AND NOT EXISTS (
                 SELECT 1 FROM manual_reply_parts AS unfinished
                 WHERE unfinished.outbox_id = drafts.id
                   AND unfinished.acknowledged_at IS NULL
             )
             AND drafts.id NOT IN (
                 SELECT kept.id FROM manual_reply_drafts AS kept
                 WHERE kept.status = 'acknowledged'
                   AND COALESCE(kept.request_id, '') != ''
                   AND COALESCE(kept.chat_id, '') != ''
                   AND NOT EXISTS (
                       SELECT 1 FROM manual_reply_parts AS unfinished_kept
                       WHERE unfinished_kept.outbox_id = kept.id
                         AND unfinished_kept.acknowledged_at IS NULL
                   )
                 ORDER BY kept.id DESC LIMIT ?
             )""",
        (_MANUAL_REPLY_FULL_RETENTION,),
    ).fetchall()
    if rows:
        con.executemany(
            """UPDATE manual_reply_drafts
               SET payload_digest = ?, user_id = '', item_id = '', content = '',
                   chat_id = '', recipient_id = NULL, media_json = '[]', last_error_code = NULL
               WHERE id = ? AND status = 'acknowledged'""",
            [
                (
                    _manual_reply_digest(row["chat_id"], row["content"], row["media_json"]),
                    int(row["id"]),
                )
                for row in rows
            ],
        )
        con.executemany(
            "UPDATE manual_reply_parts SET sent_media_json = '[]' WHERE outbox_id = ?",
            [(int(row["id"]),) for row in rows],
        )
    con.execute(
        """DELETE FROM manual_reply_drafts
           WHERE status = 'acknowledged'
             AND COALESCE(request_id, '') != ''
             AND COALESCE(chat_id, '') = ''
             AND COALESCE(item_id, '') = ''
             AND content = ''
             AND COALESCE(media_json, '[]') = '[]'
             AND COALESCE(payload_digest, '') != ''
             AND id NOT IN (
                 SELECT id FROM manual_reply_drafts
                 WHERE status = 'acknowledged'
                   AND COALESCE(request_id, '') != ''
                   AND COALESCE(chat_id, '') = ''
                   AND COALESCE(item_id, '') = ''
                   AND content = ''
                   AND COALESCE(media_json, '[]') = '[]'
                   AND COALESCE(payload_digest, '') != ''
                 ORDER BY id DESC LIMIT ?
             )""",
        (_MANUAL_REPLY_TOMBSTONE_RETENTION,),
    )
    con.execute(
        """DELETE FROM manual_reply_parts
           WHERE outbox_id NOT IN (SELECT id FROM manual_reply_drafts)"""
    )


def _manual_reply_public_media(value):
    media = _normalise_media(value)
    for item in media:
        item.pop("name", None)
        if item.get("type") == "image" and not item.get("url"):
            item["alt"] = "图片"
            item["label"] = "图片"
    return media


def _mark_manual_reply_invalid_payload(con, row) -> None:
    """Best-effort quarantine for an active malformed parent inside its reader transaction."""
    if row is None or str(row["status"] or "") not in _MANUAL_REPLY_ACTIVE_STATUSES:
        return
    try:
        con.execute(
            """UPDATE manual_reply_drafts
               SET status = 'manual_review', lease_owner = NULL,
                   lease_until = NULL, last_error_code = 'invalid_payload',
                   updated_at = ?
               WHERE id = ?
                 AND status IN ('queued', 'retry', 'sending', 'manual_review')
                 AND (
                     status != 'manual_review'
                     OR lease_owner IS NOT NULL
                     OR lease_until IS NOT NULL
                     OR COALESCE(last_error_code, '') != 'invalid_payload'
                 )""",
            (time.time(), int(row["id"])),
        )
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return


def _manual_reply_redacted_payload(row, *, include_content: bool = False):
    """Return only safe metadata for a malformed manual reply."""
    try:
        attempts = int(row["attempts"] or 0)
    except (TypeError, ValueError, OverflowError):
        attempts = 0
    try:
        updated_at = float(row["updated_at"] or 0)
    except (TypeError, ValueError, OverflowError):
        updated_at = 0.0
    if not math.isfinite(updated_at) or updated_at < 0:
        updated_at = 0.0
    payload = {
        "reply_id": str(row["request_id"] or ""),
        "outbox_id": int(row["id"]),
        "chat_id": str(row["chat_id"] or ""),
        "item_id": str(row["item_id"] or ""),
        "status": "manual_review",
        "attempts": max(attempts, 0),
        "platform_acknowledged": False,
        "current_part": None,
        "parts": [],
        "created_at": str(row["created_at"] or ""),
        "updated_at": updated_at,
    }
    if include_content:
        payload.update(
            {
                "role": "assistant_manual",
                "content": "",
                "content_type": "text",
                "media": [],
                "time": str(row["created_at"] or ""),
                "delivery_status": "manual_review",
            }
        )
    return payload


def _manual_reply_content_type(content, media):
    if not media:
        return "text"
    if len(media) == 1 and media[0].get("type") == "image":
        return "image"
    return "rich"


def _manual_reply_source(value):
    """Parse the legacy/base key and the planned per-part source suffixes."""
    pieces = str(value or "").split(":")
    if len(pieces) < 2 or pieces[0] != "manual_reply" or not pieces[1].isdigit():
        return None
    outbox_id = int(pieces[1])
    if outbox_id < 1:
        return None
    if len(pieces) == 2:
        return outbox_id, "base", 0
    if len(pieces) == 3 and pieces[2] == "text":
        return outbox_id, "text", None
    if (
        len(pieces) == 4
        and pieces[2] == "image"
        and pieces[3].isdigit()
        and 2 <= int(pieces[3]) <= 8
    ):
        return outbox_id, "image", int(pieces[3]) - 1
    return None


def _manual_reply_source_part_index(source, parts):
    if source is None:
        return 0
    _outbox_id, kind, part_index = source
    if kind != "text":
        return int(part_index or 0)
    for part in parts or []:
        if str(part["kind"]) == "text":
            return int(part["part_index"])
    # The canonical text suffix is only used when images precede it.  Keep an
    # orphaned/corrupt source stable after every possible image part.
    return 8


def _manual_reply_pending_content(row, parts):
    private_media = _normalise_manual_reply_private_media(row["media_json"])
    pending_media = []
    text_pending = False
    for part in parts:
        if part["acknowledged_at"] is not None:
            continue
        kind = str(part["kind"])
        if kind == "text":
            text_pending = True
            continue
        if kind != "image" or part["media_index"] is None:
            raise sqlite3.IntegrityError("manual reply part is invalid")
        media_index = int(part["media_index"])
        if (
            media_index < 0
            or media_index >= len(private_media)
            or private_media[media_index].get("type") != "image"
        ):
            raise sqlite3.IntegrityError("manual reply media part is invalid")
        item = dict(private_media[media_index])
        item["url"] = ""
        pending_media.append(item)
    content = _message_content(row["content"]) if text_pending else ""
    return content, _manual_reply_public_media(pending_media)


def _manual_reply_payload(row, *, include_content: bool = False, parts=None):
    if row is None:
        return None
    parts = list(parts or [])
    raw_status = str(row["status"] or "draft")
    status, current_part, parts_payload = _manual_reply_parts_payload(raw_status, parts)
    platform_acknowledged = status == "acknowledged" and current_part is None
    payload = {
        "reply_id": str(row["request_id"] or ""),
        "outbox_id": int(row["id"]),
        "chat_id": str(row["chat_id"] or ""),
        "item_id": str(row["item_id"] or ""),
        "status": status,
        "attempts": int(row["attempts"] or 0),
        "platform_acknowledged": platform_acknowledged,
        "current_part": current_part,
        "parts": parts_payload,
        "created_at": str(row["created_at"] or ""),
        "updated_at": float(row["updated_at"] or 0),
    }
    if include_content:
        if parts and current_part is not None:
            content, media = _manual_reply_pending_content(row, parts)
        elif parts and current_part is None:
            # A completed multi-part parent is not itself a platform message.
            # Return a single accurate part only when the task had exactly one.
            if len(parts) == 1 and str(parts[0]["kind"]) == "text":
                content, media = _message_content(row["content"]), []
            elif len(parts) == 1 and str(parts[0]["kind"]) == "image":
                content = ""
                media = _manual_reply_public_media(parts[0]["sent_media_json"])
            else:
                content, media = "", []
        else:
            content = _message_content(row["content"])
            media = _manual_reply_public_media(
                row["media_json"] if "media_json" in row.keys() else None
            )
        payload.update(
            {
                "role": "assistant_manual_draft" if status == "draft" else "assistant_manual",
                "content": content,
                "content_type": _manual_reply_content_type(content, media),
                "media": media,
                "time": str(row["created_at"] or ""),
                "delivery_status": status,
            }
        )
    return payload


def _state_rows(con):
    if not _table_exists(con, "conversation_state"):
        return {}
    try:
        return {
            str(row["chat_id"]): row
            for row in con.execute(
                "SELECT chat_id, last_read_message_id, takeover_enabled, takeover_expires_at, updated_at FROM conversation_state"
            ).fetchall()
        }
    except sqlite3.Error:
        return {}


def _manual_mode_rows(user_id: int, account_key: str):
    """Read worker takeover state when the delivery database exists."""
    con = _connect(user_id, "delivery_state.db", account_key)
    if con is None or not _table_exists(con, "manual_modes"):
        if con is not None:
            con.close()
        return None
    try:
        now = time.time()
        return {
            str(row["chat_id"]): float(row["expires_at"] or 0)
            for row in con.execute(
                "SELECT chat_id, expires_at FROM manual_modes WHERE expires_at > ?",
                (now,),
            ).fetchall()
        }
    except (sqlite3.Error, TypeError, ValueError):
        return {}
    finally:
        con.close()


def _truncate(text, limit=80):
    if not isinstance(text, str):
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _message_content(text, limit=16000):
    if not isinstance(text, str):
        return ""
    return text.strip()[:limit]


def _message_time_value(value) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = 0.0
    if math.isfinite(numeric) and numeric > 0:
        return numeric
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _buyer_label(value) -> str:
    text = "".join(character for character in str(value or "") if character.isalnum())
    return f"买家 · {text[-6:]}" if text else "买家咨询"


def conversations(
    user_id: int,
    limit: int = 50,
    account_key: str = DEFAULT_ACCOUNT_ID,
    *,
    search: str = "",
    unread_only: bool = False,
):
    """Return account-scoped conversation summaries for the workbench.

    ``last_read_message_id`` is an id cursor rather than a wall-clock value,
    so messages with unusual platform timestamps cannot make an old message
    appear unread again.  The worker's ``manual_modes`` table remains the
    source of truth for an active takeover whenever it exists.
    """
    con = _connect(user_id, "chat_history.db", account_key)
    if con is None:
        return []
    try:
        # Public API callers cap ``limit`` at 200; the internal count helper
        # may inspect a larger bounded window without changing that contract.
        maximum = min(max(int(limit), 1), 1000)
        needle = str(search or "").strip().casefold()[:120]
        # Pull a bounded window before applying a search filter.  This keeps
        # the endpoint predictable while still allowing a match outside the
        # first page of a busy inbox.
        query_limit = min(1000 if (needle or unread_only) else maximum, 1000)
        groups = con.execute(
            """SELECT chat_id, MAX(id) AS last_id, COUNT(*) AS message_count,
                      MAX(CASE WHEN role = 'user' THEN id ELSE 0 END) AS last_inbound_id
               FROM messages
               WHERE COALESCE(chat_id, '') != ''
               GROUP BY chat_id ORDER BY last_id DESC LIMIT ?""",
            (query_limit,),
        ).fetchall()
        states = _state_rows(con)
        manual_modes = _manual_mode_rows(user_id, account_key)
        out = []
        for group in groups:
            chat_id = str(group["chat_id"] or "")
            latest = con.execute(
                """SELECT id, user_id, item_id, role, content, timestamp
                   FROM messages WHERE id = ?""",
                (group["last_id"],),
            ).fetchone()
            if latest is None:
                continue
            state_row = states.get(chat_id)
            read_cursor = int((state_row["last_read_message_id"] if state_row else 0) or 0)
            unread_count_row = con.execute(
                """SELECT COUNT(*) AS total FROM messages
                   WHERE chat_id = ? AND role = 'user' AND id > ?""",
                (chat_id, read_cursor),
            ).fetchone()
            unread_count = int(unread_count_row["total"] or 0)
            worker_expiry = manual_modes.get(chat_id) if manual_modes is not None else None
            if manual_modes is not None:
                takeover = bool(worker_expiry and worker_expiry > time.time())
                takeover_expires_at = worker_expiry if takeover else None
            else:
                state_expiry = float((state_row["takeover_expires_at"] if state_row else 0) or 0)
                takeover = bool(
                    state_row
                    and int(state_row["takeover_enabled"] or 0)
                    and state_expiry > time.time()
                )
                takeover_expires_at = state_expiry if takeover else None
            preview = _truncate(latest["content"])
            buyer = con.execute(
                """SELECT user_id FROM messages
                   WHERE chat_id = ? AND role = 'user'
                   ORDER BY id DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
            buyer_label = _buyer_label(buyer["user_id"] if buyer else latest["user_id"])
            if needle:
                haystack = " ".join(
                    (chat_id, str(latest["item_id"] or ""), buyer_label, preview)
                ).casefold()
                if needle not in haystack:
                    history_match = con.execute(
                        """SELECT 1 FROM messages
                           WHERE chat_id = ?
                             AND INSTR(LOWER(COALESCE(content, '')), LOWER(?)) > 0
                           LIMIT 1""",
                        (chat_id, needle),
                    ).fetchone()
                    if history_match is None:
                        continue
            if unread_only and unread_count <= 0:
                continue
            status = "manual" if takeover else "needs_reply" if unread_count else "handled"
            out.append(
                {
                    "chat_id": chat_id,
                    "item_id": str(latest["item_id"] or ""),
                    "buyer_label": buyer_label,
                    "preview": preview,
                    "time": str(latest["timestamp"] or ""),
                    "message_count": int(group["message_count"] or 0),
                    "last_role": str(latest["role"] or ""),
                    "last_message_id": int(latest["id"] or 0),
                    "last_inbound_id": int(group["last_inbound_id"] or 0),
                    "unread": unread_count > 0,
                    "unread_count": unread_count,
                    "status": status,
                    "takeover": takeover,
                    "takeover_expires_at": takeover_expires_at,
                    "search_match": bool(needle),
                }
            )
            if len(out) >= maximum:
                break
        return out
    except (sqlite3.Error, TypeError, ValueError):
        return []
    finally:
        con.close()


def conversation_unread_totals(
    user_id: int,
    account_key: str = DEFAULT_ACCOUNT_ID,
    *,
    search: str = "",
):
    """Return bounded inbox counts independent of the visible page size."""
    rows = conversations(user_id, 1000, account_key, search=search, unread_only=False)
    return {
        "conversations": sum(1 for row in rows if row.get("unread")),
        "messages": sum(int(row.get("unread_count", 0) or 0) for row in rows),
    }


def messages(
    user_id: int,
    limit: int = 50,
    chat_id: str = "",
    account_key: str = DEFAULT_ACCOUNT_ID,
    *,
    search: str = "",
):
    """Return complete messages for one selected chat, never a mixed inbox."""
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        return []
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_manual_reply_outbox(con)
        maximum = min(max(limit, 1), 200)
        selected = str(chat_id or "").strip()
        needle = str(search or "").strip().casefold()[:120]
        if not selected:
            latest = con.execute(
                """SELECT chat_id FROM messages
                   WHERE COALESCE(chat_id, '') != '' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            selected = str(latest["chat_id"] or "") if latest else ""

        message_columns = {
            str(row[1]) for row in con.execute("PRAGMA table_info(messages)").fetchall()
        }
        source_select = "source_id" if "source_id" in message_columns else "NULL AS source_id"
        type_select = "content_type" if "content_type" in message_columns else "'text' AS content_type"
        media_select = "media_json" if "media_json" in message_columns else "'[]' AS media_json"
        if selected:
            if needle:
                rows = con.execute(
                    f"""SELECT id, role, content, timestamp, chat_id, item_id,
                               {source_select}, {type_select}, {media_select}
                        FROM messages
                        WHERE chat_id = ?
                          AND INSTR(LOWER(COALESCE(content, '')), LOWER(?)) > 0
                        ORDER BY id DESC LIMIT ?""",
                    (selected, needle, maximum),
                ).fetchall()
            else:
                rows = con.execute(
                    f"""SELECT id, role, content, timestamp, chat_id, item_id,
                               {source_select}, {type_select}, {media_select}
                        FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?""",
                    (selected, maximum),
                ).fetchall()
        else:
            rows = con.execute(
                f"""SELECT id, role, content, timestamp, chat_id, item_id,
                           {source_select}, {type_select}, {media_select}
                    FROM messages ORDER BY id DESC LIMIT ?""",
                (maximum,),
            ).fetchall()
        out = []
        manual_parts_seen = set()
        manual_last_sort = {}
        for row in reversed(rows):
            source_id = str(row["source_id"] or "")
            try:
                manual_source = _manual_reply_source(source_id)
            except (TypeError, ValueError, OverflowError):
                manual_source = None
            manual = manual_source is not None
            media = _manual_reply_public_media(row["media_json"])
            content_type = str(row["content_type"] or "text").strip().lower()
            if content_type not in {"text", "image", "emoji", "audio", "video", "file", "link", "rich", "unknown"}:
                content_type = "unknown"
            content = _message_content(row["content"])
            sort_time = _message_time_value(row["timestamp"])
            message = {
                "role": "assistant_manual" if manual else row["role"],
                "content": content,
                "content_type": content_type,
                "media": media,
                "time": str(row["timestamp"] or ""),
                "chat_id": str(row["chat_id"] or ""),
                "item_id": str(row["item_id"] or ""),
                "_sort_key": (sort_time, 0, int(row["id"]), 0),
                **({"delivery_status": "acknowledged"} if manual else {}),
            }
            if manual:
                outbox_id = int(manual_source[0])
                try:
                    parts = _manual_reply_part_rows(con, outbox_id)
                    part_index = _manual_reply_source_part_index(manual_source, parts)
                except (TypeError, ValueError, OverflowError):
                    part_index = 0
                manual_parts_seen.add((outbox_id, part_index))
                manual_last_sort[outbox_id] = max(sort_time, manual_last_sort.get(outbox_id, 0.0))
            if needle:
                message["matched"] = needle in content.casefold()
            out.append(message)
        if selected:
            if needle:
                drafts = con.execute(
                    """SELECT id, request_id, content, created_at, chat_id, item_id,
                              status, attempts, updated_at, media_json
                       FROM manual_reply_drafts
                       WHERE chat_id = ?
                         AND INSTR(LOWER(COALESCE(content, '')), LOWER(?)) > 0
                       ORDER BY id DESC LIMIT ?""",
                    (selected, needle, maximum),
                ).fetchall()
            else:
                drafts = con.execute(
                    """SELECT id, request_id, content, created_at, chat_id, item_id,
                              status, attempts, updated_at, media_json
                       FROM manual_reply_drafts WHERE chat_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (selected, maximum),
                ).fetchall()
        else:
            drafts = con.execute(
                """SELECT id, request_id, content, created_at, chat_id, item_id,
                          status, attempts, updated_at, media_json
                   FROM manual_reply_drafts ORDER BY id DESC LIMIT ?""",
                (maximum,),
            ).fetchall()
        for draft in drafts:
            outbox_id = int(draft["id"])
            created_sort = _message_time_value(draft["created_at"])
            sort_time = max(created_sort, manual_last_sort.get(outbox_id, created_sort))
            try:
                raw_status = str(draft["status"] or "draft")
                parts = _ensure_manual_reply_parts(con, draft)
                status, current_part, parts_payload = _manual_reply_parts_payload(raw_status, parts)
                if parts:
                    if current_part is None:
                        # Every modern part is represented by its independently
                        # acknowledged messages; never synthesize a combined row.
                        continue
                    content, draft_media = _manual_reply_pending_content(draft, parts)
                else:
                    # Legacy drafts and acknowledged rows predate the parts table.
                    # If their historical message already exists, do not duplicate it.
                    if raw_status == "acknowledged" and (outbox_id, 0) in manual_parts_seen:
                        continue
                    content = _message_content(draft["content"])
                    draft_media = _manual_reply_public_media(draft["media_json"])
            except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
                _mark_manual_reply_invalid_payload(con, draft)
                if needle:
                    continue
                message = _manual_reply_redacted_payload(draft, include_content=True)
                message["_sort_key"] = (sort_time, 1, outbox_id, 0)
                out.append(message)
                continue
            if needle and needle not in content.casefold():
                continue
            if not content and not draft_media:
                continue
            message = {
                "role": "assistant_manual_draft" if status == "draft" else "assistant_manual",
                "content": content,
                "content_type": _manual_reply_content_type(content, draft_media),
                "media": draft_media,
                "time": str(draft["created_at"] or ""),
                "chat_id": str(draft["chat_id"] or ""),
                "item_id": str(draft["item_id"] or ""),
                "reply_id": str(draft["request_id"] or ""),
                "outbox_id": outbox_id,
                "delivery_status": status,
                "current_part": current_part,
                "parts": parts_payload,
                "_sort_key": (sort_time, 1, outbox_id, current_part or 0),
            }
            if needle:
                message["matched"] = True
            out.append(message)
        con.commit()
        out.sort(key=lambda item: item["_sort_key"])
        public = []
        for item in out[-maximum:]:
            clean = dict(item)
            clean.pop("_sort_key", None)
            public.append(clean)
        return public
    except (sqlite3.Error, TypeError, ValueError):
        if con.in_transaction:
            con.rollback()
        return []
    finally:
        con.close()


def message_match_count(
    user_id: int,
    chat_id: str,
    search: str,
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    """Count account-scoped text matches across the full selected history."""
    selected = str(chat_id or "").strip()
    needle = str(search or "").strip()[:120]
    if not selected or not needle:
        return 0
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        return 0
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_manual_reply_outbox(con)
        message_row = con.execute(
            """SELECT COUNT(*) AS total FROM messages
               WHERE chat_id = ?
                 AND INSTR(LOWER(COALESCE(content, '')), LOWER(?)) > 0""",
            (selected, needle),
        ).fetchone()
        total = int(message_row["total"] or 0)
        drafts = con.execute(
            """SELECT id, content, media_json, status, chat_id, item_id
               FROM manual_reply_drafts
               WHERE chat_id = ?
                 AND INSTR(LOWER(COALESCE(content, '')), LOWER(?)) > 0""",
            (selected, needle),
        ).fetchall()
        for draft in drafts:
            try:
                parts = _ensure_manual_reply_parts(con, draft)
            except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
                _mark_manual_reply_invalid_payload(con, draft)
                continue
            if not parts:
                if str(draft["status"] or "draft") != "acknowledged":
                    total += 1
                continue
            text_part = next((part for part in parts if str(part["kind"]) == "text"), None)
            if text_part is not None and text_part["acknowledged_at"] is None:
                total += 1
        con.commit()
        return total
    except (sqlite3.Error, TypeError, ValueError):
        if con.in_transaction:
            con.rollback()
        return 0
    finally:
        con.close()


def _price_display(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "价格待同步"
    if not math.isfinite(number) or number <= 0:
        return "价格待同步"
    if number.is_integer():
        return f"¥{int(number)}"
    return f"¥{number:.2f}".rstrip("0").rstrip(".")


def products(user_id: int, limit: int = 500, account_key: str = DEFAULT_ACCOUNT_ID):
    """Return only the account-bound read-only shop snapshot."""
    snapshot = load_verified_snapshot(user_id, account_key)
    if snapshot is None:
        return []
    ordered = [item.copy() for item in snapshot.get("products", []) if isinstance(item, dict)]
    ordered = ordered[: min(max(limit, 1), 500)]
    for card in ordered:
        card["price_display"] = _price_display(card.get("price"))
        card.setdefault("description", "")
        card.setdefault("source", "cookie")
    return ordered


def append_manual_draft(
    user_id: int,
    content: str,
    chat_id: str = "",
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    """Persist a seller-authored draft without claiming platform delivery."""
    try:
        path = os.path.join(_account_root(user_id, account_key), "chat_history.db")
    except OSError:
        return None
    if not os.path.exists(path):
        return None
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        _ensure_manual_reply_outbox(con)
        selected = str(chat_id or "").strip()
        if not selected:
            return None
        target = con.execute(
            """SELECT chat_id, item_id FROM messages
               WHERE chat_id = ? ORDER BY id DESC LIMIT 1""",
            (selected,),
        ).fetchone()
        if target is None:
            return None
        resolved_chat_id = str(target["chat_id"] or "")
        item_id = str(target["item_id"] or "")
        con.execute(
            """INSERT INTO manual_reply_drafts(
                   user_id, item_id, content, created_at, chat_id,
                   status, available_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'draft', 0, ?)""",
            (str(user_id), item_id, content, now, resolved_chat_id, time.time()),
        )
        con.execute(
            """DELETE FROM manual_reply_drafts
               WHERE status = 'draft'
                 AND id NOT IN (
                     SELECT id FROM manual_reply_drafts
                     WHERE status = 'draft' ORDER BY id DESC LIMIT 500
                 )"""
        )
        con.commit()
    except sqlite3.Error:
        con.rollback()
        return None
    finally:
        con.close()
    return {
        "role": "assistant_manual_draft",
        "content": content,
        "time": now,
        "chat_id": resolved_chat_id,
        "item_id": item_id,
    }


def enqueue_manual_reply(
    user_id: int,
    content: str,
    chat_id: str,
    request_id: str,
    account_key: str = DEFAULT_ACCOUNT_ID,
    media=None,
):
    """Queue one seller reply in the selected account's private chat database."""
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        raise ManualReplyQueueError("conversation_not_found", "当前还没有可回复的对话")
    selected = str(chat_id or "").strip()
    key = str(request_id or "").strip()
    clean_media = _normalise_manual_reply_media_input(media)
    if not str(content or "").strip() and not clean_media:
        raise ManualReplyQueueError("invalid_payload", "请输入文字或选择图片")
    payload_digest = _manual_reply_digest(selected, content, clean_media)
    now = time.time()
    created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_manual_reply_outbox(con)
        existing = con.execute(
            """SELECT id, request_id, payload_digest, chat_id, item_id, content, media_json, created_at,
                      status, attempts, updated_at
               FROM manual_reply_drafts WHERE request_id = ?""",
            (key,),
        ).fetchone()
        if existing is not None:
            existing_digest = str(existing["payload_digest"] or "")
            if not existing_digest:
                existing_digest = _manual_reply_digest(
                    existing["chat_id"], existing["content"], existing["media_json"]
                )
                con.execute(
                    "UPDATE manual_reply_drafts SET payload_digest = ? WHERE id = ?",
                    (existing_digest, int(existing["id"])),
                )
            if existing_digest != payload_digest:
                raise ManualReplyQueueError(
                    "idempotency_conflict", "这次发送标识已用于另一条回复，请重新发送"
                )
            parts = _ensure_manual_reply_parts(con, existing)
            con.commit()
            payload = _manual_reply_payload(existing, include_content=True, parts=parts)
            if not str(existing["content"] or ""):
                payload.update(
                    {
                        "chat_id": selected,
                        "content": content,
                        "time": str(existing["created_at"] or ""),
                    }
                )
            return payload

        clean_media = _validate_manual_reply_media(user_id, account_key, clean_media)
        manual_modes = _manual_mode_rows(user_id, account_key)
        if not manual_modes or selected not in manual_modes:
            raise ManualReplyQueueError(
                "manual_takeover_required", "请先人工接管当前对话再发送"
            )
        target = con.execute(
            """SELECT user_id, item_id FROM messages
               WHERE chat_id = ? AND role = 'user'
               ORDER BY id DESC LIMIT 1""",
            (selected,),
        ).fetchone()
        if target is None or not str(target["user_id"] or "").strip():
            raise ManualReplyQueueError("conversation_not_found", "当前还没有可回复的对话")
        cursor = con.execute(
            """INSERT INTO manual_reply_drafts(
                   user_id, item_id, content, created_at, chat_id,
                   request_id, payload_digest, recipient_id, status, attempts, max_attempts,
                   available_at, lease_owner, lease_until, last_error_code, media_json,
                   updated_at, acknowledged_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 10, ?, NULL, NULL, NULL, ?, ?, NULL)""",
            (
                str(user_id),
                str(target["item_id"] or ""),
                content,
                created_at,
                selected,
                key,
                payload_digest,
                str(target["user_id"]),
                now,
                _media_json(clean_media),
                now,
            ),
        )
        row = con.execute(
            """SELECT id, request_id, chat_id, item_id, content, media_json, created_at,
                      status, attempts, updated_at
               FROM manual_reply_drafts WHERE id = ?""",
            (int(cursor.lastrowid),),
        ).fetchone()
        parts = _ensure_manual_reply_parts(con, row)
        con.execute(
            """DELETE FROM manual_reply_drafts
               WHERE status = 'draft'
                 AND COALESCE(request_id, '') = ''
               AND id NOT IN (
                     SELECT id FROM manual_reply_drafts
                     WHERE status = 'draft' AND COALESCE(request_id, '') = ''
                     ORDER BY id DESC LIMIT 2000
                 )"""
        )
        _compact_manual_reply_outbox(con)
        con.commit()
        return _manual_reply_payload(row, include_content=True, parts=parts)
    except ManualReplyQueueError:
        con.rollback()
        raise
    except sqlite3.IntegrityError as error:
        con.rollback()
        raise ManualReplyQueueError(
            "idempotency_conflict", "这次发送标识已被使用，请重新发送"
        ) from error
    except sqlite3.Error as error:
        con.rollback()
        raise ManualReplyQueueError("reply_queue_unavailable", "回复暂时无法排队，请稍后重试") from error
    finally:
        con.close()


def manual_reply_status(
    user_id: int,
    request_id: str,
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        return None
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_manual_reply_outbox(con)
        row = con.execute(
            """SELECT id, request_id, chat_id, item_id, content, media_json,
                      created_at, status, attempts, updated_at
               FROM manual_reply_drafts WHERE request_id = ?""",
            (str(request_id or "").strip(),),
        ).fetchone()
        if row is None:
            con.commit()
            return None
        try:
            parts = _ensure_manual_reply_parts(con, row)
            payload = _manual_reply_payload(row, include_content=False, parts=parts)
        except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
            _mark_manual_reply_invalid_payload(con, row)
            payload = _manual_reply_redacted_payload(row, include_content=False)
        con.commit()
        return payload
    except sqlite3.Error:
        if con.in_transaction:
            con.rollback()
        return None
    finally:
        con.close()


def manual_reply_attention(
    user_id: int,
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    """Return redacted retry/review counts for one account's reply outbox."""
    con = _connect(user_id, "chat_history.db", account_key)
    if con is None or not _table_exists(con, "manual_reply_drafts"):
        if con is not None:
            con.close()
        return []
    try:
        columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(manual_reply_drafts)").fetchall()
        }
        if "status" not in columns:
            return []
        rows = con.execute(
            """SELECT status, COUNT(*) AS total
               FROM manual_reply_drafts
               WHERE status IN ('retry', 'manual_review')
               GROUP BY status"""
        ).fetchall()
        items = []
        for row in rows:
            status = str(row["status"] or "")
            total = int(row["total"] or 0)
            if total <= 0:
                continue
            if status == "manual_review":
                items.append(
                    {
                        "kind": "manual_reply",
                        "code": "manual_reply_review",
                        "severity": "error",
                        "count": total,
                        "message": f"有 {total} 条人工回复需要重新处理。",
                    }
                )
            else:
                items.append(
                    {
                        "kind": "manual_reply",
                        "code": "manual_reply_retry",
                        "severity": "warning",
                        "count": total,
                        "message": f"有 {total} 条人工回复等待重试。",
                    }
                )
        return items
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _conversation_exists(con, chat_id: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def conversation_exists(user_id: int, chat_id: str, account_key: str = DEFAULT_ACCOUNT_ID) -> bool:
    con = _connect(user_id, "chat_history.db", account_key)
    if con is None:
        return False
    try:
        return _conversation_exists(con, str(chat_id or "").strip())
    finally:
        con.close()


def mark_conversation_read(
    user_id: int,
    chat_id: str,
    read: bool = True,
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    """Advance (or reset) one chat's unread cursor and return its summary."""
    selected = str(chat_id or "").strip()
    if not selected:
        return None
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        return None
    try:
        con.execute("BEGIN IMMEDIATE")
        if not _conversation_exists(con, selected):
            con.rollback()
            return None
        latest = con.execute(
            "SELECT COALESCE(MAX(id), 0) AS last_id FROM messages WHERE chat_id = ?",
            (selected,),
        ).fetchone()
        cursor = int(latest["last_id"] or 0) if read else 0
        _ensure_conversation_state(con)
        now = time.time()
        con.execute(
            """INSERT INTO conversation_state(
                   chat_id, user_id, last_read_message_id,
                   takeover_enabled, takeover_expires_at, updated_at
               ) VALUES (?, ?, ?, 0, NULL, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   user_id = excluded.user_id,
                   last_read_message_id = excluded.last_read_message_id,
                   updated_at = excluded.updated_at""",
            (selected, str(user_id), cursor, now),
        )
        con.execute(
            """INSERT INTO conversation_controls(
                   event_id, chat_id, action, enabled, created_at, applied_at, status
               ) VALUES (?, ?, 'read', ?, ?, ?, 'applied')""",
            (f"api:{uuid.uuid4().hex}", selected, 1 if read else 0, now, now),
        )
        con.execute(
            """DELETE FROM conversation_controls
               WHERE rowid NOT IN (
                   SELECT rowid FROM conversation_controls
                   ORDER BY created_at DESC LIMIT 1000
               )"""
        )
        con.commit()
    except sqlite3.Error:
        con.rollback()
        return None
    finally:
        con.close()
    selected_rows = conversations(user_id, 200, account_key)
    return next((row for row in selected_rows if row["chat_id"] == selected), None)


def _set_worker_manual_mode(user_id: int, chat_id: str, enabled: bool, account_key: str):
    """Apply the same chat-scoped mode the worker checks before replying."""
    con = _connect_writable(user_id, "delivery_state.db", account_key, create=True)
    if con is None:
        return None
    now = time.time()
    expires_at = now + 3600 if enabled else None
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """CREATE TABLE IF NOT EXISTS manual_modes (
                   chat_id TEXT PRIMARY KEY,
                   expires_at REAL NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS manual_control_events (
                   control_key TEXT PRIMARY KEY,
                   chat_id TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   created_at REAL NOT NULL
               )"""
        )
        if enabled:
            con.execute(
                """INSERT INTO manual_modes(chat_id, expires_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       expires_at = excluded.expires_at,
                       updated_at = excluded.updated_at""",
                (chat_id, expires_at, now),
            )
            mode = "manual"
        else:
            con.execute("DELETE FROM manual_modes WHERE chat_id = ?", (chat_id,))
            mode = "auto"
        con.execute(
            "INSERT INTO manual_control_events(control_key, chat_id, mode, created_at) VALUES (?, ?, ?, ?)",
            (f"api:{uuid.uuid4().hex}", chat_id, mode, now),
        )
        con.execute(
            "DELETE FROM manual_control_events WHERE created_at < ?",
            (now - 30 * 86400,),
        )
        con.commit()
        try:
            path = os.path.join(_account_root(user_id, account_key), "delivery_state.db")
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"enabled": enabled, "expires_at": expires_at, "updated_at": now}
    except sqlite3.Error:
        con.rollback()
        return None
    finally:
        con.close()


def set_conversation_takeover(
    user_id: int,
    chat_id: str,
    enabled: bool = True,
    account_key: str = DEFAULT_ACCOUNT_ID,
):
    """Persist and apply a manual takeover for one account-local chat."""
    selected = str(chat_id or "").strip()
    if not selected:
        return None
    # Verify the chat before touching the worker database.  This is the key
    # boundary that prevents a caller from toggling an arbitrary chat ID.
    if not conversation_exists(user_id, selected, account_key):
        return None
    worker_state = _set_worker_manual_mode(user_id, selected, bool(enabled), account_key)
    if worker_state is None:
        return None
    con = _connect_writable(user_id, "chat_history.db", account_key)
    if con is None:
        return None
    now = time.time()
    expires_at = worker_state["expires_at"]
    try:
        con.execute("BEGIN IMMEDIATE")
        if not _conversation_exists(con, selected):
            con.rollback()
            return None
        _ensure_conversation_state(con)
        con.execute(
            """INSERT INTO conversation_state(
                   chat_id, user_id, last_read_message_id,
                   takeover_enabled, takeover_expires_at, updated_at
               ) VALUES (?, ?, 0, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   user_id = excluded.user_id,
                   takeover_enabled = excluded.takeover_enabled,
                   takeover_expires_at = excluded.takeover_expires_at,
                   updated_at = excluded.updated_at""",
            (selected, str(user_id), 1 if enabled else 0, expires_at, now),
        )
        con.execute(
            """INSERT INTO conversation_controls(
                   event_id, chat_id, action, enabled, created_at, applied_at, status
               ) VALUES (?, ?, 'takeover', ?, ?, ?, 'applied')""",
            (f"api:{uuid.uuid4().hex}", selected, 1 if enabled else 0, now, now),
        )
        con.execute(
            """DELETE FROM conversation_controls
               WHERE rowid NOT IN (
                   SELECT rowid FROM conversation_controls
                   ORDER BY created_at DESC LIMIT 1000
               )"""
        )
        con.commit()
    except sqlite3.Error:
        con.rollback()
        return None
    finally:
        con.close()
    selected_rows = conversations(user_id, 200, account_key)
    return next((row for row in selected_rows if row["chat_id"] == selected), None)


def orders(user_id: int, limit: int = 50, account_key: str = DEFAULT_ACCOUNT_ID):
    con = _connect(user_id, "delivery_state.db", account_key)
    if con is None:
        return []
    try:
        rows = con.execute(
            """SELECT order_key, status, item_id, quantity, platform_status,
                      paid_amount, delivered_at, created_at
               FROM delivery_events ORDER BY created_at DESC LIMIT ?""",
            (min(max(limit, 1), 200),),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "order_key": r["order_key"][:14],
                "status": r["status"],
                "item_id": r["item_id"],
                "quantity": r["quantity"],
                "platform_status": r["platform_status"],
                "paid_amount": r["paid_amount"],
                "delivered_at": _fmt(r["delivered_at"]),
                "created_at": _fmt(r["created_at"]),
            })
        return out
    finally:
        con.close()


def summary(user_id: int, account_key: str = DEFAULT_ACCOUNT_ID):
    """Return aggregate counters without exposing message or delivery payloads."""
    result = {
        "messages_total": 0,
        "orders_total": 0,
        "delivered_total": 0,
        "attention_total": 0,
        "last_activity": "",
    }
    chat = _connect(user_id, "chat_history.db", account_key)
    message_activity = ""
    manual_attention = 0
    if chat is not None and _table_exists(chat, "messages"):
        try:
            row = chat.execute(
                "SELECT COUNT(*) AS total, MAX(timestamp) AS last_activity FROM messages"
            ).fetchone()
            result["messages_total"] = int(row["total"] or 0)
            message_activity = str(row["last_activity"] or "")
            result["last_activity"] = message_activity
            if _table_exists(chat, "manual_reply_drafts"):
                columns = _table_columns(chat, "manual_reply_drafts")
                if "status" in columns:
                    attention = chat.execute(
                        """SELECT COUNT(*) AS total
                           FROM manual_reply_drafts
                           WHERE status IN ('retry', 'manual_review')"""
                    ).fetchone()
                    manual_attention = int(attention["total"] or 0)
        except sqlite3.Error:
            pass
        finally:
            chat.close()
    elif chat is not None:
        if _table_exists(chat, "manual_reply_drafts"):
            columns = _table_columns(chat, "manual_reply_drafts")
            if "status" in columns:
                try:
                    attention = chat.execute(
                        """SELECT COUNT(*) AS total
                           FROM manual_reply_drafts
                           WHERE status IN ('retry', 'manual_review')"""
                    ).fetchone()
                    manual_attention = int(attention["total"] or 0)
                except sqlite3.Error:
                    pass
        chat.close()
    result["attention_total"] = manual_attention

    delivery = _connect(user_id, "delivery_state.db", account_key)
    if delivery is not None and _table_exists(delivery, "delivery_events"):
        try:
            event_columns = _table_columns(delivery, "delivery_events")
            activity_columns = [
                column
                for column in ("delivered_at", "updated_at", "created_at")
                if column in event_columns
            ]
            activity_expr = (
                "COALESCE(" + ", ".join(f"events.{column}" for column in activity_columns) + ")"
                if len(activity_columns) > 1
                else f"events.{activity_columns[0]}"
                if activity_columns
                else "NULL"
            )
            review_columns = _table_columns(delivery, "manual_reviews")
            has_reviews = (
                {"order_key", "status"}.issubset(review_columns)
                and "order_key" in event_columns
            )
            if has_reviews:
                row = delivery.execute(
                    f"""SELECT COUNT(*) AS total,
                              SUM(CASE WHEN events.status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                              SUM(CASE WHEN events.status IN ('retry', 'failed')
                                         OR (events.status = 'manual_review'
                                             AND (reviews.order_key IS NULL OR reviews.status = 'open'))
                                         OR reviews.status = 'open'
                                       THEN 1 ELSE 0 END) AS attention,
                              MAX({activity_expr}) AS last_activity
                       FROM delivery_events AS events
                       LEFT JOIN manual_reviews AS reviews
                         ON reviews.order_key = events.order_key"""
                ).fetchone()
            else:
                unqualified_activity = activity_expr.replace("events.", "")
                row = delivery.execute(
                    f"""SELECT COUNT(*) AS total,
                              SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                              SUM(CASE WHEN status IN ('manual_review', 'retry', 'failed') THEN 1 ELSE 0 END) AS attention,
                              MAX({unqualified_activity}) AS last_activity
                       FROM delivery_events"""
                ).fetchone()
            result["orders_total"] = int(row["total"] or 0)
            result["delivered_total"] = int(row["delivered"] or 0)
            result["attention_total"] = int(row["attention"] or 0) + manual_attention
            delivery_activity = row["last_activity"]
            if _timestamp_epoch(delivery_activity) > _timestamp_epoch(message_activity):
                result["last_activity"] = _fmt(delivery_activity) or str(delivery_activity or "")
        except sqlite3.Error:
            pass
        finally:
            delivery.close()
    elif delivery is not None:
        delivery.close()

    return result


# These are deliberately kept small and explicit.  A future worker status must
# not silently become a "failure" in the customer-facing aggregate until its
# semantics have been reviewed.
_DELIVERY_SUCCESS_STATUSES = frozenset({"delivered"})
_DELIVERY_FAILURE_STATUSES = frozenset(
    {"failed", "manual_review", "retry", "cancelled", "expired", "quarantined"}
)
_ANALYTICS_PERIODS = frozenset({1, 7, 30})


def _analytics_period(period_days: int):
    """Return local calendar-day buckets for the bounded analytics window."""
    try:
        days = int(period_days)
    except (TypeError, ValueError):
        raise ValueError("analytics period is invalid")
    if days not in _ANALYTICS_PERIODS:
        raise ValueError("analytics period is invalid")
    today = date.today()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    return days, dates


def _analytics_bucket(date_value: date):
    return {
        "date": date_value.isoformat(),
        "messages_total": 0,
        "buyer_messages_total": 0,
        "auto_replies_total": 0,
        "manual_takeovers_total": 0,
        "fulfillment_success_total": 0,
        "fulfillment_failed_total": 0,
    }


def _table_columns(con, table_name: str):
    if con is None or not _table_exists(con, table_name):
        return set()
    try:
        return {
            str(row["name"])
            for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except sqlite3.Error:
        return set()


def _local_day_epoch(date_value: date) -> float:
    # ``mktime`` follows the host's configured local timezone, the same
    # timezone used by the worker's ``datetime.fromtimestamp`` timestamps.
    return time.mktime((date_value.year, date_value.month, date_value.day, 0, 0, 0, 0, 0, -1))


def _count_day_messages(chat, day_text: str):
    """Count bounded message roles without selecting message content."""
    if chat is None:
        return 0, 0, 0
    columns = _table_columns(chat, "messages")
    if "timestamp" not in columns or "role" not in columns:
        return 0, 0, 0
    try:
        assistant_filter = "role = 'assistant'"
        if "source_id" in columns:
            assistant_filter += " AND (source_id IS NULL OR source_id NOT LIKE 'manual_reply:%')"
        row = chat.execute(
            f"""SELECT COUNT(*) AS total,
                      SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS buyer_total,
                      SUM(CASE WHEN {assistant_filter} THEN 1 ELSE 0 END) AS assistant_total
               FROM messages
               WHERE CAST(timestamp AS TEXT) LIKE ?""",
            (day_text + "%",),
        ).fetchone()
        messages_total = int(row["total"] or 0)
        buyer_total = int(row["buyer_total"] or 0)
        assistant_total = int(row["assistant_total"] or 0)

        # ``messages`` is bounded and can prune old replies.  The durable
        # outcome ledger preserves terminal assistant outcomes, so include
        # outcomes that are not still represented by a message row.  Source
        # IDs are opaque identifiers and never leave this function.
        outcomes = 0
        outcome_columns = _table_columns(chat, "assistant_outcomes")
        if {"role", "updated_at", "source_id"}.issubset(outcome_columns):
            if "source_id" in columns:
                outcome_row = chat.execute(
                    """SELECT COUNT(*) AS total
                       FROM assistant_outcomes AS outcomes
                       WHERE outcomes.role = 'assistant'
                         AND (outcomes.source_id IS NULL
                              OR outcomes.source_id NOT LIKE 'manual_reply:%')
                         AND CAST(outcomes.updated_at AS TEXT) LIKE ?
                         AND NOT EXISTS (
                             SELECT 1 FROM messages
                             WHERE messages.source_id = outcomes.source_id
                         )""",
                    (day_text + "%",),
                ).fetchone()
            else:
                outcome_row = chat.execute(
                    """SELECT COUNT(*) AS total
                       FROM assistant_outcomes
                       WHERE role = 'assistant'
                         AND CAST(updated_at AS TEXT) LIKE ?""",
                    (day_text + "%",),
                ).fetchone()
            outcomes = int(outcome_row["total"] or 0)
        return messages_total, buyer_total, assistant_total + outcomes
    except (sqlite3.Error, TypeError, ValueError):
        return 0, 0, 0


def _count_day_manual_takeovers(delivery, chat, day_start: float, day_end: float, day_text: str):
    """Count takeover activations once, preferring the worker's ledger."""
    if delivery is not None:
        columns = _table_columns(delivery, "manual_control_events")
        if {"mode", "created_at"}.issubset(columns):
            try:
                row = delivery.execute(
                    """SELECT COUNT(*) AS total
                       FROM manual_control_events
                       WHERE mode = 'manual' AND created_at >= ? AND created_at < ?""",
                    (day_start, day_end),
                ).fetchone()
                # A present worker table is authoritative even when this
                # window has no rows; API writes the same control to both
                # stores and counting the fallback would double it.
                return int(row["total"] or 0)
            except (sqlite3.Error, TypeError, ValueError):
                return 0

    # Legacy databases may only have the API-side conversation control table.
    if chat is None:
        return 0
    columns = _table_columns(chat, "conversation_controls")
    if not {"action", "enabled", "created_at"}.issubset(columns):
        return 0
    try:
        row = chat.execute(
            """SELECT COUNT(*) AS total
               FROM conversation_controls
               WHERE action = 'takeover' AND enabled = 1
                 AND created_at >= ? AND created_at < ?""",
            (day_start, day_end),
        ).fetchone()
        return int(row["total"] or 0)
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _count_day_delivery(delivery, day_start: float, day_end: float):
    """Return successful and failed delivery counts for one local day."""
    if delivery is None:
        return 0, 0
    columns = _table_columns(delivery, "delivery_events")
    if not {"status", "created_at"}.issubset(columns):
        return 0, 0
    timestamp_expr = "created_at"
    if "delivered_at" in columns and "updated_at" in columns:
        timestamp_expr = "COALESCE(delivered_at, updated_at, created_at)"
    elif "delivered_at" in columns:
        timestamp_expr = "COALESCE(delivered_at, created_at)"
    elif "updated_at" in columns:
        timestamp_expr = "COALESCE(updated_at, created_at)"
    try:
        success_statuses = tuple(sorted(_DELIVERY_SUCCESS_STATUSES))
        failure_statuses = tuple(sorted(_DELIVERY_FAILURE_STATUSES - {"manual_review"}))
        success_marks = ",".join("?" for _ in success_statuses)
        failure_marks = ",".join("?" for _ in failure_statuses)
        success_params = success_statuses + (day_start, day_end)
        qualified_timestamp_expr = (
            timestamp_expr
            .replace("delivered_at", "events.delivered_at")
            .replace("updated_at", "events.updated_at")
            .replace("created_at", "events.created_at")
        )
        success = delivery.execute(
            f"""SELECT COUNT(*) AS total FROM delivery_events
                WHERE status IN ({success_marks}) AND {timestamp_expr} >= ? AND {timestamp_expr} < ?""",
            success_params,
        ).fetchone()
        if _table_exists(delivery, "manual_reviews"):
            failed = delivery.execute(
                f"""SELECT COUNT(*) AS total FROM delivery_events AS events
                    LEFT JOIN manual_reviews AS reviews
                      ON reviews.order_key = events.order_key
                    WHERE (
                        events.status IN ({failure_marks})
                        OR (events.status = 'manual_review'
                            AND (reviews.order_key IS NULL OR reviews.status = 'open'))
                        OR reviews.status = 'open'
                    )
                    AND {qualified_timestamp_expr} >= ?
                    AND {qualified_timestamp_expr} < ?""",
                failure_statuses + (day_start, day_end),
            ).fetchone()
        else:
            legacy_failure_statuses = tuple(sorted(_DELIVERY_FAILURE_STATUSES))
            legacy_failure_marks = ",".join("?" for _ in legacy_failure_statuses)
            failure_params = legacy_failure_statuses + (day_start, day_end)
            failed = delivery.execute(
                f"""SELECT COUNT(*) AS total FROM delivery_events
                    WHERE status IN ({legacy_failure_marks}) AND {timestamp_expr} >= ? AND {timestamp_expr} < ?""",
                failure_params,
            ).fetchone()
        return int(success["total"] or 0), int(failed["total"] or 0)
    except (sqlite3.Error, TypeError, ValueError):
        return 0, 0


def analytics_summary(
    user_id: int,
    account_key: str = DEFAULT_ACCOUNT_ID,
    period_days: int = 1,
):
    """Return account-scoped, redacted operating aggregates.

    This is a derived view only.  New totals and buckets contain counts and
    calendar dates, never message/order identifiers, content, inventory, or
    credentials.  ``summary`` retains the existing ``last_activity`` display
    field for workbench compatibility.
    """
    days, dates = _analytics_period(period_days)
    legacy = summary(user_id, account_key)
    buckets = [_analytics_bucket(day) for day in dates]

    chat = _connect(user_id, "chat_history.db", account_key)
    delivery = _connect(user_id, "delivery_state.db", account_key)
    try:
        for bucket, day in zip(buckets, dates):
            day_text = day.isoformat()
            day_start = _local_day_epoch(day)
            day_end = _local_day_epoch(day + timedelta(days=1))
            messages_total, buyer_messages, auto_replies = _count_day_messages(chat, day_text)
            successful, failed = _count_day_delivery(delivery, day_start, day_end)
            bucket.update(
                {
                    "messages_total": messages_total,
                    "buyer_messages_total": buyer_messages,
                    "auto_replies_total": auto_replies,
                    "manual_takeovers_total": _count_day_manual_takeovers(
                        delivery, chat, day_start, day_end, day_text
                    ),
                    "fulfillment_success_total": successful,
                    "fulfillment_failed_total": failed,
                }
            )
    finally:
        if chat is not None:
            chat.close()
        if delivery is not None:
            delivery.close()

    metric_keys = (
        "messages_total",
        "buyer_messages_total",
        "auto_replies_total",
        "manual_takeovers_total",
        "fulfillment_success_total",
        "fulfillment_failed_total",
    )
    totals = {key: sum(int(bucket[key]) for bucket in buckets) for key in metric_keys}
    unread = conversation_unread_totals(user_id, account_key)
    totals.update(
        {
            "unread_conversations_total": int(unread["conversations"]),
            "unread_messages_total": int(unread["messages"]),
        }
    )
    # Keep old summary keys available at the top level for simple clients,
    # while also nesting the exact compatibility payload for new consumers.
    result = dict(legacy)
    result.update(
        {
            "ok": True,
            "period": {
                "days": days,
                "start": dates[0].isoformat(),
                "end": dates[-1].isoformat(),
            },
            "summary": dict(legacy),
            "totals": totals,
            "buckets": buckets,
        }
    )
    return result


def _fmt(ts):
    if not ts:
        return ""
    try:
        value = float(ts)
        if not math.isfinite(value):
            return ""
        return time.strftime("%m-%d %H:%M", time.localtime(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _timestamp_epoch(value):
    """Parse numeric or ISO timestamps only for internal ordering."""
    if value in (None, ""):
        return 0.0
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else 0.0
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0

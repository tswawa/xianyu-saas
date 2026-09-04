import sqlite3
import os
import json
import hashlib
import math
import re
import stat
import time
from datetime import datetime
from loguru import logger


def stable_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


_MEDIA_TYPES = frozenset({"image", "emoji", "audio", "video", "file", "link", "unknown"})
_MANUAL_REPLY_MAX_IMAGES = 8
_MANUAL_REPLY_MAX_CONTENT_CHARS = 4096
_MANUAL_IMAGE_NAME = re.compile(
    r"^manual_reply_[0-9a-f]{32}\.(?:jpg|png|gif|webp)$"
)


def normalize_media(media, *, allow_paths=True):
    if media is None:
        return []
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(media, list):
        return []
    normalized = []
    for raw in media:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "unknown").strip().lower()
        if kind not in _MEDIA_TYPES:
            kind = "unknown"
        url = str(raw.get("url") or "").strip()
        if url and (not url.startswith("https://") or len(url) > 2048):
            url = ""
        path = str(raw.get("path") or "").strip()
        if not allow_paths:
            path = ""
        if path and (
            len(path) > 255 or path in {".", ".."} or "/" in path or "\\" in path
            or any(ord(char) < 32 for char in path)
        ):
            path = ""
        if not url and not path and kind not in {"emoji", "unknown"}:
            has_summary = any(
                isinstance(raw.get(field), str) and raw.get(field).strip()
                for field in ("label", "alt", "name")
            )
            if not has_summary:
                continue
        def bounded_int(value, maximum):
            try:
                number = int(value)
            except (TypeError, ValueError):
                return 0
            return number if 0 <= number <= maximum else 0
        item = {
            "type": kind,
            "url": url,
            "alt": str(raw.get("alt") or "")[:160],
            "width": bounded_int(raw.get("width"), 10000),
            "height": bounded_int(raw.get("height"), 10000),
            "duration_ms": bounded_int(raw.get("duration_ms"), 86_400_000),
            "label": str(raw.get("label") or "")[:160],
        }
        if path:
            item["path"] = path
        if raw.get("mime"):
            item["mime"] = str(raw.get("mime"))[:80]
        if raw.get("name"):
            item["name"] = str(raw.get("name"))[:160]
        normalized.append(item)
    return normalized


def _manual_image_filename(value):
    name = str(value or "").strip()
    return name if _MANUAL_IMAGE_NAME.fullmatch(name) else ""


def normalize_manual_reply_media(media):
    """Strictly normalize all private images without truncating legacy rows."""
    if media is None:
        raw_items = []
    elif isinstance(media, str):
        try:
            raw_items = json.loads(media)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise sqlite3.IntegrityError("manual reply media is invalid") from exc
    else:
        raw_items = media
    if not isinstance(raw_items, list) or len(raw_items) > _MANUAL_REPLY_MAX_IMAGES:
        raise sqlite3.IntegrityError("manual reply media is invalid")
    normalized = normalize_media(raw_items)
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


def media_json(media):
    clean = normalize_media(media)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded.encode("utf-8")) <= 64 * 1024 else "[]"


_MANUAL_REPLY_ACTIVE_STATUSES = frozenset({"queued", "retry", "sending", "manual_review"})


def _manual_reply_part_specs(content, media):
    items = normalize_manual_reply_media(media)
    specs = [(index, "image", index) for index in range(len(items))]
    if str(content or "").strip():
        specs.append((len(specs), "text", None))
    return specs


def _manual_reply_part_rows(conn, reply_id):
    return conn.execute(
        """SELECT outbox_id, part_index, kind, media_index,
                  acknowledged_at, sent_media_json
           FROM manual_reply_parts
           WHERE outbox_id = ? ORDER BY part_index""",
        (int(reply_id),),
    ).fetchall()


def _ensure_manual_reply_parts(conn, row):
    if row is None:
        return []
    reply_id = int(row["id"])
    parts = _manual_reply_part_rows(conn, reply_id)
    status = str(row["status"] or "draft")
    specs = _manual_reply_part_specs(row["content"], row["media_json"])
    if not parts and status in _MANUAL_REPLY_ACTIVE_STATUSES:
        if not specs:
            raise sqlite3.IntegrityError("manual reply has no deliverable parts")
        conn.executemany(
            """INSERT OR IGNORE INTO manual_reply_parts(
                   outbox_id, part_index, kind, media_index,
                   acknowledged_at, sent_media_json
               ) VALUES (?, ?, ?, ?, NULL, '[]')""",
            [(reply_id, part_index, kind, media_index) for part_index, kind, media_index in specs],
        )
        parts = _manual_reply_part_rows(conn, reply_id)
    tombstone = (
        status == "acknowledged"
        and not str(row["chat_id"] or "")
        and not str(row["item_id"] or "")
        and not str(row["content"] or "")
        and not normalize_media(row["media_json"])
    )
    if not parts or tombstone:
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
    return parts


def _manual_reply_part_source_id(reply_id, part, parts):
    kind = str(part["kind"])
    if kind == "image":
        media_index = int(part["media_index"])
        return (
            f"manual_reply:{int(reply_id)}"
            if media_index == 0
            else f"manual_reply:{int(reply_id)}:image:{media_index + 1}"
        )
    has_images = any(str(candidate["kind"]) == "image" for candidate in parts)
    return f"manual_reply:{int(reply_id)}:text" if has_images else f"manual_reply:{int(reply_id)}"


def _validate_claimable_manual_reply(row, parts):
    content = str(row["content"] or "")
    media = normalize_manual_reply_media(row["media_json"])
    if (
        not str(row["chat_id"] or "").strip()
        or not str(row["recipient_id"] or "").strip()
        or (not content.strip() and not media)
        or len(content) > _MANUAL_REPLY_MAX_CONTENT_CHARS
    ):
        raise sqlite3.IntegrityError("manual reply parent payload is invalid")
    pending_seen = False
    pending_count = 0
    for part in parts:
        if part["acknowledged_at"] is None:
            pending_seen = True
            pending_count += 1
        elif pending_seen:
            raise sqlite3.IntegrityError("manual reply acknowledgements are out of order")
    if pending_count == 0:
        raise sqlite3.IntegrityError("manual reply has no pending part")


def _manual_reply_ack_payload(row, part, sent_media):
    kind = str(part["kind"])
    if kind == "image":
        raw_media = [sent_media] if isinstance(sent_media, dict) else sent_media
        if isinstance(raw_media, str):
            try:
                raw_media = json.loads(raw_media)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("manual reply image acknowledgement is invalid") from exc
        if (
            not isinstance(raw_media, list)
            or len(raw_media) != 1
            or not isinstance(raw_media[0], dict)
            or str(raw_media[0].get("type") or "").strip().lower() != "image"
            or raw_media[0].get("path") not in (None, "")
        ):
            raise ValueError("manual reply image acknowledgement is invalid")
        persisted_media = normalize_media(raw_media, allow_paths=False)
        if (
            len(persisted_media) != 1
            or persisted_media[0].get("type") != "image"
            or not persisted_media[0].get("url")
        ):
            raise ValueError("manual reply image acknowledgement is invalid")
        return "", "image", persisted_media
    if kind == "text":
        if isinstance(sent_media, str):
            try:
                sent_media = json.loads(sent_media)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("manual reply text acknowledgement is invalid") from exc
        if sent_media not in (None, []):
            raise ValueError("manual reply text acknowledgement is invalid")
        content = str(row["content"] or "")
        if not content.strip():
            raise ValueError("manual reply text acknowledgement is invalid")
        return content, "text", []
    raise ValueError("manual reply part kind is invalid")


def _manual_image_has_active_reference(conn, name):
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "manual_reply_drafts" not in tables:
        return True
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(manual_reply_drafts)").fetchall()
    }
    if not {"id", "status", "media_json"}.issubset(columns):
        return True
    parts_available = "manual_reply_parts" in tables
    rows = conn.execute(
        """SELECT id, media_json FROM manual_reply_drafts
           WHERE status IN ('queued', 'retry', 'sending', 'manual_review')"""
    ).fetchall()
    for row in rows:
        raw_items = row["media_json"]
        if isinstance(raw_items, str):
            try:
                raw_items = json.loads(raw_items)
            except (TypeError, ValueError, json.JSONDecodeError):
                return True
        if not isinstance(raw_items, list):
            return True
        for media_index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("path") or "").strip() != name:
                continue
            if not parts_available:
                return True
            part = conn.execute(
                """SELECT acknowledged_at FROM manual_reply_parts
                   WHERE outbox_id = ? AND kind = 'image' AND media_index = ?""",
                (int(row["id"]), media_index),
            ).fetchone()
            if part is None or part["acknowledged_at"] is None:
                return True
    return False


class ChatContextManager:
    """
    聊天上下文管理器

    负责存储和检索用户与商品之间的对话历史，使用SQLite数据库进行持久化存储。
    支持按会话ID检索对话历史，以及议价次数统计。
    """

    TERMINAL_ASSISTANT_ROLES = {
        "assistant",
        "assistant_cancelled",
        "assistant_no_reply",
    }
    OUTCOME_RETENTION_SECONDS = 90 * 86400
    MAX_CACHED_ITEMS = 1000
    MANUAL_REPLY_MAX_ATTEMPTS = 10

    def __init__(self, max_history=100, db_path="data/chat_history.db", now_fn=time.time):
        """
        初始化聊天上下文管理器

        Args:
            max_history: 每个对话保留的最大消息数
            db_path: SQLite数据库文件路径
        """
        if isinstance(max_history, bool) or not isinstance(max_history, int) or max_history < 1:
            raise ValueError("max_history must be a positive integer")
        self.max_history = max_history
        self.db_path = db_path
        self.now_fn = now_fn
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        # 确保数据库目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, mode=0o700, exist_ok=True)
        if db_dir:
            os.chmod(db_dir, 0o700)

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        cursor = conn.cursor()

        # 创建消息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            chat_id TEXT,
            source_id TEXT,
            content_type TEXT NOT NULL DEFAULT 'text',
            media_json TEXT NOT NULL DEFAULT '[]'
        )
        ''')
        # 检查是否需要添加chat_id字段（兼容旧数据库）
        cursor.execute("PRAGMA table_info(messages)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'chat_id' not in columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN chat_id TEXT')
            logger.info("已为messages表添加chat_id字段")
        if 'source_id' not in columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN source_id TEXT')
            logger.info("已为messages表添加source_id字段")
        if 'content_type' not in columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
            logger.info("已为messages表添加content_type字段")
        if 'media_json' not in columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'")
            logger.info("已为messages表添加media_json字段")

        # 创建索引以加速查询
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_item ON messages (user_id, item_id)
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_chat_id ON messages (chat_id)
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages (timestamp)
        ''')

        cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_id
        ON messages (source_id) WHERE source_id IS NOT NULL
        ''')

        # Seller-authored replies use the former draft table as an account-local
        # outbox.  Existing draft rows remain ``draft`` and are never sent.
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS manual_reply_drafts (
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
        )
        ''')
        cursor.execute("PRAGMA table_info(manual_reply_drafts)")
        manual_columns = {column[1] for column in cursor.fetchall()}
        manual_extra_columns = {
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
        for column, definition in manual_extra_columns.items():
            if column not in manual_columns:
                cursor.execute(
                    f"ALTER TABLE manual_reply_drafts ADD COLUMN {column} {definition}"
                )
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_manual_reply_drafts_chat
        ON manual_reply_drafts (chat_id, created_at)
        ''')
        cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_reply_request
        ON manual_reply_drafts (request_id)
        WHERE request_id IS NOT NULL AND request_id != ''
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_manual_reply_available
        ON manual_reply_drafts (status, available_at, id)
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS manual_reply_parts (
            outbox_id INTEGER NOT NULL,
            part_index INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('image', 'text')),
            media_index INTEGER,
            acknowledged_at REAL,
            sent_media_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (outbox_id, part_index)
        )
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_manual_reply_parts_pending
        ON manual_reply_parts (outbox_id, acknowledged_at, part_index)
        ''')

        # Reply outcomes are kept independently from the bounded chat history.
        # This prevents an old/replayed buyer message from being answered again
        # after its message row has been pruned.
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistant_outcomes (
            source_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_assistant_outcomes_chat
        ON assistant_outcomes (chat_id, updated_at)
        ''')
        cursor.execute('''
        INSERT OR IGNORE INTO assistant_outcomes(
            source_id, chat_id, user_id, item_id, role,
            content, created_at, updated_at
        )
        SELECT source_id, COALESCE(chat_id, ''), user_id, item_id, role,
               content, timestamp, timestamp
        FROM messages
        WHERE source_id LIKE 'assistant:%'
          AND role IN (
              'assistant_pending', 'assistant',
              'assistant_cancelled', 'assistant_no_reply'
          )
        ''')
        cutoff = datetime.fromtimestamp(
            self.now_fn() - self.OUTCOME_RETENTION_SECONDS
        ).isoformat()
        cursor.execute(
            """
            DELETE FROM assistant_outcomes
            WHERE role IN (
                'assistant', 'assistant_cancelled', 'assistant_no_reply'
            ) AND updated_at < ?
            """,
            (cutoff,),
        )

        # 创建基于会话ID的议价次数表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_bargain_counts (
            chat_id TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 创建商品信息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            price REAL,
            description TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        conn.commit()
        conn.close()
        os.chmod(self.db_path, 0o600)
        logger.info("聊天历史数据库初始化完成")



    def _now_iso(self):
        return datetime.fromtimestamp(self.now_fn()).isoformat()

    def _prune_assistant_outcomes(self, conn, now):
        cutoff = datetime.fromtimestamp(
            now - self.OUTCOME_RETENTION_SECONDS
        ).isoformat()
        conn.execute(
            """
            DELETE FROM assistant_outcomes
            WHERE role != 'assistant_pending' AND updated_at < ?
            """,
            (cutoff,),
        )

    @staticmethod
    def _manual_error_code(value):
        text = str(value or "send_error").strip().lower()
        if not text or len(text) > 64 or not text.isascii():
            return "send_error"
        if any(not (character.isalnum() or character == "_") for character in text):
            return "send_error"
        return text

    def cleanup_manual_reply_image(self, path):
        """Delete one private image only after an atomic local-reference check."""
        name = _manual_image_filename(path)
        if not name:
            return "invalid"
        root = os.path.realpath(
            os.path.dirname(os.path.abspath(self.db_path)) or os.curdir
        )
        candidate = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(candidate) != root or os.path.basename(candidate) != name:
            return "invalid"
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            root_flags |= os.O_NOFOLLOW
        try:
            root_fd = os.open(root, root_flags)
        except OSError:
            return "unavailable"
        conn = None
        descriptor = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("BEGIN IMMEDIATE")
            if _manual_image_has_active_reference(conn, name):
                conn.rollback()
                return "active"
            try:
                current_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                conn.commit()
                return "not_found"
            if not stat.S_ISREG(current_stat.st_mode):
                conn.rollback()
                return "invalid"
            file_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            if hasattr(os, "O_NOFOLLOW"):
                file_flags |= os.O_NOFOLLOW
            descriptor = os.open(name, file_flags, dir_fd=root_fd)
            file_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or (file_stat.st_dev, file_stat.st_ino)
                != (current_stat.st_dev, current_stat.st_ino)
            ):
                conn.rollback()
                return "invalid"
            os.close(descriptor)
            descriptor = None
            os.unlink(name, dir_fd=root_fd)
            conn.commit()
            return "deleted"
        except FileNotFoundError:
            if conn is not None:
                conn.commit()
            return "not_found"
        except (OSError, sqlite3.Error):
            if conn is not None:
                conn.rollback()
            return "unavailable"
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if conn is not None:
                conn.close()
            os.close(root_fd)

    def completed_manual_reply_image_cleanup_batch(self, before_id=0, limit=16):
        """Return one bounded, conservative batch of completed private images."""
        try:
            before_id = int(before_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual image cleanup cursor is invalid") from exc
        if (
            before_id < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 100
        ):
            raise ValueError("manual image cleanup batch is invalid")
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            conn.execute("BEGIN")
            if before_id:
                rows = conn.execute(
                    """SELECT id, chat_id, item_id, content, media_json, status
                       FROM manual_reply_drafts
                       WHERE status = 'acknowledged' AND id < ?
                       ORDER BY id DESC LIMIT ?""",
                    (before_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, chat_id, item_id, content, media_json, status
                       FROM manual_reply_drafts
                       WHERE status = 'acknowledged'
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            next_before_id = (
                int(rows[-1]["id"])
                if len(rows) == limit
                else 0
            )
            paths = []
            seen_paths = set()
            for row in rows:
                try:
                    media = normalize_manual_reply_media(row["media_json"])
                    if not media:
                        continue
                    parts = _ensure_manual_reply_parts(conn, row)
                    if not parts or any(part["acknowledged_at"] is None for part in parts):
                        continue
                    parent_paths = []
                    valid_parent = True
                    for part in parts:
                        source_id = _manual_reply_part_source_id(
                            int(row["id"]), part, parts
                        )
                        existing = conn.execute(
                            """SELECT chat_id, item_id, role, content,
                                      content_type, media_json
                               FROM messages WHERE source_id = ?""",
                            (source_id,),
                        ).fetchone()
                        if existing is None:
                            valid_parent = False
                            break
                        expected_content, expected_type, expected_media = (
                            _manual_reply_ack_payload(
                                row, part, part["sent_media_json"]
                            )
                        )
                        try:
                            _, existing_type, existing_media = (
                                _manual_reply_ack_payload(
                                    row, part, existing["media_json"]
                                )
                            )
                        except ValueError:
                            valid_parent = False
                            break
                        if (
                            str(existing["chat_id"] or "")
                            != str(row["chat_id"] or "")
                            or str(existing["item_id"] or "")
                            != str(row["item_id"] or "")
                            or str(existing["role"] or "") != "assistant"
                            or str(existing["content"] or "") != expected_content
                            or str(existing["content_type"] or "") != expected_type
                            or existing_type != expected_type
                            or existing_media != expected_media
                        ):
                            valid_parent = False
                            break
                        if str(part["kind"] or "") == "image":
                            media_index = int(part["media_index"])
                            if media_index < 0 or media_index >= len(media):
                                valid_parent = False
                                break
                            parent_paths.append(str(media[media_index]["path"]))
                    if not valid_parent:
                        continue
                    for path in parent_paths:
                        if path not in seen_paths:
                            seen_paths.add(path)
                            paths.append(path)
                except (
                    sqlite3.IntegrityError,
                    TypeError,
                    ValueError,
                    OverflowError,
                ):
                    continue
            conn.commit()
            return {"paths": paths, "before_id": next_before_id}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_manual_replies(self, owner, limit=1, lease_seconds=300):
        """Claim queued seller replies with a crash-recoverable SQLite lease."""
        owner = str(owner or "").strip()
        if not owner or len(owner) > 128:
            raise ValueError("manual reply lease owner is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 20:
            raise ValueError("manual reply claim limit is invalid")
        try:
            lease_seconds = float(lease_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual reply lease duration is invalid") from exc
        if not math.isfinite(lease_seconds) or lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("manual reply lease duration is invalid")
        now = float(self.now_fn())
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE manual_reply_drafts
                   SET status = CASE
                           WHEN attempts >= max_attempts THEN 'manual_review'
                           ELSE 'retry'
                       END,
                       available_at = ?, lease_owner = NULL, lease_until = NULL,
                       last_error_code = 'lease_expired', updated_at = ?
                   WHERE status = 'sending'
                     AND COALESCE(lease_until, 0) <= ?""",
                (now, now, now),
            )
            conn.execute(
                """UPDATE manual_reply_drafts
                   SET status = 'manual_review', lease_owner = NULL,
                       lease_until = NULL, last_error_code = 'retry_exhausted',
                       updated_at = ?
                   WHERE status IN ('queued', 'retry')
                     AND attempts >= max_attempts""",
                (now,),
            )
            candidates = conn.execute(
                """SELECT id, request_id, chat_id, recipient_id, item_id,
                          content, media_json, status, attempts, max_attempts,
                          available_at, lease_until
                   FROM manual_reply_drafts
                   WHERE status IN ('queued', 'retry')
                     AND attempts < max_attempts
                     AND available_at <= ?
                   ORDER BY available_at, id""",
                (now,),
            ).fetchall()
            claimed = []
            for candidate in candidates:
                if len(claimed) >= limit:
                    break
                reply_id = int(candidate["id"])
                try:
                    parts = _ensure_manual_reply_parts(conn, candidate)
                    _validate_claimable_manual_reply(candidate, parts)
                except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
                    conn.execute(
                        """UPDATE manual_reply_drafts
                           SET status = 'manual_review', lease_owner = NULL,
                               lease_until = NULL, last_error_code = 'invalid_payload',
                               updated_at = ?
                           WHERE id = ? AND status IN ('queued', 'retry')""",
                        (now, reply_id),
                    )
                    continue
                updated = conn.execute(
                    """UPDATE manual_reply_drafts
                       SET status = 'sending', attempts = attempts + 1,
                           lease_owner = ?, lease_until = ?,
                           last_error_code = NULL, updated_at = ?
                       WHERE id = ? AND status IN ('queued', 'retry')
                         AND attempts < max_attempts AND available_at <= ?""",
                    (owner, now + lease_seconds, now, reply_id, now),
                )
                if updated.rowcount != 1:
                    continue
                row = conn.execute(
                    """SELECT id, request_id, chat_id, recipient_id, item_id,
                              content, media_json, status, attempts, max_attempts,
                              available_at, lease_until
                       FROM manual_reply_drafts WHERE id = ?""",
                    (reply_id,),
                ).fetchone()
                if row is not None:
                    payload = dict(row)
                    payload["parts"] = [dict(part) for part in parts]
                    claimed.append(payload)
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_manual_reply_lease(self, reply_id, owner, lease_seconds=300):
        """Extend an unexpired sending lease owned by the current worker."""
        try:
            reply_id = int(reply_id)
            lease_seconds = float(lease_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual reply lease renewal is invalid") from exc
        owner = str(owner or "").strip()
        if (
            reply_id < 1
            or not owner
            or len(owner) > 128
            or not math.isfinite(lease_seconds)
            or lease_seconds < 30
            or lease_seconds > 3600
        ):
            raise ValueError("manual reply lease renewal is invalid")
        now = float(self.now_fn())
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE manual_reply_drafts
                   SET lease_until = ?, updated_at = ?
                   WHERE id = ? AND status = 'sending'
                     AND lease_owner = ? AND COALESCE(lease_until, 0) > ?""",
                (now + lease_seconds, now, reply_id, owner, now),
            )
            conn.commit()
            return updated.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_manual_reply(
        self,
        reply_id,
        owner,
        error_code,
        *,
        retry_delay=5,
        terminal=False,
    ):
        """Release one claimed reply into retry or manual review."""
        try:
            reply_id = int(reply_id)
            retry_delay = float(retry_delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual reply failure update is invalid") from exc
        if reply_id < 1 or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("manual reply failure update is invalid")
        now = float(self.now_fn())
        owner = str(owner or "")
        code = self._manual_error_code(error_code)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT status, attempts, max_attempts, lease_owner, lease_until
                   FROM manual_reply_drafts WHERE id = ?""",
                (reply_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["status"] == "acknowledged":
                conn.commit()
                return "acknowledged"
            if (
                row["status"] != "sending"
                or str(row["lease_owner"] or "") != owner
                or float(row["lease_until"] or 0) <= now
            ):
                conn.rollback()
                return None
            exhausted = int(row["attempts"] or 0) >= int(row["max_attempts"] or self.MANUAL_REPLY_MAX_ATTEMPTS)
            status = "manual_review" if terminal or exhausted else "retry"
            available_at = now if status == "manual_review" else now + min(retry_delay, 3600)
            conn.execute(
                """UPDATE manual_reply_drafts
                   SET status = ?, available_at = ?, lease_owner = NULL,
                       lease_until = NULL, last_error_code = ?, updated_at = ?
                   WHERE id = ?""",
                (status, available_at, code, now, reply_id),
            )
            conn.commit()
            return status
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def acknowledge_manual_reply_part(
        self,
        reply_id,
        owner,
        sender_id,
        part_index,
        sent_media=None,
    ):
        """Persist one ordered platform ACK and complete the parent when ready."""
        try:
            reply_id = int(reply_id)
            part_index = int(part_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual reply acknowledgement is invalid") from exc
        owner = str(owner or "")
        sender_id = str(sender_id or "").strip()
        if reply_id < 1 or part_index < 0 or not sender_id:
            raise ValueError("manual reply acknowledgement is invalid")
        now = float(self.now_fn())
        timestamp = datetime.fromtimestamp(now).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, chat_id, item_id, content, media_json, status,
                          lease_owner, lease_until
                   FROM manual_reply_drafts WHERE id = ?""",
                (reply_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            parts = _ensure_manual_reply_parts(conn, row)
            selected = next(
                (part for part in parts if int(part["part_index"]) == part_index),
                None,
            )
            if selected is None:
                conn.rollback()
                return None
            remaining = [part for part in parts if part["acknowledged_at"] is None]
            if selected["acknowledged_at"] is not None:
                content, persisted_type, persisted_media = _manual_reply_ack_payload(
                    row, selected, sent_media
                )
                stored_content, stored_type, stored_media = _manual_reply_ack_payload(
                    row, selected, selected["sent_media_json"]
                )
                source_id = _manual_reply_part_source_id(reply_id, selected, parts)
                existing = conn.execute(
                    """SELECT user_id, chat_id, item_id, role, content,
                              content_type, media_json
                       FROM messages WHERE source_id = ?""",
                    (source_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("manual reply acknowledgement collision")
                try:
                    _, existing_type, existing_media = _manual_reply_ack_payload(
                        row, selected, existing["media_json"]
                    )
                except ValueError as exc:
                    raise RuntimeError("manual reply acknowledgement collision") from exc
                if (
                    content != stored_content
                    or persisted_type != stored_type
                    or persisted_media != stored_media
                    or str(existing["user_id"] or "") != sender_id
                    or str(existing["chat_id"] or "") != str(row["chat_id"] or "")
                    or str(existing["item_id"] or "") != str(row["item_id"] or "")
                    or str(existing["role"] or "") != "assistant"
                    or str(existing["content"] or "") != content
                    or str(existing["content_type"] or "") != persisted_type
                    or existing_type != persisted_type
                    or existing_media != persisted_media
                ):
                    raise RuntimeError("manual reply acknowledgement collision")
                complete = not remaining
                conn.commit()
                return {
                    "status": "acknowledged" if complete else "sending",
                    "complete": complete,
                    "part_index": part_index,
                    "kind": str(selected["kind"]),
                    "media_index": selected["media_index"],
                }
            if (
                str(row["status"] or "") != "sending"
                or str(row["lease_owner"] or "") != owner
                or float(row["lease_until"] or 0) <= now
            ):
                conn.rollback()
                return None
            current = remaining[0] if remaining else None
            if current is None or int(current["part_index"]) != part_index:
                conn.rollback()
                return None

            content, persisted_type, persisted_media = _manual_reply_ack_payload(
                row, selected, sent_media
            )
            kind = str(selected["kind"])
            persisted_media_json = media_json(persisted_media)
            source_id = _manual_reply_part_source_id(reply_id, selected, parts)
            existing = conn.execute(
                """SELECT user_id, chat_id, item_id, role, content,
                          content_type, media_json
                   FROM messages WHERE source_id = ?""",
                (source_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO messages(
                           user_id, item_id, role, content, timestamp, chat_id, source_id,
                           content_type, media_json
                       ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?)""",
                    (
                        sender_id,
                        str(row["item_id"] or ""),
                        content,
                        timestamp,
                        str(row["chat_id"] or ""),
                        source_id,
                        persisted_type,
                        persisted_media_json,
                    ),
                )
            else:
                try:
                    _, existing_type, existing_media = _manual_reply_ack_payload(
                        row, selected, existing["media_json"]
                    )
                except ValueError as exc:
                    raise RuntimeError("manual reply acknowledgement collision") from exc
                if (
                    str(existing["user_id"] or "") != sender_id
                    or str(existing["chat_id"] or "") != str(row["chat_id"] or "")
                    or str(existing["item_id"] or "") != str(row["item_id"] or "")
                    or str(existing["role"] or "") != "assistant"
                    or str(existing["content"] or "") != content
                    or str(existing["content_type"] or "") != persisted_type
                    or existing_type != persisted_type
                    or existing_media != persisted_media
                ):
                    raise RuntimeError("manual reply acknowledgement collision")

            updated = conn.execute(
                """UPDATE manual_reply_parts
                   SET acknowledged_at = ?, sent_media_json = ?
                   WHERE outbox_id = ? AND part_index = ? AND acknowledged_at IS NULL""",
                (now, persisted_media_json, reply_id, part_index),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            remaining_count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM manual_reply_parts
                       WHERE outbox_id = ? AND acknowledged_at IS NULL""",
                    (reply_id,),
                ).fetchone()[0]
            )
            if remaining_count == 0:
                parent = conn.execute(
                    """UPDATE manual_reply_drafts
                       SET status = 'acknowledged', lease_owner = NULL,
                           lease_until = NULL, last_error_code = NULL,
                           updated_at = ?, acknowledged_at = ?
                       WHERE id = ? AND status = 'sending'
                         AND lease_owner = ? AND COALESCE(lease_until, 0) > ?""",
                    (now, now, reply_id, owner, now),
                )
            else:
                parent = conn.execute(
                    """UPDATE manual_reply_drafts SET updated_at = ?
                       WHERE id = ? AND status = 'sending'
                         AND lease_owner = ? AND COALESCE(lease_until, 0) > ?""",
                    (now, reply_id, owner, now),
                )
            if parent.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return {
                "status": "acknowledged" if remaining_count == 0 else "sending",
                "complete": remaining_count == 0,
                "part_index": part_index,
                "kind": kind,
                "media_index": selected["media_index"],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def acknowledge_manual_reply(self, reply_id, owner, sender_id, sent_media=None):
        """Compatibility wrapper for existing single-part callers and tests."""
        reply = self.get_manual_reply(reply_id)
        if reply is None:
            return False
        parts = [part for part in reply.get("parts", []) if isinstance(part, dict)]
        if str(reply.get("status") or "") == "acknowledged":
            if not parts:
                return True
            current = parts[0] if len(parts) == 1 else None
        else:
            current = next(
                (part for part in parts if part.get("acknowledged_at") is None),
                None,
            )
        if current is None:
            return False
        result = self.acknowledge_manual_reply_part(
            reply_id,
            owner,
            sender_id,
            current["part_index"],
            sent_media=sent_media,
        )
        return bool(result and result.get("complete"))

    def get_manual_reply(self, reply_id):
        try:
            reply_id = int(reply_id)
        except (TypeError, ValueError):
            return None
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, request_id, chat_id, recipient_id, item_id,
                          content, media_json, status, attempts, max_attempts,
                          available_at, lease_owner, lease_until,
                          last_error_code, acknowledged_at
                   FROM manual_reply_drafts WHERE id = ?""",
                (reply_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            payload = dict(row)
            try:
                parts = _ensure_manual_reply_parts(conn, row)
            except (sqlite3.IntegrityError, TypeError, ValueError, OverflowError):
                if str(row["status"] or "") in _MANUAL_REPLY_ACTIVE_STATUSES:
                    now = float(self.now_fn())
                    conn.execute(
                        """UPDATE manual_reply_drafts
                           SET status = 'manual_review', lease_owner = NULL,
                               lease_until = NULL, last_error_code = 'invalid_payload',
                               updated_at = ? WHERE id = ?""",
                        (now, reply_id),
                    )
                    payload.update(
                        {
                            "status": "manual_review",
                            "lease_owner": None,
                            "lease_until": None,
                            "last_error_code": "invalid_payload",
                            "updated_at": now,
                        }
                    )
                parts = _manual_reply_part_rows(conn, reply_id)
            payload["parts"] = [dict(part) for part in parts]
            conn.commit()
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_item_info(self, item_id, item_data):
        """
        保存商品信息到数据库

        Args:
            item_id: 商品ID
            item_data: 商品信息字典
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            # 从商品数据中提取有用信息
            try:
                price = float(item_data.get('soldPrice', 0))
            except (TypeError, ValueError, OverflowError):
                price = 0.0
            if not math.isfinite(price) or price < 0:
                price = 0.0
            raw_description = item_data.get('desc', '')
            description = raw_description[:2000] if isinstance(raw_description, str) else ''

            # 将整个商品数据转换为JSON字符串
            data_json = json.dumps(item_data, ensure_ascii=False, allow_nan=False)
            if len(data_json.encode("utf-8")) > 1024 * 1024:
                raise ValueError("item cache payload is too large")

            updated_at = self._now_iso()
            cursor.execute(
                """
                INSERT INTO items (item_id, data, price, description, last_updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id)
                DO UPDATE SET data = ?, price = ?, description = ?, last_updated = ?
                """,
                (
                    item_id, data_json, price, description, updated_at,
                    data_json, price, description, updated_at
                )
            )

            conn.commit()
            cursor.execute(
                """
                DELETE FROM items
                WHERE item_id NOT IN (
                    SELECT item_id FROM items
                    ORDER BY last_updated DESC, item_id DESC
                    LIMIT ?
                )
                """,
                (self.MAX_CACHED_ITEMS,),
            )
            conn.commit()
            logger.debug("商品信息已保存 item={}", stable_ref(item_id))
        except Exception as e:
            logger.error("保存商品信息失败: {}", type(e).__name__)
            conn.rollback()
        finally:
            conn.close()

    def get_item_info(self, item_id, max_age=None):
        """
        从数据库获取商品信息

        Args:
            item_id: 商品ID

        Returns:
            dict: 商品信息字典，如果不存在返回None
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            if max_age is not None:
                try:
                    max_age = float(max_age)
                except (TypeError, ValueError) as exc:
                    raise ValueError("item cache max age is invalid") from exc
                if not math.isfinite(max_age) or max_age < 0:
                    raise ValueError("item cache max age is invalid")
            cursor.execute(
                "SELECT data, last_updated FROM items WHERE item_id = ?",
                (item_id,)
            )

            result = cursor.fetchone()
            if result:
                if max_age is not None:
                    try:
                        updated_at = datetime.fromisoformat(str(result[1])).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        return None
                    age = self.now_fn() - updated_at
                    if not math.isfinite(age) or age > max_age:
                        return None
                return json.loads(result[0])
            return None
        except Exception as e:
            logger.error("获取商品信息失败: {}", type(e).__name__)
            return None
        finally:
            conn.close()

    def add_message_by_chat(
        self, chat_id, user_id, item_id, role, content, source_id=None,
        *, content_type="text", media=None,
    ):
        """
        基于会话ID添加新消息到对话历史

        Args:
            chat_id: 会话ID
            user_id: 用户ID (用户消息存真实user_id，助手消息存卖家ID)
            item_id: 商品ID
            role: 消息角色 (user/assistant)
            content: 消息内容
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            clean_type = str(content_type or "text").strip().lower()
            if clean_type not in {"text", "image", "emoji", "audio", "video", "file", "link", "rich", "unknown"}:
                clean_type = "unknown"
            encoded_media = media_json(media)
            # 插入新消息，使用chat_id作为额外标识
            cursor.execute(
                """
                INSERT OR IGNORE INTO messages
                    (user_id, item_id, role, content, timestamp, chat_id, source_id, content_type, media_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                , (
                    user_id, item_id, role, content, self._now_iso(), chat_id, source_id,
                    clean_type, encoded_media,
                )
            )

            inserted = cursor.rowcount == 1

            cursor.execute(
                """
                DELETE FROM messages
                WHERE chat_id = ?
                  AND role != 'assistant_pending'
                  AND id NOT IN (
                    SELECT id FROM messages
                    WHERE chat_id = ? AND role != 'assistant_pending'
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                  )
                """,
                (chat_id, chat_id, self.max_history),
            )

            conn.commit()
            return inserted
        except Exception as e:
            logger.error("添加消息到数据库失败: {}", type(e).__name__)
            conn.rollback()
            return False
        finally:
            conn.close()

    def latest_item_id_by_chat(self, chat_id):
        """Return the most recent item context for one chat, if available."""
        selected = str(chat_id or "").strip()
        if not selected:
            return ""
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            row = conn.execute(
                """SELECT item_id FROM messages
                   WHERE chat_id = ? ORDER BY id DESC LIMIT 1""",
                (selected,),
            ).fetchone()
            return str(row[0] or "") if row else ""
        except sqlite3.Error:
            return ""
        finally:
            conn.close()

    def get_source_message(self, source_id):
        if not source_id:
            return None
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content, content_type, media_json
                FROM messages WHERE source_id = ? LIMIT 1
                """,
                (str(source_id),),
            ).fetchone()
            if row is not None:
                return dict(row)
            outcome = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content,
                       'text' AS content_type, '[]' AS media_json
                FROM assistant_outcomes WHERE source_id = ? LIMIT 1
                """,
                (str(source_id),),
            ).fetchone()
            return dict(outcome) if outcome is not None else None
        except Exception as e:
            logger.error("读取消息来源失败: {}", type(e).__name__)
            return None
        finally:
            conn.close()

    def prepare_assistant_reply(
        self, chat_id, user_id, item_id, content, source_id
    ):
        if (
            not source_id
            or not isinstance(content, str)
            or not content
            or len(content) > 4096
        ):
            raise ValueError("assistant reply draft is invalid")
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_assistant_outcomes(conn, self.now_fn())
            source_key = str(source_id)
            expected = {
                "chat_id": str(chat_id),
                "user_id": str(user_id),
                "item_id": str(item_id),
            }
            allowed_roles = self.TERMINAL_ASSISTANT_ROLES | {"assistant_pending"}

            message = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM messages WHERE source_id = ?
                """,
                (source_key,),
            ).fetchone()
            outcome = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM assistant_outcomes WHERE source_id = ?
                """,
                (source_key,),
            ).fetchone()

            for record, label in ((message, "assistant reply"), (outcome, "assistant outcome")):
                if record is None:
                    continue
                if any(record[field] != value for field, value in expected.items()):
                    raise RuntimeError(f"{label} source collision")
                if record["role"] not in allowed_roles:
                    raise RuntimeError(f"{label} has invalid state")

            # The durable outcome is authoritative once it exists. This also
            # handles direct callers after the bounded message history pruned
            # the original row, and preserves a pending draft verbatim.
            if outcome is not None:
                if message is not None and message["role"] != outcome["role"]:
                    raise RuntimeError("assistant reply state collision")
                conn.commit()
                return dict(outcome)

            if message is None:
                now = self._now_iso()
                conn.execute(
                    """
                    INSERT INTO messages
                        (user_id, item_id, role, content, timestamp, chat_id, source_id)
                    VALUES (?, ?, 'assistant_pending', ?, ?, ?, ?)
                    """,
                    (
                        expected["user_id"],
                        expected["item_id"],
                        content,
                        now,
                        expected["chat_id"],
                        source_key,
                    ),
                )
                message = conn.execute(
                    """
                    SELECT chat_id, user_id, item_id, role, content
                    FROM messages WHERE source_id = ?
                    """,
                    (source_key,),
                ).fetchone()
                if message is None:
                    raise RuntimeError("assistant reply draft was not persisted")

            # A legacy terminal message may predate assistant_outcomes. Make
            # it visible to the idempotency ledger before returning it.
            now = self._now_iso()
            conn.execute(
                """
                INSERT INTO assistant_outcomes(
                    source_id, chat_id, user_id, item_id, role,
                    content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO NOTHING
                """,
                (
                    source_key,
                    message["chat_id"],
                    message["user_id"],
                    message["item_id"],
                    message["role"],
                    message["content"],
                    now,
                    now,
                ),
            )
            outcome = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM assistant_outcomes WHERE source_id = ?
                """,
                (source_key,),
            ).fetchone()
            if (
                outcome is None
                or any(outcome[field] != value for field, value in expected.items())
                or outcome["role"] not in allowed_roles
            ):
                raise RuntimeError("assistant outcome source collision")
            conn.commit()
            return dict(outcome)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_assistant_reply(self, source_id, content=None):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_assistant_outcomes(conn, self.now_fn())
            row = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM messages WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT chat_id, user_id, item_id, role, content
                    FROM assistant_outcomes WHERE source_id = ?
                    """,
                    (str(source_id),),
                ).fetchone()
            if row is None:
                raise RuntimeError("assistant reply draft is missing")
            if row["role"] == "assistant":
                conn.commit()
                return
            if row["role"] != "assistant_pending":
                raise RuntimeError("assistant reply draft has invalid state")
            final_content = row["content"] if content is None else str(content)
            if content is None:
                conn.execute(
                    "UPDATE messages SET role = 'assistant' WHERE source_id = ?",
                    (str(source_id),),
                )
            else:
                conn.execute(
                    """
                    UPDATE messages SET role = 'assistant', content = ?
                    WHERE source_id = ?
                    """,
                    (str(content), str(source_id)),
                )
            now = self._now_iso()
            conn.execute(
                """
                INSERT INTO assistant_outcomes(
                    source_id, chat_id, user_id, item_id, role,
                    content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    role = 'assistant', content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (
                    str(source_id),
                    row["chat_id"],
                    row["user_id"],
                    row["item_id"],
                    final_content,
                    now,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_assistant_reply(self, source_id):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_assistant_outcomes(conn, self.now_fn())
            row = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM messages WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT chat_id, user_id, item_id, role, content
                    FROM assistant_outcomes WHERE source_id = ?
                    """,
                    (str(source_id),),
                ).fetchone()
            if row is None:
                raise RuntimeError("assistant reply draft is missing")
            if row["role"] in self.TERMINAL_ASSISTANT_ROLES:
                conn.commit()
                return
            if row["role"] != "assistant_pending":
                raise RuntimeError("assistant reply draft has invalid state")
            conn.execute(
                """
                UPDATE messages SET role = 'assistant_cancelled'
                WHERE source_id = ? AND role = 'assistant_pending'
                """,
                (str(source_id),),
            )
            now = self._now_iso()
            conn.execute(
                """
                INSERT INTO assistant_outcomes(
                    source_id, chat_id, user_id, item_id, role,
                    content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'assistant_cancelled', ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    role = 'assistant_cancelled', updated_at = excluded.updated_at
                """,
                (
                    str(source_id),
                    row["chat_id"],
                    row["user_id"],
                    row["item_id"],
                    row["content"],
                    now,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_assistant_outcome(
        self, chat_id, user_id, item_id, source_id, role, content=""
    ):
        if role not in {"assistant_cancelled", "assistant_no_reply"}:
            raise ValueError("assistant outcome role is invalid")
        if not source_id or not isinstance(content, str) or len(content) > 4096:
            raise ValueError("assistant outcome is invalid")
        now = self._now_iso()
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_assistant_outcomes(conn, self.now_fn())
            conn.execute(
                """
                INSERT OR IGNORE INTO messages(
                    user_id, item_id, role, content, timestamp, chat_id, source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    str(item_id),
                    role,
                    content,
                    now,
                    str(chat_id),
                    str(source_id),
                ),
            )
            message = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM messages WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
            if (
                message is None
                or message["chat_id"] != str(chat_id)
                or message["user_id"] != str(user_id)
                or message["item_id"] != str(item_id)
                or message["role"] != role
            ):
                raise RuntimeError("assistant message source collision")
            conn.execute(
                """
                INSERT INTO assistant_outcomes(
                    source_id, chat_id, user_id, item_id, role,
                    content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO NOTHING
                """,
                (
                    str(source_id),
                    str(chat_id),
                    str(user_id),
                    str(item_id),
                    role,
                    content,
                    now,
                    now,
                ),
            )
            outcome = conn.execute(
                """
                SELECT chat_id, user_id, item_id, role, content
                FROM assistant_outcomes WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
            if (
                outcome is None
                or outcome["chat_id"] != str(chat_id)
                or outcome["user_id"] != str(user_id)
                or outcome["item_id"] != str(item_id)
                or outcome["role"] != role
            ):
                raise RuntimeError("assistant outcome source collision")
            conn.commit()
            return dict(outcome)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_recent_assistant_replies(self, chat_id, limit=8):
        """Return bounded, successfully sent assistant texts for duplicate defense."""
        try:
            selected_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("recent assistant reply limit is invalid") from exc
        if selected_limit < 1 or selected_limit > 50:
            raise ValueError("recent assistant reply limit is invalid")
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            rows = conn.execute(
                """
                SELECT content FROM messages
                WHERE chat_id = ? AND role = 'assistant'
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (str(chat_id), selected_limit),
            ).fetchall()
            return [
                str(row[0]).strip()
                for row in reversed(rows)
                if isinstance(row[0], str) and row[0].strip()
            ]
        except sqlite3.Error as exc:
            logger.error("读取近期助手回复失败: {}", type(exc).__name__)
            raise RuntimeError("recent assistant replies are unavailable") from exc
        finally:
            conn.close()

    def get_context_by_chat(self, chat_id):
        """
        基于会话ID获取对话历史

        Args:
            chat_id: 会话ID

        Returns:
            list: 包含对话历史的列表
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content, timestamp FROM messages
                    WHERE chat_id = ? AND role IN ('user', 'assistant')
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC, id ASC
                """,
                (chat_id, self.max_history),
            )

            messages = [{"role": role, "content": content} for role, content in cursor.fetchall()]

            # 获取议价次数并添加到上下文中
            bargain_count = self.get_bargain_count_by_chat(chat_id)
            if bargain_count > 0:
                messages.append({
                    "role": "system",
                    "content": f"议价次数: {bargain_count}"
                })

        except Exception as e:
            logger.error("获取对话历史失败: {}", type(e).__name__)
            messages = []
        finally:
            conn.close()

        return messages

    def increment_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID增加议价次数

        Args:
            chat_id: 会话ID
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            # 使用UPSERT语法直接基于chat_id增加议价次数
            cursor.execute(
                """
                INSERT INTO chat_bargain_counts (chat_id, count, last_updated)
                VALUES (?, 1, ?)
                ON CONFLICT(chat_id)
                DO UPDATE SET count = count + 1, last_updated = ?
                """,
                (chat_id, self._now_iso(), self._now_iso())
            )

            conn.commit()
            logger.debug("会话议价次数已增加 chat={}", stable_ref(chat_id))
        except Exception as e:
            logger.error("增加议价次数失败: {}", type(e).__name__)
            conn.rollback()
        finally:
            conn.close()

    def get_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID获取议价次数

        Args:
            chat_id: 会话ID

        Returns:
            int: 议价次数
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT count FROM chat_bargain_counts WHERE chat_id = ?",
                (chat_id,)
            )

            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error("获取议价次数失败: {}", type(e).__name__)
            return 0
        finally:
            conn.close()

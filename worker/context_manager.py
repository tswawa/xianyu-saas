import sqlite3
import os
import json
import hashlib
import math
import time
from datetime import datetime
from loguru import logger


def stable_ref(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


_MEDIA_TYPES = frozenset({"image", "emoji", "audio", "video", "file", "link", "unknown"})


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
    for raw in media[:8]:
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


def media_json(media):
    clean = normalize_media(media)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded.encode("utf-8")) <= 64 * 1024 else "[]"


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
                """SELECT id FROM manual_reply_drafts
                   WHERE status IN ('queued', 'retry')
                     AND attempts < max_attempts
                     AND available_at <= ?
                   ORDER BY available_at, id LIMIT ?""",
                (now, limit),
            ).fetchall()
            claimed = []
            for candidate in candidates:
                reply_id = int(candidate["id"])
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
                    claimed.append(dict(row))
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

    def acknowledge_manual_reply(self, reply_id, owner, sender_id, sent_media=None):
        """Atomically persist the platform ACK and the seller chat message.

        ``sent_media`` is the public platform media summary returned after a
        successful upload/send.  The outbox's private local path is retained
        only for retry and cleanup, never as the chat message's display media.
        """
        try:
            reply_id = int(reply_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("manual reply id is invalid") from exc
        owner = str(owner or "")
        sender_id = str(sender_id or "").strip()
        if reply_id < 1 or not sender_id:
            raise ValueError("manual reply acknowledgement is invalid")
        now = float(self.now_fn())
        timestamp = datetime.fromtimestamp(now).isoformat()
        source_id = f"manual_reply:{reply_id}"
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT chat_id, item_id, content, media_json, status, lease_owner, lease_until
                   FROM manual_reply_drafts WHERE id = ?""",
                (reply_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            if row["status"] == "acknowledged":
                conn.commit()
                return True
            if (
                row["status"] != "sending"
                or str(row["lease_owner"] or "") != owner
                or float(row["lease_until"] or 0) <= now
            ):
                conn.rollback()
                return False
            existing = conn.execute(
                """SELECT chat_id, item_id, role, content FROM messages
                   WHERE source_id = ?""",
                (source_id,),
            ).fetchone()
            candidate_media = sent_media
            if isinstance(candidate_media, dict):
                candidate_media = [candidate_media]
            persisted_media = normalize_media(candidate_media, allow_paths=False)
            if not persisted_media:
                persisted_media = normalize_media(row["media_json"], allow_paths=False)
            persisted_media_json = media_json(persisted_media)
            persisted_type = (
                "image" if len(persisted_media) == 1 and persisted_media[0].get("type") == "image"
                else "rich" if persisted_media
                else "text"
            )
            if existing is None:
                conn.execute(
                    """                    INSERT INTO messages(
                           user_id, item_id, role, content, timestamp, chat_id, source_id,
                           content_type, media_json
                       ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?)""",

                    (
                        sender_id,
                        str(row["item_id"] or ""),
                        str(row["content"] or ""),
                        timestamp,
                        str(row["chat_id"] or ""),
                        source_id,
                        persisted_type,
                        persisted_media_json,
                    ),
                )
            elif (
                str(existing["chat_id"] or "") != str(row["chat_id"] or "")
                or str(existing["item_id"] or "") != str(row["item_id"] or "")
                or str(existing["role"] or "") != "assistant"
                or str(existing["content"] or "") != str(row["content"] or "")
            ):
                raise RuntimeError("manual reply acknowledgement collision")
            conn.execute(
                """UPDATE manual_reply_drafts
                   SET status = 'acknowledged', lease_owner = NULL,
                       lease_until = NULL, last_error_code = NULL,
                       updated_at = ?, acknowledged_at = ?
                   WHERE id = ?""",
                (now, now, reply_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_manual_reply(self, reply_id):
        try:
            reply_id = int(reply_id)
        except (TypeError, ValueError):
            return None
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT id, request_id, chat_id, recipient_id, item_id,
                          content, media_json, status, attempts, max_attempts,
                          available_at, lease_owner, lease_until,
                          last_error_code, acknowledged_at
                   FROM manual_reply_drafts WHERE id = ?""",
                (reply_id,),
            ).fetchone()
            return dict(row) if row is not None else None
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

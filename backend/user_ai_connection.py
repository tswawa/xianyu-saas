"""One encrypted, revisioned AI connection per control-plane user.

A missing row is legacy mode; a cleared row is a permanent user-scope tombstone.
Neither construction nor metadata reads initialize account storage. Network I/O
always happens outside the DB lock, and credentials and metadata commit together.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from account_storage import AccountStorageError, normalize_account_key
from ai_customer_service import AIServiceError, CONNECTION_FILE, VERIFICATION_TTL_SECONDS, _bounded_text
from ai_provider_adapters import ProviderAdapterError, is_api_key_required, normalize_provider


_NAMESPACE = b"xianyu-saas:user-ai-connection:v1\0"
_PUBLIC_COLUMNS = (
    "provider, base_url, model, api_key_configured, connection_status, "
    "revision, key_revision, last_error_code"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_ai_connections (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'openai_chat_completions',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key_configured INTEGER NOT NULL CHECK (api_key_configured IN (0, 1)),
    connection_status TEXT NOT NULL CHECK (connection_status IN ('verified', 'unconfigured')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    key_revision INTEGER NOT NULL CHECK (key_revision > 0),
    last_error_code TEXT NOT NULL DEFAULT '',
    nonce BLOB,
    ciphertext BLOB,
    CHECK ((api_key_configured = 0 AND nonce IS NULL AND ciphertext IS NULL)
        OR (api_key_configured = 1 AND typeof(nonce) = 'blob' AND length(nonce) = 12
            AND typeof(ciphertext) = 'blob' AND length(ciphertext) > 16))
)
"""


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _guard(method):
    """Never expose SQLite, filesystem, crypto or requester exception messages."""
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except AIServiceError:
            raise
        except sqlite3.Error as exc:
            raise AIServiceError("credential_store_unavailable", 503) from exc
        except AccountStorageError as exc:
            raise AIServiceError("credential_unavailable", 503) from exc
        except ProviderAdapterError as exc:
            raise AIServiceError(exc.code, 400) from exc
        except Exception as exc:
            raise AIServiceError("service_unavailable", 503) from exc
    return wrapped


def _integer(value, *, positive=False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AIServiceError("invalid_payload", 400)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AIServiceError("invalid_payload", 400) from exc
    if result < (1 if positive else 0) or result >= 2**63 - 1:
        raise AIServiceError("invalid_payload", 400)
    return result


def _defaults() -> dict:
    return {
        "scope": "user", "initialized": False,
        "provider": "openai_chat_completions", "base_url": "", "model": "",
        "api_key_configured": False, "connection_status": "unconfigured",
        "revision": 0, "key_revision": 0, "last_error_code": "",
    }


class UserAIConnections:
    @_guard
    def __init__(self, db, ai_service):
        self.db = db
        self.ai_service = ai_service
        # No migration, key loading, tenant directories, PRAGMAs or other tables.
        with self.db._lock:
            self.db.con.execute(_SCHEMA)
            self._user_disabled_column = "disabled_at" in {row[1] for row in self.db.con.execute("PRAGMA table_info(users)")}

    def _user_exists(self, uid: int) -> None:
        active = " AND disabled_at IS NULL" if self._user_disabled_column else ""
        if self.db.con.execute("SELECT id FROM users WHERE id = ?" + active, (uid,)).fetchone() is None:
            # Recheck after network calls and inside commits: an earlier HTTP
            # authorization is not permission to save for a now-disabled user.
            raise AIServiceError("user_not_found", 404, "用户不存在或已停用")

    @staticmethod
    def _metadata(row) -> dict:
        result = _defaults()
        if row is not None:
            result.update({name: row[name] for name in result if name not in {"scope", "initialized"}})
            result["initialized"] = True
            result["api_key_configured"] = row["api_key_configured"] == 1
        return result

    def _snapshot(self, uid: int, *, secret=False) -> dict:
        columns = _PUBLIC_COLUMNS + (", nonce, ciphertext" if secret else "")
        with self.db._lock:
            self._user_exists(uid)
            cursor = self.db.con.execute(
                f"SELECT {columns} FROM user_ai_connections WHERE user_id = ?", (uid,)
            )
            raw = cursor.fetchone()
            row = dict(zip((column[0] for column in cursor.description), raw)) if raw is not None else None
        result = self._metadata(row)
        if secret:
            result.update(nonce=row["nonce"] if row else None, ciphertext=row["ciphertext"] if row else None)
        return result

    @_guard
    def read(self, user_id: int) -> dict:
        """Return only the public allowlist; do not load keys or account files."""
        return self._snapshot(_integer(user_id, positive=True))

    @_guard
    def initialized(self, user_id: int) -> bool:
        return self.read(user_id)["initialized"]

    @_guard
    def revision(self, user_id: int) -> int:
        return self.read(user_id)["revision"]

    def _keys(self) -> tuple[bytes, bytes]:
        encryption, signing = self.ai_service._master_keys()
        # Derivation and AAD/signature prefixes are independent of shop scope.
        return (
            hmac.new(encryption, _NAMESPACE + b"encryption", hashlib.sha256).digest(),
            hmac.new(signing, _NAMESPACE + b"verification", hashlib.sha256).digest(),
        )

    @staticmethod
    def _aad(uid: int, metadata: dict) -> bytes:
        return _NAMESPACE + b"credential\0" + _json({
            "uid": uid,
            **{key: metadata[key] for key in (
                "provider", "base_url", "model", "revision", "key_revision", "connection_status",
            )},
        })

    def _encrypt(self, uid: int, metadata: dict, api_key: str) -> tuple[bytes | None, bytes | None]:
        if not api_key:
            return None, None
        encryption, _ = self._keys()
        nonce = secrets.token_bytes(12)
        return nonce, AESGCM(encryption).encrypt(nonce, api_key.encode("utf-8"), self._aad(uid, metadata))

    def _decrypt(self, uid: int, row: dict) -> str:
        try:
            encryption, _ = self._keys()
            raw = AESGCM(encryption).decrypt(row["nonce"], row["ciphertext"], self._aad(uid, row))
            return _bounded_text(raw.decode("utf-8"), 4096, required=True)
        except AIServiceError as exc:
            if exc.code == "credential_store_unavailable":
                raise
            raise AIServiceError("credential_unavailable", 503) from exc
        except Exception as exc:
            raise AIServiceError("credential_unavailable", 503) from exc

    @_guard
    def runtime(self, user_id: int) -> dict:
        uid = _integer(user_id, positive=True)
        row = self._snapshot(uid, secret=True)
        provider = normalize_provider(row["provider"])
        if not row["initialized"] or row["connection_status"] == "unconfigured":
            raise AIServiceError("connection_unconfigured", 503)
        if row["connection_status"] != "verified":
            raise AIServiceError("connection_unverified", 503)
        if is_api_key_required(provider) and not row["api_key_configured"]:
            raise AIServiceError("connection_unconfigured", 503)
        api_key = self._decrypt(uid, row) if row["api_key_configured"] else ""
        base_url = self.ai_service.normalize_base_url(row["base_url"], provider)
        model = _bounded_text(row["model"], 200, required=True)
        # A deletion racing decryption must not return the old credential.
        if self.read(uid)["revision"] != row["revision"]:
            raise AIServiceError("revision_conflict", 409)
        return {"provider": provider, "base_url": base_url, "model": model,
                "api_key": api_key, "revision": row["revision"]}

    def _source_scope(self, uid: int, account_key: str) -> tuple[int, int, str]:
        with self.db._lock:
            self._user_exists(uid)
            row = self.db.con.execute(
                "SELECT id FROM shop_accounts WHERE user_id = ? AND account_key = ? AND enabled = 1",
                (uid, account_key),
            ).fetchone()
        if row is None:
            raise AIServiceError("source_not_found", 404, "来源店铺不存在或已停用")
        return self.ai_service._scope(uid, int(row[0]), account_key)

    @staticmethod
    def _source_fingerprint(metadata: dict) -> str:
        return hashlib.sha256(_json({key: metadata.get(key) for key in (
            "provider", "base_url", "model", "api_key_configured", "connection_status",
            "revision", "key_revision",
        )})).hexdigest()

    def _check_source(self, source: dict) -> dict:
        try:
            scope = self._source_scope(source["uid"], source["account_key"])
        except AIServiceError as exc:
            if exc.code == "source_not_found":
                raise AIServiceError("source_revision_conflict", 409, "来源连接已更新，请重新测试") from exc
            raise
        if scope[1] != source["shop_account_id"]:
            raise AIServiceError("source_revision_conflict", 409, "来源连接已更新，请重新测试")
        # Never call get_runtime_connection: it may already delegate back here.
        metadata = self.ai_service.get_connection(*scope)
        if (metadata["revision"] != source["revision"]
                or self._source_fingerprint(metadata) != source["fingerprint"]):
            raise AIServiceError("source_revision_conflict", 409, "来源连接已更新，请重新测试")
        return metadata

    def _candidate(self, user_id, provider, base_url, model, api_key,
                   expected_revision, source_account_key, source_revision) -> dict:
        uid = _integer(user_id, positive=True)
        expected_revision = _integer(expected_revision)
        current = self._snapshot(uid, secret=True)
        if current["revision"] != expected_revision:
            raise AIServiceError("revision_conflict", 409)
        provider = normalize_provider(provider)
        base_url = self.ai_service.normalize_base_url(base_url, provider)
        model = _bounded_text(model, 200, required=True)
        key = _bounded_text(api_key, 4096)
        account_key = _bounded_text(source_account_key, 128)
        source = None
        if account_key:
            try:
                account_key = normalize_account_key(account_key)
            except AccountStorageError as exc:
                raise AIServiceError("invalid_payload", 400) from exc
            selected_revision = _integer(source_revision)
            scope = self._source_scope(uid, account_key)
            legacy = self.ai_service.get_connection(*scope)
            if legacy["revision"] != selected_revision:
                raise AIServiceError("source_revision_conflict", 409, "来源连接已更新，请重新测试")
            source = {"uid": uid, "account_key": account_key, "shop_account_id": scope[1],
                      "revision": selected_revision, "fingerprint": self._source_fingerprint(legacy)}
            if not key and normalize_provider(legacy["provider"]) == provider and legacy["api_key_configured"]:
                key = self.ai_service._decrypt_key(scope, legacy)
            self._check_source(source)
        elif source_revision is not None:
            raise AIServiceError("invalid_payload", 400)
        elif not key and current["provider"] == provider and current["api_key_configured"]:
            key = self._decrypt(uid, current)
        if not key and is_api_key_required(provider):
            raise AIServiceError("connection_unconfigured", 409, "请填写对应接口的 API Key 或明确选择旧连接")
        return {"uid": uid, "current": current, "provider": provider, "base_url": base_url,
                "model": model, "api_key": key, "source": source,
                "revision": expected_revision, "key_revision": current["key_revision"] + 1}

    def _claims(self, candidate: dict) -> dict:
        _, signing = self._keys()
        fingerprint = hmac.new(signing, _NAMESPACE + b"candidate\0" + _json({
            key: candidate[key] for key in ("provider", "base_url", "model", "api_key")
        }), hashlib.sha256).hexdigest()
        return {"scope": "user", "v": 1, "uid": candidate["uid"],
                "revision": candidate["revision"], "key_revision": candidate["key_revision"],
                "source": candidate["source"], "fingerprint": fingerprint}

    def _issue_token(self, candidate: dict) -> str:
        now = int(self.ai_service.clock())
        claims = {**self._claims(candidate), "iat": now, "exp": now + VERIFICATION_TTL_SECONDS}
        raw = base64.urlsafe_b64encode(_json(claims)).rstrip(b"=")
        _, signing = self._keys()
        signature = hmac.new(signing, _NAMESPACE + b"token\0" + raw, hashlib.sha256).digest()
        return raw.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def _verify_token(self, token: str, candidate: dict) -> int:
        # Configuration/master-key errors remain distinguishable from bad tokens.
        _, signing = self._keys()
        expected = self._claims(candidate)
        try:
            raw_text, signature_text = _bounded_text(token, 4096, required=True).split(".")
            raw = raw_text.encode("ascii")
            signature = base64.b64decode(signature_text + "=" * (-len(signature_text) % 4),
                                         altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(signing, _NAMESPACE + b"token\0" + raw, hashlib.sha256).digest()):
                raise ValueError("signature")
            claims = json.loads(base64.b64decode(raw_text + "=" * (-len(raw_text) % 4),
                                               altchars=b"-_", validate=True))
            now = int(self.ai_service.clock())
            if (not isinstance(claims, dict) or set(claims) != set(expected) | {"iat", "exp"}
                    or any(claims.get(key) != value for key, value in expected.items())
                    or type(claims["iat"]) is not int or type(claims["exp"]) is not int
                    or claims["exp"] - claims["iat"] != VERIFICATION_TTL_SECONDS
                    or not claims["iat"] <= now < claims["exp"]):
                raise ValueError("claims")
            return claims["exp"]
        except Exception as exc:
            raise AIServiceError("verification_invalid", 409) from exc

    @contextmanager
    def _transaction(self):
        with self.db._lock:
            # Do not commit/rollback a transaction owned by an unrelated caller.
            if self.db.con.in_transaction:
                raise AIServiceError("credential_store_unavailable", 503)
            with self.db.con:
                self.db.con.execute("BEGIN IMMEDIATE")
                yield

    def _commit(self, uid: int, previous: dict, saved: dict, *, source=None, expires=None) -> dict:
        with self._transaction():
            self._user_exists(uid)
            current = self.db.con.execute(
                "SELECT revision FROM user_ai_connections WHERE user_id = ?", (uid,)
            ).fetchone()
            if (current is not None) != previous["initialized"] or (int(current[0]) if current else 0) != previous["revision"]:
                raise AIServiceError("revision_conflict", 409)
            if source is not None:
                self._check_source(source)
            if expires is not None and int(self.ai_service.clock()) >= expires:
                raise AIServiceError("verification_invalid", 409)
            values = tuple(saved[name] for name in (
                "provider", "base_url", "model", "api_key_configured", "connection_status",
                "revision", "key_revision", "last_error_code", "nonce", "ciphertext",
            ))
            if current is None:
                cursor = self.db.con.execute(
                    "INSERT INTO user_ai_connections (user_id, " + _PUBLIC_COLUMNS + ", nonce, ciphertext) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
                    (uid, *values),
                )
            else:
                cursor = self.db.con.execute(
                    "UPDATE user_ai_connections SET provider=?, base_url=?, model=?, api_key_configured=?, "
                    "connection_status=?, revision=?, key_revision=?, last_error_code=?, nonce=?, ciphertext=? "
                    "WHERE user_id=? AND revision=?",
                    (*values, uid, previous["revision"]),
                )
            if cursor.rowcount != 1:
                raise AIServiceError("revision_conflict", 409)
        return self._metadata(saved)

    @_guard
    def test(self, user_id: int, *, provider: str, base_url: str, model: str, api_key: str = "",
             expected_revision: int, source_account_key: str = "", source_revision=None,
             routing_session: str = "") -> dict:
        candidate = self._candidate(user_id, provider, base_url, model, api_key,
                                    expected_revision, source_account_key, source_revision)
        # Fail before a request if token signing is unavailable; no keys persist.
        self._keys()
        routing_session = _bounded_text(routing_session, 200)
        if any(ord(char) < 32 or ord(char) == 127 for char in routing_session):
            raise AIServiceError("invalid_payload", 400)
        payload = {
            "messages": [{"role": "system", "content": "Return only OK."},
                         {"role": "user", "content": "Connection test."}],
            "max_tokens": 256 if candidate["provider"] == "openai_responses" else 32,
            "temperature": 0,
        }
        if (candidate["provider"] == "openai_chat_completions"
                and candidate["model"].lower() in {"deepseek-v4-flash", "deepseek-v4-pro"}):
            payload["thinking"] = {"type": "disabled"}
        if not routing_session:
            routing_session = self.ai_service._routing_session(("user", candidate["uid"]), payload["messages"])
        response = self.ai_service._request_json(
            candidate["provider"], candidate["base_url"], candidate["model"], candidate["api_key"],
            payload, routing_session=routing_session,
        )
        self.ai_service._response_text(response)
        if self.read(candidate["uid"])["revision"] != candidate["revision"]:
            raise AIServiceError("revision_conflict", 409)
        if candidate["source"] is not None:
            self._check_source(candidate["source"])
        return {"ok": True, "status": "verified", "verification_token": self._issue_token(candidate),
                "expires_in": VERIFICATION_TTL_SECONDS}

    @_guard
    def save(self, user_id: int, *, provider: str, base_url: str, model: str, api_key: str = "",
             expected_revision: int, verification_token: str, confirm: bool,
             source_account_key: str = "", source_revision=None) -> dict:
        if confirm is not True:
            raise AIServiceError("confirmation_required", 409, "请确认此修改影响本人全部店铺")
        try:
            candidate = self._candidate(user_id, provider, base_url, model, api_key,
                                        expected_revision, source_account_key, source_revision)
        except AIServiceError as exc:
            if exc.code == "source_not_found":
                raise AIServiceError("source_revision_conflict", 409, "来源连接已更新，请重新测试") from exc
            raise
        expires = self._verify_token(verification_token, candidate)
        saved = {"provider": candidate["provider"], "base_url": candidate["base_url"],
                 "model": candidate["model"], "api_key_configured": bool(candidate["api_key"]),
                 "connection_status": "verified", "revision": candidate["revision"] + 1,
                 "key_revision": candidate["key_revision"], "last_error_code": ""}
        saved["nonce"], saved["ciphertext"] = self._encrypt(candidate["uid"], saved, candidate["api_key"])
        return self._commit(candidate["uid"], candidate["current"], saved,
                            source=candidate["source"], expires=expires)

    @_guard
    def delete(self, user_id: int, *, expected_revision: int, confirm: bool) -> dict:
        if confirm is not True:
            raise AIServiceError("confirmation_required", 409, "请确认此删除影响本人全部店铺")
        uid = _integer(user_id, positive=True)
        expected_revision = _integer(expected_revision)
        current = self._snapshot(uid)
        if current["revision"] != expected_revision:
            raise AIServiceError("revision_conflict", 409)
        saved = {**_defaults(), "revision": expected_revision + 1,
                 "key_revision": current["key_revision"] + 1, "nonce": None, "ciphertext": None}
        # Keep the row even when no shared connection has ever been saved.
        # Do not touch legacy connections, shop content, enabled flags or files.
        return self._commit(uid, current, saved)

    @_guard
    def legacy_sources(self, user_id: int) -> list[dict]:
        uid = _integer(user_id, positive=True)
        with self.db._lock:
            self._user_exists(uid)
            rows = self.db.con.execute(
                "SELECT id, account_key, display_name FROM shop_accounts "
                "WHERE user_id = ? AND enabled = 1 ORDER BY id", (uid,),
            ).fetchall()
        result = []
        for shop_id, account_key, display_name in rows:
            # Missing legacy files are not sources; probing must not initialize a
            # never-used shop directory just to show migration choices.
            path = self.ai_service.storage.account_dir(uid, account_key) / CONNECTION_FILE
            if not path.exists() and not path.is_symlink():
                continue
            scope = self.ai_service._scope(uid, shop_id, account_key)
            try:
                legacy = self.ai_service.get_connection(*scope)
                summary = {key: legacy[key] for key in _defaults() if key not in {"scope", "initialized"}}
            except AIServiceError as exc:
                summary = {key: value for key, value in _defaults().items() if key not in {"scope", "initialized"}}
                summary.update(connection_status="credential_unavailable", last_error_code=exc.code)
            name = str(display_name or "")[:200]
            result.append({"account_key": account_key, "shop_account_id": shop_id,
                           "display_name": name, "name": name, **summary})
        return result


__all__ = ["UserAIConnections"]

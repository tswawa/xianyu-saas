"""账号私有认证材料的分层读写。

长期 cookies.txt 永远只读；运行时只持久化两个 MTOP 短期 Cookie。
"""

import json
import os
import stat
import uuid


MTOP_COOKIE_NAMES = frozenset({"_m_h5_tk", "_m_h5_tk_enc"})
MAX_COOKIE_FILE_BYTES = 256 * 1024
MAX_SHORT_COOKIE_VALUE_CHARS = 8192


def parse_cookie_header(cookie_header):
    if not isinstance(cookie_header, str):
        return {}
    cookies = {}
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name or any(character in name for character in "\r\n;="):
            continue
        if any(character in value for character in "\r\n;"):
            continue
        cookies[name] = value
    return cookies


def cookie_header(cookies):
    return "; ".join(
        f"{name}={value}"
        for name, value in cookies.items()
        if isinstance(name, str)
        and isinstance(value, str)
        and name
        and not any(character in name for character in "\r\n;=")
        and not any(character in value for character in "\r\n;")
    )


class PrivateAuthStorage:
    def __init__(self, state_dir):
        self.state_dir = os.path.abspath(state_dir)
        self.long_cookie_path = os.path.join(self.state_dir, "cookies.txt")
        self.short_cookie_path = os.path.join(self.state_dir, "mtop_cookies.json")

    @staticmethod
    def _read_regular_file(path, maximum_bytes, *, require_private=False):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(path, flags)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum_bytes:
                raise RuntimeError("private auth file is invalid")
            if require_private and stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise RuntimeError("private auth file mode is invalid")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                return handle.read()
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def load_long_cookie_header(self, fallback=""):
        """读取长期 Cookie 基线；文件不存在时兼容现有环境变量注入。"""
        try:
            raw = self._read_regular_file(
                self.long_cookie_path, MAX_COOKIE_FILE_BYTES
            )
        except FileNotFoundError:
            raw = fallback
        return cookie_header(parse_cookie_header(raw.strip()))

    def load_short_cookies(self):
        try:
            raw = self._read_regular_file(
                self.short_cookie_path, 32 * 1024, require_private=True
            )
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("mtop cookie file is invalid") from exc
        if not isinstance(payload, dict) or set(payload) - MTOP_COOKIE_NAMES:
            raise RuntimeError("mtop cookie file contains unsupported fields")
        normalized = {}
        for name, value in payload.items():
            if (
                name not in MTOP_COOKIE_NAMES
                or not isinstance(value, str)
                or not value
                or len(value) > MAX_SHORT_COOKIE_VALUE_CHARS
                or any(character in value for character in "\r\n;")
            ):
                raise RuntimeError("mtop cookie value is invalid")
            normalized[name] = value
        return normalized

    def merged_cookie_header(self, fallback=""):
        merged = parse_cookie_header(self.load_long_cookie_header(fallback))
        merged.update(self.load_short_cookies())
        return cookie_header(merged)

    @staticmethod
    def _cookie_mapping(cookies):
        values = {}
        if hasattr(cookies, "__iter__") and not isinstance(cookies, dict):
            try:
                for item in cookies:
                    name = str(item.name)
                    value = str(item.value)
                    if name in MTOP_COOKIE_NAMES:
                        values[name] = value
                return values
            except (AttributeError, TypeError):
                pass
        if hasattr(cookies, "items"):
            for name, value in cookies.items():
                if str(name) in MTOP_COOKIE_NAMES:
                    values[str(name)] = str(value)
        return values

    def persist_short_cookies(self, cookies):
        """仅原子写入白名单短期 Cookie，绝不触碰 cookies.txt。"""
        values = self._cookie_mapping(cookies)
        values = {
            name: value
            for name, value in values.items()
            if value
            and len(value) <= MAX_SHORT_COOKIE_VALUE_CHARS
            and not any(character in value for character in "\r\n;")
        }
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        temporary_path = os.path.join(
            self.state_dir,
            f".mtop_cookies.{os.getpid()}.{uuid.uuid4().hex}.tmp",
        )
        descriptor = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                json.dump(values, handle, ensure_ascii=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.short_cookie_path)
            os.chmod(self.short_cookie_path, 0o600)
            directory_descriptor = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        return values

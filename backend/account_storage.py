"""Account-scoped runtime storage with a legacy ``default`` account.

The control plane historically stored a user's runtime files directly under
``<tenants_root>/<user_id>``.  This module keeps that location for the
``default`` account and gives every other account an isolated
``accounts/<account_id>`` directory.  It deliberately has no dependency on
the rest of the backend so callers can migrate one storage user at a time.

Only single path components are accepted for IDs and file names.  Runtime
directories are private (0700), and writes use a same-directory temporary file
followed by ``os.replace`` (0600), so a reader never observes a partial file.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Union


DEFAULT_ACCOUNT_ID = "default"
DEFAULT_TENANTS_ROOT = "/var/lib/xianyu-saas/tenants"
# Kept as a public compatibility constant, while the module-level wrappers
# read the environment at call time so isolated tests can change it safely.
TENANTS_ROOT = os.environ.get("SAAS_TENANTS_DIR", DEFAULT_TENANTS_ROOT)

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BytesLike = Union[bytes, bytearray, memoryview]


class AccountStorageError(ValueError):
    """Raised when an account or storage path is invalid or unsafe."""


def _root_path(value: str | os.PathLike[str] | None) -> Path:
    configured = value
    if configured is None:
        configured = os.environ.get("SAAS_TENANTS_DIR", DEFAULT_TENANTS_ROOT)
    try:
        text = os.fspath(configured)
    except TypeError:
        raise AccountStorageError("tenants root must be a path") from None
    if isinstance(text, bytes):
        raise AccountStorageError("tenants root must be text")
    if not text or "\x00" in text:
        raise AccountStorageError("tenants root is invalid")
    path = Path(text).expanduser().resolve(strict=False)
    # Refuse an accidental process-wide target.  A caller can still choose a
    # private temporary directory or a normal absolute application root.
    if path == Path(path.anchor):
        raise AccountStorageError("tenants root must not be the filesystem root")
    return path


def _component(value, field: str, *, default: bool = False) -> str:
    if isinstance(value, bool):
        raise AccountStorageError(f"{field} is invalid")
    if value is None and default:
        return DEFAULT_ACCOUNT_ID
    if isinstance(value, int):
        if value <= 0:
            raise AccountStorageError(f"{field} is invalid")
        text = str(value)
    else:
        try:
            text = os.fspath(value)
        except TypeError:
            raise AccountStorageError(f"{field} is invalid") from None
        if isinstance(text, bytes):
            raise AccountStorageError(f"{field} is invalid")
    if not isinstance(text, str) or not _COMPONENT_RE.fullmatch(text):
        raise AccountStorageError(f"{field} is invalid")
    return text


def _user_component(value) -> str:
    """Normalize a user ID without allowing path syntax.

    Production callers currently use positive integer IDs, but accepting a
    safe opaque string keeps this small storage primitive usable by migrations
    and test fixtures without weakening its traversal checks.
    """
    return _component(value, "user_id")


def _account_component(value) -> str:
    if value is None:
        return DEFAULT_ACCOUNT_ID
    return _component(value, "account_id")


def normalize_account_key(value=None) -> str:
    """Return a validated directory component for a shop account key."""
    return _account_component(value)


def _file_component(value) -> str:
    try:
        text = os.fspath(value)
    except TypeError:
        raise AccountStorageError("file name is invalid") from None
    if isinstance(text, bytes):
        raise AccountStorageError("file name is invalid")
    if (
        not isinstance(text, str)
        or not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or "\x00" in text
        or _CONTROL_RE.search(text)
        or len(text) > 255
    ):
        raise AccountStorageError("file name is invalid")
    return text


def _private_directory(path: Path) -> None:
    """Check an existing path and force the required private mode."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise AccountStorageError(f"storage directory disappeared: {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise AccountStorageError("storage path must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise AccountStorageError("storage path is not a directory")
    os.chmod(path, PRIVATE_DIR_MODE)


def _make_private_directory(path: Path) -> None:
    try:
        os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)
    except OSError as error:
        raise AccountStorageError(f"cannot create storage directory: {path}") from error
    _private_directory(path)


def _ensure_no_symlink_components(path: Path, root: Path) -> None:
    """Reject symlinked components between ``root`` and ``path``.

    The root itself is resolved once at construction time and is trusted as a
    configuration value.  Every component below it is checked with ``lstat``
    so an existing symlink cannot redirect one account into another location.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise AccountStorageError("path is outside tenants root") from None
    if any(part in {".", ".."} for part in relative.parts):
        raise AccountStorageError("path traversal is not allowed")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise AccountStorageError("storage path must not be a symlink")


def _coerce_bytes(data, encoding: str = "utf-8") -> bytes:
    if isinstance(data, str):
        try:
            return data.encode(encoding)
        except (LookupError, UnicodeError) as error:
            raise AccountStorageError("invalid text encoding") from error
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise AccountStorageError("file content must be text or bytes")


class AccountStorage:
    """Resolve and safely persist files for one tenants root."""

    def __init__(self, tenants_root: str | os.PathLike[str] | None = None):
        self.root = _root_path(tenants_root)

    def tenant_dir(self, user_id) -> Path:
        return self.root / _user_component(user_id)

    def account_dir(self, user_id, account_id=DEFAULT_ACCOUNT_ID) -> Path:
        tenant = self.tenant_dir(user_id)
        account = _account_component(account_id)
        if account == DEFAULT_ACCOUNT_ID:
            return tenant
        return tenant / "accounts" / account

    # ``runtime_dir`` is the domain-facing name used by new callers.
    runtime_dir = account_dir

    def ensure_tenant_dir(self, user_id) -> Path:
        tenant = self.tenant_dir(user_id)
        _make_private_directory(self.root)
        _ensure_no_symlink_components(tenant, self.root)
        _make_private_directory(tenant)
        return tenant

    def ensure_account_dir(self, user_id, account_id=DEFAULT_ACCOUNT_ID) -> Path:
        account = _account_component(account_id)
        tenant = self.ensure_tenant_dir(user_id)
        if account == DEFAULT_ACCOUNT_ID:
            return tenant
        accounts = tenant / "accounts"
        _ensure_no_symlink_components(accounts, self.root)
        _make_private_directory(accounts)
        target = accounts / account
        _ensure_no_symlink_components(target, self.root)
        _make_private_directory(target)
        return target

    # Short alias for callers that use the older ``ensure_dir`` convention.
    ensure_dir = ensure_account_dir

    def _file_path(self, user_id, account_id, name) -> Path:
        directory = self.ensure_account_dir(user_id, account_id)
        filename = _file_component(name)
        target = directory / filename
        _ensure_no_symlink_components(target.parent, self.root)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode):
                raise AccountStorageError("storage file must not be a symlink")
        return target

    def atomic_write_path(
        self,
        path: str | os.PathLike[str],
        data: str | _BytesLike,
        *,
        encoding: str = "utf-8",
    ) -> Path:
        """Atomically write an already-resolved path below this tenants root."""
        try:
            target = Path(path)
        except (TypeError, ValueError):
            raise AccountStorageError("file path is invalid") from None
        if not target.is_absolute():
            raise AccountStorageError("file path must be absolute")
        _file_component(target.name)
        # Do not resolve existing symlinks: checking each component with lstat
        # is what prevents a symlinked account directory from escaping.
        _ensure_no_symlink_components(target.parent, self.root)
        try:
            target.parent.relative_to(self.root)
        except ValueError:
            raise AccountStorageError("file path is outside tenants root") from None
        _private_directory(target.parent)
        payload = _coerce_bytes(data, encoding)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            # ``mkstemp`` and fchmod already provide this mode; chmod after the
            # rename also covers unusual filesystems with a permissive umask.
            try:
                os.chmod(target, PRIVATE_FILE_MODE, follow_symlinks=False)
            except TypeError:  # pragma: no cover - compatibility fallback
                os.chmod(target, PRIVATE_FILE_MODE)
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return target
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    # Concise alias for callers that keep the destination path themselves.
    atomic_write = atomic_write_path

    def write_file(self, user_id, account_id, name, data, *, encoding: str = "utf-8") -> Path:
        return self.atomic_write_path(
            self._file_path(user_id, account_id, name), data, encoding=encoding
        )

    def write_bytes(self, user_id, account_id, name, data: _BytesLike) -> Path:
        return self.write_file(user_id, account_id, name, data)

    def write_text(self, user_id, account_id, name, text: str, *, encoding: str = "utf-8") -> Path:
        if not isinstance(text, str):
            raise AccountStorageError("file content must be text")
        return self.write_file(user_id, account_id, name, text, encoding=encoding)

    def read_bytes(self, user_id, account_id, name) -> bytes:
        path = self._file_path(user_id, account_id, name)
        try:
            with path.open("rb") as stream:
                return stream.read()
        except OSError as error:
            raise AccountStorageError("cannot read storage file") from error

    def read_text(self, user_id, account_id, name, *, encoding: str = "utf-8") -> str:
        try:
            return self.read_bytes(user_id, account_id, name).decode(encoding)
        except (LookupError, UnicodeDecodeError) as error:
            raise AccountStorageError("cannot decode storage file") from error


def _storage() -> AccountStorage:
    return AccountStorage()


def tenant_dir(user_id) -> Path:
    return _storage().tenant_dir(user_id)


def account_dir(user_id, account_id=DEFAULT_ACCOUNT_ID) -> Path:
    return _storage().account_dir(user_id, account_id)


runtime_dir = account_dir


def ensure_tenant_dir(user_id) -> Path:
    return _storage().ensure_tenant_dir(user_id)


def ensure_account_dir(user_id, account_id=DEFAULT_ACCOUNT_ID) -> Path:
    return _storage().ensure_account_dir(user_id, account_id)


ensure_dir = ensure_account_dir


def write_file(user_id, account_id, name, data, *, encoding: str = "utf-8") -> Path:
    return _storage().write_file(user_id, account_id, name, data, encoding=encoding)


def write_bytes(user_id, account_id, name, data: _BytesLike) -> Path:
    return _storage().write_bytes(user_id, account_id, name, data)


def write_text(user_id, account_id, name, text: str, *, encoding: str = "utf-8") -> Path:
    return _storage().write_text(user_id, account_id, name, text, encoding=encoding)


def read_bytes(user_id, account_id, name) -> bytes:
    return _storage().read_bytes(user_id, account_id, name)


def read_text(user_id, account_id, name, *, encoding: str = "utf-8") -> str:
    return _storage().read_text(user_id, account_id, name, encoding=encoding)


def atomic_write(
    path: str | os.PathLike[str],
    data: str | _BytesLike,
    *,
    encoding: str = "utf-8",
) -> Path:
    """Module-level atomic writer for a path below ``SAAS_TENANTS_DIR``."""
    return _storage().atomic_write_path(path, data, encoding=encoding)


__all__ = [
    "AccountStorage",
    "AccountStorageError",
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_TENANTS_ROOT",
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "TENANTS_ROOT",
    "account_dir",
    "atomic_write",
    "ensure_account_dir",
    "ensure_dir",
    "ensure_tenant_dir",
    "normalize_account_key",
    "read_bytes",
    "read_text",
    "runtime_dir",
    "tenant_dir",
    "write_bytes",
    "write_file",
    "write_text",
]

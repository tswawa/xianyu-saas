#!/usr/bin/env python3
"""Offline contract for account-scoped runtime storage."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from account_storage import AccountStorage, AccountStorageError  # noqa: E402


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def rejects(callable_obj, *args, **kwargs):
    try:
        callable_obj(*args, **kwargs)
    except AccountStorageError:
        return
    raise AssertionError("unsafe storage input was accepted")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xianyu-account-storage-") as root:
        tenants = Path(root) / "tenants"
        storage = AccountStorage(tenants)

        assert storage.account_dir(7, "default") == tenants / "7"
        assert storage.account_dir(7, None) == tenants / "7"
        assert storage.account_dir("owner-7", "default") == tenants / "owner-7"
        assert storage.runtime_dir(7, "shop-2") == tenants / "7" / "accounts" / "shop-2"

        legacy = storage.ensure_account_dir(7, "default")
        isolated = storage.ensure_account_dir(7, "shop-2")
        for directory in (tenants, tenants / "7", tenants / "7" / "accounts", isolated):
            assert directory.is_dir()
            assert mode(directory) == 0o700, (directory, oct(mode(directory)))
        assert legacy == tenants / "7"

        target = storage.write_text(7, "shop-2", "state.json", "first")
        assert target.read_text(encoding="utf-8") == "first"
        assert mode(target) == 0o600
        old_inode = target.stat().st_ino
        storage.write_text(7, "shop-2", "state.json", "second")
        assert target.read_text(encoding="utf-8") == "second"
        assert mode(target) == 0o600
        assert target.stat().st_ino != old_inode

        # IDs and file names are single components; traversal and separators
        # must never create a path outside the selected account directory.
        for value in ("../escape", "/tmp/escape", "7\\escape", "", ".", ".."):
            rejects(storage.account_dir, 7, value)
        for value in ("../escape", "/tmp/escape", "nested/state", "nested\\state", ""):
            rejects(storage.write_text, 7, "shop-2", value, "bad")
        rejects(storage.account_dir, "../outside", "default")
        assert not (Path(root) / "escape").exists()

        # A pre-existing symlink cannot redirect an account directory.
        link = tenants / "8"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(Path(root) / "outside", target_is_directory=True)
        rejects(storage.ensure_account_dir, 8, "default")

        linked_file = isolated / "linked.txt"
        linked_file.symlink_to(Path(root) / "outside-file")
        rejects(storage.read_text, 7, "shop-2", "linked.txt")
        rejects(storage.write_text, 7, "shop-2", "linked.txt", "no")

        outside = Path(root) / "outside-file"
        rejects(storage.atomic_write, outside, "no")
        rejects(storage.atomic_write, tenants / "7" / ".." / "escape", "no")

    print(
        "account-storage contract: legacy mapping, isolation, permissions "
        "and atomic writes passed"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline contract for the bounded card-pool save recovery.

Recovery here compensates exceptions caught while the two card-pool files are
being published.  It is not crash or power-loss atomicity: a hard kill between
the two publishes is explicitly not recovered.  The test is Windows-portable,
so the actual ``_save_cards_locked`` function is extracted from
``backend/app.py`` with AST instead of importing the fcntl-based application
module.

Known platform gap: this host cannot create symlinks and does not enforce
POSIX file modes, so real symlink rejection and 0600/0700 enforcement stay
covered by ``account-storage-contract.py`` on Linux; this test labels the
symlink skip at runtime and never weakens those production checks.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from account_storage import (  # noqa: E402
    AccountStorage,
    AccountStorageError,
    AccountStorageRecoveryError,
)


# Deliberately awkward old bytes: recovery must preserve them exactly.
CODES_BYTES = b'[ {"code": "OLD", "used": true} ]\n'
POOL_BYTES = b'{"name":"old-pool","note":"keep me"}\n'
NORMAL_SAVE_FAILURE = "卡密保存失败，请稍后重试"


@contextmanager
def failing_writes(actions):
    """Drive AccountStorage.atomic_write_path through a per-file plan.

    A plan entry is a list of ``"ok"``, ``"fail"`` (raise before publishing)
    or ``"fail_after"`` (publish then raise, like a late chmod/fsync error).
    Missing/extra calls default to ``"ok"``.  The yielded dict counts calls
    per file name.
    """
    real = AccountStorage.atomic_write_path
    calls = {}

    def flaky(self, path, data, **kwargs):
        name = Path(os.fspath(path)).name
        index = calls.get(name, 0)
        calls[name] = index + 1
        plan = actions.get(name, ())
        action = plan[index] if index < len(plan) else "ok"
        if action == "fail":
            raise OSError("simulated write failure for " + name)
        result = real(self, path, data, **kwargs)
        if action == "fail_after":
            raise OSError("simulated after-publish failure for " + name)
        return result

    with patch.object(AccountStorage, "atomic_write_path", flaky):
        yield calls


def transact(storage, writes, user_id=7, account_key="shop-2"):
    names = tuple(name for name, _ in writes)
    with storage.compensating_write(user_id, account_key, names) as write:
        for name, content in writes:
            write(name, content)


def write_unlisted(storage):
    with storage.compensating_write(7, "shop-2", ("redeem_codes.json",)) as write:
        write("unlisted.json", "x")


def capture(callable_obj):
    try:
        callable_obj()
    except Exception as error:  # noqa: BLE001 - the tests assert what was caught
        return error
    raise AssertionError("expected an exception")


def assert_plain_write_error(error):
    """A write failure where every snapshot could be restored."""
    assert isinstance(error, (OSError, AccountStorageError))
    assert not isinstance(error, AccountStorageRecoveryError)


def storage_contract(work: Path) -> None:
    storage = AccountStorage(work / "tenants")
    account_dir = storage.ensure_account_dir(7, "shop-2")
    other_dir = storage.ensure_account_dir(8, "default")
    storage.write_bytes(8, "default", "redeem_codes.json", b"other-account\n")

    def reset() -> None:
        storage.write_bytes(7, "shop-2", "redeem_codes.json", CODES_BYTES)
        storage.write_bytes(7, "shop-2", "card_pool.json", POOL_BYTES)

    def stored():
        return (
            (account_dir / "redeem_codes.json").read_bytes(),
            (account_dir / "card_pool.json").read_bytes(),
        )

    def save_new_codes():
        transact(
            storage,
            [("redeem_codes.json", "new-codes"), ("card_pool.json", "new-pool")],
        )

    # Success publishes both files.
    reset()
    save_new_codes()
    assert stored() == (b"new-codes", b"new-pool")

    # The first write fails before publishing: nothing changed, nothing restored.
    reset()
    with failing_writes({"redeem_codes.json": ["fail"]}) as calls:
        error = capture(save_new_codes)
    assert_plain_write_error(error)
    assert stored() == (CODES_BYTES, POOL_BYTES)
    assert calls == {"redeem_codes.json": 1}

    # The second write fails: the first target is restored byte-for-byte.
    reset()
    with failing_writes({"card_pool.json": ["fail"]}):
        error = capture(save_new_codes)
    assert_plain_write_error(error)
    assert stored() == (CODES_BYTES, POOL_BYTES)

    # A writer can raise after publishing its target; both variants recover.
    for plan in ({"redeem_codes.json": ["fail_after"]}, {"card_pool.json": ["fail_after"]}):
        reset()
        with failing_writes(plan):
            error = capture(save_new_codes)
        assert_plain_write_error(error)
        assert stored() == (CODES_BYTES, POOL_BYTES)

    # Targets that were absent are removed again after a failed save.
    (account_dir / "redeem_codes.json").unlink()
    (account_dir / "card_pool.json").unlink()
    with failing_writes({"card_pool.json": ["fail_after"]}):
        error = capture(save_new_codes)
    assert_plain_write_error(error)
    assert not (account_dir / "redeem_codes.json").exists()
    assert not (account_dir / "card_pool.json").exists()

    # Mixed: an existing target is restored while the absent one disappears.
    storage.write_bytes(7, "shop-2", "redeem_codes.json", CODES_BYTES)
    with failing_writes({"card_pool.json": ["fail_after"]}):
        error = capture(save_new_codes)
    assert_plain_write_error(error)
    assert (account_dir / "redeem_codes.json").read_bytes() == CODES_BYTES
    assert not (account_dir / "card_pool.json").exists()

    # An unchanged target is never rewritten during recovery.
    reset()
    with failing_writes({"card_pool.json": ["fail"]}) as calls:
        error = capture(
            lambda: transact(
                storage,
                [("redeem_codes.json", CODES_BYTES), ("card_pool.json", b"new-pool")],
            )
        )
    assert_plain_write_error(error)
    assert calls == {"redeem_codes.json": 1, "card_pool.json": 1}
    assert stored() == (CODES_BYTES, POOL_BYTES)

    # An unreadable snapshot aborts before any content write.
    reset()
    real_open = Path.open

    def denied(path, mode="r", *args, **kwargs):
        if path.name == "redeem_codes.json":
            raise PermissionError("simulated unreadable snapshot")
        return real_open(path, mode, *args, **kwargs)

    with patch.object(Path, "open", denied), failing_writes({}) as calls:
        error = capture(save_new_codes)
    assert isinstance(error, AccountStorageError)
    assert not isinstance(error, AccountStorageRecoveryError)
    assert calls == {}
    assert stored() == (CODES_BYTES, POOL_BYTES)

    # A non-regular target is rejected before any content write.
    reset()
    target = account_dir / "redeem_codes.json"
    target.unlink()
    target.mkdir()
    error = capture(save_new_codes)
    target.rmdir()
    assert isinstance(error, AccountStorageError)
    assert (account_dir / "card_pool.json").read_bytes() == POOL_BYTES

    # A symlinked target is rejected and the link destination stays untouched.
    reset()
    link = account_dir / "redeem_codes.json"
    link.unlink()
    try:
        link.symlink_to(work / "outside.json")
    except (OSError, NotImplementedError):
        print("card-pool-save contract: symlink rejection skipped (host needs symlink privilege)")
    else:
        error = capture(save_new_codes)
        assert isinstance(error, AccountStorageError)
        assert not (work / "outside.json").exists()
        assert (account_dir / "card_pool.json").read_bytes() == POOL_BYTES
        link.unlink()

    # Recovery failure is explicit and still restores the other target.
    reset()
    with failing_writes(
        {"redeem_codes.json": ["ok", "fail"], "card_pool.json": ["fail_after"]}
    ) as calls:
        error = capture(save_new_codes)
    assert isinstance(error, AccountStorageRecoveryError)
    assert error.files == ("redeem_codes.json",)
    assert isinstance(error.__cause__, OSError)
    assert (account_dir / "redeem_codes.json").read_bytes() == b"new-codes"
    assert (account_dir / "card_pool.json").read_bytes() == POOL_BYTES
    assert calls == {"redeem_codes.json": 2, "card_pool.json": 2}

    # Recovery refuses to delete a non-regular file that replaced an absent
    # target; it reports that failure instead of following or removing it.
    (account_dir / "redeem_codes.json").unlink()
    (account_dir / "card_pool.json").unlink()
    real_write = AccountStorage.atomic_write_path

    def sabotage(self, path, data, **kwargs):
        result = real_write(self, path, data, **kwargs)
        if Path(os.fspath(path)).name == "card_pool.json":
            Path(os.fspath(path)).unlink()
            Path(os.fspath(path)).mkdir()
            raise OSError("simulated publish then sabotage")
        return result

    with patch.object(AccountStorage, "atomic_write_path", sabotage):
        error = capture(save_new_codes)
    assert isinstance(error, AccountStorageRecoveryError)
    assert error.files == ("card_pool.json",)
    assert (account_dir / "card_pool.json").is_dir()
    assert not (account_dir / "redeem_codes.json").exists()
    (account_dir / "card_pool.json").rmdir()

    # Undeclared names are refused and untouched accounts stay outside.
    reset()
    error = capture(lambda: write_unlisted(storage))
    assert isinstance(error, AccountStorageError)
    assert not (account_dir / "unlisted.json").exists()

    error = capture(lambda: transact(storage, [("redeem_codes.json", "x"), ("redeem_codes.json", "y")]))
    assert isinstance(error, AccountStorageError)
    assert (account_dir / "redeem_codes.json").read_bytes() == CODES_BYTES

    storage.write_bytes(7, "default", "redeem_codes.json", b"legacy-account\n")
    with failing_writes({"card_pool.json": ["fail_after"]}):
        error = capture(save_new_codes)
    assert_plain_write_error(error)
    assert (account_dir / "redeem_codes.json").read_bytes() == CODES_BYTES
    assert (storage.ensure_account_dir(7, "default") / "redeem_codes.json").read_bytes() == b"legacy-account\n"
    assert (other_dir / "redeem_codes.json").read_bytes() == b"other-account\n"

    error = capture(lambda: transact(storage, [("redeem_codes.json", "x")], account_key="../escape"))
    assert isinstance(error, AccountStorageError)
    assert not (work / "tenants" / "escape").exists()
    assert other_dir.is_dir()


def load_save_cards(storage: AccountStorage):
    """Extract the real app helpers without importing the fcntl-based app."""
    source = (BACKEND / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = ("_read_codes", "_cards_payload", "_save_cards_locked")
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    missing = [name for name in wanted if name not in functions]
    assert not missing, "backend/app.py no longer defines " + ", ".join(missing)

    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *(functions[name] for name in wanted),
        ],
        type_ignores=[],
    )
    code = compile(ast.fix_missing_locations(module), str(BACKEND / "app.py"), "exec")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def read_secret(user_id, name, account_key="default"):
        try:
            return storage.read_text(user_id, account_key, name).strip()
        except (OSError, AccountStorageError):
            return ""

    namespace = {
        "json": json,
        "HTTPException": HTTPException,
        "AccountStorage": lambda *args, **kwargs: storage,
        "AccountStorageError": AccountStorageError,
        "AccountStorageRecoveryError": AccountStorageRecoveryError,
        "read_secret": read_secret,
    }
    exec(code, namespace)
    return namespace["_save_cards_locked"], HTTPException


class CardsBody:
    def __init__(self, name, note, codes):
        self.name = name
        self.note = note
        self.codes = codes


def api_contract(work: Path) -> None:
    storage = AccountStorage(work / "tenants-api")
    save_cards, HTTPException = load_save_cards(storage)
    user = {"id": 7}
    account = {"account_key": "shop-2"}
    account_dir = storage.ensure_account_dir(7, "shop-2")
    default_dir = storage.ensure_account_dir(7, "default")

    def reset() -> None:
        storage.write_bytes(7, "shop-2", "redeem_codes.json", CODES_BYTES)
        storage.write_bytes(7, "shop-2", "card_pool.json", POOL_BYTES)

    def stored():
        return (
            (account_dir / "redeem_codes.json").read_bytes(),
            (account_dir / "card_pool.json").read_bytes(),
        )

    # Success merges old stock and reports the existing response shape.
    reset()
    result = save_cards(
        CardsBody("我的卡池", "备注", [{"code": "NEW", "used": False}]), user, account
    )
    assert result["ok"] is True
    assert result["pool"]["name"] == "我的卡池"
    assert result["pool"]["note"] == "备注"
    assert result["pool"]["total"] == 2
    assert result["stats"] == {"pools": 1, "total": 2, "available": 1, "reserved": 0, "used": 1}
    payload = json.loads((account_dir / "redeem_codes.json").read_text(encoding="utf-8"))
    by_code = {item["code"]: item for item in payload}
    assert set(by_code) == {"OLD", "NEW"}
    assert by_code["OLD"]["used"] is True
    assert by_code["NEW"]["used"] is False
    assert json.loads((account_dir / "card_pool.json").read_text(encoding="utf-8")) == {
        "name": "我的卡池",
        "note": "备注",
    }

    # Metadata-only saves keep the inventory and only rename the pool.
    result = save_cards(CardsBody("改名池", "新备注", []), user, account)
    assert result["ok"] is True
    assert result["pool"]["name"] == "改名池"
    assert result["pool"]["total"] == 2
    assert set(item["code"] for item in json.loads(
        (account_dir / "redeem_codes.json").read_text(encoding="utf-8")
    )) == {"OLD", "NEW"}

    # First write fails: normal 503, both files exactly as before.
    reset()
    with failing_writes({"redeem_codes.json": ["fail"]}) as calls:
        error = capture(
            lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
        )
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert error.detail == NORMAL_SAVE_FAILURE
    assert calls == {"redeem_codes.json": 1}
    assert stored() == (CODES_BYTES, POOL_BYTES)

    # Second write fails: the published first file is restored exactly.
    reset()
    with failing_writes({"card_pool.json": ["fail"]}):
        error = capture(
            lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
        )
    assert error.status_code == 503
    assert error.detail == NORMAL_SAVE_FAILURE
    assert stored() == (CODES_BYTES, POOL_BYTES)

    # A write that raises after publishing is still compensated.
    for plan in ({"redeem_codes.json": ["fail_after"]}, {"card_pool.json": ["fail_after"]}):
        reset()
        with failing_writes(plan):
            error = capture(
                lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
            )
        assert error.status_code == 503
        assert error.detail == NORMAL_SAVE_FAILURE
        assert stored() == (CODES_BYTES, POOL_BYTES)

    # Failed first save leaves no partial files behind.
    (account_dir / "redeem_codes.json").unlink()
    (account_dir / "card_pool.json").unlink()
    with failing_writes({"card_pool.json": ["fail_after"]}):
        error = capture(
            lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
        )
    assert error.status_code == 503
    assert not (account_dir / "redeem_codes.json").exists()
    assert not (account_dir / "card_pool.json").exists()

    # An unreadable snapshot is a plain save failure with no content writes.
    reset()
    directory_target = account_dir / "redeem_codes.json"
    directory_target.unlink()
    directory_target.mkdir()
    error = capture(
        lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
    )
    directory_target.rmdir()
    assert error.status_code == 503
    assert error.detail == NORMAL_SAVE_FAILURE
    assert (account_dir / "card_pool.json").read_bytes() == POOL_BYTES

    # Recovery failure has its own explicit error and is never reported as a
    # success or as a plain save failure.
    reset()
    with failing_writes(
        {"redeem_codes.json": ["ok", "fail"], "card_pool.json": ["fail_after"]}
    ):
        error = capture(
            lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
        )
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert error.detail != NORMAL_SAVE_FAILURE
    assert "恢复" in error.detail
    assert isinstance(error.__cause__, AccountStorageRecoveryError)
    assert error.__cause__.files == ("redeem_codes.json",)
    assert (account_dir / "card_pool.json").read_bytes() == POOL_BYTES

    # The write path stays scoped to the requested account.
    reset()
    default_bytes = b"default-codes\n"
    storage.write_bytes(7, "default", "redeem_codes.json", default_bytes)
    with failing_writes({"card_pool.json": ["fail_after"]}):
        error = capture(
            lambda: save_cards(CardsBody("失败池", "", [{"code": "X"}]), user, account)
        )
    assert error.status_code == 503
    assert (default_dir / "redeem_codes.json").read_bytes() == default_bytes

    # The compensating operation is used only for the two-file card save.
    source = (BACKEND / "app.py").read_text(encoding="utf-8")
    assert source.count("compensating_write(") == 1


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xianyu-card-pool-save-") as temp:
        work = Path(temp)
        storage_contract(work)
        api_contract(work)
    print(
        "card-pool-save contract: compensating writes, exact restores, absent-target "
        "removal, snapshot aborts, distinct recovery failure and card save integration passed"
    )


if __name__ == "__main__":
    main()

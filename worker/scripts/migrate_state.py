#!/usr/bin/env python3
"""Migrate legacy runtime state into the secured SQLite-backed data directory."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from context_manager import ChatContextManager  # noqa: E402
from delivery_store import DeliveryStore  # noqa: E402


STATE_FILES = {
    "redeem_codes.json": list,
    "trial_codes.json": list,
    "trial_sent.json": dict,
    "pan_links.json": dict,
}
REQUIRED_FILES = {"redeem_codes.json", "trial_codes.json", "pan_links.json"}
OUTPUT_FILES = set(STATE_FILES) | {"chat_history.db", "delivery_state.db"}
LEGACY_LEDGER_FILES = ("redeem_sent.json", "pan_sent.json")
QUARANTINE_LEDGER = "legacy_delivery_ledger.json"
OUTPUT_FILES.add(QUARANTINE_LEDGER)
MIGRATION_MARKER = ".migration-manifest.json"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_incomplete(destination: Path) -> None:
    """Remove only files recorded by an interrupted, tool-owned commit."""
    marker = destination / MIGRATION_MARKER
    if not marker.exists():
        return
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError("migration marker is not a regular file")
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("migration marker is corrupt; inspect destination manually") from exc
    outputs = manifest.get("outputs")
    staging_name = manifest.get("staging")
    if (
        not isinstance(outputs, list)
        or not outputs
        or not all(isinstance(name, str) and name in OUTPUT_FILES for name in outputs)
        or not isinstance(staging_name, str)
        or not staging_name.startswith(".migration-")
        or "/" in staging_name
    ):
        raise RuntimeError("migration marker has an invalid scope")
    for name in outputs:
        target = destination / name
        if target.is_symlink() or target.is_dir():
            raise RuntimeError("migration recovery found an unexpected target type")
        if target.exists():
            target.unlink()
    staging = destination / staging_name
    if staging.exists():
        if not staging.is_dir() or staging.is_symlink():
            raise RuntimeError("migration staging path has an invalid type")
        shutil.rmtree(staging)
    marker.unlink()
    _fsync_directory(destination)


def _load_json(path: Path, expected_type: type):
    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 16 * 1024 * 1024:
            raise RuntimeError(f"invalid state file: {path.name}")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except RuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid state file: {path.name}") from exc
    if not isinstance(payload, expected_type):
        raise RuntimeError(f"invalid state structure: {path.name}")
    return payload


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)
    os.chmod(destination, 0o600)


def _find_chat_database(source: Path):
    candidates = (source / "data" / "chat_history.db", source / "chat_history.db")
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if len(existing) > 1:
        raise RuntimeError("multiple legacy chat databases found")
    if not existing:
        return None
    if not stat.S_ISREG(existing[0].lstat().st_mode):
        raise RuntimeError("legacy chat database must be a regular file")
    return existing[0]


def _quarantine_legacy_ledgers(source: Path, staging: Path) -> bool:
    """Record only hashes/counts for old send ledgers; never copy their contents."""
    records = {}
    for name in LEGACY_LEDGER_FILES:
        candidates = (source / name, source / "data" / name)
        existing = [path for path in candidates if path.exists()]
        if len(existing) > 1:
            raise RuntimeError(f"multiple legacy ledgers found: {name}")
        if not existing:
            continue
        path = existing[0]
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 16 * 1024 * 1024:
            raise RuntimeError(f"invalid legacy ledger: {name}")
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid legacy ledger: {name}") from exc
        if not isinstance(payload, (dict, list)):
            raise RuntimeError(f"invalid legacy ledger structure: {name}")
        records[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "records": len(payload),
        }
    if not records:
        return False
    target = staging / QUARANTINE_LEDGER
    target.write_text(
        json.dumps(
            {
                "status": "quarantined",
                "reason": "automatic_delivery_disabled",
                "files": records,
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    return True


def _check_sqlite(path: Path, foreign_keys: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {path.name}")
        if foreign_keys and conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError(f"SQLite foreign key check failed: {path.name}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def migrate(source_dir: str, destination_dir: str) -> dict:
    source = Path(source_dir).resolve()
    destination = Path(destination_dir).resolve()
    if not source.is_dir():
        raise RuntimeError("legacy source directory does not exist")
    if source == destination:
        raise RuntimeError("source and destination directories must differ")

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    _recover_incomplete(destination)
    conflicts = sorted(name for name in OUTPUT_FILES if (destination / name).exists())
    if conflicts:
        raise RuntimeError("destination already contains migrated state")

    missing = sorted(name for name in REQUIRED_FILES if not (source / name).is_file())
    if missing:
        raise RuntimeError("required legacy state files are missing")

    staging = Path(tempfile.mkdtemp(prefix=".migration-", dir=destination))
    os.chmod(staging, 0o700)
    marker = destination / MIGRATION_MARKER
    manifest = {"version": 1, "staging": staging.name, "outputs": []}
    try:
        for name, expected_type in STATE_FILES.items():
            source_file = source / name
            if not source_file.exists():
                continue
            _load_json(source_file, expected_type)
            target = staging / name
            shutil.copyfile(source_file, target)
            os.chmod(target, 0o600)

        legacy_ledger_quarantined = _quarantine_legacy_ledgers(source, staging)

        chat_source = _find_chat_database(source)
        chat_target = staging / "chat_history.db"
        if chat_source is not None:
            _sqlite_backup(chat_source, chat_target)
        ChatContextManager(db_path=str(chat_target))

        delivery_target = staging / "delivery_state.db"
        store = DeliveryStore(
            str(delivery_target),
            redeem_pool_path=str(staging / "redeem_codes.json"),
            trial_pool_path=str(staging / "trial_codes.json"),
            trial_sent_path=(
                str(staging / "trial_sent.json")
                if (staging / "trial_sent.json").exists()
                else None
            ),
        )

        _check_sqlite(chat_target)
        _check_sqlite(delivery_target, foreign_keys=True)
        with sqlite3.connect(chat_target) as conn:
            chat_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        inventory_counts = store.inventory_counts()

        outputs = []
        for path in staging.iterdir():
            if path.name.endswith(("-wal", "-shm")):
                continue
            if path.name not in OUTPUT_FILES:
                raise RuntimeError("migration produced an unexpected file")
            outputs.append(path.name)
        expected_outputs = OUTPUT_FILES - (
            {"trial_sent.json"} if not (staging / "trial_sent.json").exists() else set()
        )
        if not legacy_ledger_quarantined:
            expected_outputs.discard(QUARANTINE_LEDGER)
        if set(outputs) != expected_outputs:
            raise RuntimeError("migration did not produce the expected state files")
        manifest["outputs"] = sorted(outputs)
        marker_fd = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            marker_payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode("utf-8")
            os.write(marker_fd, marker_payload)
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        _fsync_directory(destination)

        moved = []
        try:
            for path in staging.iterdir():
                if path.name.endswith(("-wal", "-shm")):
                    continue
                os.chmod(path, 0o600)
                os.replace(path, destination / path.name)
                moved.append(path.name)
            marker.unlink()
            _fsync_directory(destination)
        except Exception:
            rollback_complete = True
            for name in moved:
                target = destination / name
                try:
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                    else:
                        rollback_complete = False
                except OSError:
                    rollback_complete = False
            if rollback_complete:
                try:
                    marker.unlink()
                    _fsync_directory(destination)
                except OSError:
                    pass
            raise

        return {
            "chat_messages": chat_messages,
            "inventory": inventory_counts,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    summary = migrate(args.source, args.destination)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()

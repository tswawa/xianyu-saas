"""Version and build metadata; never inspect the runtime Git working tree."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


# Must match package.json; image builds validate this before publication.
VERSION = "0.4.4"
ASSET_VERSION = "20260914-01"
RELEASE_CHANNEL = "release"
# Same value promises bidirectional data compatibility for Docker updates.
# Increment for breaking database, configuration or credential format changes.
UPDATE_DATA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
BUILD_INFO_FILE = Path(__file__).with_name("build-info.json")


def _build_info() -> dict:
    try:
        info = json.loads(BUILD_INFO_FILE.read_text(encoding="utf-8"))
        if isinstance(info, dict) and info.get("version") == VERSION:
            return info
    except (OSError, ValueError):
        pass
    return {}


def _commit(value) -> str:
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else ""


def _build_time(value) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    return ""


def _merged_build_info(existing: dict, commit: str, dirty: bool | None, build_time: str) -> dict:
    """Merge build arguments with same-version packaged metadata.

    A Release source ZIP ships ``backend/build-info.json`` with release
    provenance, but the Dockerfile runs ``--write-build-info`` in an image
    without a Git checkout. Explicit ``SAAS_BUILD_COMMIT``/``SAAS_BUILD_DIRTY``
    arguments always win; when they are absent or unknown, valid same-version
    packaged fields are retained instead of being erased. The existing file is
    parsed as JSON only, never executed, and its presence is not a trust claim.
    """
    if commit:
        return {"version": VERSION, "commit": commit, "build_time": build_time, "dirty": dirty}
    retained = existing if isinstance(existing, dict) else {}
    retained_dirty = retained.get("dirty") if isinstance(retained.get("dirty"), bool) else None
    return {
        "version": VERSION,
        "commit": _commit(retained.get("commit")),
        "build_time": _build_time(retained.get("build_time")) or build_time,
        "dirty": dirty if dirty is not None else retained_dirty,
    }


_INFO = _build_info()
BUILD_COMMIT = _commit(_INFO.get("commit", os.environ.get("SAAS_BUILD_COMMIT")))
BUILD_TIME = _build_time(_INFO.get("build_time", os.environ.get("SAAS_BUILD_TIME")))
BUILD_DIRTY = _INFO.get("dirty") if isinstance(_INFO.get("dirty"), bool) else None


def deployment_kind() -> str:
    configured = os.environ.get("SAAS_DEPLOYMENT_MODE", "source").strip().lower()
    if configured == "docker" or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "docker"
    return "systemd" if configured == "systemd" else "source"


def version_payload(channel: str = RELEASE_CHANNEL) -> dict:
    # The optional argument is retained for installed artifact readers, not a UI setting.
    return {
        "version": VERSION,
        "commit": BUILD_COMMIT,
        "build_time": BUILD_TIME,
        "build_dirty": BUILD_DIRTY,
        "deployment": deployment_kind(),
        "asset_version": ASSET_VERSION,
    }


def local_release_notes() -> str:
    """Return only the installed version section, not another version's notes."""
    try:
        with (ROOT / "CHANGELOG.md").open(encoding="utf-8") as source:
            document = source.read(128_000)
    except (OSError, UnicodeError):
        return ""
    heading = re.search(r"^##\s+\[?" + re.escape(VERSION) + r"\]?(?=\s|$)[^\n]*\n", document, re.MULTILINE)
    if heading is None:
        return ""
    section = document[heading.end():]
    end = re.search(r"^##\s", section, re.MULTILINE)
    return section[:end.start() if end else len(section)].strip()[:16_000]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--write-build-info", action="store_true", required=True)
    parser.parse_args()
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != VERSION:
        raise SystemExit("package.json and backend/version.py versions must match")
    dirty_value = os.environ.get("SAAS_BUILD_DIRTY", "unknown").strip().lower()
    record = _merged_build_info(
        _build_info(),
        _commit(os.environ.get("SAAS_BUILD_COMMIT")),
        True if dirty_value == "true" else False if dirty_value == "false" else None,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    BUILD_INFO_FILE.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

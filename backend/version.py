"""Build metadata embedded in release artifacts without reading ``.git`` at runtime."""

from __future__ import annotations

import os


# package.json is the release source of truth.  Release automation updates this
# generated constant before creating the signed artifact.
VERSION = "0.1.0"
BUILD_COMMIT = os.environ.get("SAAS_BUILD_COMMIT", "development").strip()[:40] or "development"
BUILD_TIME = os.environ.get("SAAS_BUILD_TIME", "development").strip()[:80] or "development"
ASSET_VERSION = "20260905-01"


def version_payload(channel: str) -> dict:
    return {
        "version": VERSION,
        "commit": BUILD_COMMIT,
        "build_time": BUILD_TIME,
        "asset_version": ASSET_VERSION,
        "update_channel": str(channel or "stable"),
    }

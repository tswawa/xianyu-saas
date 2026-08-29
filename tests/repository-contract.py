#!/usr/bin/env python3
"""Contracts for a portable repository without runtime secrets."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md",
    "SECURITY.md",
    "LICENSE",
    "LICENSING.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    ".editorconfig",
    ".gitattributes",
    "package.json",
    "backend/requirements-dev.txt",
    "config/saas.env.example",
    "docs/ARCHITECTURE.md",
    "docs/NEW_UBUNTU_HANDOFF.md",
    "docs/DEPLOYMENT.md",
    "docs/PUBLIC_RELEASE_CHECKLIST.md",
    "handoff/AGENTS.md",
    "handoff/MEMORY.md",
    "worker/LICENSE",
    "worker/NOTICE.md",
    "worker/main.py",
    "worker/tests/test_packaging.py",
    ".github/workflows/ci.yml",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
)
FORBIDDEN_NAMES = {
    ".env",
    "test-codes.txt",
    "redeem_codes.json",
    "redeem_sent.json",
    "trial_codes.json",
    "trial_sent.json",
    "pan_links.json",
    "pan_sent.json",
    "reply_rules.json",
}
SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
)
PORTABLE_PATHS = (
    "README.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "LICENSING.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "package.json",
    "scripts",
    "tests",
    "docs",
    ".github",
)
FORBIDDEN_PATH_PARTS = {"runtime-data", ".local", "test-results", ".narrafork", ".lock", ".tools", "tools"}
HOST_SPECIFIC_PATHS = (
    "/home/admin",
    "/home/nalnana",
    "/opt/",
    "/var/lib/",
)


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


for relative in REQUIRED:
    assert (ROOT / relative).is_file(), relative

files = candidate_files()
relative_files = [path.relative_to(ROOT) for path in files]
for relative in relative_files:
    assert relative.name not in FORBIDDEN_NAMES, relative
    assert not any(part in FORBIDDEN_PATH_PARTS for part in relative.parts), relative
    assert relative.suffix not in {".db", ".sqlite", ".log", ".lock"}, relative

for path in files:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"possible secret in {path.relative_to(ROOT)}"

for relative in PORTABLE_PATHS:
    target = ROOT / relative
    paths = [target] if target.is_file() else list(target.rglob("*"))
    for path in paths:
        if not path.is_file() or path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for fragment in HOST_SPECIFIC_PATHS:
            assert fragment not in text, f"host-specific path in {path.relative_to(ROOT)}: {fragment}"

products = json.loads((ROOT / "worker/products_config.json").read_text(encoding="utf-8"))
assert products == {"types": []}, "worker template must not contain production item IDs"

saas_example = (ROOT / "config/saas.env.example").read_text(encoding="utf-8")
worker_example = (ROOT / "worker/.env.example").read_text(encoding="utf-8")
dev_api = (ROOT / "scripts/dev-api.sh").read_text(encoding="utf-8")
assert "SAAS_PLATFORM_AI_KEY=\n" in saas_example
assert "SAAS_AI_MASTER_KEY=\n" in saas_example
assert "ensure_development_master_key" in dev_api and ".local/ai-master-key" in dev_api
assert "COOKIES_STR=\n" in worker_example and "API_KEY=\n" in worker_example

notice = (ROOT / "worker/NOTICE.md").read_text(encoding="utf-8")
assert "shaxiu/XianyuAutoAgent" in notice and "GPL-3.0" in notice
root_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
assert "GNU GENERAL PUBLIC LICENSE" in root_license and "Version 3" in root_license
licensing = (ROOT / "LICENSING.md").read_text(encoding="utf-8")
assert "GPL-3.0-only" in licensing and "OFL" in licensing
runtime_requirements = (ROOT / "backend/requirements.txt").read_text(encoding="utf-8")
dev_requirements = (ROOT / "backend/requirements-dev.txt").read_text(encoding="utf-8")
assert "httpx2" not in runtime_requirements
assert "httpx2==2.10.0" in dev_requirements
package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
assert package.get("private") is True
assert package.get("license") == "GPL-3.0-only"
assert package.get("repository", {}).get("url", "").endswith("tswawa/xianyu-saas.git")

print("repository contract: portable layout, provenance and secret exclusions passed")

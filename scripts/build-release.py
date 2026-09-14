#!/usr/bin/env python3
"""Build offline, reproducible signed OTA and complete source assets from Git blobs."""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class BundleError(RuntimeError):
    """Only stable codes, never Git output, environment values or secret bytes."""


@dataclass(frozen=True)
class Blob:
    path: str
    oid: str
    size: int
    executable: bool


PRIVATE_PARTS = frozenset({
    ".git", ".local", ".narrafork", ".venv", "venv", "env", "node_modules",
    "__pycache__", "__pypackages__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".hypothesis", "test-results", "coverage", "htmlcov",
    "runtime", "runtime-data", "runtime-state", "runtime_state", "data", "state",
    "logs", "backups", "tenants", "current", "releases", "staging", "update-intents",
    "credentials", "secrets", "cookies", ".ssh", ".aws", ".azure", ".tools", "tools",
    ".lock", "handoff", ".idea", ".vscode", "dist", "build", "downloads",
    "orders", "inventory", "netdisk", "卡密", "网盘", "订单",
})
PRIVATE_NAMES = frozenset({
    "agents.md", "memory.md", "memory_operations.md", "competitor-analysis-plan.md",
    "test-codes.txt", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "intent.json",
    ".xianyu-release.json", ".xianyu-manifest.json", ".xianyu-manifest.sig",
})
PRIVATE_DATA = re.compile(
    r"(?:^|[._-])(?:cookies?|tokens?|credentials?|secrets?|private[._-]?keys?|"
    r"signing[._-]?keys?|redeem[._-](?:codes|sent)|trial[._-](?:codes|sent)|"
    r"pan[._-](?:links|sent)|reply[._-]rules|legacy[._-]delivery[._-]ledger|"
    r"products[._-]config|auth[._-]state|orders?|card[._-]codes|netdisk|卡密|网盘|订单)"
    r"(?:[._-]|$)"
)
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".md", ".pub",
    ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif",
    ".woff", ".woff2", ".ttf", ".otf",
})
SECRET_PATTERNS = (
    re.compile(rb"(?m)^-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----\r?$"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    # pip's vendored SPDX table contains the public identifier
    # "sk-linking-protocols-exception"; keep real token-shaped values blocked.
    re.compile(rb"sk-(?!linking-protocols-exception(?:[^A-Za-z0-9_-]|$))[A-Za-z0-9_-]{24,}"),
)
REQUIRED_LICENSES = ("LICENSE", "worker/LICENSE", "worker/NOTICE.md", "frontend/assets/OFL-NotoSansSC.txt")
REQUIRED_DOCKER_INSTALL_FILES = (
    "Dockerfile",
    "docker/entrypoint.sh",
    "deploy/docker-install.sh",
    "docker-compose.yml",
    "docker-compose.updates.yml",
    "config/saas.env.docker.example",
    "deploy/update-signing.pub",
)
PUBLIC_RUNTIME_LOCKS = frozenset({
    "deploy/runtime/backend.lock.json",
    "deploy/runtime/python-build-standalone.lock.json",
    "deploy/runtime/worker.lock.json",
})
STANDALONE_ARCHITECTURES = ("x86_64", "aarch64")
STANDALONE_TARGETS = tuple(f"linux-{architecture}" for architecture in STANDALONE_ARCHITECTURES)
MANAGER_PROTOCOL = 1
REQUIRED_STANDALONE_ROOTS = frozenset({"backend", "frontend", "worker", "runtime", "manager"})


def git_environment() -> dict[str, str]:
    # No signing key, API credentials, user Git config, filters or optional index writes.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "protocol.allow", "GIT_CONFIG_VALUE_1": "never",
    })
    return env


def git(root: Path, *args: str, data: bytes | None = None, codes=(0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, input=data, capture_output=True,
            env=git_environment(), timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise BundleError("release_git_failed") from None
    if result.returncode not in codes:
        raise BundleError("release_git_failed")
    return result


def canonical_path(path: str, *, max_length=500, max_component=240) -> None:
    parts = path.split("/")
    if (not path or len(path) > max_length or any(char in path for char in '\\:<>"|?*')
            or unicodedata.normalize("NFC", path) != path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {"", ".", ".."} or len(part) > max_component
                   or part.endswith((".", " ")) for part in parts)):
        raise BundleError("release_path_invalid")
    for part in parts:
        if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part, re.IGNORECASE):
            raise BundleError("release_path_invalid")


def private_path(path: str) -> None:
    canonical_path(path)
    if path in PUBLIC_RUNTIME_LOCKS:
        return
    parts = tuple(part.casefold() for part in path.split("/"))
    name = parts[-1]
    if any(part in PRIVATE_PARTS for part in parts) or name in PRIVATE_NAMES:
        raise BundleError("release_private_path_rejected")
    if path == "backend/build-info.json":
        raise BundleError("release_generated_file_tracked")
    # Examples remain subject to content scanning, and forbidden directories stay forbidden.
    if name.endswith(".example"):
        return
    if (any(part.startswith(".env") for part in parts) or name.endswith((".env", ".local", ".secret", ".secrets", ".key", ".p12", ".pfx"))
            or parts[0] == "config"
            or re.search(r"\.(?:db|sqlite3?|log|pid|cookie|lock)(?:[.-].*)?$", name)
            or name.endswith((".pyc", ".pyo"))):
        raise BundleError("release_private_path_rejected")
    if path == "worker/products_config.json":
        return
    if PurePosixPath(name).suffix not in SOURCE_SUFFIXES and PRIVATE_DATA.search(name):
        raise BundleError("release_private_path_rejected")


def read_tree(root: Path, commit: str) -> list[Blob]:
    raw = git(root, "ls-tree", "-r", "-l", "-z", commit).stdout
    blobs = []
    seen = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            path = encoded.decode("utf-8", "strict")
        except (ValueError, UnicodeError):
            raise BundleError("release_tree_invalid") from None
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise BundleError("release_link_or_special_file_rejected")
        private_path(path)
        # Also reject differently-cased parent directories, not just leaf collisions.
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            folded = prefix.casefold()
            if folded in seen and seen[folded] != prefix:
                raise BundleError("release_path_collision")
            seen[folded] = prefix
        blobs.append(Blob(path, oid.decode("ascii"), int(size), mode == b"100755"))
    return sorted(blobs, key=lambda blob: blob.path)


def load_protocol(source: bytes):
    """Use the commit's exact pure updater definitions, without importing its runtime.

    In particular, importing the whole Linux updater on Windows would import fcntl
    and version.py would inspect local build-info. Neither is needed to build assets.
    No verifier logic is copied, relaxed or replaced here.
    """
    constants = {
        "RELEASE_ASSET_PREFIX", "MAX_MANIFEST_BYTES", "MAX_STANDALONE_MANIFEST_BYTES", "MAX_SIGNATURE_BYTES", "MAX_ARCHIVE_BYTES",
        "MAX_STANDALONE_ARCHIVE_BYTES", "MAX_UNPACKED_BYTES", "MAX_STANDALONE_UNPACKED_BYTES", "MAX_FILE_BYTES",
        "MAX_STANDALONE_FILE_BYTES", "MAX_ARCHIVE_MEMBERS", "MAX_STANDALONE_ARCHIVE_MEMBERS", "MAX_RELEASE_NOTES_CHARS",
        "MAX_PATH_LENGTH", "MAX_PATH_COMPONENT", "SEMVER_RE", "SHA256_RE",
        "ALLOWED_TOP_LEVEL_DIRS", "ALLOWED_ROOT_FILES", "FORBIDDEN_PATH_PARTS",
        "RELEASE_OWNER", "RELEASE_REPOSITORY",
    }
    definitions = {
        "PlatformUpdateError", "SemVer", "ReleaseAsset", "ReleaseInfo", "ManifestFile",
        "_asset_names", "_validate_release_path", "_manifest_files", "parse_manifest",
    }
    tree = ast.parse(source, filename="backend/platform_update.py")
    selected, found = [], set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            selected.append(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in constants:
                selected.append(node)
                found.add(name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in definitions:
            selected.append(node)
            found.add(node.name)
    if found != constants | definitions:
        raise BundleError("release_protocol_unsupported")
    module = types.ModuleType("_release_bundle_protocol")
    module.__dict__.update(
        re=re,
        json=json,
        dataclass=dataclass,
        PurePosixPath=PurePosixPath,
        STANDALONE_RELEASE_KIND="standalone",
    )
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=selected, type_ignores=[]), "backend/platform_update.py", "exec"), module.__dict__)
    return module


def read_blobs(root: Path, blobs: list[Blob], protocol) -> dict[str, tuple[bytes, bool]]:
    if not blobs or len(blobs) + 1 > protocol.MAX_ARCHIVE_MEMBERS:
        raise BundleError("release_too_many_files")
    if any(blob.size > protocol.MAX_FILE_BYTES for blob in blobs) or sum(blob.size for blob in blobs) > protocol.MAX_UNPACKED_BYTES:
        raise BundleError("release_too_large")
    raw = git(root, "cat-file", "--batch", data=b"".join(blob.oid.encode() + b"\n" for blob in blobs)).stdout
    stream = io.BytesIO(raw)
    result = {}
    for blob in blobs:
        if stream.readline() != f"{blob.oid} blob {blob.size}\n".encode():
            raise BundleError("release_blob_invalid")
        payload = stream.read(blob.size)
        if len(payload) != blob.size or stream.read(1) != b"\n":
            raise BundleError("release_blob_invalid")
        canonical_path(blob.path, max_length=protocol.MAX_PATH_LENGTH, max_component=protocol.MAX_PATH_COMPONENT)
        result[blob.path] = (payload, blob.executable)
    if stream.read(1):
        raise BundleError("release_blob_invalid")
    return result


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def release_version(files: dict, protocol) -> str:
    try:
        package = json.loads(files["package.json"][0])
        lock = json.loads(files["package-lock.json"][0])
        # Parse literals, never import/execute version.py or other source runtime.
        tree = ast.parse(files["backend/version.py"][0], filename="backend/version.py")
        assignments = [node for node in tree.body if isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "VERSION"]
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "VERSION"
                  and isinstance(node.ctx, (ast.Store, ast.Del))]
        version = package["version"]
        if not isinstance(version, str) or len(version) > 128 or version != version.strip():
            raise BundleError("release_version_invalid")
        protocol.SemVer.parse(version)
        if (len(assignments) != 1 or len(stores) != 1 or not isinstance(assignments[0].value, ast.Constant)
                or assignments[0].value.value != version
                or lock["version"] != version or lock["packages"][""]["version"] != version):
            raise BundleError("release_version_mismatch")
        canonical_path(f"xianyu-saas-{version}-source.zip", max_component=protocol.MAX_PATH_COMPONENT)
        return version
    except (KeyError, TypeError, ValueError, UnicodeError, SyntaxError, RecursionError, protocol.PlatformUpdateError):
        raise BundleError("release_version_invalid") from None


RELEASE_GUIDE_TEMPLATE = """# xianyu-saas {version} 发布说明

官方仓库：https://github.com/tswawa/xianyu-saas

本版本提供 Docker（推荐）与 Ubuntu 原生安装两种方式。普通用户通过下方下载入口安装即可；其余附件由安装器和更新器自动调用，无需手动下载。

## 用户下载入口

| 部署方式 | 适用环境 | 下载文件 |
| --- | --- | --- |
| Docker 部署（推荐） | Linux 服务器、Docker Desktop + WSL2 | [`xianyu-saas-{version}-source.zip`](https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-source.zip) |
| Ubuntu 原生安装（x86_64） | Ubuntu 22.04 / 24.04、Debian 12（x86_64） | [`xianyu-saas-{version}-linux-x86_64`](https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-linux-x86_64) |
| Ubuntu 原生安装（ARM64） | Ubuntu 22.04 / 24.04、Debian 12（ARM64） | [`xianyu-saas-{version}-linux-aarch64`](https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-linux-aarch64) |

> 提示：推荐下载项目发布的 `xianyu-saas-{version}-source.zip`。它包含构建元数据并与签名清单绑定；GitHub 自动打包的 Source code 不包含发布构建信息。

---

## 首次安装

### 1. Docker 部署（推荐）

系统需具备 `curl`、`unzip`，以及支持 Engine API v1.47 的 Linux Docker Engine 与 Compose 插件。在本地 Linux 终端或 WSL2 Linux 文件系统中执行：

```bash
curl -fLO https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-source.zip
unzip -q xianyu-saas-{version}-source.zip
cd xianyu-saas-{version}
sudo bash deploy/docker-install.sh
```

- **访问地址**：`http://127.0.0.1:4173/xianyu-saas/`
- **数据路径**：业务数据保存在项目目录下的 `./data`

---

### 2. Ubuntu 原生安装（x86_64 与 ARM64 择一执行）

支持 Ubuntu 22.04、24.04 及 Debian 12。系统需具备 `systemd`、`systemd-analyze`、`useradd` 和 `curl`。根据机器架构选择对应的安装命令执行：

**x86_64 架构：**
```bash
curl -fLO https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-linux-x86_64
chmod +x xianyu-saas-{version}-linux-x86_64
sudo ./xianyu-saas-{version}-linux-x86_64 install --version {version}
```

**ARM64 架构：**
```bash
curl -fLO https://github.com/tswawa/xianyu-saas/releases/download/v{version}/xianyu-saas-{version}-linux-aarch64
chmod +x xianyu-saas-{version}-linux-aarch64
sudo ./xianyu-saas-{version}-linux-aarch64 install --version {version}
```

- **访问地址**：`http://127.0.0.1:8096/xianyu-saas/`
- **数据路径**：数据保存在 `/var/lib/xianyu-saas`，配置文件位于 `/etc/xianyu-saas.env`
- **日常维护**：安装后使用统一的 `sudo xianyu-saas status`、`start`、`stop`、`restart`、`doctor` 管理服务

---

## 已有用户更新

- **网页更新**：已完成登记的 Docker 与 Ubuntu 实例，在网页控制台的版本更新页面执行升级。升级只切换代码与镜像，保留业务数据；若启动失败自动回滚代码。
- **Docker 重复运行**：对已登记的 Docker 实例，重复执行 `deploy/docker-install.sh` 只会检查并启动现有容器，不会重建镜像、不加载本地新配置，也不会覆盖业务数据。常规暂停只需 `docker stop xianyu-saas`，恢复直接重新运行安装脚本。Docker 升级时不自动备份数据库，维护前请自行备份 `./data` 目录。
- **Ubuntu 备份与恢复**：原生更新器执行升级时包含数据库备份步骤，配置位于 `/etc/xianyu-saas.env`。仍建议维护者在操作前做好数据备份。
- **历史未登记实例**：未经安装器初始化的旧版容器或早期源码运行实例不在网页自动升级支持范围内，需由维护者参考文档手动迁移。

---

## 附件说明

本版本发布附件共 14 项（包括 Docker 源码包与清单、Ubuntu 双架构管理器与运行时包、发布索引及签名）。其余清单与签名附件供安装器和更新器自动校验使用，完整说明见维护文档 `docs/RELEASING.md`。"""


def release_guide(version: str, protocol) -> str:
    """Render the reviewed installation guide with the release version."""
    return RELEASE_GUIDE_TEMPLATE.replace("{version}", version).strip()


def release_notes(files: dict, version: str, protocol) -> bytes:
    try:
        document = files["CHANGELOG.md"][0].decode("utf-8")
    except (KeyError, UnicodeError):
        raise BundleError("release_notes_missing") from None
    headings = list(re.finditer(r"^##\s+\[?" + re.escape(version) + r"\]?(?=\s|$)[^\n]*\n", document, re.MULTILINE))
    if len(headings) != 1:
        raise BundleError("release_notes_missing")
    section = document[headings[0].end():]
    end = re.search(r"^##\s", section, re.MULTILINE)
    body = section[:end.start() if end else len(section)].strip()
    if not body:
        raise BundleError("release_notes_invalid")
    notes = release_guide(version, protocol) + "\n\n## 本次版本变更\n\n" + body + "\n"
    notes = notes.replace("\r\n", "\n").rstrip("\n") + "\n"
    if len(notes) > protocol.MAX_RELEASE_NOTES_CHARS:
        raise BundleError("release_notes_invalid")
    return notes.encode("utf-8")


def signing_key(encoded: str | None) -> tuple[Ed25519PrivateKey, bytes]:
    if not encoded:
        raise BundleError("release_signing_key_missing")
    try:
        seed = base64.b64decode(encoded, validate=True)
        if len(seed) != 32 or base64.b64encode(seed).decode("ascii") != encoded:
            raise ValueError()
        return Ed25519PrivateKey.from_private_bytes(seed), seed
    except (ValueError, TypeError, binascii.Error):
        raise BundleError("release_signing_key_invalid") from None


def public_key(raw: bytes) -> bytes:
    try:
        value = raw.strip()
        if not value or len(raw) > 4096:
            raise ValueError()
        if value.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(value)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError()
        else:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 32:
                raise ValueError()
            key = Ed25519PublicKey.from_public_bytes(decoded)
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise BundleError("release_public_key_invalid") from None


def scan_payloads(files: dict, seed: bytes, encoded: str) -> None:
    markers = (seed, encoded.encode("ascii"))
    for path, (payload, _) in files.items():
        if any(marker in payload or marker in path.encode("utf-8") for marker in markers) or any(pattern.search(payload) for pattern in SECRET_PATTERNS):
            raise BundleError("release_secret_content_rejected")
        if path == "worker/products_config.json":
            try:
                if json.loads(payload) != {"types": []}:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise BundleError("release_products_template_invalid") from None


def release_epoch(root: Path, commit: str) -> int:
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        value = git(root, "show", "-s", "--format=%ct", commit).stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise BundleError("release_timestamp_invalid")
    epoch = int(value)
    if epoch > 0xFFFFFFFF:
        raise BundleError("release_timestamp_invalid")
    return epoch


def output_path(root: Path, supplied: str | None, version: str) -> Path:
    candidate = Path(supplied) if supplied else Path(".local") / "releases" / version
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    allowed = root / ".local" / "releases"
    if allowed not in candidate.parents:
        raise BundleError("release_output_not_ignored")
    for path in (candidate, *candidate.parents):
        if path == root:
            break
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise BundleError("release_output_link_rejected")
    relative = (candidate.relative_to(root) / ".bundle-probe").as_posix()
    if git(root, "check-ignore", "--quiet", "--no-index", "--", relative, codes=(0, 1)).returncode != 0:
        raise BundleError("release_output_not_ignored")
    ensure_empty_output(candidate)
    return candidate


def ensure_empty_output(path: Path) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise BundleError("release_output_not_empty")


def _reject_linked_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise BundleError("release_notes_output_invalid")
        parent = current.parent
        if parent == current:
            break
        current = parent


def notes_output_path(root: Path, supplied: str | None, output: Path) -> Path:
    """Resolve a fresh notes path that can never overwrite repo source or release assets."""
    candidate = Path(supplied) if supplied else output.parent / (output.name + "-release-notes.md")
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate == output or output in candidate.parents:
        raise BundleError("release_notes_output_invalid")
    if candidate == root or root in candidate.parents:
        ignored = root / ".local"
        if candidate == ignored or ignored not in candidate.parents:
            raise BundleError("release_notes_output_invalid")
    _reject_linked_ancestors(candidate)
    # A pre-existing notes path is never overwritten, so build cleanup can only
    # ever remove the file this build created.
    if candidate.exists():
        raise BundleError("release_notes_output_invalid")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def write_notes_output(path: Path, payload: bytes) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise BundleError("release_notes_output_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_tar(path: Path, files: dict, epoch: int) -> None:
    with path.open("xb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=epoch, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, (payload, executable) in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o755 if executable else 0o644
                    info.mtime = epoch
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(payload))


def write_source_zip(path: Path, files: dict, version: str, epoch: int) -> None:
    # DOS timestamps are UTC, rounded to two seconds and clamped at ZIP's 1980 floor.
    stamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc)
    date_time = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second // 2 * 2)
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (payload, executable) in sorted(files.items()):
            info = zipfile.ZipInfo(f"xianyu-saas-{version}/{name}", date_time=date_time)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            compression = zipfile.ZIP_DEFLATED
            if len(payload) > 1024 * 1024:
                compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
                packed_size = len(compressor.compress(payload)) + len(compressor.flush())
                if len(payload) > packed_size * 200:
                    # Keep legitimate repetitive Git blobs within the verifier's
                    # anti-bomb profile rather than shipping an unusable ZIP.
                    compression = zipfile.ZIP_STORED
            info.compress_type = compression
            archive.writestr(info, payload, compress_type=compression, compresslevel=9)


def asset_record(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return {"name": path.name, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def standalone_protocol():
    path = Path(__file__).resolve().parents[1] / "backend" / "standalone_runtime.py"
    spec = importlib.util.spec_from_file_location("_release_standalone_runtime", path)
    if spec is None or spec.loader is None:
        raise BundleError("release_standalone_protocol_missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise BundleError("release_standalone_protocol_missing") from None
    return module


def read_standalone_tree(
    bundle: Path,
    seed: bytes,
    encoded: str,
    source_executables: set[str],
) -> dict[str, tuple[bytes, bool]]:
    if bundle.is_symlink() or not bundle.is_dir():
        raise BundleError("release_standalone_input_invalid")
    for required in REQUIRED_STANDALONE_ROOTS:
        path = bundle / required
        if path.is_symlink() or not path.is_dir():
            raise BundleError("release_standalone_layout_invalid")
    if (bundle / "app").exists() or (bundle / "app").is_symlink():
        raise BundleError("release_standalone_layout_invalid")
    files = {}
    total = 0
    for current, dirnames, filenames in os.walk(bundle, topdown=True, followlinks=False):
        current_path = Path(current)
        for dirname in dirnames:
            path = current_path / dirname
            if path.is_symlink():
                raise BundleError("release_standalone_link_rejected")
        for filename in filenames:
            path = current_path / filename
            try:
                metadata = path.lstat()
            except OSError:
                raise BundleError("release_standalone_input_invalid") from None
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise BundleError("release_standalone_link_rejected")
            relative = path.relative_to(bundle).as_posix()
            canonical_path(relative, max_length=1000)
            top = PurePosixPath(relative).parts[0]
            if top not in REQUIRED_STANDALONE_ROOTS and relative != "package.json":
                raise BundleError("release_standalone_layout_invalid")
            if relative in files:
                raise BundleError("release_path_collision")
            if metadata.st_size > 256 * 1024 * 1024:
                raise BundleError("release_standalone_too_large")
            payload = path.read_bytes()
            if len(payload) != metadata.st_size:
                raise BundleError("release_standalone_input_invalid")
            if seed in payload or encoded.encode("ascii") in payload or any(pattern.search(payload) for pattern in SECRET_PATTERNS):
                raise BundleError("release_secret_content_rejected")
            total += len(payload)
            if total > 1536 * 1024 * 1024 or len(files) >= 20000:
                raise BundleError("release_standalone_too_large")
            runtime_python = bool(re.fullmatch(
                r"runtime/python/bin/python(?:3(?:\.[0-9]+)?)?", relative
            ))
            executable = (
                bool(stat.S_IMODE(metadata.st_mode) & 0o111)
                or relative in source_executables
                or relative == "manager/xianyu-saas"
                or runtime_python
            )
            files[relative] = (payload, executable)
    if not files:
        raise BundleError("release_standalone_input_invalid")
    return files


def standalone_inputs(
    root: Path,
    version: str,
    commit: str,
    seed: bytes,
    encoded: str,
    runtime_protocol,
    source_executables: set[str],
) -> dict[str, dict]:
    if root.is_symlink() or not root.is_dir():
        raise BundleError("release_standalone_inputs_missing")
    if {path.name for path in root.iterdir()} != set(STANDALONE_TARGETS):
        raise BundleError("release_standalone_inputs_missing")
    result = {}
    for architecture, target in zip(STANDALONE_ARCHITECTURES, STANDALONE_TARGETS):
        directory = root / target
        bundle = directory / "bundle"
        manager = directory / "manager"
        if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != {"bundle", "manager"}:
            raise BundleError("release_standalone_input_invalid")
        files = read_standalone_tree(bundle, seed, encoded, source_executables)
        try:
            runtime = json.loads(files["runtime/runtime.json"][0])
            metadata = runtime_protocol.parse_runtime_metadata(
                json.dumps(runtime, ensure_ascii=True, separators=(",", ":")),
                expected_version=version,
                expected_architecture=architecture,
            )
        except (KeyError, TypeError, ValueError, UnicodeError, runtime_protocol.StandaloneRuntimeError):
            raise BundleError("release_standalone_runtime_invalid") from None
        if metadata.commit != commit or metadata.target != target or metadata.manager_protocol != MANAGER_PROTOCOL:
            raise BundleError("release_standalone_runtime_invalid")
        try:
            manager_metadata = manager.lstat()
        except OSError:
            raise BundleError("release_standalone_manager_invalid") from None
        if (manager.is_symlink() or not stat.S_ISREG(manager_metadata.st_mode)
                or not 0 < manager_metadata.st_size <= 512 * 1024 * 1024):
            raise BundleError("release_standalone_manager_invalid")
        manager_raw = manager.read_bytes()
        embedded = files.get("manager/xianyu-saas")
        if embedded is None or not embedded[1] or embedded[0] != manager_raw:
            raise BundleError("release_standalone_manager_invalid")
        if seed in manager_raw or encoded.encode("ascii") in manager_raw or any(pattern.search(manager_raw) for pattern in SECRET_PATTERNS):
            raise BundleError("release_secret_content_rejected")
        result[target] = {"architecture": architecture, "files": files, "runtime": runtime, "manager": manager_raw}
    return result


def content_metadata(version: str) -> dict[str, tuple[str, str]]:
    base = f"xianyu-saas-{version}"
    result = {
        f"{base}-source.zip": ("docker-source", "docker"),
        f"{base}.docker.manifest.json": ("docker-manifest", "docker"),
        f"{base}.docker.manifest.sig": ("docker-signature", "docker"),
        # The schema-1 runtime inventory stays published unsigned; the signed
        # Docker manifest binds it through runtime_manifest_sha256.
        f"{base}.manifest.json": ("runtime-manifest", "docker"),
    }
    for architecture, target in zip(STANDALONE_ARCHITECTURES, STANDALONE_TARGETS):
        standalone_base = f"{base}-{target}"
        result.update({
            f"{standalone_base}.tar.gz": ("standalone-archive", target),
            f"{standalone_base}.manifest.json": ("standalone-manifest", target),
            f"{standalone_base}.manifest.sig": ("standalone-signature", target),
            standalone_base: ("bootstrap-manager", target),
        })
    return result


def build(args, encoded: str | None) -> dict:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()).resolve()
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{args.ref}^{{commit}}").stdout.decode("ascii").strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise BundleError("release_commit_invalid")
    head = git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip()
    if commit == head and git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=none").stdout:
        raise BundleError("release_worktree_dirty")
    blobs = read_tree(root, commit)
    by_path = {blob.path: blob for blob in blobs}
    if "backend/platform_update.py" not in by_path:
        raise BundleError("release_protocol_missing")
    protocol_blob = by_path["backend/platform_update.py"]
    if protocol_blob.size > 1024 * 1024:
        raise BundleError("release_protocol_unsupported")
    protocol = load_protocol(git(root, "cat-file", "blob", protocol_blob.oid).stdout)
    files = read_blobs(root, blobs, protocol)
    for required in REQUIRED_LICENSES:
        if required not in files or not files[required][0].strip():
            raise BundleError("release_license_missing")
    for required in REQUIRED_DOCKER_INSTALL_FILES:
        if required not in files or not files[required][0].strip():
            raise BundleError("release_docker_source_missing")
    version = release_version(files, protocol)
    notes = release_notes(files, version, protocol)
    key, seed = signing_key(encoded)
    canonical_path(args.public_key_file)
    if args.public_key_file not in files:
        raise BundleError("release_public_key_missing")
    public_raw = public_key(files[args.public_key_file][0])
    derived = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if derived != public_raw:
        raise BundleError("release_public_key_mismatch")
    scan_payloads(files, seed, encoded)
    runtime_protocol = standalone_protocol()
    protocol.validate_standalone_manifest = runtime_protocol.validate_standalone_manifest
    protocol.StandaloneRuntimeError = runtime_protocol.StandaloneRuntimeError
    standalone_root = Path(args.standalone_input_root)
    if not standalone_root.is_absolute():
        standalone_root = root / standalone_root
    standalone_root = Path(os.path.abspath(standalone_root))
    local_root = root / ".local"
    if local_root not in standalone_root.parents:
        raise BundleError("release_standalone_input_invalid")
    source_executables = {path for path, (_payload, executable) in files.items() if executable}
    inputs = standalone_inputs(
        standalone_root, version, commit, seed, encoded, runtime_protocol, source_executables
    )
    epoch = release_epoch(root, commit)
    files["backend/build-info.json"] = (json_bytes({
        "version": version, "commit": commit, "dirty": False,
        "build_time": datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds"),
    }), False)
    if sum(len(payload) for payload, _ in files.values()) > protocol.MAX_UNPACKED_BYTES:
        raise BundleError("release_too_large")
    ota = {}
    for path, value in files.items():
        if path in PUBLIC_RUNTIME_LOCKS:
            continue
        try:
            protocol._validate_release_path(path)
        except protocol.PlatformUpdateError as exc:
            if (
                exc.code == "update_archive_path_invalid"
                or (exc.code == "update_runtime_path_rejected" and (path.endswith(".example") or path in PUBLIC_RUNTIME_LOCKS))
            ):
                continue
            raise BundleError("release_private_path_rejected") from None
        ota[path] = value
    if any(path not in ota for path in REQUIRED_LICENSES):
        raise BundleError("release_license_missing")
    output = output_path(root, args.output, version)
    notes_path = notes_output_path(root, args.notes_output, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=output.parent))
    try:
        artifact_name, manifest_name, signature_name = protocol._asset_names(version)
        # A temporary source tar only computes the runtime inventory's historical
        # artifact fields for the schema-1 manifest kept above. The tar and its
        # detached signature are never published: deployed Docker clients bind
        # this manifest through the signed docker manifest's runtime hash.
        temporary_tar = stage / artifact_name
        write_tar(temporary_tar, ota, epoch)
        archive_record = asset_record(temporary_tar)
        temporary_tar.unlink()
        if archive_record["size"] > protocol.MAX_ARCHIVE_BYTES:
            raise BundleError("release_too_large")
        manifest = {
            "schema": 1, "version": version, "artifact": artifact_name,
            "artifact_sha256": archive_record["sha256"], "artifact_size": archive_record["size"],
            "files": [{"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                       "executable": executable} for path, (payload, executable) in sorted(ota.items())],
        }
        manifest_raw = json_bytes(manifest)
        if len(manifest_raw) > protocol.MAX_MANIFEST_BYTES:
            raise BundleError("release_manifest_too_large")
        release = protocol.ReleaseInfo("offline", version, f"v{version}", "", "", bool(protocol.SemVer.parse(version).prerelease),
                                       protocol.ReleaseAsset(1, artifact_name, archive_record["size"]),
                                       protocol.ReleaseAsset(2, manifest_name, len(manifest_raw)),
                                       protocol.ReleaseAsset(3, signature_name, 88))
        protocol.parse_manifest(manifest_raw, release)
        (stage / manifest_name).write_bytes(manifest_raw)
        source_name = f"xianyu-saas-{version}-source.zip"
        write_source_zip(stage / source_name, files, version, epoch)
        source_record = asset_record(stage / source_name)
        if source_record["size"] > protocol.MAX_ARCHIVE_BYTES:
            raise BundleError("release_too_large")
        # Docker authenticates every build input, without changing the existing
        # schema-1 OTA descriptor, its signature bytes or its path allowlist.
        docker_manifest_raw = json_bytes({
            "schema": 1, "protocol": 1, "version": version, "commit": commit,
            "source": source_record, "runtime_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        })
        docker_signature = base64.b64encode(key.sign(docker_manifest_raw))
        key.public_key().verify(base64.b64decode(docker_signature, validate=True), docker_manifest_raw)
        (stage / f"xianyu-saas-{version}.docker.manifest.json").write_bytes(docker_manifest_raw)
        (stage / f"xianyu-saas-{version}.docker.manifest.sig").write_bytes(docker_signature)
        for target in STANDALONE_TARGETS:
            standalone = inputs[target]
            architecture = standalone["architecture"]
            artifact_name = f"xianyu-saas-{version}-{target}.tar.gz"
            manifest_name = f"xianyu-saas-{version}-{target}.manifest.json"
            signature_name = f"xianyu-saas-{version}-{target}.manifest.sig"
            manager_name = f"xianyu-saas-{version}-{target}"
            write_tar(stage / artifact_name, standalone["files"], epoch)
            archive_record = asset_record(stage / artifact_name)
            if archive_record["size"] > 512 * 1024 * 1024:
                raise BundleError("release_standalone_too_large")
            standalone_manifest = {
                "schema": 2,
                "kind": "standalone",
                "version": version,
                "artifact": artifact_name,
                "artifact_sha256": archive_record["sha256"],
                "artifact_size": archive_record["size"],
                "platform": "linux",
                "architecture": architecture,
                "target": target,
                "manager_protocol": MANAGER_PROTOCOL,
                "update_data_version": standalone["runtime"]["update_data_version"],
                "runtime": standalone["runtime"],
                "files": [
                    {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "executable": executable}
                    for path, (payload, executable) in sorted(standalone["files"].items())
                ],
            }
            try:
                runtime_protocol.validate_standalone_manifest(
                    standalone_manifest,
                    expected_version=version,
                    expected_target=target,
                    expected_artifact=artifact_name,
                )
            except runtime_protocol.StandaloneRuntimeError:
                raise BundleError("release_standalone_manifest_invalid") from None
            standalone_raw = json_bytes(standalone_manifest)
            if len(standalone_raw) > protocol.MAX_STANDALONE_MANIFEST_BYTES:
                raise BundleError("release_standalone_too_large")
            standalone_release = protocol.ReleaseInfo(
                "offline", version, f"v{version}", "", "", bool(protocol.SemVer.parse(version).prerelease),
                protocol.ReleaseAsset(1, artifact_name, archive_record["size"]),
                protocol.ReleaseAsset(2, manifest_name, len(standalone_raw)),
                protocol.ReleaseAsset(3, signature_name, 88),
                kind="standalone", target=target,
            )
            protocol.parse_manifest(standalone_raw, standalone_release)
            standalone_signature = base64.b64encode(key.sign(standalone_raw))
            key.public_key().verify(base64.b64decode(standalone_signature, validate=True), standalone_raw)
            (stage / manifest_name).write_bytes(standalone_raw)
            (stage / signature_name).write_bytes(standalone_signature)
            manager_path = stage / manager_name
            manager_path.write_bytes(standalone["manager"])
            manager_path.chmod(0o755)
        metadata = content_metadata(version)
        if {path.name for path in stage.iterdir()} != set(metadata):
            raise BundleError("release_asset_set_invalid")
        records = []
        for path in sorted(stage.iterdir()):
            kind, target = metadata[path.name]
            records.append({**asset_record(path), "kind": kind, "target": target, "manager_protocol": MANAGER_PROTOCOL})
        result = {
            "schema": 2,
            "version": version,
            "commit": commit,
            "manager_protocol": MANAGER_PROTOCOL,
            "public_key_fingerprint": "sha256:" + hashlib.sha256(public_raw).hexdigest(),
            "files": records,
        }
        index_raw = json_bytes(result)
        (stage / "artifacts.json").write_bytes(index_raw)
        (stage / "artifacts.json.sig").write_bytes(base64.b64encode(key.sign(index_raw)))
        verifier = Path(__file__).with_name("verify-public-release.py")
        # Historical releases must use the selected Git blob, never a dirty key
        # from the current checkout. Keep this verifier input outside the assets.
        with tempfile.TemporaryDirectory(prefix="release-verification-", dir=output.parent) as verification_dir:
            verification_key = Path(verification_dir) / "update-signing.pub"
            verification_key.write_bytes(files[args.public_key_file][0])
            verification = subprocess.run(
                [sys.executable, "-B", str(verifier), "--directory", str(stage), "--version", version,
                 "--commit", commit, "--public-key", str(verification_key)],
                cwd=root, capture_output=True, env=git_environment(), timeout=120, check=False,
            )
        if verification.returncode != 0:
            raise BundleError("release_verification_failed")
        ensure_empty_output(output)
        write_notes_output(notes_path, notes)
        try:
            if output.exists():
                output.rmdir()
            os.rename(stage, output)
        except BaseException:
            notes_path.unlink(missing_ok=True)
            raise
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git commit or ref; never package worktree contents")
    parser.add_argument("--output", help="Ignored directory below .local/releases/ (default: version)")
    parser.add_argument("--notes-output", help="Release notes path outside the published asset directory (default: ignored sibling of --output)")
    parser.add_argument("--signing-key-env", default="RELEASE_SIGNING_KEY", help="Environment variable holding a standard Base64 32-byte Ed25519 seed")
    parser.add_argument("--public-key-file", default="deploy/update-signing.pub", help="Public key path inside the selected commit")
    parser.add_argument("--standalone-input-root", required=True, help="Ignored root containing linux-*/bundle and linux-*/manager inputs")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.signing_key_env):
        print("release_signing_key_env_invalid", file=sys.stderr)
        return 1
    encoded = os.environ.pop(args.signing_key_env, None)
    try:
        result = build(args, encoded)
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Do not print exception repr/tracebacks: dependencies may include input values.
        print("release_build_failed", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

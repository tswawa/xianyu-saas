"""Portable signed Docker source protocol; never import or execute application code.

The caller supplies a previously installed trusted public key, not the public key
in the downloaded release. Extraction requires a non-existent destination inside
a caller-controlled private parent. No application/runtime imports or locks are
needed, including on native Windows.
"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PROTOCOL = 1
MAX_MANIFEST_BYTES = 16 * 1024
MAX_SIGNATURE_BYTES = 4096
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_IDENTITY_BYTES = 4 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_PATH_LENGTH = 500
MAX_PATH_COMPONENT = 240
CHUNK_BYTES = 128 * 1024
SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
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
PUBLIC_RUNTIME_LOCKS = frozenset({
    "deploy/runtime/backend.lock.json",
    "deploy/runtime/python-build-standalone.lock.json",
    "deploy/runtime/worker.lock.json",
})
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".md", ".pub",
    ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif",
    ".woff", ".woff2", ".ttf", ".otf",
})


class DockerUpdateError(ValueError):
    """Stable, non-sensitive error code for API and independent executor callers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DockerManifest:
    version: str
    commit: str
    source_name: str
    source_size: int
    source_sha256: str
    runtime_manifest_sha256: str
    protocol: int = PROTOCOL


def docker_asset_names(version: str) -> tuple[str, str, str]:
    if not isinstance(version, str) or len(version) > 128:
        raise DockerUpdateError("docker_version_invalid")
    match = SEMVER_RE.fullmatch(version)
    if match is None or any(part.isdigit() and len(part) > 1 and part[0] == "0"
                            for part in (match.group(4) or "").split(".")):
        raise DockerUpdateError("docker_version_invalid")
    base = f"xianyu-saas-{version}"
    return f"{base}-source.zip", f"{base}.docker.manifest.json", f"{base}.docker.manifest.sig"


def _json(raw: bytes, code: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError()

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise DockerUpdateError(code) from None


def _parse_manifest(raw: bytes, expected_version=None) -> DockerManifest:
    value = _json(raw, "docker_manifest_invalid")
    fields = {"schema", "protocol", "version", "commit", "source", "runtime_manifest_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise DockerUpdateError("docker_manifest_invalid")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise DockerUpdateError("docker_manifest_invalid")
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL:
        raise DockerUpdateError("docker_protocol_unsupported")
    version = value["version"]
    source_name = docker_asset_names(version)[0]
    if expected_version is not None and version != expected_version:
        raise DockerUpdateError("docker_version_mismatch")
    source = value["source"]
    if (not isinstance(source, dict) or set(source) != {"name", "size", "sha256"}
            or source["name"] != source_name
            or type(source["size"]) is not int or not 0 < source["size"] <= MAX_ARCHIVE_BYTES
            or not isinstance(source["sha256"], str) or not SHA256_RE.fullmatch(source["sha256"])
            or not isinstance(value["commit"], str) or not COMMIT_RE.fullmatch(value["commit"])
            or not isinstance(value["runtime_manifest_sha256"], str)
            or not SHA256_RE.fullmatch(value["runtime_manifest_sha256"])):
        raise DockerUpdateError("docker_manifest_invalid")
    return DockerManifest(version, value["commit"], source_name, source["size"], source["sha256"],
                          value["runtime_manifest_sha256"], value["protocol"])


def load_docker_public_key(raw: bytes) -> Ed25519PublicKey:
    """Validate a preinstalled public key without reading paths or application state."""
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 4096:
            raise ValueError()
        if len(raw) == 32:
            return Ed25519PublicKey.from_public_bytes(raw)
        value = raw.strip()
        if value.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(value)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError()
            return key
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded) != value:
            raise ValueError()
        return Ed25519PublicKey.from_public_bytes(decoded)
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise DockerUpdateError("docker_public_key_invalid") from None


def verify_docker_manifest(raw: bytes, signature: bytes, public_key_bytes: bytes,
                           expected_version=None) -> DockerManifest:
    """Authenticate exact descriptor bytes with an out-of-band trusted Ed25519 key."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MANIFEST_BYTES:
        raise DockerUpdateError("docker_manifest_invalid")
    key = load_docker_public_key(public_key_bytes)
    try:
        if not isinstance(signature, bytes) or not 0 < len(signature) <= MAX_SIGNATURE_BYTES:
            raise ValueError()
        encoded = signature.strip()
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) != 64 or base64.b64encode(decoded) != encoded:
            raise ValueError()
        key.verify(decoded, raw)
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        raise DockerUpdateError("docker_signature_invalid") from None
    return _parse_manifest(raw, expected_version)


def _source_path(path: str) -> None:
    parts = path.split("/")
    if (not path or len(path) > MAX_PATH_LENGTH or any(char in path for char in '\\:<>"|?*')
            or unicodedata.normalize("NFC", path) != path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {"", ".", ".."} or len(part) > MAX_PATH_COMPONENT
                   or part.endswith((".", " ")) for part in parts)):
        raise DockerUpdateError("docker_source_path_invalid")
    for part in parts:
        if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part, re.IGNORECASE):
            raise DockerUpdateError("docker_source_path_invalid")
    if path in PUBLIC_RUNTIME_LOCKS:
        return
    folded = tuple(part.casefold() for part in parts)
    name = folded[-1]
    if any(part in PRIVATE_PARTS for part in folded) or name in PRIVATE_NAMES:
        raise DockerUpdateError("docker_source_private_path")
    if name.endswith(".example"):
        return
    if (any(part.startswith(".env") for part in folded)
            or name.endswith((".env", ".local", ".secret", ".secrets", ".key", ".p12", ".pfx", ".pyc", ".pyo"))
            or folded[0] == "config"
            or re.search(r"\.(?:db|sqlite3?|log|pid|cookie|lock)(?:[.-].*)?$", name)):
        raise DockerUpdateError("docker_source_private_path")
    if path == "worker/products_config.json":
        return
    if PurePosixPath(name).suffix not in SOURCE_SUFFIXES and PRIVATE_DATA.search(name):
        raise DockerUpdateError("docker_source_private_path")


def _no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DockerUpdateError("docker_source_link_rejected")


def _copy_source(zip_path: Path, target, manifest: DockerManifest) -> None:
    _no_links(zip_path)
    before = zip_path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DockerUpdateError("docker_source_link_rejected")
    # A shared regular file can become a FIFO between lstat and open. Do not
    # block waiting for a writer: validate the opened descriptor before reading.
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(zip_path, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise DockerUpdateError("docker_source_link_rejected")
        if opened.st_size != manifest.source_size:
            raise DockerUpdateError("docker_source_size_mismatch")
        digest, size = hashlib.sha256(), 0
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            size += len(chunk)
            if size > manifest.source_size or size > MAX_ARCHIVE_BYTES:
                raise DockerUpdateError("docker_source_size_mismatch")
            digest.update(chunk)
            target.write(chunk)
        if size != manifest.source_size:
            raise DockerUpdateError("docker_source_size_mismatch")
        if digest.hexdigest() != manifest.source_sha256:
            raise DockerUpdateError("docker_source_hash_mismatch")
    target.flush()
    target.seek(0)


def _zip_directory(source, size: int) -> tuple[int, int]:
    # Official source ZIPs have no comment/ZIP64/multi-disk extensions. Bound the
    # central-directory allocation BEFORE ZipFile parses it into Python objects.
    if size < 22:
        raise DockerUpdateError("docker_source_archive_invalid")
    source.seek(size - 22)
    magic, disk, cd_disk, local_count, count, cd_size, offset, comment = struct.unpack("<4s4H2LH", source.read(22))
    if (magic != b"PK\x05\x06" or disk or cd_disk or comment or local_count != count
            or not 0 < count <= MAX_ARCHIVE_MEMBERS or cd_size > MAX_ARCHIVE_MEMBERS * (46 + MAX_PATH_LENGTH * 4)
            or offset + cd_size != size - 22):
        raise DockerUpdateError("docker_source_archive_invalid")
    # ZipFile follows a ZIP64 locator before it trusts the normal EOCD. Never
    # let a forged legacy count/size hide an alternate ZIP64 directory from the
    # allocation bounds above. Official bundles have neither extension.
    if size >= 42:
        source.seek(size - 42)
        if source.read(4) == b"PK\x06\x07":
            raise DockerUpdateError("docker_source_archive_invalid")
    position, actual_count, end = offset, 0, offset + cd_size
    while position < end:
        if actual_count >= count or actual_count >= MAX_ARCHIVE_MEMBERS or end - position < 46:
            raise DockerUpdateError("docker_source_archive_invalid")
        source.seek(position)
        header = source.read(46)
        if len(header) != 46:
            raise DockerUpdateError("docker_source_archive_invalid")
        fields = struct.unpack("<4s6H3I5H2I", header)
        name_size, extra_size, comment_size, disk_start = fields[10:14]
        if (fields[0] != b"PK\x01\x02" or disk_start or not name_size
                or fields[8] == 0xFFFFFFFF or fields[9] == 0xFFFFFFFF or fields[16] == 0xFFFFFFFF
                or position + 46 + name_size + extra_size + comment_size > end):
            raise DockerUpdateError("docker_source_archive_invalid")
        # Reject all extras before ZipInfo._decodeExtra can follow ZIP64 sizes,
        # offsets or Unix link metadata. No variable-sized buffers are needed.
        if extra_size or comment_size or fields[2] > 20:
            raise DockerUpdateError("docker_source_member_invalid")
        position += 46 + name_size
        actual_count += 1
    if position != end or actual_count != count:
        raise DockerUpdateError("docker_source_archive_invalid")
    source.seek(0)
    return count, offset


def _zip_members(archive, source, manifest: DockerManifest, count: int, cd_offset: int):
    infos = archive.infolist()
    if len(infos) != count:
        raise DockerUpdateError("docker_source_archive_invalid")
    prefix = f"xianyu-saas-{manifest.version}/"
    seen, declared, members = {}, set(), []
    total, next_offset = 0, 0
    for info in infos:
        original = info.orig_filename
        if (original != info.filename or not original.startswith(prefix)
                or not original.removeprefix(prefix)):
            raise DockerUpdateError("docker_source_path_invalid")
        relative = original.removeprefix(prefix)
        is_directory = relative.endswith("/")
        relative = relative[:-1] if is_directory else relative
        _source_path(relative)
        mode = info.external_attr >> 16
        # No extra fields, including Unix link/hardlink metadata or alternate
        # Unicode names; inspect LOCAL extras as well as the central directory.
        if (info.create_system != 3 or stat.S_IFMT(mode) != (stat.S_IFDIR if is_directory else stat.S_IFREG)
                or mode & 0o7000 or info.extra or info.comment
                or info.flag_bits & ~0x800 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or bool(info.external_attr & 0x10) != is_directory
                or is_directory and (info.file_size or info.compress_size)):
            raise DockerUpdateError("docker_source_member_invalid")
        if (info.file_size > MAX_FILE_BYTES or info.compress_size > manifest.source_size
                or info.file_size > max(1024 * 1024, info.compress_size * MAX_COMPRESSION_RATIO)):
            raise DockerUpdateError("docker_source_too_large")
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise DockerUpdateError("docker_source_too_large")
        parts = relative.split("/")
        for index in range(1, len(parts) + 1):
            name = "/".join(parts[:index])
            kind = "directory" if index < len(parts) or is_directory else "file"
            folded = name.casefold()
            if folded in seen and seen[folded] != (name, kind):
                raise DockerUpdateError("docker_source_path_collision")
            seen[folded] = (name, kind)
        if relative in declared:
            raise DockerUpdateError("docker_source_path_collision")
        declared.add(relative)
        if info.header_offset != next_offset:
            raise DockerUpdateError("docker_source_archive_invalid")
        source.seek(info.header_offset)
        header = source.read(30)
        if len(header) != 30:
            raise DockerUpdateError("docker_source_archive_invalid")
        magic, version, flags, compression, _time, _date, crc, packed, unpacked, name_size, extra_size = struct.unpack("<4s5H3I2H", header)
        encoded_name = original.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
        if (magic != b"PK\x03\x04" or version > 20 or flags != info.flag_bits or compression != info.compress_type
                or crc != info.CRC or packed != info.compress_size or unpacked != info.file_size
                or name_size != len(encoded_name) or extra_size or source.read(name_size) != encoded_name):
            raise DockerUpdateError("docker_source_member_invalid")
        next_offset = info.header_offset + 30 + name_size + info.compress_size
        if next_offset > cd_offset:
            raise DockerUpdateError("docker_source_archive_invalid")
        members.append((info, relative, is_directory))
    if next_offset != cd_offset:
        raise DockerUpdateError("docker_source_archive_invalid")
    return members


def _read_identity(root: Path, name: str) -> bytes:
    path = root / name
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_IDENTITY_BYTES:
        raise DockerUpdateError("docker_source_identity_invalid")
    return path.read_bytes()


def _verify_identity(root: Path, manifest: DockerManifest) -> None:
    try:
        source = _read_identity(root, "backend/version.py")
        tree = ast.parse(source, filename="backend/version.py")
        assignments = [node for node in tree.body if isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "VERSION"]
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "VERSION"
                  and isinstance(node.ctx, (ast.Store, ast.Del))]
        if (len(assignments) != 1 or len(stores) != 1
                or not isinstance(assignments[0].value, ast.Constant)
                or assignments[0].value.value != manifest.version):
            raise DockerUpdateError("docker_source_version_mismatch")
        for name in ("package.json", "package-lock.json", "backend/build-info.json"):
            value = _json(_read_identity(root, name), "docker_source_identity_invalid")
            if not isinstance(value, dict) or value.get("version") != manifest.version:
                raise DockerUpdateError("docker_source_version_mismatch")
            if name == "package-lock.json" and value.get("packages", {}).get("", {}).get("version") != manifest.version:
                raise DockerUpdateError("docker_source_version_mismatch")
            if name == "backend/build-info.json" and (value.get("commit") != manifest.commit or value.get("dirty") is not False):
                raise DockerUpdateError("docker_source_commit_mismatch")
        for name in ("Dockerfile", "docker/entrypoint.sh"):
            _read_identity(root, name)
        template = root / "worker/products_config.json"
        if template.exists() and _json(_read_identity(root, "worker/products_config.json"), "docker_source_private_path") != {"types": []}:
            raise DockerUpdateError("docker_source_private_path")
    except (SyntaxError, UnicodeError, TypeError, AttributeError, RecursionError, ValueError) as exc:
        if isinstance(exc, DockerUpdateError):
            raise
        raise DockerUpdateError("docker_source_identity_invalid") from None


def extract_verified_source(zip_path, destination, manifest: DockerManifest) -> Path:
    """Hash a private snapshot, safely stream-extract, and statically check identity.

    Only the builder's bounded ZIP profile is accepted. Files are never executed;
    extraction never uses extractall or archive-controlled filesystem metadata.
    The entire newly-created destination is removed on every validation failure.
    """
    if not isinstance(manifest, DockerManifest):
        raise DockerUpdateError("docker_manifest_invalid")
    _parse_manifest(json.dumps({"schema": 1, "protocol": manifest.protocol, "version": manifest.version,
                               "commit": manifest.commit, "runtime_manifest_sha256": manifest.runtime_manifest_sha256,
                               "source": {"name": manifest.source_name, "size": manifest.source_size,
                                          "sha256": manifest.source_sha256}}).encode())
    destination = Path(os.path.abspath(destination))
    zip_path = Path(os.path.abspath(zip_path))
    created = False
    try:
        _no_links(destination.parent)
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError:
            raise DockerUpdateError("docker_destination_exists") from None
        created = True
        root = destination / f"xianyu-saas-{manifest.version}"
        root.mkdir(mode=0o700)
        with tempfile.TemporaryFile(mode="w+b", dir=destination) as private_source:
            _copy_source(zip_path, private_source, manifest)
            count, offset = _zip_directory(private_source, manifest.source_size)
            with zipfile.ZipFile(private_source) as archive:
                members = _zip_members(archive, private_source, manifest, count, offset)
                total = 0
                for info, relative, is_directory in members:
                    target = root / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if is_directory:
                        target.mkdir(mode=0o700, exist_ok=True)
                        continue
                    size = 0
                    with archive.open(info, "r") as source, target.open("xb") as output:
                        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                            size += len(chunk)
                            total += len(chunk)
                            if size > info.file_size or size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES:
                                raise DockerUpdateError("docker_source_too_large")
                            output.write(chunk)
                    if size != info.file_size:
                        raise DockerUpdateError("docker_source_size_mismatch")
                    target.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
        _verify_identity(root, manifest)
        return root
    except DockerUpdateError:
        if created:
            shutil.rmtree(destination)
        raise
    except (OSError, ValueError, EOFError, UnicodeError, NotImplementedError, RuntimeError,
            struct.error, zipfile.BadZipFile, zlib.error):
        if created:
            shutil.rmtree(destination)
        raise DockerUpdateError("docker_source_archive_invalid") from None

#!/usr/bin/env python3
"""Offline/native-Windows Docker source contract with real ephemeral signatures."""

from __future__ import annotations

import base64
import builtins
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0-rc.1+build.2"
COMMIT = "ab" * 20


def load_protocol():
    real_import = builtins.__import__

    def portable_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"fcntl", "app", "version", "platform_update", "runtime_settings"}, name
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("docker_source_contract_protocol", ROOT / "backend/docker_update_protocol.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.object(builtins, "__import__", portable_import):
        spec.loader.exec_module(module)
    return module


PROTOCOL = load_protocol()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def reject(code, operation):
    try:
        operation()
    except PROTOCOL.DockerUpdateError as exc:
        assert exc.code == str(exc)
        if code is not None:
            assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError("untrusted source accepted")


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.prefix = f"xianyu-saas-{VERSION}/"
        self.files = {
            "Dockerfile": b"FROM scratch\nCOPY . /app\n",
            "docker/entrypoint.sh": b"#!/bin/sh\nexit 0\n",
            "backend/version.py": f'VERSION = "{VERSION}"\nraise AssertionError("source must never run")\n'.encode(),
            "backend/build-info.json": encoded({"version": VERSION, "commit": COMMIT, "dirty": False}),
            "backend/example.py": b"raise AssertionError('do not execute source')\n",
            "package.json": encoded({"version": VERSION}),
            "package-lock.json": encoded({"version": VERSION, "packages": {"": {"version": VERSION}}}),
            "worker/products_config.json": encoded({"types": []}),
            "worker/.env.example": b"SAAS_SECRET=\n",
            "config/saas.env.example": b"SAAS_ADMIN_TOKEN=\n",
            "docs/说明.md": "公开说明\n".encode(),
            "docs/assets/orders.png": b"public screenshot fixture\n",
            "tests/test_private_key.py": b"# Code, not a private key.\n",
            "deploy/update-signing.pub": base64.b64encode(self.public) + b"\n",
            "deploy/runtime/backend.lock.json": b"{}\n",
            "deploy/runtime/python-build-standalone.lock.json": b"{}\n",
            "deploy/runtime/worker.lock.json": b"{}\n",
        }
        self.counter = 0

    def archive(self, changes=None, extra=(), configure=None):
        self.counter += 1
        target = self.root / f"source-{self.counter}.zip"
        files = dict(self.files)
        for name, value in (changes or {}).items():
            if value is None:
                files.pop(name, None)
            else:
                files[name] = value
        entries = [(self.prefix + name, value) for name, value in sorted(files.items())] + list(extra)
        with warnings.catch_warnings(), zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            warnings.simplefilter("ignore", UserWarning)
            for name, value in entries:
                info = zipfile.ZipInfo(name)
                # Preserve malicious bytes: ZipInfo's constructor normalizes
                # backslashes on Windows and truncates NULs before writing.
                info.filename = info.orig_filename = name
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                if configure:
                    configure(info)
                archive.writestr(info, value)
        return target

    def descriptor(self, archive):
        return {"schema": 1, "protocol": 1, "version": VERSION, "commit": COMMIT,
                "source": {"name": PROTOCOL.docker_asset_names(VERSION)[0], "size": archive.stat().st_size,
                           "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
                "runtime_manifest_sha256": hashlib.sha256(b"external legacy runtime manifest").hexdigest()}

    def verify(self, value, public=None, expected=VERSION):
        raw = value if isinstance(value, bytes) else encoded(value)
        signature = base64.b64encode(self.key.sign(raw))
        return PROTOCOL.verify_docker_manifest(raw, signature, self.public if public is None else public, expected)

    def extract(self, archive, manifest=None, destination=None):
        destination = destination or self.root / f"unpack-{self.counter}"
        return PROTOCOL.extract_verified_source(archive, destination, manifest or self.verify(self.descriptor(archive)))

    def bad_archive(self, code=None, **kwargs):
        archive = self.archive(**kwargs)
        destination = self.root / f"bad-{self.counter}"
        reject(code, lambda: self.extract(archive, destination=destination))
        assert not destination.exists(), "failed validation left executable source behind"


def signature_contract(fixture):
    archive = fixture.archive()
    value = fixture.descriptor(archive)
    manifest = fixture.verify(value)
    assert manifest.commit == COMMIT and manifest.protocol == 1 and manifest.version == VERSION
    assert PROTOCOL.docker_asset_names(VERSION) == (f"xianyu-saas-{VERSION}-source.zip", f"xianyu-saas-{VERSION}.docker.manifest.json", f"xianyu-saas-{VERSION}.docker.manifest.sig")
    assert fixture.verify(value, base64.b64encode(fixture.public) + b"\n") == manifest
    pem = fixture.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert fixture.verify(value, pem) == manifest
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    reject("docker_signature_invalid", lambda: fixture.verify(value, other))
    reject("docker_public_key_invalid", lambda: fixture.verify(value, b"untrusted-release-key"))
    reject("docker_version_mismatch", lambda: fixture.verify(value, expected="0.3.0"))
    raw = encoded(value)
    signed = base64.b64encode(fixture.key.sign(raw))
    for key in ("commit", "runtime_manifest_sha256", "version", "protocol"):
        modified = dict(value)
        modified[key] = "0" * 64 if key != "protocol" else 2
        reject("docker_signature_invalid", lambda modified=modified: PROTOCOL.verify_docker_manifest(encoded(modified), signed, fixture.public))
    for key in ("name", "size", "sha256"):
        modified = copy.deepcopy(value)
        modified["source"][key] = 123 if key == "size" else "tampered"
        reject("docker_signature_invalid", lambda modified=modified: PROTOCOL.verify_docker_manifest(encoded(modified), signed, fixture.public))
    for signature in (b"invalid!", b"", base64.b64encode(b"short"), signed[:-4] + b"AAAA", b"a" * (PROTOCOL.MAX_SIGNATURE_BYTES + 1)):
        reject("docker_signature_invalid", lambda signature=signature: PROTOCOL.verify_docker_manifest(raw, signature, fixture.public))
    reject("docker_signature_invalid", lambda: PROTOCOL.verify_docker_manifest(raw + b" ", signed, fixture.public))
    for key, bad in (("schema", True), ("schema", 2), ("commit", "abc1234"), ("commit", "AB" * 20),
                     ("runtime_manifest_sha256", "wrong"), ("unexpected_command", "execute")):
        modified = dict(value)
        modified[key] = bad
        reject("docker_manifest_invalid", lambda modified=modified: fixture.verify(modified))
    for invalid in (True, 0, 2, "1"):
        modified = dict(value, protocol=invalid)
        reject("docker_protocol_unsupported", lambda modified=modified: fixture.verify(modified))
    for key, bad in (("name", "../../source.zip"), ("size", True), ("size", -1), ("size", 0),
                     ("size", PROTOCOL.MAX_ARCHIVE_BYTES + 1), ("size", 1.0), ("sha256", "AB" * 32), ("extra", "field")):
        modified = copy.deepcopy(value)
        modified["source"][key] = bad
        reject("docker_manifest_invalid", lambda modified=modified: fixture.verify(modified))
    duplicate = raw[:-2] + b',"schema":1}\n'
    reject("docker_manifest_invalid", lambda: fixture.verify(duplicate))
    duplicate_source = raw.replace(b'"size":', b'"name":"duplicate","size":', 1)
    reject("docker_manifest_invalid", lambda: fixture.verify(duplicate_source))
    reject("docker_manifest_invalid", lambda: fixture.verify(raw.replace(b'"schema":1', b'"schema":NaN')))
    reject("docker_manifest_invalid", lambda: fixture.verify(b" " * (PROTOCOL.MAX_MANIFEST_BYTES + 1)))
    for version in ("01.0.0", "1.0.0-01", "v1.0.0", "1.0.0/evil", "1.0.0\n", " 1.0.0", "1.0", None):
        reject("docker_version_invalid", lambda version=version: PROTOCOL.docker_asset_names(version))
    print("docker source: real Ed25519, pinned keys, strict descriptor and all signed bindings passed")


def source_contract(fixture):
    archive = fixture.archive()
    manifest = fixture.verify(fixture.descriptor(archive))
    with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("whole-file ZIP reads forbidden")), \
         patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("unsafe extraction forbidden")), \
         patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("unsafe extraction forbidden")):
        root = fixture.extract(archive, manifest)
    assert root.name == f"xianyu-saas-{VERSION}"
    assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()} == fixture.files
    if os.name != "nt":
        assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "docker/entrypoint.sh").stat().st_mode) == 0o755
    for filename in ("Dockerfile", "backend/example.py"):
        modified = fixture.archive({filename: bytes([fixture.files[filename][0] ^ 1]) + fixture.files[filename][1:]})
        reject(None, lambda modified=modified: fixture.extract(modified, manifest))
        assert not (fixture.root / f"unpack-{fixture.counter}").exists()
    original = fixture.archive()
    trusted = fixture.verify(fixture.descriptor(original))
    raw = bytearray(original.read_bytes())
    raw[70] ^= 1
    original.write_bytes(raw)
    reject("docker_source_hash_mismatch", lambda: fixture.extract(original, trusted))
    original.write_bytes(bytes(raw) + b"suffix")
    reject("docker_source_size_mismatch", lambda: fixture.extract(original, trusted))
    # Changes to the shared file after digest verification cannot change build inputs.
    snapshot = fixture.archive()
    trusted = fixture.verify(fixture.descriptor(snapshot))
    directory_reader = PROTOCOL._zip_directory

    def change_shared(source, size):
        snapshot.write_bytes(b"replaced by an untrusted writer")
        return directory_reader(source, size)

    with patch.object(PROTOCOL, "_zip_directory", side_effect=change_shared):
        root = fixture.extract(snapshot, trusted)
    assert (root / "Dockerfile").read_bytes() == fixture.files["Dockerfile"]
    existing = fixture.root / "existing-destination"
    existing.mkdir()
    (existing / "keep").write_bytes(b"must survive")
    archive = fixture.archive()
    reject("docker_destination_exists", lambda: fixture.extract(archive, destination=existing))
    assert (existing / "keep").read_bytes() == b"must survive"
    hardlinked = fixture.root / "hardlinked.zip"
    os.link(archive, hardlinked)
    reject("docker_source_link_rejected", lambda: fixture.extract(hardlinked))
    hardlinked.unlink()
    actual_lstat = Path.lstat

    def symlink_metadata(candidate, *args, **kwargs):
        result = actual_lstat(candidate, *args, **kwargs)
        if candidate == archive:
            values = list(result)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return result

    with patch.object(Path, "lstat", symlink_metadata):
        reject("docker_source_link_rejected", lambda: fixture.extract(archive))
    print("docker source: independent hash, source/Dockerfile tampering, private snapshot and cleanup passed")


def open_race_contract(fixture):
    actual_open, actual_fstat = os.open, os.fstat
    native_nonblock = getattr(os, "O_NONBLOCK", 0)
    nonblock = native_nonblock or (1 << 29)
    for changed in (None, "fifo", "hardlink", "inode"):
        archive = fixture.archive()
        manifest = fixture.verify(fixture.descriptor(archive))
        destination = fixture.root / f"open-race-{fixture.counter}"
        source_fd = None
        metadata_replaced = False

        def checked_open(path, flags, *args, **kwargs):
            nonlocal source_fd
            if Path(path) != archive:
                return actual_open(path, flags, *args, **kwargs)
            assert flags & nonblock, "source open can block if a regular file becomes a FIFO"
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
            # Windows has no real O_NONBLOCK. Assert the portable feature branch
            # deterministically, removing only the synthetic bit at the OS edge.
            source_fd = actual_open(path, flags if native_nonblock else flags & ~nonblock, *args, **kwargs)
            return source_fd

        def replaced_metadata(fd):
            nonlocal metadata_replaced
            info = actual_fstat(fd)
            if fd != source_fd or changed is None or metadata_replaced:
                return info
            metadata_replaced = True
            values = list(info)
            if changed == "fifo":
                values[0] = stat.S_IFIFO | 0o600
            elif changed == "hardlink":
                values[3] = 2
            else:
                values[1] += 1
            return os.stat_result(values)

        with patch.object(os, "O_NONBLOCK", nonblock, create=True), \
             patch.object(os, "open", side_effect=checked_open), \
             patch.object(os, "fstat", side_effect=replaced_metadata):
            if changed is None:
                fixture.extract(archive, manifest, destination)
            else:
                reject("docker_source_link_rejected", lambda: fixture.extract(archive, manifest, destination))
                assert not destination.exists()
        assert source_fd is not None
        try:
            actual_fstat(source_fd)
        except OSError:
            pass
        else:
            raise AssertionError("source descriptor leaked after validation")
    if sys.platform.startswith("linux"):
        with tempfile.TemporaryDirectory(prefix="xianyu-source-fifo-race-") as temporary:
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--fifo-race-child", temporary],
                                    capture_output=True, timeout=10, check=False)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
            assert b"real FIFO race rejected" in result.stdout
        print("docker source: nonblocking flags, opened identity and real FIFO race subprocess passed")
    else:
        print("docker source: nonblocking flags and opened identity passed; real FIFO subprocess is Linux-only")


def fifo_race_child(directory):
    assert sys.platform.startswith("linux") and hasattr(os, "O_NONBLOCK")
    fixture = Fixture(Path(directory) / "fixture")
    archive = fixture.archive()
    manifest = fixture.verify(fixture.descriptor(archive))
    actual_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == archive and not replaced:
            # Deterministically swap only after both lstat checks. Without
            # O_NONBLOCK this open waits forever because no FIFO writer exists.
            archive.unlink()
            os.mkfifo(archive, 0o600)
            replaced = True
        return actual_open(path, flags, *args, **kwargs)

    with patch.object(os, "open", side_effect=replace_before_open):
        reject("docker_source_link_rejected", lambda: fixture.extract(archive, manifest))
    assert replaced and stat.S_ISFIFO(archive.lstat().st_mode)
    assert not (fixture.root / f"unpack-{fixture.counter}").exists()
    print("real FIFO race rejected")


def zip_contract(fixture):
    invalid_paths = ("../outside", "/absolute", "backend/../../outside", "C:/outside", "\\\\server\\share", "backend\\escape",
                     "backend/file:stream", "backend/./file", "backend//file", "backend/nul.txt", "backend/COM¹.py", "backend/file.",
                     "backend/file ", "backend/bad?.py", "backend/control\x01.py", "backend/nul\x00hidden", "docs/e\u0301.md")
    for name in invalid_paths:
        fixture.bad_archive(extra=[(fixture.prefix + name, b"bad")])
    fixture.bad_archive(extra=[("another-root/Dockerfile", b"bad")])
    for name in ("Dockerfile", "BACKEND/extra.py", "backend", "backend/example.py/child", "docker/ENTRYPOINT.SH"):
        fixture.bad_archive("docker_source_path_collision", extra=[(fixture.prefix + name, b"bad")])
    for name in (".env", ".env.local", ".git/config", ".local/updates/request.json", ".narrafork/private.json", "backend/data/a.json",
                 "backend/saas.db", "worker/auth_cookie.json", "worker/redeem_codes.json", "worker/logs/app.log", "config/saas.env",
                 "backend/a.pyc", "deploy/private.key", "backend/.xianyu-release.json", "worker/卡密.txt"):
        fixture.bad_archive("docker_source_private_path", extra=[(fixture.prefix + name, b"bad")])
    for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, 0):
        def special(info, kind=kind):
            if info.filename.endswith("example.py"):
                info.external_attr = (kind | 0o644) << 16
        fixture.bad_archive("docker_source_member_invalid", configure=special)

    def hardlink_extra(info):
        if info.filename.endswith("example.py"):
            # ASi Unix link metadata must not be accepted, even for S_IFREG.
            value = struct.pack("<IHHHI", 0, stat.S_IFREG | 0o644, 1, 1, 1) + b"../../outside"
            info.extra = struct.pack("<HH", 0x756E, len(value)) + value

    fixture.bad_archive("docker_source_member_invalid", configure=hardlink_extra)
    fixture.bad_archive("docker_source_member_invalid", configure=lambda info: setattr(info, "create_system", 0))
    fixture.bad_archive("docker_source_member_invalid", configure=lambda info: setattr(info, "external_attr", info.external_attr | (0o4000 << 16)))
    fixture.bad_archive("docker_source_member_invalid", configure=lambda info: setattr(info, "compress_type", zipfile.ZIP_BZIP2))
    fixture.bad_archive("docker_source_too_large", extra=[(fixture.prefix + "frontend/bomb.txt", b"0" * (2 * 1024 * 1024))])
    with patch.object(PROTOCOL, "MAX_FILE_BYTES", 8):
        fixture.bad_archive("docker_source_too_large")
    with patch.object(PROTOCOL, "MAX_UNPACKED_BYTES", 32):
        fixture.bad_archive("docker_source_too_large")
    with patch.object(PROTOCOL, "MAX_ARCHIVE_MEMBERS", 2):
        fixture.bad_archive("docker_source_archive_invalid")
    # Truncation and central/local name divergence are independently rejected after signing.
    for kind in ("truncated", "local-name", "local-extra", "trailing", "multidisk", "encrypted", "crc"):
        archive = fixture.archive()
        data = bytearray(archive.read_bytes())
        if kind == "truncated":
            data = data[:-7]
        elif kind == "local-name":
            data[30] ^= 1
        elif kind == "local-extra":
            struct.pack_into("<H", data, 28, 1)
        elif kind == "trailing":
            data += b"unrecognized trailing records"
        elif kind == "multidisk":
            struct.pack_into("<H", data, len(data) - 18, 1)
        elif kind == "encrypted":
            struct.pack_into("<H", data, 6, 1)
        else:
            start = 30 + struct.unpack_from("<H", data, 26)[0]
            data[start] ^= 0x80
        archive.write_bytes(data)
        destination = fixture.root / f"structural-{fixture.counter}"
        reject(None, lambda: fixture.extract(archive, destination=destination))
        assert not destination.exists()
    print("docker source: path/case collisions, runtime files, link metadata, special files and ZIP bombs passed")


def central_directory_contract(fixture):
    archive = fixture.archive()
    original = archive.read_bytes()
    end = list(struct.unpack("<4s4H2LH", original[-22:]))

    class FixedHeaderReader(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 46, "central directory preflight allocated a variable-sized buffer"
            return super().read(size)

    assert PROTOCOL._zip_directory(FixedHeaderReader(original), len(original)) == (end[4], end[6])
    undercount = bytearray(original)
    struct.pack_into("<HH", undercount, len(undercount) - 14, 1, 1)
    truncated_name = bytearray(original)
    struct.pack_into("<H", truncated_name, end[6] + 28, 0xFFFF)
    extra_record = bytearray(original)
    struct.pack_into("<H", extra_record, end[6] + 30, 1)
    zip64_end = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, end[4], end[4], end[5], end[6])
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(original) - 22, 1)
    forged_end = list(end)
    forged_end[3] = forged_end[4] = 1
    forged_end[5] += len(zip64_end) + len(locator)
    zip64_archive = original[:-22] + zip64_end + locator + struct.pack("<4s4H2LH", *forged_end)
    no_locator_end = list(end)
    no_locator_end[5] += len(zip64_end)
    zip64_record_only = original[:-22] + zip64_end + struct.pack("<4s4H2LH", *no_locator_end)
    candidates = ((undercount, None), (undercount, 4), (truncated_name, None),
                  (extra_record, None), (zip64_archive, None), (zip64_record_only, None))
    for payload, limit in candidates:
        archive = fixture.archive()
        archive.write_bytes(payload)
        manifest = fixture.verify(fixture.descriptor(archive))
        destination = fixture.root / f"directory-allocation-{fixture.counter}"
        with contextlib.ExitStack() as stack:
            if limit is not None:
                stack.enter_context(patch.object(PROTOCOL, "MAX_ARCHIVE_MEMBERS", limit))
            reader = stack.enter_context(patch.object(zipfile, "ZipFile", wraps=zipfile.ZipFile))
            infos = stack.enter_context(patch.object(zipfile, "ZipInfo", side_effect=AssertionError("untrusted directory reached ZipInfo allocation")))
            reject(None, lambda: fixture.extract(archive, manifest, destination))
            reader.assert_not_called()
            infos.assert_not_called()
        assert not destination.exists()
    print("docker source: actual central count, record boundaries and ZIP64 rejected before ZipInfo allocation")


def identity_contract(fixture):
    fixture.bad_archive("docker_source_version_mismatch", changes={"backend/version.py": b'VERSION = "1.0.0"\n'})
    fixture.bad_archive("docker_source_version_mismatch", changes={"backend/version.py": f'"""VERSION = "{VERSION}"\n"""\nVERSION = "1.0.0"\n'.encode()})
    fixture.bad_archive("docker_source_version_mismatch", changes={"backend/version.py": f'VERSION = "{VERSION}"\nVERSION = "1.0.0"\n'.encode()})
    fixture.bad_archive("docker_source_version_mismatch", changes={"backend/version.py": b'VERSION = get_version()\n'})
    for name in ("package.json", "package-lock.json", "backend/build-info.json"):
        fixture.bad_archive("docker_source_version_mismatch", changes={name: encoded({"version": "1.0.0"})})
    fixture.bad_archive("docker_source_version_mismatch", changes={"package-lock.json": encoded({"version": VERSION, "packages": {"": {"version": "wrong"}}})})
    for field, bad in (("commit", "cd" * 20), ("commit", COMMIT[:7]), ("dirty", True), ("dirty", None), ("dirty", 0)):
        value = {"version": VERSION, "commit": COMMIT, "dirty": False}
        value[field] = bad
        fixture.bad_archive("docker_source_commit_mismatch", changes={"backend/build-info.json": encoded(value)})
    for name in ("Dockerfile", "docker/entrypoint.sh", "backend/build-info.json", "package.json", "package-lock.json", "backend/version.py"):
        fixture.bad_archive("docker_source_identity_invalid", changes={name: None})
    fixture.bad_archive("docker_source_private_path", changes={"worker/products_config.json": encoded({"types": [{"id": "business-data"}]})})
    print("docker source: literal-only version, package lock, build commit and required Docker inputs passed")


def main():
    with tempfile.TemporaryDirectory(prefix="xianyu-docker-source-contract-") as directory:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            fixture = Fixture(Path(directory) / "fixture")
            signature_contract(fixture)
            source_contract(fixture)
            open_race_contract(fixture)
            zip_contract(fixture)
            central_directory_contract(fixture)
            identity_contract(fixture)
    print("docker update protocol contract: ok (portable; temporary source and signing keys only)")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fifo-race-child":
        fifo_race_child(sys.argv[2])
    else:
        main()

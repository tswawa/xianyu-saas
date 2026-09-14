#!/usr/bin/env python3
"""Offline contracts for the fixed GitHub systemd bootstrap script."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "install.sh"
PUBLIC_KEY = ROOT / "deploy" / "update-signing.pub"
VERSION = "9.8.7"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
sys.dont_write_bytecode = True


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def static_contracts() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    bash = shutil.which("bash")
    if bash:
        result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr

    assert 'REPOSITORY="tswawa/xianyu-saas"' in source
    assert 'GITHUB_ROOT="https://github.com/${REPOSITORY}"' in source
    assert "releases/latest/download" in source
    assert "releases/download/v${release_version}" in source
    assert "api.github.com" not in source and "http://" not in source
    assert "eval" not in source and "wget" not in source and "git clone" not in source
    assert "XIANYU_INSTALL_TEST" not in source
    assert not re.search(r"XIANYU_INSTALL_(?:URL|REPOSITORY|REPO|HOST)", source)
    assert len(re.findall(r"^    curl ", source, re.MULTILINE)) == 1
    assert 'fetch "${release_root}/artifacts.json"' in source
    assert 'fetch "${release_root}/artifacts.json.sig"' in source
    assert 'manager_url="${GITHUB_ROOT}/releases/download/v${release_version}/${manager_name}"' in source
    assert "DEBIAN_FRONTEND=noninteractive apt-get update" in source
    assert "apt-get install --yes --no-install-recommends" in source
    for package in ("ca-certificates", "curl", "jq", "openssl"):
        assert package in source
    for distribution in ("ubuntu:22.04", "ubuntu:24.04", "debian:12"):
        assert distribution in source
    assert "x86_64|amd64" in source and "aarch64|arm64" in source
    assert "openssl base64 -d -A" in source
    assert "openssl pkeyutl -verify -pubin" in source and "-rawin" in source
    assert '.schema != 2' in source and '.manager_protocol != 1' in source
    assert '.kind == "bootstrap-manager"' in source and '.target == $target' in source
    assert '($matches | length) != 1' in source
    assert 'expected_name="${PRODUCT}-${release_version}-linux-${architecture}"' in source
    assert "MAX_MANAGER_BYTES" in source and "manager_sha256_mismatch" in source
    assert "chmod 0755" in source
    assert 'env -i PATH="$SYSTEM_PATH" LANG=C.UTF-8 LC_ALL=C.UTF-8' in source
    assert 'manager_arguments=(install)' in source
    assert "trap cleanup EXIT HUP INT TERM" in source
    assert PUBLIC_KEY.read_text(encoding="ascii").strip() in source
    print("systemd bootstrap: static fixed-network, trust and CLI boundaries passed")


def fixture_index(architecture: str, manager: bytes, *, bad_hash: bool = False, duplicate: bool = False) -> bytes:
    expected_hash = "0" * 64 if bad_hash else hashlib.sha256(manager).hexdigest()
    selected = {
        "name": f"xianyu-saas-{VERSION}-linux-{architecture}",
        "size": len(manager),
        "sha256": expected_hash,
        "kind": "bootstrap-manager",
        "target": f"linux-{architecture}",
        "manager_protocol": 1,
    }
    other_architecture = "aarch64" if architecture == "x86_64" else "x86_64"
    other = {
        "name": f"xianyu-saas-{VERSION}-linux-{other_architecture}",
        "size": 17,
        "sha256": hashlib.sha256(b"unselected-manager").hexdigest(),
        "kind": "bootstrap-manager",
        "target": f"linux-{other_architecture}",
        "manager_protocol": 1,
    }
    files = [selected, other]
    if duplicate:
        files.append(dict(selected))
    return (json.dumps({
        "schema": 2,
        "version": VERSION,
        "commit": "1" * 40,
        "manager_protocol": 1,
        "files": files,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def prepare_case(root: Path, architecture: str, *, bad_hash: bool = False,
                 bad_signature: bool = False, duplicate: bool = False) -> tuple[dict[str, str], Path, Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    fixture = root / "fixture"
    tools = root / "tools"
    temporary = root / "tmp"
    fixture.mkdir(parents=True)
    tools.mkdir()
    temporary.mkdir()
    manager_log = fixture / "manager.log"
    curl_log = fixture / "curl.log"

    manager = (
        "#!/bin/sh\n"
        f"printf 'PATH=%s\\nLANG=%s\\nLC_ALL=%s\\n' \"$PATH\" \"$LANG\" \"$LC_ALL\" > '{manager_log}'\n"
        f"for argument in \"$@\"; do printf 'ARG=%s\\n' \"$argument\" >> '{manager_log}'; done\n"
    ).encode("utf-8")
    index = fixture_index(architecture, manager, bad_hash=bad_hash, duplicate=duplicate)
    private_key = Ed25519PrivateKey.generate()
    signature = bytearray(private_key.sign(index))
    if bad_signature:
        signature[0] ^= 1
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    (fixture / "artifacts.json").write_bytes(index)
    (fixture / "artifacts.json.sig").write_bytes(base64.b64encode(signature))
    (fixture / "manager").write_bytes(manager)
    (fixture / "architecture").write_text(architecture, encoding="ascii")
    os_release = fixture / "os-release"
    os_release.write_text('ID="ubuntu"\nVERSION_ID="24.04"\n', encoding="ascii")

    write_executable(tools / "id", "#!/bin/sh\nprintf '0\\n'\n")
    write_executable(tools / "uname", "#!/bin/sh\n/bin/cat \"$XIANYU_INSTALL_FIXTURE_DIR/architecture\"\n")
    write_executable(tools / "dpkg-query", "#!/bin/sh\nprintf 'install ok installed\\n'\n")
    write_executable(tools / "apt-get", "#!/bin/sh\nprintf 'unexpected apt-get\\n' >&2\nexit 97\n")
    write_executable(tools / "curl", """#!/bin/sh
output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        --connect-timeout|--max-time|--retry|--max-filesize|--proto|--proto-redir) shift 2 ;;
        --fail|--show-error|--silent|--location|--tlsv1.2) shift ;;
        *) url="$1"; shift ;;
    esac
done
printf '%s\n' "$url" >> "$XIANYU_INSTALL_FIXTURE_DIR/curl.log"
case "$url" in
    https://github.com/tswawa/xianyu-saas/releases/latest/download/artifacts.json)
        /bin/cp "$XIANYU_INSTALL_FIXTURE_DIR/artifacts.json" "$output" ;;
    https://github.com/tswawa/xianyu-saas/releases/latest/download/artifacts.json.sig)
        /bin/cp "$XIANYU_INSTALL_FIXTURE_DIR/artifacts.json.sig" "$output" ;;
    https://github.com/tswawa/xianyu-saas/releases/download/v9.8.7/xianyu-saas-9.8.7-linux-x86_64|https://github.com/tswawa/xianyu-saas/releases/download/v9.8.7/xianyu-saas-9.8.7-linux-aarch64)
        /bin/cp "$XIANYU_INSTALL_FIXTURE_DIR/manager" "$output" ;;
    *) printf 'unexpected URL: %s\n' "$url" >&2; exit 96 ;;
esac
""")

    source = SCRIPT.read_text(encoding="utf-8")
    source = source.replace(
        f'readonly SYSTEM_PATH="{SYSTEM_PATH}"',
        f'readonly SYSTEM_PATH="{tools}:{SYSTEM_PATH}"',
        1,
    )
    source = source.replace('OS_RELEASE_FILE="/etc/os-release"', f'OS_RELEASE_FILE="{os_release}"', 1)
    source = source.replace(PUBLIC_KEY.read_text(encoding="ascii").strip(), public_pem.decode("ascii").strip(), 1)
    test_script = root / "install.sh"
    write_executable(test_script, source)

    environment = os.environ.copy()
    environment.update({
        "XIANYU_INSTALL_FIXTURE_DIR": str(fixture),
        "TMPDIR": str(temporary),
    })
    return environment, test_script, manager_log, curl_log


def invoke(root: Path, architecture: str, arguments: tuple[str, ...] = (), **fixture_options):
    environment, test_script, manager_log, curl_log = prepare_case(root, architecture, **fixture_options)
    result = subprocess.run(
        [shutil.which("bash") or "bash", str(test_script), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not list((root / "tmp").iterdir()), "bootstrap temporary directory escaped trap cleanup"
    return result, manager_log, curl_log


def dynamic_contracts() -> None:
    if os.name != "posix":
        print("systemd bootstrap: native Linux dynamic contracts skipped")
        return
    missing = [name for name in ("bash", "jq", "openssl") if shutil.which(name) is None]
    assert not missing, f"Linux contract dependencies missing: {missing}"

    with tempfile.TemporaryDirectory(prefix="xianyu-bootstrap-contract-") as directory:
        run = Path(directory)
        for architecture in ("x86_64", "aarch64"):
            arguments = () if architecture == "x86_64" else ("--version", VERSION)
            result, manager_log, curl_log = invoke(run / architecture, architecture, arguments)
            assert result.returncode == 0, result.stderr
            manager_lines = manager_log.read_text(encoding="utf-8").splitlines()
            expected_arguments = ["ARG=install"]
            if arguments:
                expected_arguments += ["ARG=--version", f"ARG={VERSION}"]
            assert [line for line in manager_lines if line.startswith("ARG=")] == expected_arguments
            assert f"PATH={run / architecture / 'tools'}:{SYSTEM_PATH}" in manager_lines
            assert "LANG=C.UTF-8" in manager_lines and "LC_ALL=C.UTF-8" in manager_lines
            urls = curl_log.read_text(encoding="utf-8").splitlines()
            assert len(urls) == 3 and all(url.startswith("https://github.com/tswawa/xianyu-saas/") for url in urls)
            assert urls[-1].endswith(f"xianyu-saas-{VERSION}-linux-{architecture}")

        result, manager_log, _ = invoke(run / "signature", "x86_64", bad_signature=True)
        assert result.returncode != 0 and "index_signature_invalid" in result.stderr
        assert not manager_log.exists()

        result, manager_log, _ = invoke(run / "hash", "x86_64", bad_hash=True)
        assert result.returncode != 0 and "manager_sha256_mismatch" in result.stderr
        assert not manager_log.exists()

        result, manager_log, _ = invoke(run / "duplicate", "aarch64", duplicate=True)
        assert result.returncode != 0 and "index_invalid" in result.stderr
        assert not manager_log.exists()

        rejection_root = run / "arguments"
        environment, test_script, manager_log, curl_log = prepare_case(rejection_root, "x86_64")
        for arguments in (
            ("https://attacker.invalid/install",),
            ("--version", "9.8.7", "status"),
            ("--version", "9.8.7/../../escape"),
            ("--repository", "attacker/repo"),
        ):
            result = subprocess.run(
                [shutil.which("bash") or "bash", str(test_script), *arguments],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=20,
            )
            assert result.returncode != 0, arguments
        assert not manager_log.exists() and not curl_log.exists()

    print("systemd bootstrap: x86_64/aarch64, signature, hash and argument contracts passed")


def main() -> None:
    static_contracts()
    dynamic_contracts()
    print("systemd bootstrap contract: passed")


if __name__ == "__main__":
    main()

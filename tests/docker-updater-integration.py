#!/usr/bin/env python3
"""Opt-in isolated Docker acceptance; the default only performs offline checks.

Native Windows example (an ALREADY RUNNING, explicitly isolated context):
  python tests/docker-updater-integration.py --run --context updates-ci --acknowledge-isolated-engine
Remote dind additionally passes a fresh runner/daemon shared path:
  --host-bind-root /test-host-bind
No Docker Desktop startup, existing app access, repository data copy, platform
request or model request occurs. All fixture application code below is synthetic.
"""
from __future__ import annotations

import argparse
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
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from docker_engine import COMPOSE_VERSION, compose_escape, compose_literal
from docker_update_protocol import docker_asset_names, extract_verified_source, verify_docker_manifest

RUN_LABEL = "io.xianyu.updater.acceptance"
ROLE = "io.xianyu.updates.role"
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
COMMIT = "b" * 40
CONTAINER_ROOT = PurePosixPath("/")
UPDATE_ROOT = (CONTAINER_ROOT / "updates").as_posix()
UPDATER_PRIVATE = (CONTAINER_ROOT / "var" / "lib" / "xianyu-updater").as_posix()
DOCKER_SOCKET = (CONTAINER_ROOT / "var" / "run" / "docker.sock").as_posix()
UPDATER_WORKDIR = (CONTAINER_ROOT / "opt" / "updater").as_posix()
SIGNING_KEY = (CONTAINER_ROOT / "app" / "update-signing.pub").as_posix()
CANDIDATE_BASE_IMAGE = "python:3.12.11-slim-bookworm"
CANDIDATE_BASE_REF = "docker.io/library/" + CANDIDATE_BASE_IMAGE
BOOTSTRAP_BASE_REF = "docker.io/library/python:3.12-slim-bookworm"
BUILDX_DU_COMMAND = ("/usr/local/bin/docker", "buildx", "du", "--builder", "default", "--verbose")
INTERRUPTION_MARKER = "acceptance-interruption.once"
# Test-only entrypoint: retain the real updater, but hold one explicitly armed
# durable boundary. An ordinary restart has a grace period and can otherwise
# arrive after the update commits, which is correctly recovered as succeeded.
FIXTURE_UPDATER = '''import threading
import docker_updater
original_phase = docker_updater.Updater._phase
def checkpoint_phase(self, journal, phase):
    marker = self.config.private / MARKER
    armed = phase == "switching" and marker.is_file()
    if armed:
        marker.unlink()
    original_phase(self, journal, phase)
    if armed:
        threading.Event().wait()
docker_updater.Updater._phase = checkpoint_phase
raise SystemExit(docker_updater.main())
'''.replace("MARKER", repr(INTERRUPTION_MARKER))

FIXTURE_DB = '''import sqlite3
class DB:
    def __init__(self, path):
        self.con=sqlite3.connect(path)
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,value TEXT)')
        self.con.execute('CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,value TEXT)')
        self.con.execute('CREATE TABLE IF NOT EXISTS shop_config(id INTEGER PRIMARY KEY,value TEXT)')
        self.con.execute('CREATE TABLE IF NOT EXISTS credentials(id INTEGER PRIMARY KEY,value BLOB)')
        self.con.commit()
    def is_ready(self):
        return self.con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
'''

FIXTURE_MAINTENANCE = '''MAINTENANCE_PROTOCOL = 1
import json
from pathlib import Path
def read_maintenance():
    path=Path('/updates/status/maintenance.json')
    if not path.exists(): return {'active':False,'operation_id':''}
    return json.loads(path.read_text())
def maintenance_active():
    return read_maintenance()['active'] is True
'''

FIXTURE_APP = '''import base64,json,os,sqlite3,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from cryptography.fernet import Fernet
from db import DB
from update_maintenance import read_maintenance
import version
root=Path('/data/custom')
root.mkdir(parents=True,exist_ok=True)
database=DB(str(root/'control.db'))
keyfile=root/'synthetic.key'
if not keyfile.exists(): keyfile.write_bytes(Fernet.generate_key())
f=Fernet(keyfile.read_bytes())
for table in ('orders','accounts','shop_config'):
    database.con.execute('INSERT OR IGNORE INTO '+table+' VALUES(1,?)',('synthetic-'+table,))
if not database.con.execute('SELECT 1 FROM credentials').fetchone():
    database.con.execute('INSERT INTO credentials VALUES(1,?)',(f.encrypt(b'synthetic-private-credential'),))
if FAIL_READY:
    database.con.execute('INSERT OR IGNORE INTO orders VALUES(2,?)',('write-after-new-start',))
database.con.commit()
database.con.close()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        path=urlsplit(self.path)
        payload={}
        status=200
        if path.path=='/health': payload={'ok':True}
        elif path.path=='/api/ready':
            status=503 if FAIL_READY else 200
            payload={'ok':not FAIL_READY,'database':'ready' if not FAIL_READY else 'failed'}
        elif path.path=='/api/version/public': payload={'version':version.VERSION,'asset_version':version.ASSET_VERSION}
        elif path.path=='/internal/v1/update/drain':
            opid=parse_qs(path.query).get('operation_id',[''])[0]
            try: state=read_maintenance()
            except (OSError,ValueError): state={}
            active=state.get('active') is True and state.get('operation_id')==opid
            payload={'schema':1,'operation_id':opid,'active':active,'active_jobs':0,'active_workers':0,'ready':active,'error_code':''}
        elif path.path=='/internal/test/state':
            db=sqlite3.connect(root/'control.db')
            payload={table:db.execute('SELECT value FROM '+table+' WHERE id=1').fetchone()[0] for table in ('orders','accounts','shop_config')}
            payload['credential']=f.decrypt(db.execute('SELECT value FROM credentials WHERE id=1').fetchone()[0]).decode()
            payload['rows']=db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            marker=db.execute('SELECT value FROM orders WHERE id=2').fetchone()
            payload['write_marker']=marker[0] if marker else ''
            db.close()
        elif path.path.startswith('/xianyu-saas/'):
            relative=path.path[len('/xianyu-saas/'): ] or 'index.html'
            if relative not in ('index.html','assets/app.js','assets/app.css'):
                status=404;payload={'error':'not-found'}
            else:
                content=(Path('/app/frontend')/relative).read_bytes()
                self.send_response(200);self.end_headers();self.wfile.write(content);return
        else: status=404
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
threading.Thread(target=ThreadingHTTPServer(('0.0.0.0',4173),Handler).serve_forever,daemon=True).start()
ThreadingHTTPServer(('0.0.0.0',8096),Handler).serve_forever()
'''


def fixture_files(version, token, *, fail=False):
    asset = "synthetic-" + version
    dockerfile = '''FROM python:3.12-slim-bookworm AS runtime
ARG SAAS_BUILD_COMMIT
ARG SAAS_BUILD_DIRTY
RUN pip install --no-cache-dir cryptography==46.0.1 && groupadd -g 10001 xianyu && useradd -u 10001 -g 10001 xianyu
WORKDIR /app
COPY backend /app/backend
COPY frontend /app/frontend
COPY docker /app/docker
RUN mkdir -p /app/backend/.venv/bin /data && ln -s /usr/local/bin/python /app/backend/.venv/bin/python && chmod +x /app/docker/entrypoint.sh && chown 10001:10001 /data
ENV SAAS_DB=/data/custom/control.db SAAS_TENANTS_DIR=/data/tenants SAAS_DOCKER_UPDATE_ROOT=/updates PYTHONDONTWRITEBYTECODE=1
LABEL io.xianyu.updater.acceptance="TOKEN"
VOLUME ["/data"]
EXPOSE 4173 8096
USER 10001:10001
HEALTHCHECK --interval=1s --timeout=2s --start-period=1s --retries=2 CMD /usr/local/bin/python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8096/health',timeout=1)"
ENTRYPOINT ["/app/docker/entrypoint.sh"]
'''.replace("TOKEN", token)
    if version != "1.0.0":
        # Different from the bootstrap image: an empty isolated Engine/BuildKit
        # cache must obtain this public base via the helper's auth-provider path.
        dockerfile = dockerfile.replace("python:3.12-slim-bookworm", CANDIDATE_BASE_IMAGE)
    return {
        "Dockerfile": dockerfile.encode(),
        "docker/entrypoint.sh": b"#!/bin/sh\nexec /usr/local/bin/python /app/backend/app.py\n",
        "backend/db.py": FIXTURE_DB.encode(),
        "backend/update_maintenance.py": FIXTURE_MAINTENANCE.encode(),
        "backend/app.py": ("FAIL_READY=" + repr(fail) + "\n" + FIXTURE_APP).encode(),
        "backend/version.py": ("VERSION = " + repr(version) + "\nASSET_VERSION = " + repr(asset) + "\nUPDATE_DATA_VERSION = 1\ndef version_payload():\n    return {'version':VERSION,'commit':" + repr(COMMIT) + ",'asset_version':ASSET_VERSION}\n").encode(),
        "backend/build-info.json": json.dumps({"version": version, "commit": COMMIT, "dirty": False}).encode(),
        "package.json": json.dumps({"version": version}).encode(),
        "package-lock.json": json.dumps({"version": version, "packages": {"": {"version": version}}}).encode(),
        "frontend/index.html": ('<link rel="stylesheet" href="/xianyu-saas/assets/app.css?v=' + asset + '"><script src="/xianyu-saas/assets/app.js?v=' + asset + '"></script>').encode(),
        "frontend/assets/app.js": ("window.fixtureVersion=" + repr(version) + ";\n").encode(),
        "frontend/assets/app.css": b"body{color:#123456}\n",
    }


def write_tree(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def bundle(root, version, key, token, *, fail=False):
    root.mkdir(parents=True, exist_ok=True)
    files = fixture_files(version, token, fail=fail)
    with zipfile.ZipFile(root / "source.zip", "w") as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo("xianyu-saas-" + version + "/" + name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
            archive.writestr(info, raw)
    raw_zip = (root / "source.zip").read_bytes()
    manifest = {"schema": 1, "protocol": 1, "version": version, "commit": COMMIT, "source": {"name": docker_asset_names(version)[0], "size": len(raw_zip), "sha256": hashlib.sha256(raw_zip).hexdigest()}, "runtime_manifest_sha256": hashlib.sha256(b"synthetic-ota-manifest").hexdigest()}
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "docker.manifest.json").write_bytes(raw)
    (root / "docker.manifest.sig").write_bytes(base64.b64encode(key.sign(raw)))
    return hashlib.sha256(raw).hexdigest()


class Docker:
    def __init__(self, context):
        self.prefix = ["docker", "--context", context]

    def run(self, *args, timeout=600, allow_failure=False, input_bytes=None, return_result=False):
        environment = os.environ.copy()
        # Native Git Bash must not turn container paths or namespace args into drive paths.
        environment["MSYS_NO_PATHCONV"] = "1"
        environment["MSYS2_ARG_CONV_EXCL"] = "*"
        result = subprocess.run([*self.prefix, *map(str, args)], env=environment, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode and not allow_failure:
            # Suppress argv/output because future fixtures might contain test secrets.
            raise RuntimeError("isolated Docker command failed: " + args[0])
        if return_result:
            return result
        return result.stdout.decode("utf-8", errors="replace").strip()

    def inspect(self, name):
        return json.loads(self.run("inspect", name))[0]

    def execute(self, container, code, *, user="0:0"):
        return self.run("exec", "--user", user, container, "/usr/local/bin/python", "-c", code)


def parse_buildx_du(raw):
    records = []
    current = {}
    for line in [*raw.splitlines(), ""]:
        if not line.strip():
            if current.get("id"):
                if not {"id", "description", "type"} <= set(current):
                    raise AssertionError("Buildx cache record is incomplete")
                records.append(current)
            current = {}
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip().lower().replace(" ", "_")
        if key in current:
            raise AssertionError("Buildx cache record contains a duplicate field")
        current[key] = value.strip()
    return records


def base_cache_records(records, reference):
    pattern = re.compile(re.escape(reference) + r"@(sha256:[0-9a-f]{64})(?:\s|$)")
    result = []
    for record in records:
        match = pattern.search(record["description"])
        if match:
            result.append({"id": record["id"], "digest": match.group(1), "type": record["type"]})
    return result


def helper_buildx_du(docker, helper):
    code = """import base64,subprocess
from docker_engine import DockerEngine
result=subprocess.run(COMMAND,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=DockerEngine._cli_environment(),timeout=60,check=False)
if result.returncode: raise SystemExit(2)
print(base64.b64encode(result.stdout).decode('ascii'))
""".replace("COMMAND", repr(list(BUILDX_DU_COMMAND)))
    try:
        return base64.b64decode(docker.execute(helper, code).strip(), validate=True).decode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise AssertionError("helper returned invalid Buildx cache output") from error


def buildx_cache_snapshot(docker, helper):
    records = parse_buildx_du(helper_buildx_du(docker, helper))
    if not records:
        raise AssertionError("default Buildx builder returned no structured cache records")
    return {
        "ids": {record["id"] for record in records},
        "candidate": base_cache_records(records, CANDIDATE_BASE_REF),
        "bootstrap": base_cache_records(records, BOOTSTRAP_BASE_REF),
    }


def image_exists(docker, reference):
    return docker.run("image", "inspect", reference, allow_failure=True, return_result=True).returncode == 0


def host_bind_task(docker, *, name, project, token, image, source, docker_root, code):
    container = docker.run(
        "create", "--name", name,
        "--label", PROJECT + "=" + project, "--label", ROLE + "=task", "--label", RUN_LABEL + "=" + token,
        "--network", "none", "--read-only", "--user", "0:0",
        "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
        "--mount", "type=bind,source=" + source + ",target=/fixture,bind-propagation=rprivate",
        "--entrypoint", "/usr/local/bin/python", image, "-I", "-c", code,
    )
    try:
        row = docker.inspect(container)
        labels = row["Config"].get("Labels") or {}
        assert labels.get(PROJECT) == project and labels.get(ROLE) == "task" and labels.get(RUN_LABEL) == token
        assert row["HostConfig"]["NetworkMode"] == "none"
        mounts = [mount for mount in row["Mounts"] if mount["Destination"] == "/fixture"]
        assert len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["RW"] is True
        assert mounts[0].get("Propagation") == "rprivate"
        assert all(mount["Destination"] != DOCKER_SOCKET for mount in row["Mounts"])
        actual_source = mounts[0]["Source"]
        actual = PurePosixPath(actual_source)
        daemon_root = PurePosixPath(docker_root)
        assert actual.is_absolute() and actual != daemon_root and not actual.is_relative_to(daemon_root), "host bind source must be outside DockerRootDir"
        docker.run("start", container)
        if docker.run("wait", container).strip() != "0":
            raise AssertionError("isolated host-bind task failed")
        return actual_source
    finally:
        docker.run("rm", "-f", container, allow_failure=True)


def prepare_host_bind(docker, *, directory, source, project, token, image, docker_root, public_key):
    runner_marker = directory / ".runner-marker"
    daemon_marker = directory / ".daemon-marker"
    runner_marker.write_text(token, encoding="utf-8")
    encoded_key = base64.b64encode(public_key).decode("ascii")
    code = """import base64,os,stat
from pathlib import Path
root=Path('/fixture')
assert {path.name for path in root.iterdir()}=={'.runner-marker'}
assert (root/'.runner-marker').read_text(encoding='utf-8')==TOKEN
key=root/'key'
data=root/'data'
key.mkdir(mode=0o700)
data.mkdir(mode=0o700)
trusted=key/'update-signing.pub'
trusted.write_bytes(base64.b64decode(KEY))
os.chown(key,0,0)
os.chown(trusted,0,0)
os.chmod(trusted,0o444)
os.chown(data,10001,10001)
os.chmod(data,0o700)
assert trusted.stat().st_uid==0 and stat.S_IMODE(trusted.stat().st_mode)==0o444
assert data.stat().st_uid==10001 and stat.S_IMODE(data.stat().st_mode)==0o700
marker=root/'.daemon-marker'
marker.write_text(TOKEN,encoding='utf-8')
os.chown(marker,0,0)
os.chmod(marker,0o444)
""".replace("TOKEN", repr(token)).replace("KEY", repr(encoded_key))
    actual_source = host_bind_task(
        docker, name=project + "-host-bind-prepare", project=project, token=token,
        image=image, source=source, docker_root=docker_root, code=code,
    )
    if PurePosixPath(actual_source).name != directory.name or daemon_marker.read_text(encoding="utf-8") != token:
        raise AssertionError("runner and daemon do not share the exact requested host-bind directory")
    return actual_source


def cleanup_host_bind(docker, *, directory, source, project, token, image, docker_root, prepared):
    if not directory.exists():
        return
    code = """import os,shutil
from pathlib import Path
root=Path('/fixture')
assert (root/'.runner-marker').read_text(encoding='utf-8')==TOKEN
if PREPARED: assert (root/'.daemon-marker').read_text(encoding='utf-8')==TOKEN
for path in list(root.iterdir()):
    if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
    else: path.unlink()
os.chmod(root,0o700)
""".replace("TOKEN", repr(token)).replace("PREPARED", repr(prepared))
    host_bind_task(
        docker, name=project + "-host-bind-cleanup", project=project, token=token,
        image=image, source=source, docker_root=docker_root, code=code,
    )
    if any(directory.iterdir()):
        raise AssertionError("host-bind fixture cleanup left unexpected files")
    directory.rmdir()


def assert_host_bind(docker, container, destination, source):
    mounts = [mount for mount in docker.inspect(container)["Mounts"] if mount["Destination"] == destination]
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Type"] == "bind" and mount["Source"] == source and mount.get("Propagation") == "rprivate"


def compose_spec(project, token, data_source, kind, key_source="/engine/trusted.pub"):
    labels = {RUN_LABEL: token}
    return {
        "name": project,
        "services": {
            "xianyu-saas": {
                "image": project + "-xianyu-saas:managed", "pull_policy": "never", "container_name": project + "-app", "restart": "unless-stopped",
                "labels": {ROLE: "app", RUN_LABEL: token},
                "environment": {"SAAS_DB": "/data/custom/control.db", "SAAS_TENANTS_DIR": "/data/tenants", "SAAS_DOCKER_UPDATE_ROOT": UPDATE_ROOT, "SAAS_DOCKER_DEPLOYMENT_ID": project + ":xianyu-saas", "SAAS_UPDATE_PUBLIC_KEY_FILE": SIGNING_KEY, "LOCAL_OVERRIDE": "synthetic-preserve", "SAAS_AI_MASTER_KEY": "synthetic-not-a-real-key", "LITERAL_ENV": "$VALUE $$ quote=\" 空格 中文\nnext"},
                "volumes": [{"type": kind, "source": data_source, "target": "/data"}, {"type": "volume", "source": "ipc", "target": UPDATE_ROOT}, {"type": "bind", "source": key_source, "target": SIGNING_KEY, "read_only": True, "bind": {"create_host_path": False}}],
                "ports": ["127.0.0.1::8096"],
                "networks": {"default": {"aliases": ["preserved-local-alias"]}, "extra": {"aliases": ["preserved-extra-alias"]}},
                "extra_hosts": ["fixture-host:127.0.0.2"], "mem_limit": "512m", "cpus": 1.0,
                "ulimits": {"nofile": {"soft": 2048, "hard": 4096}}, "security_opt": ["no-new-privileges:true"],
            },
            "xianyu-updater": {
                "image": project + "-xianyu-updater:local", "pull_policy": "never", "restart": "unless-stopped", "user": "0:0", "read_only": True,
                "entrypoint": ["/usr/local/bin/python", "-c", FIXTURE_UPDATER],
                "network_mode": "bridge", "labels": {ROLE: "updater", RUN_LABEL: token},
                "environment": {"SAAS_DOCKER_APP_SERVICE": "xianyu-saas", "SAAS_DOCKER_UPDATER_HEALTH_TIMEOUT": "20", "SAAS_DOCKER_UPDATE_ROOT": UPDATE_ROOT, "SAAS_DOCKER_UPDATER_STATE_DIR": UPDATER_PRIVATE, "SAAS_UPDATE_PUBLIC_KEY_FILE": SIGNING_KEY},
                "volumes": [{"type": "volume", "source": "ipc", "target": UPDATE_ROOT}, {"type": "volume", "source": "private", "target": UPDATER_PRIVATE}, {"type": "bind", "source": DOCKER_SOCKET, "target": DOCKER_SOCKET, "read_only": True}, {"type": "bind", "source": key_source, "target": SIGNING_KEY, "read_only": True, "bind": {"create_host_path": False}}],
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=64m,mode=1777"], "cap_drop": ["ALL"], "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"], "security_opt": ["no-new-privileges:true"],
            },
        },
        "volumes": {"ipc": {"labels": labels}, "private": {"labels": labels}, **({"business": {"labels": labels}} if kind == "volume" else {})},
        "networks": {"default": {"internal": True, "labels": labels}, "extra": {"internal": True, "labels": labels}},
    }


def read_json(docker, container, path):
    raw = docker.execute(container, "from pathlib import Path;p=Path(" + repr(path) + ");print(p.read_text() if p.exists() else '{}')")
    return json.loads(raw)


def wait_for(callback, predicate, *, timeout=240, description="condition"):
    deadline = time.monotonic() + timeout
    last_phase, last_error = None, None
    while time.monotonic() < deadline:
        try:
            value = callback()
            last_phase = value.get("phase") if isinstance(value, dict) else None
            last_error = None
            if predicate(value):
                return value
        except (ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
            last_error = type(error).__name__
        time.sleep(0.15)
    raise AssertionError("timed out waiting for " + description + f" (last_phase={last_phase!r}, last_error={last_error!r})")


def stage(docker, app, directory, version, digest, *, action="apply", current):
    operation_id = uuid.uuid4().hex
    target = "/tmp/acceptance-stage-" + operation_id
    docker.execute(app, "from pathlib import Path;Path(" + repr(target) + ").mkdir(mode=0o755)")
    docker.run("cp", str(directory) + "/.", app + ":" + target)
    code = "import json,time,shutil,os;from pathlib import Path;root=Path('/updates');op=" + repr(operation_id) + ";dest=root/'artifacts'/op;dest.mkdir(mode=0o700);"
    code += "[shutil.copyfile(Path(" + repr(target) + ")/name,dest/name) for name in ['source.zip','docker.manifest.json','docker.manifest.sig']];"
    request = {"schema": 1, "operation_id": operation_id, "action": action, "version": version, "expected_current_version": current, "manifest_sha256": digest, "requested_by": 1}
    code += "request=" + repr(request) + ";request['requested_at']=time.time();temporary=root/'requests'/('.'+op);temporary.write_text(json.dumps(request));os.chmod(temporary,0o600);temporary.replace(root/'requests'/(op+'.json'))"
    docker.execute(app, code, user="10001:10001")
    return operation_id


def status(docker, helper, operation_id):
    return read_json(docker, helper, "/updates/status/operations/" + operation_id + ".json")


def initialize_updater(docker, compose, helper):
    rendered = docker.run(*compose, "config", "--format", "json").encode("utf-8")
    for _ in range(100):
        result = docker.run(
            "exec", "-i", helper, "/usr/local/bin/python", (PurePosixPath(UPDATER_WORKDIR) / "docker_updater.py").as_posix(), "initialize",
            input_bytes=rendered, allow_failure=True, return_result=True,
        )
        try:
            payload = json.loads(result.stdout)
        except (UnicodeError, ValueError):
            payload = {}
        if result.returncode == 0 and payload.get("ok") is True:
            assert payload.get("deployment_id")
            assert payload.get("compose_version") == COMPOSE_VERSION
            return
        if payload.get("error_code") != "update_executor_busy":
            raise AssertionError("private Docker deployment registration failed")
        time.sleep(0.05)
    raise AssertionError("timed out registering private Docker deployment")


def state(docker, app):
    return json.loads(docker.execute(app, "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8096/internal/test/state',timeout=3).read().decode())", user="10001:10001"))


def assert_permissions(docker, helper, app):
    capabilities = (PurePosixPath(UPDATE_ROOT) / "status" / "capabilities.json").as_posix()
    forgery = (PurePosixPath(UPDATE_ROOT) / "status" / "forgery.json").as_posix()
    helper_code = (
        "import os,stat;from pathlib import Path;"
        f"r=Path({UPDATE_ROOT!r});private=Path({UPDATER_PRIVATE!r});capabilities=Path({capabilities!r});trusted=private/'trusted-update-signing.pub';"
        "assert r.stat().st_uid==0;assert (r/'status').stat().st_uid==0;"
        "assert (r/'requests').stat().st_uid==10001;assert (r/'artifacts').stat().st_uid==10001;"
        "assert stat.S_IMODE(private.stat().st_mode)==0o700;assert trusted.stat().st_uid==0;assert stat.S_IMODE(trusted.stat().st_mode)==0o600;"
        "assert stat.S_IMODE(capabilities.stat().st_mode)==0o644"
    )
    docker.execute(helper, helper_code)
    app_code = (
        "from pathlib import Path;"
        f"assert not Path({DOCKER_SOCKET!r}).exists();assert not Path({UPDATER_PRIVATE!r}).exists();p=Path({forgery!r});"
        "\ntry:p.write_text('{}')\nexcept PermissionError:pass\nelse:raise AssertionError('app can forge status')"
    )
    docker.execute(app, app_code, user="10001:10001")
    for mount in docker.inspect(app)["Mounts"]:
        assert mount["Destination"] not in {DOCKER_SOCKET, UPDATER_PRIVATE}


def cleanup(docker, project, token, additional_volumes, host_fixture=None):
    # Only random-project resources, with independently checked identity. No
    # global removal/prune, no user volume deletion, and no default-project match.
    for cid in docker.run("ps", "-aq", "--filter", "label=" + PROJECT + "=" + project).splitlines():
        row = docker.inspect(cid)
        labels = row["Config"].get("Labels") or {}
        assert labels.get(PROJECT) == project
        assert labels.get(ROLE) in {"app", "updater", "task"}
        assert row["Name"].lstrip("/").startswith(project + "-")
        assert labels.get(RUN_LABEL) == token or labels.get(ROLE) == "task"
        docker.run("rm", "-f", cid)
    if host_fixture is not None:
        cleanup_host_bind(docker, project=project, token=token, image=project + "-xianyu-updater:local", **host_fixture)
    for kind, args in (("network", ["network", "ls", "-q"]), ("volume", ["volume", "ls", "-q"])):
        ids = docker.run(*args, "--filter", "label=" + PROJECT + "=" + project).splitlines()
        if kind == "volume":
            ids += additional_volumes
        for resource in dict.fromkeys(ids):
            row = json.loads(docker.run(kind, "inspect", resource))[0]
            assert row.get("Labels", {}).get(RUN_LABEL) == token
            docker.run(kind, "rm", resource)
    for reference in docker.run("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", "--filter", "reference=" + project + "-*").splitlines():
        info = json.loads(docker.run("image", "inspect", reference))[0]
        assert info.get("Config", {}).get("Labels", {}).get(RUN_LABEL) == token
        docker.run("image", "rm", reference, allow_failure=True)


def acceptance(docker, root, token, kind, *, host_bind_root, host_bind_root_source, docker_root, smoke=False):
    project = "update-it-" + token[:12] + ("-named" if kind == "volume" else "-bind")
    work = root / project
    work.mkdir()
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    helper_context = work / "helper"
    backend = helper_context / "backend"
    backend.mkdir(parents=True)
    # Whitelisted code only: no repository config, business source tree or data.
    for name in ("docker_updater.py", "docker_engine.py", "docker_update_protocol.py", "update_maintenance.py"):
        shutil.copyfile(ROOT / "backend" / name, backend / name)
    original_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Embed the ephemeral trusted public key only in the test helper image.
    updater_workdir = "WORKDIR " + UPDATER_WORKDIR
    helper_dockerfile = original_dockerfile.replace(
        "USER 0:0\n" + updater_workdir,
        "COPY trusted.pub " + SIGNING_KEY + "\nUSER 0:0\n" + updater_workdir,
    )
    assert helper_dockerfile != original_dockerfile
    (helper_context / "Dockerfile").write_text(helper_dockerfile, encoding="utf-8")
    (helper_context / "trusted.pub").write_bytes(pub)
    initial = work / "initial"
    write_tree(initial, fixture_files("1.0.0", token))
    additional_volumes = []
    host_fixture = None
    compose_file = work / "compose.json"
    try:
        docker.run("build", "--target", "updater", "--label", RUN_LABEL + "=" + token, "--tag", project + "-xianyu-updater:local", helper_context, timeout=1200)
        docker.run("build", "--target", "runtime", "--tag", project + "-xianyu-saas:managed", initial, timeout=1200)
        child = project + "-" + token + "-host-bind"
        host_directory = host_bind_root / child
        host_directory.mkdir(mode=0o700)
        assert host_directory.parent.resolve() == host_bind_root.resolve() and not host_directory.is_symlink()
        requested_source = (PurePosixPath(host_bind_root_source) / child).as_posix() if host_bind_root_source else str(host_directory.resolve())
        host_fixture = {"directory": host_directory, "source": requested_source, "docker_root": docker_root, "prepared": False}
        actual_source = prepare_host_bind(
            docker, directory=host_directory, source=requested_source, project=project, token=token,
            image=project + "-xianyu-updater:local", docker_root=docker_root, public_key=pub,
        )
        host_fixture.update(source=actual_source, prepared=True)
        key_source = (PurePosixPath(actual_source) / "key/update-signing.pub").as_posix()
        data_source = (PurePosixPath(actual_source) / "data").as_posix() if kind == "bind" else "business"
        spec = compose_spec(project, token, data_source, kind, key_source)
        # Reserve a concrete random host port through an isolated disposable fixture
        # is unnecessary: accept a caller-selected OS-free port, then inspect its
        # actual binding. Explicit host port is required by the updater contract.
        import socket
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        spec["services"]["xianyu-saas"]["ports"] = ["127.0.0.1:" + str(port) + ":8096"]
        compose_file.write_text(json.dumps(compose_escape(spec)), encoding="utf-8")
        compose = ["compose", "-p", project, "-f", str(compose_file)]
        docker.run(*compose, "up", "-d", "--no-build")
        app = project + "-app"
        helper = docker.run(*compose, "ps", "-q", "xianyu-updater")
        assert helper and docker.inspect(helper)["Config"]["Labels"][PROJECT] == project
        assert_host_bind(docker, app, SIGNING_KEY, key_source)
        assert_host_bind(docker, helper, SIGNING_KEY, key_source)
        if kind == "bind":
            assert_host_bind(docker, app, "/data", data_source)
        initialize_updater(docker, compose, helper)
        wait_for(lambda: read_json(docker, helper, "/updates/status/capabilities.json"), lambda v: v.get("ready") is True, timeout=120, description="trusted helper capability")
        assert_permissions(docker, helper, app)
        before = docker.inspect(app)
        synthetic_before = state(docker, app)
        assert synthetic_before["credential"] == "synthetic-private-credential"
        cold_cache = None
        if kind == "volume" and not smoke:
            cold_cache = buildx_cache_snapshot(docker, helper)
            assert cold_cache["bootstrap"], "bootstrap base cache record is missing"
            assert not cold_cache["candidate"], "candidate base cache was already present before staging"
            assert not image_exists(docker, CANDIDATE_BASE_IMAGE), "candidate base image was already present before staging"

        first_bundle = work / "first-bundle"
        first_digest = bundle(first_bundle, "1.1.0", key, token)
        operation = stage(docker, app, first_bundle, "1.1.0", first_digest, current="1.0.0")
        result = wait_for(lambda: status(docker, helper, operation), lambda v: v.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"}, timeout=600, description="successful upgrade")
        assert result["phase"] == "succeeded", result
        if kind == "volume" and not smoke:
            after_cache = buildx_cache_snapshot(docker, helper)
            new_candidate = [record for record in after_cache["candidate"] if record["id"] not in cold_cache["ids"]]
            assert new_candidate, "candidate base produced no new Buildx cache record"
            candidate_digests = {record["digest"] for record in new_candidate}
            assert len(candidate_digests) == 1 and all(record["type"] == "regular" for record in new_candidate)
            bootstrap_digests = {record["digest"] for record in cold_cache["bootstrap"]}
            assert candidate_digests.isdisjoint(bootstrap_digests), "candidate base digest was already warmed by bootstrap"
            build_log = (PurePosixPath(UPDATER_PRIVATE) / "logs" / (operation + ".build.log")).as_posix()
            log_state = json.loads(docker.execute(helper, "import json;from pathlib import Path;p=Path(" + repr(build_log) + ");print(json.dumps({'exists':p.is_file(),'size':p.stat().st_size if p.is_file() else 0}))"))
            assert log_state["exists"] and log_state["size"] > 0, "private candidate build log is unavailable"
        assert state(docker, app) == synthetic_before
        after = docker.inspect(app)
        assert_host_bind(docker, app, SIGNING_KEY, key_source)
        if kind == "bind":
            assert_host_bind(docker, app, "/data", data_source)
        # Keep user overrides; base-image defaults may change with the new image.
        image_environment = json.loads(docker.run("image", "inspect", after["Image"], "--format", "{{json .Config.Env}}"))
        expected_environment = dict(value.split("=", 1) for value in image_environment)
        expected_environment.update(spec["services"]["xianyu-saas"]["environment"])
        assert dict(value.split("=", 1) for value in after["Config"]["Env"]) == expected_environment, "application environment changed"
        for key_name in ("PortBindings", "RestartPolicy", "ExtraHosts", "Memory", "NanoCpus", "Ulimits"):
            assert after["HostConfig"][key_name] == before["HostConfig"][key_name], key_name
        assert sorted((m["Type"], m.get("Name", m["Source"]), m["Destination"], m["RW"]) for m in after["Mounts"]) == sorted((m["Type"], m.get("Name", m["Source"]), m["Destination"], m["RW"]) for m in before["Mounts"]), "mount identity changed"
        for network in before["NetworkSettings"]["Networks"]:
            prior_aliases = set(before["NetworkSettings"]["Networks"][network]["Aliases"]) - {before["Id"], before["Id"][:12]}
            assert prior_aliases <= set(after["NetworkSettings"]["Networks"][network]["Aliases"])
        updated_image = after["Image"]
        docker.run(*compose, "up", "-d", "--no-build")
        assert docker.inspect(app)["Image"] == updated_image, "compose downgraded managed alias"
        assert_permissions(docker, helper, app)

        failed_bundle = work / "failed-bundle"
        failed_digest = bundle(failed_bundle, "1.2.0", key, token, fail=True)
        operation = stage(docker, app, failed_bundle, "1.2.0", failed_digest, current="1.1.0")
        result = wait_for(lambda: status(docker, helper, operation), lambda v: v.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"}, timeout=600, description="health-failure rollback")
        assert result["phase"] == "rolled_back", result
        assert docker.inspect(app)["Image"] == updated_image
        after_failed_start = state(docker, app)
        assert after_failed_start == {**synthetic_before, "rows": synthetic_before["rows"] + 1, "write_marker": "write-after-new-start"}, "rollback must preserve writes made after the new version starts"
        if smoke:
            assert read_json(docker, helper, "/updates/status/maintenance.json")["active"] is False
            print("PASS isolated " + kind + ": signed upgrade, same configuration/data volume, failed-start rollback, preserved new writes and permissions", flush=True)
            return

        interrupted_bundle = work / "interrupted-bundle"
        interrupted_digest = bundle(interrupted_bundle, "1.3.0", key, token)
        marker = (PurePosixPath(UPDATER_PRIVATE) / INTERRUPTION_MARKER).as_posix()
        docker.execute(helper, "from pathlib import Path;Path(" + repr(marker) + ").touch(exist_ok=False)")
        operation = stage(docker, app, interrupted_bundle, "1.3.0", interrupted_digest, current="1.1.0")
        wait_for(lambda: status(docker, helper, operation), lambda v: v.get("phase") == "switching", timeout=600, description="durable switching checkpoint")
        docker.run("restart", "--timeout", "0", helper)
        result = wait_for(lambda: status(docker, helper, operation), lambda v: v.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"}, timeout=180, description="updater process recovery")
        assert result["phase"] == "rolled_back", result
        assert docker.inspect(app)["Image"] == updated_image
        assert state(docker, app) == after_failed_start
        assert read_json(docker, helper, "/updates/status/maintenance.json")["active"] is False
        # Same opid replay after restart may not create any new app or change state.
        terminal = status(docker, helper, operation)
        docker.run("restart", helper)
        wait_for(lambda: read_json(docker, helper, "/updates/status/capabilities.json"), lambda v: v.get("ready") is True, timeout=120)
        assert status(docker, helper, operation) == terminal

        retry = stage(docker, app, interrupted_bundle, "1.3.0", interrupted_digest, current="1.1.0")
        result = wait_for(lambda: status(docker, helper, retry), lambda v: v.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"}, timeout=600)
        assert result["phase"] == "succeeded", result
        wait_for(lambda: read_json(docker, helper, "/updates/status/capabilities.json"), lambda v: {"version": "1.1.0", "manifest_sha256": first_digest} in v.get("rollback_versions", []), timeout=120)
        rollback = stage(docker, app, first_bundle, "1.1.0", first_digest, action="rollback", current="1.3.0")
        result = wait_for(lambda: status(docker, helper, rollback), lambda v: v.get("phase") in {"succeeded", "rolled_back", "failed", "recovery_failed"}, timeout=600)
        assert result["phase"] == "rolled_back", result
        assert docker.inspect(app)["Image"] == updated_image
        assert state(docker, app) == after_failed_start
        docker.run(*compose, "up", "-d", "--no-build")
        assert docker.inspect(app)["Image"] == updated_image
        cold = "candidate base absent before staging and new ref/digest cache after successful build, " if kind == "volume" else ""
        print("PASS isolated " + kind + ": " + cold + "signed update, configuration/data, alias, rollback, restart, replay and permissions")
    finally:
        cleanup(docker, project, token, additional_volumes, host_fixture)


def compose_frontend():
    candidates = []
    fixed = ROOT / ".local/compose-v5.5.1/docker-compose-windows-x86_64.exe"
    if fixed.is_file():
        candidates.append([str(fixed)])
    docker = shutil.which("docker")
    if docker:
        candidates.append([docker, "compose"])
    standalone = shutil.which("docker-compose")
    if standalone:
        candidates.append([standalone])
    for command in candidates:
        version = subprocess.run([*command, "version", "--short"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        if version.returncode == 0:
            return command
    return None


def check_overlay_compose(root):
    """Render base + user !override + the updates overlay in the required order."""
    compose = compose_frontend()
    if compose is None:
        print("SKIP offline Compose rendering: Compose frontend unavailable (explicit --run does not skip)")
        return
    base = root / "base.yml"
    local = root / "local.yml"
    overlay = root / "updates.yml"
    (root / "config").mkdir()
    (root / "config/saas.env").write_text("SYNTHETIC_ONLY=1\n", encoding="utf-8")
    (root / "empty.env").write_text("", encoding="utf-8")
    public_key = root / "trusted.pub"
    public_key.write_bytes(b"synthetic-public-key")
    base.write_text((ROOT / "docker-compose.yml").read_text(encoding="utf-8"), encoding="utf-8")
    local.write_text("""services:
  xianyu-saas:
    ports: !override
      - \"127.0.0.1:4174:4173\"
      - \"127.0.0.1:8096:8096\"
    environment:
      LOCAL_LITERAL: \"price $$$$5, quote=\\\"ok\\\", 空格 中文\"
    extra_hosts:
      - \"synthetic.local:127.0.0.2\"
    volumes: !override
      - windows-data:/data
volumes:
  windows-data: {}
""", encoding="utf-8")
    overlay.write_text((ROOT / "docker-compose.updates.yml").read_text(encoding="utf-8"), encoding="utf-8")
    environment = os.environ.copy()
    environment.update(COMPOSE_PROJECT_NAME="offline-acceptance", SAAS_UPDATE_PUBLIC_KEY_HOST_FILE=str(public_key))
    command = [*compose, "--project-directory", str(root), "--env-file", str(root / "empty.env"), "-f", str(base), "-f", str(local), "-f", str(overlay), "config", "--format", "json"]
    result = subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if result.returncode:
        raise AssertionError("offline Compose overlay rendering failed")
    spec = json.loads(result.stdout)
    app, helper = spec["services"]["xianyu-saas"], spec["services"]["xianyu-updater"]
    assert app["image"] == "offline-acceptance-xianyu-saas:managed"
    assert app["container_name"] == "xianyu-saas"
    assert app["environment"]["SAAS_DOCKER_DEPLOYMENT_ID"] == "offline-acceptance:xianyu-saas"
    assert {int(port["target"]): int(port["published"]) for port in app["ports"]} == {4173: 4174, 8096: 8096}
    app_mounts = {volume["target"]: volume for volume in app["volumes"]}
    assert set(("/data", UPDATE_ROOT, SIGNING_KEY)) <= set(app_mounts)
    assert app_mounts["/data"]["source"] == "windows-data"
    assert app_mounts[SIGNING_KEY]["read_only"] is True
    assert all(volume["target"] not in {DOCKER_SOCKET, UPDATER_PRIVATE} for volume in app["volumes"])
    assert "synthetic.local" in json.dumps(app.get("extra_hosts"), sort_keys=True)
    assert helper["network_mode"] == "bridge" and not helper.get("ports")
    assert helper["build"]["target"] == "updater" and app["build"]["target"] == "runtime"
    helper_targets = {volume["target"] for volume in helper["volumes"]}
    assert {DOCKER_SOCKET, UPDATER_PRIVATE, UPDATE_ROOT, SIGNING_KEY} <= helper_targets
    print("PASS offline Compose overlay rendering: user override survives and required update mounts are restored last")


def check_literal_roundtrip(root):
    compose = compose_frontend()
    if compose is None:
        return
    semantic_environment = {
        "DOLLARS": "$VALUE and $$ and ${LATER}",
        "QUOTES": "single ' double \"",
        "SPACES": "  leading and trailing  ",
        "UNICODE": "中文与换行\nnext",
    }
    model = compose_escape({"name": "literal-roundtrip", "services": {"app": {"image": "fixture:managed", "environment": semantic_environment}}})
    first = root / "literal-first.json"
    second = root / "literal-second.json"
    empty = root / "empty.env"
    first.write_text(json.dumps(model), encoding="utf-8")
    command = [*compose, "--project-directory", str(root), "--env-file", str(empty), "-f"]
    initial = subprocess.run([*command, str(first), "config", "--format", "json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if initial.returncode or initial.stderr.strip():
        raise AssertionError("literal Compose config roundtrip failed")
    rendered = json.loads(initial.stdout)
    second.write_bytes(initial.stdout)
    repeated = subprocess.run([*command, str(second), "config", "--format", "json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if repeated.returncode or repeated.stderr.strip():
        raise AssertionError("second literal Compose config roundtrip failed")
    rerendered = json.loads(repeated.stdout)
    first_environment = rendered["services"]["app"]["environment"]
    second_environment = rerendered["services"]["app"]["environment"]
    assert first_environment == second_environment
    assert compose_literal(first_environment) == semantic_environment
    print("PASS offline Compose literal roundtrip: dollars, quotes, spaces, Unicode and newlines remain stable")


def check_interruption_checkpoint(root):
    from types import SimpleNamespace
    from unittest.mock import patch

    private = root / "checkpoint-private"
    private.mkdir()
    marker = private / INTERRUPTION_MARKER
    phases = []

    class OfflineUpdater:
        def __init__(self):
            self.config = SimpleNamespace(private=private)

        def _phase(self, _journal, phase):
            if phase == "switching":
                assert not marker.exists(), "consume the one-shot marker before publishing switching"
            phases.append(phase)

    fake = SimpleNamespace(Updater=OfflineUpdater, main=lambda: 0)
    with patch.dict(sys.modules, {"docker_updater": fake}), patch("threading.Event") as event:
        spec = compose_spec("checkpoint-fixture", "synthetic", "business", "volume")
        entrypoint = spec["services"]["xianyu-updater"]["entrypoint"]
        assert entrypoint[:2] == ["/usr/local/bin/python", "-c"]
        try:
            exec(entrypoint[2], {})
        except SystemExit as exited:
            assert exited.code == 0
        updater = OfflineUpdater()
        marker.touch()
        updater._phase({}, "verifying")
        assert marker.exists() and not event.called
        event.return_value.wait.side_effect = InterruptedError("synthetic crash")
        try:
            updater._phase({}, "switching")
        except InterruptedError:
            pass
        else:
            raise AssertionError("armed checkpoint did not hold the updater")
        assert phases == ["verifying", "switching"] and not marker.exists()
        event.return_value.wait.assert_called_once_with()
        # A new operation after recovery must not hit the same one-shot barrier.
        OfflineUpdater()._phase({}, "switching")
        assert phases == ["verifying", "switching", "switching"]
        event.return_value.wait.assert_called_once_with()
    with patch.object(time, "monotonic", side_effect=[0, 0, 2]), patch.object(time, "sleep"):
        try:
            wait_for(lambda: {"phase": "succeeded", "private": "do-not-log"}, lambda _value: False, timeout=1)
        except AssertionError as error:
            assert "last_phase='succeeded'" in str(error) and "do-not-log" not in str(error)
        else:
            raise AssertionError("missing timeout diagnostic")
    print("PASS offline interruption fixture: durable one-shot barrier, retry and sanitized timeout diagnostic")


def self_test():
    with tempfile.TemporaryDirectory(prefix="docker-update-offline-") as temporary:
        root = Path(temporary)
        check_overlay_compose(root)
        check_literal_roundtrip(root)
        check_interruption_checkpoint(root)
        key = Ed25519PrivateKey.generate()
        for version, fail in (("1.1.0", False), ("1.2.0", True)):
            folder = root / version
            digest = bundle(folder, version, key, "synthetic-offline", fail=fail)
            raw = (folder / "docker.manifest.json").read_bytes()
            manifest = verify_docker_manifest(raw, (folder / "docker.manifest.sig").read_bytes(), key.public_key().public_bytes_raw(), expected_version=version)
            source = extract_verified_source(folder / "source.zip", root / ("extracted-" + version), manifest)
            assert digest == hashlib.sha256(raw).hexdigest()
            for name in ("app.py", "db.py", "version.py", "update_maintenance.py"):
                compile((source / "backend" / name).read_bytes(), name, "exec")
        bootstrap_digest = "sha256:" + "1" * 64
        candidate_digest = "sha256:" + "2" * 64
        synthetic_du = (
            "ID:\tbootstrap-record\nDescription:\tmetadata " + BOOTSTRAP_BASE_REF + "@" + bootstrap_digest + " cache\nType:\tregular\n\n"
            "ID:\tcandidate-record\nDescription:\tsemantic " + CANDIDATE_BASE_REF + "@" + candidate_digest + " cache\nType:\tregular\n"
        )
        class OfflineDocker:
            def __init__(self):
                self.code = ""

            def execute(self, helper, code):
                assert helper == "synthetic-helper"
                self.code = code
                return base64.b64encode(synthetic_du.encode()).decode("ascii")

            def run(self, *_args, **_kwargs):
                raise AssertionError("Buildx cache query must execute inside the updater helper")

        offline_docker = OfflineDocker()
        cache = buildx_cache_snapshot(offline_docker, "synthetic-helper")
        assert cache["candidate"] == [{"id": "candidate-record", "digest": candidate_digest, "type": "regular"}]
        assert cache["bootstrap"][0]["digest"] == bootstrap_digest
        assert BUILDX_DU_COMMAND == ("/usr/local/bin/docker", "buildx", "du", "--builder", "default", "--verbose")
        assert "DockerEngine._cli_environment()" in offline_docker.code
        assert "pulled from" not in synthetic_du
        for kind in ("volume", "bind"):
            spec = compose_spec("isolated-fixture", "synthetic", "business" if kind == "volume" else "/synthetic/private-bind", kind)
            app_mounts = spec["services"]["xianyu-saas"]["volumes"]
            assert all(m["target"] not in {DOCKER_SOCKET, UPDATER_PRIVATE} for m in app_mounts)
            assert spec["services"]["xianyu-updater"]["network_mode"] == "bridge"
            assert not spec["services"]["xianyu-updater"].get("ports")
    print("PASS offline integration fixtures: signatures, safe ZIP, Compose and structured Buildx cache evidence; no Docker engine accessed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--context")
    parser.add_argument("--acknowledge-isolated-engine", action="store_true")
    parser.add_argument("--mount-kind", choices=("both", "volume", "bind"), default="both")
    parser.add_argument("--smoke", action="store_true", help="only verify an upgrade and failed-start rollback; reuse existing cache")
    parser.add_argument("--host-bind-root", help="absolute POSIX path shared by the runner and isolated Docker daemon")
    args = parser.parse_args()
    self_test()
    if not args.run:
        return 0
    if not args.context or not args.acknowledge_isolated_engine:
        parser.error("--run requires --context and --acknowledge-isolated-engine; an existing isolated engine is required")
    if args.context in {"default", "desktop-linux", "desktop-windows"}:
        parser.error("create a dedicated isolated-test context; default/Desktop contexts are intentionally refused")
    docker = Docker(args.context)
    info = json.loads(docker.run("info", "--format", "{{json .}}"))
    if info.get("OSType") != "linux" or info.get("Swarm", {}).get("LocalNodeState") not in (None, "inactive", ""):
        parser.error("isolated Linux Engine without Swarm is required")
    docker_root = PurePosixPath(str(info.get("DockerRootDir") or ""))
    if not docker_root.is_absolute():
        parser.error("isolated Engine must report an absolute DockerRootDir")
    provided_root = None
    host_bind_path = None
    if args.host_bind_root:
        provided_root = PurePosixPath(args.host_bind_root)
        if not provided_root.is_absolute() or ".." in provided_root.parts:
            parser.error("--host-bind-root must be an absolute normalized POSIX path")
        if provided_root == docker_root or provided_root.is_relative_to(docker_root):
            parser.error("--host-bind-root must be outside DockerRootDir")
        host_bind_path = Path(args.host_bind_root)
        if not host_bind_path.is_dir() or host_bind_path.is_symlink():
            parser.error("--host-bind-root must be an existing non-symlink directory")
    with tempfile.TemporaryDirectory(prefix="docker-updater-acceptance-") as temporary:
        root = Path(temporary)
        token = uuid.uuid4().hex
        bind_root = host_bind_path or root
        bind_source = provided_root.as_posix() if provided_root is not None else None
        for kind in (("volume", "bind") if args.mount_kind == "both" else (args.mount_kind,)):
            acceptance(docker, root, token, kind, host_bind_root=bind_root, host_bind_root_source=bind_source, docker_root=docker_root.as_posix(), smoke=args.smoke)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

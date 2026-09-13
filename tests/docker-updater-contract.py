#!/usr/bin/env python3
"""Native/offline updater contract: no Docker socket, platform, model or real data."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_ROOT = PurePosixPath("/")
UPDATER_PRIVATE = (CONTAINER_ROOT / "var" / "lib" / "xianyu-updater").as_posix()
sys.path.insert(0, str(ROOT / "backend"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from docker_engine import COMPOSE_VERSION, CONFIG_HASH, PUBLIC_KEY, DockerEngine, EngineError, OP_LABEL, PROJECT, ROLE, SERVICE, compose_escape, compose_literal, mount_spec, valid_drain_ack
from docker_updater import CRITICAL, TERMINAL, Config, Store, Updater, UpdaterError, _version_key, atomic_json, canonical, validate_request
from docker_update_protocol import docker_asset_names


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
        result = subprocess.run([*command, "version", "--short"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        if result.returncode == 0 and result.stdout.decode("ascii", errors="ignore").strip() == COMPOSE_VERSION:
            return command
    return None


class Crash(BaseException):
    pass


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def explicit_environment():
    return {
        "SAAS_DB": "/data/custom/control.db",
        "SAAS_TENANTS_DIR": "/data/tenants",
        "SAAS_DOCKER_UPDATE_ROOT": "/updates",
        "SAAS_UPDATE_PUBLIC_KEY_FILE": "/app/update-signing.pub",
        "SAAS_AI_MASTER_KEY": "private-synthetic-secret",
        "LOCAL_OVERRIDE": "preserve me",
        "LITERAL_ENV": "$VALUE $$ quote=\" 空格 中文\nnext",
    }


def image_defaults(version):
    defaults = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHON_VERSION": version,
        "PYTHON_SHA256": hashlib.sha256(version.encode()).hexdigest(),
        "SYNTHETIC_IMAGE_DEFAULT": version,
        "SYNTHETIC_UNSET_DEFAULT": "must-not-appear",
    }
    if version != "1.0.0":
        defaults["SYNTHETIC_CANDIDATE_ONLY_DEFAULT"] = "candidate-" + version
    return defaults


def expected_environment(version):
    result = image_defaults(version)
    result.update(explicit_environment())
    result.pop("SYNTHETIC_UNSET_DEFAULT", None)
    return result


def fixture_container():
    environment = expected_environment("1.0.0")
    return {
        "Id": "a" * 64, "Image": "sha256:" + "1" * 64, "Name": "/fixture-app",
        "Config": {"User": "10001:10001", "Entrypoint": ["/app/docker/entrypoint.sh"], "Cmd": None, "WorkingDir": "/app", "Image": "fixture-xianyu-saas:managed",
                   "Env": [key + "=" + value for key, value in environment.items()],
                   "Labels": {PROJECT: "fixture", SERVICE: "xianyu-saas", ROLE: "app", CONFIG_HASH: "1" * 64, "com.docker.compose.image": "fixture-xianyu-saas:managed"},
                   "Healthcheck": {"Test": ["CMD", "true"]}, "Volumes": {"/data": {}}, "ExposedPorts": {"8096/tcp": {}}},
        "HostConfig": {"NetworkMode": "fixture_default", "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}, "PortBindings": {"8096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "38096"}]},
                       "Memory": 768 * 1024 * 1024, "NanoCpus": 1_500_000_000, "CpuShares": 512, "PidsLimit": 256, "ExtraHosts": ["gateway:host-gateway"], "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 8192}],
                       "SecurityOpt": ["no-new-privileges:true"], "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m"}}, "Binds": ["/isolated-synthetic/data:/data:rw", "fixture_ipc:/updates:rw", "/engine/trusted.pub:/app/update-signing.pub:ro"], "IpcMode": "private"},
        "Mounts": [{"Type": "bind", "Source": "/isolated-synthetic/data", "Destination": "/data", "RW": True, "Propagation": "rprivate"}, {"Type": "volume", "Name": "fixture_ipc", "Source": "/synthetic/volumes/ipc", "Destination": "/updates", "RW": True}, {"Type": "bind", "Source": "/engine/trusted.pub", "Destination": "/app/update-signing.pub", "RW": False, "Propagation": "rprivate"}],
        "NetworkSettings": {"Networks": {"fixture_default": {"Aliases": ["xianyu-saas", "fixture-app", "aaaaaaaaaaaa"], "IPAMConfig": None, "IPAddress": "172.31.0.2"}, "fixture_extra": {"Aliases": ["local-extra"], "IPAMConfig": None}}},
        "State": {"Running": True, "Paused": False, "Restarting": False, "Health": {"Status": "healthy"}},
    }


def metadata(version, commit="b" * 40):
    return {"version": version, "commit": commit, "update_data_version": 1, "asset_version": "synthetic-" + version, "assets": {"assets/app.js": "a" * 64, "assets/app.css": "b" * 64}, "uid": 10001}


def compose_config():
    return {
        "name": "fixture",
        "services": {
            "xianyu-saas": {
                "image": "fixture-xianyu-saas:managed",
                "container_name": "fixture-app",
                "pull_policy": "never",
                "build": {"context": "F:/must-not-reach-linux-runtime", "target": "runtime"},
                "labels": {ROLE: "app"},
                "environment": compose_escape({**explicit_environment(), "SYNTHETIC_UNSET_DEFAULT": None}),
                "volumes": [
                    {"type": "bind", "source": "F:/host/data", "target": "/data"},
                    {"type": "volume", "source": "ipc", "target": "/updates"},
                    {"type": "bind", "source": "F:/host/trusted.pub", "target": PUBLIC_KEY, "read_only": True},
                ],
                "ports": [{"mode": "ingress", "target": 8096, "published": "38096", "host_ip": "127.0.0.1", "protocol": "tcp"}],
                "networks": {"default": {"aliases": ["xianyu-saas", "fixture-app"]}, "extra": {"aliases": ["local-extra"]}},
                "restart": "unless-stopped",
                "extra_hosts": ["gateway=host-gateway"],
                "mem_limit": 768 * 1024 * 1024,
                "cpus": 1.5,
                "cpu_shares": 512,
                "pids_limit": 256,
                "ulimits": {"nofile": {"soft": 4096, "hard": 8192}},
                "security_opt": ["no-new-privileges:true"],
                "logging": {"driver": "json-file", "options": {"max-size": "10m"}},
                "ipc": "private",
                "depends_on": {"xianyu-updater": {"condition": "service_started", "required": True}},
            },
            "xianyu-updater": {"image": "helper-image", "environment": {"OTHER_SERVICE_SECRET": "must-not-be-stored"}},
        },
        "volumes": {"ipc": {"name": "fixture_ipc"}},
        "networks": {"default": {"name": "fixture_default"}, "extra": {"name": "fixture_extra"}},
    }


class FakeEngine(DockerEngine):
    def __init__(self):
        super().__init__()
        target = fixture_container()
        self.containers = {target["Id"]: target}
        self.images = {target["Image"]: metadata("1.0.0", "a" * 40)}
        self.image_environments = {target["Image"]: image_defaults("1.0.0")}
        self.alias = {"fixture-xianyu-saas:managed": target["Image"]}
        self.events = []
        self.fail = None
        self.crash_after = None
        self.protocol = True
        self.synthetic_data = {"orders": ["synthetic-order"], "accounts": ["synthetic-account"], "shop_config": "synthetic-config", "credential": "synthetic-encrypted-bytes"}
        self.candidate_data_version = 1
        self.probe_version_mismatch = False
        self.registration_error = None
        self.compose_generation = 0
        self.runtime_template = copy.deepcopy(target)

    def event(self, name, *args):
        self.events.append((name, *args))
        if self.fail == name:
            raise EngineError("update_fake_" + name + "_failed")

    def after(self, name):
        if self.crash_after == name:
            self.crash_after = None
            raise Crash(name)

    def request(self, method, path, payload=None, *, missing=False):
        if path.startswith("/networks/") and method == "GET":
            return {"Driver": "bridge", "Scope": "local", "Labels": {PROJECT: "fixture"}}
        if path.startswith("/containers/") and path.endswith("/json"):
            name = path[len("/containers/"):-len("/json")]
            for target in self.containers.values():
                if target["Id"] == name or target["Name"].lstrip("/") == name:
                    return copy.deepcopy(target)
            if missing:
                return None
            raise EngineError("update_container_missing")
        if path.startswith("/containers/create?"):
            raise AssertionError("application lifecycle must not POST /containers/create")
        raise AssertionError((method, path))

    def identity(self, self_id, app_service, updater_service, public_key=PUBLIC_KEY):
        return {
            "project": "fixture", "service": app_service, "updater_service": updater_service, "self_id": "helper", "helper_image": "helper-image",
            "shared_mount": {"Type": "volume", "Name": "fixture_ipc", "Destination": "/updates", "RW": True},
            "private_mount": {"Type": "volume", "Name": "fixture_private", "Source": "/synthetic/private", "Destination": UPDATER_PRIVATE, "RW": True},
            "public_key_mount": {"Type": "bind", "Source": "/engine/trusted.pub", "Destination": PUBLIC_KEY, "RW": False},
            "public_key_path": PUBLIC_KEY,
        }

    def register(self, value, deployment, target):
        if value.get("name") != deployment["project"] or deployment["service"] not in value.get("services", {}):
            raise EngineError("update_compose_config_invalid")
        model = compose_escape({
            "name": deployment["project"],
            "services": {
                deployment["service"]: {
                    "image": "fixture-xianyu-saas:managed",
                    "pull_policy": "never",
                    "container_name": "fixture-app",
                    "labels": {ROLE: "app"},
                    "environment": {**explicit_environment(), "SYNTHETIC_UNSET_DEFAULT": None},
                    "volumes": [
                        {"type": "bind", "source": "/isolated-synthetic/data", "target": "/data"},
                        {"type": "volume", "source": "registered-volume-1", "target": "/updates"},
                        {"type": "bind", "source": "/engine/trusted.pub", "target": PUBLIC_KEY, "read_only": True},
                    ],
                    "networks": {"registered-network-0": {"aliases": ["xianyu-saas", "fixture-app"]}, "registered-network-1": {"aliases": ["local-extra"]}},
                }
            },
            "volumes": {"registered-volume-1": {"name": "fixture_ipc", "external": True}},
            "networks": {"registered-network-0": {"name": "fixture_default", "external": True}, "registered-network-1": {"name": "fixture_extra", "external": True}},
        })
        record = {
            "schema": 2, "protocol": 1, "compose_version": COMPOSE_VERSION,
            "project": "fixture", "service": deployment["service"], "alias": "fixture-xianyu-saas:managed",
            "host_config_hash": "1" * 64, "runtime_config_hash": "2" * 64,
            "runtime_digest": self.runtime_digest(target, explicit_environment()),
            "model_sha256": hashlib.sha256(canonical(model)).hexdigest(), "target_name": "fixture-app", "explicit_environment": explicit_environment(), "unset_environment": ["SYNTHETIC_UNSET_DEFAULT"], "inherited_config_fields": ["Healthcheck", "StopSignal", "StopTimeout"],
            "mounts": [
                {"target": "/app/update-signing.pub", "type": "bind", "source": "/engine/trusted.pub", "read_only": True},
                {"target": "/data", "type": "bind", "source": "/isolated-synthetic/data", "read_only": False},
                {"target": "/updates", "type": "volume", "source": "fixture_ipc", "read_only": False},
            ],
            "volumes": {"fixture_ipc": "3" * 64},
            "networks": {"fixture_default": "4" * 64, "fixture_extra": "5" * 64},
        }
        return record, model

    def validate_registration(self, registration, model, deployment):
        if self.registration_error:
            raise EngineError(self.registration_error)
        if registration.get("model_sha256") != hashlib.sha256(canonical(model)).hexdigest() or set(model.get("services", {})) != {deployment["service"]}:
            raise EngineError("update_compose_registration_invalid")

    def targets(self, deployment):
        return [copy.deepcopy(target) for target in self.containers.values() if target["Config"]["Labels"].get(PROJECT) == deployment["project"] and target["Config"]["Labels"].get(SERVICE) == deployment["service"]]

    def inspect(self, container_id):
        if container_id not in self.containers:
            raise EngineError("update_container_missing")
        return copy.deepcopy(self.containers[container_id])

    def metadata(self, image):
        return copy.deepcopy(self.images[self.alias.get(image, image)])

    def image_id(self, image):
        result = self.alias.get(image, image)
        if result not in self.images:
            raise EngineError("update_image_missing")
        return result

    def image_exists(self, image):
        return image in self.images or image in self.alias

    def image_environment(self, image):
        image = self.alias.get(image, image)
        if image not in self.image_environments:
            raise EngineError("update_image_missing")
        return copy.deepcopy(self.image_environments[image])

    def check_protocol(self, target, config):
        if not self.protocol:
            raise EngineError("update_maintenance_protocol_unavailable")

    def build(self, root, deployment, operation_id, commit, log_path):
        self.event("build")
        assert any(c["State"]["Running"] for c in self.containers.values())
        version = json.loads((root / "package.json").read_text())["version"]
        image = "sha256:" + hashlib.sha256(operation_id.encode()).hexdigest()
        self.images[image] = {**metadata(version, commit), "update_data_version": self.candidate_data_version}
        self.image_environments[image] = image_defaults(version)
        self.after("build")
        return image

    def preflight(self, target, candidate, deployment):
        self.event("preflight")
        assert self.containers[target["Id"]]["State"]["Running"]
        self.after("preflight")

    def drain(self, target, operation_id, config):
        self.event("drain")
        state = json.loads((config.shared / "status/maintenance.json").read_text())
        assert state["active"] is True and state["operation_id"] == operation_id
        self.after("drain")

    def stop(self, target, deployment, operation_id, log_path):
        self.event("compose_stop", target["Id"])
        self.containers[target["Id"]]["State"]["Running"] = False
        self.after("compose_stop")

    def compose_up(self, deployment, registration, config, operation_id, log_path):
        self.event("compose_up_before")
        self.after("compose_up_before")
        image = self.alias[registration["alias"]]
        self.compose_generation += 1
        container_id = hashlib.sha256((operation_id + image + str(self.compose_generation)).encode()).hexdigest()
        row = copy.deepcopy(self.runtime_template)
        row.update(Id=container_id, Image=image, Name="/" + registration["target_name"])
        row["Config"]["Image"] = registration["alias"]
        row["Config"]["Labels"][CONFIG_HASH] = registration["runtime_config_hash"]
        row["Config"]["Labels"]["com.docker.compose.image"] = registration["alias"]
        environment = self.expected_environment(self.image_environment(image), registration["explicit_environment"], registration["unset_environment"])
        row["Config"]["Env"] = [key + "=" + value for key, value in environment.items()]
        row["HostConfig"]["Binds"] = None
        row["HostConfig"]["Mounts"] = []
        for mount in row["Mounts"]:
            transport = {"Type": mount["Type"], "Source": mount.get("Name") if mount["Type"] == "volume" else mount["Source"], "Target": mount["Destination"], "ReadOnly": not mount["RW"]}
            if mount["Type"] == "bind":
                transport["BindOptions"] = {"Propagation": mount.get("Propagation") or "rprivate", "CreateMountpoint": False}
            else:
                transport["VolumeOptions"] = {"NoCopy": False}
            row["HostConfig"]["Mounts"].append(transport)
        for endpoint in row["NetworkSettings"]["Networks"].values():
            endpoint["Aliases"] = [alias for alias in endpoint.get("Aliases") or [] if alias not in {self.runtime_template["Id"], self.runtime_template["Id"][:12]}]
        row["State"] = {"Running": True, "Paused": False, "Restarting": False, "Health": {"Status": "healthy"}}
        for current_id, current in list(self.containers.items()):
            if current["Config"]["Labels"].get(PROJECT) == deployment["project"] and current["Config"]["Labels"].get(SERVICE) == deployment["service"]:
                del self.containers[current_id]
        self.containers[container_id] = row
        self.event("compose_up")
        self.after("compose_up")
        return copy.deepcopy(row)

    def verify(self, container_id, expected, config):
        self.event("verify")
        if self.probe_version_mismatch and expected["version"] != "1.0.0":
            self.synthetic_data["orders"].append("write-after-new-start")
            raise EngineError("update_independent_verification_failed")
        assert self.containers[container_id]["State"]["Running"]
        assert self.metadata(self.containers[container_id]["Image"])["version"] == expected["version"]
        self.after("verify")

    def retain(self, image, alias):
        self.event("retain")
        current = self.alias.get(alias)
        if current not in (None, image):
            raise EngineError("update_recovery_alias_conflict")
        self.alias[alias] = image
        self.after("retain")

    def tag(self, image, alias):
        self.event("tag")
        self.alias[alias] = image
        self.after("tag")

def signed_fixture(directory, key, version="1.1.0", *, files=None):
    directory.mkdir(parents=True, exist_ok=True)
    commit = "b" * 40
    content = {
        "Dockerfile": b"FROM scratch AS runtime\n", "docker/entrypoint.sh": b"#!/bin/sh\nexit 0\n",
        "backend/version.py": ('VERSION = "' + version + '"\nUPDATE_DATA_VERSION = 1\n').encode(),
        "backend/update_maintenance.py": b"MAINTENANCE_PROTOCOL = 1\nimport json\nfrom pathlib import Path\ndef read_maintenance():\n    return json.loads(Path('/updates/status/maintenance.json').read_text())\ndef maintenance_active():\n    return read_maintenance()['active']\n",
        "backend/build-info.json": canonical({"version": version, "commit": commit, "dirty": False}),
        "package.json": canonical({"version": version}), "package-lock.json": canonical({"version": version, "packages": {"": {"version": version}}}),
    }
    for name, value in (files or {}).items():
        if value is None:
            content.pop(name, None)
        else:
            content[name] = value
    with zipfile.ZipFile(directory / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path, value in content.items():
            info = zipfile.ZipInfo("xianyu-saas-" + version + "/" + path)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, value)
    source = (directory / "source.zip").read_bytes()
    raw = canonical({"schema": 1, "protocol": 1, "version": version, "commit": commit, "source": {"name": docker_asset_names(version)[0], "size": len(source), "sha256": hashlib.sha256(source).hexdigest()}, "runtime_manifest_sha256": "c" * 64})
    (directory / "docker.manifest.json").write_bytes(raw)
    (directory / "docker.manifest.sig").write_bytes(base64.b64encode(key.sign(raw)))
    return hashlib.sha256(raw).hexdigest()


class Harness:
    def __init__(self, root, *, initialize=True):
        self.root = Path(root)
        self.key = Ed25519PrivateKey.generate()
        pub = self.root / "trusted.pub"
        pub.write_bytes(self.key.public_key().public_bytes_raw())
        self.clock = FakeClock()
        self.config = Config(self.root / "shared", self.root / "private", pub, "helper", enforce_permissions=False)
        self.engine = FakeEngine()
        self.updater = Updater(self.config, self.engine, self.clock)
        if initialize:
            self.updater.initialize(compose_config())

    def request(self, version="1.1.0", **changes):
        opid = changes.pop("operation_id", uuid.uuid4().hex)
        digest = signed_fixture(self.config.shared / "artifacts" / opid, self.key, version)
        current = self.engine.metadata(self.engine.alias["fixture-xianyu-saas:managed"])["version"]
        request = {"schema": 1, "operation_id": opid, "action": "apply", "version": version, "expected_current_version": current, "manifest_sha256": digest, "requested_at": self.clock.time(), "requested_by": 1, **changes}
        atomic_json(self.config.shared / "requests" / (opid + ".json"), request)
        return opid, request

    def run(self, opid):
        for _ in range(30):
            self.updater.tick()
            path = self.updater.store.journal(opid)
            if path.exists():
                value = self.updater.store.load(path)
                if value["phase"] in TERMINAL:
                    return value
        raise AssertionError("operation failed to reach terminal state")

    def until(self, opid, phase):
        for _ in range(30):
            self.updater.tick()
            value = self.updater.store.load(self.updater.store.journal(opid))
            if value["phase"] == phase:
                return value
            if value["phase"] in TERMINAL:
                raise AssertionError(value)
        raise AssertionError(phase)

    def restart(self):
        self.updater = Updater(self.config, self.engine, self.clock)


class UpdaterContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="docker-updater-contract-")
        self.h = Harness(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_registration_is_required_private_and_one_time(self):
        with tempfile.TemporaryDirectory(prefix="registration-contract-") as root:
            harness = Harness(root, initialize=False)
            capability = harness.updater.refresh_capability()
            self.assertFalse(capability["ready"])
            self.assertEqual(capability["reason"], "update_compose_not_initialized")
            result = harness.updater.initialize(compose_config())
            self.assertEqual(result["deployment_id"], "fixture:xianyu-saas")
            model = harness.updater.store.load(harness.updater.store.compose_file)
            serialized = json.dumps(model)
            self.assertEqual(set(model["services"]), {"xianyu-saas"})
            self.assertNotIn("OTHER_SERVICE_SECRET", serialized)
            self.assertNotIn("build", model["services"]["xianyu-saas"])
            with self.assertRaises(UpdaterError):
                harness.updater.initialize(compose_config())
            self.assertTrue(harness.updater.refresh_capability()["ready"])

    def test_registration_rejects_wrong_project_without_consuming_slot(self):
        with tempfile.TemporaryDirectory(prefix="registration-project-") as root:
            harness = Harness(root, initialize=False)
            value = compose_config()
            value["name"] = "other-project"
            with self.assertRaises(EngineError):
                harness.updater.initialize(value)
            harness.updater.initialize(compose_config())
            self.assertTrue(harness.updater.refresh_capability()["ready"])

    def test_success_order_context_alias_and_no_secrets(self):
        old = copy.deepcopy(next(iter(self.h.engine.containers.values())))
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "succeeded")
        names = [event[0] for event in self.h.engine.events]
        self.assertLess(names.index("build"), names.index("drain"))
        self.assertLess(names.index("preflight"), names.index("compose_stop"))
        self.assertLess(names.index("compose_stop"), names.index("compose_up"))
        self.assertNotIn("backup", result)
        self.assertNotIn("snapshot_digest", result)
        self.assertFalse((self.h.config.private / "backups").exists())
        self.assertFalse((self.h.config.private / "preflight").exists())
        self.assertLess(names.index("tag"), names.index("compose_up"))
        self.assertLess(names.index("compose_up"), names.index("verify"))
        new = self.h.engine.containers[result["new_id"]]
        self.assertNotEqual(new["Id"], old["Id"])
        self.assertEqual(DockerEngine.runtime_digest(new, explicit_environment()), DockerEngine.runtime_digest(old, explicit_environment()))
        self.assertEqual(dict(value.split("=", 1) for value in new["Config"]["Env"]), expected_environment("1.1.0"))
        self.assertEqual(result["old"], {"Id": old["Id"]})
        self.assertEqual([(mount["Type"], mount.get("Name", mount.get("Source")), mount["Destination"], mount["RW"]) for mount in new["Mounts"]], [(mount["Type"], mount.get("Name", mount.get("Source")), mount["Destination"], mount["RW"]) for mount in old["Mounts"]])
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], result["candidate"])
        self.assertEqual(self.h.engine.alias[result["recovery_alias"]], old["Image"])
        self.assertFalse(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        for path in (self.h.config.shared / "status").rglob("*.json"):
            self.assertNotIn("private-synthetic-secret", path.read_text())
        self.assertEqual(self.h.updater.refresh_capability()["rollback_versions"], [])

    def test_request_validation_and_expiry_never_build(self):
        cases = [dict(arbitrary_container="victim"), dict(image="arbitrary"), dict(command=["sh"]), dict(project="other"), dict(service="victim"), dict(compose_files=["F:/secret.yml"]), dict(mounts=["/"]), dict(requested_at=self.h.clock.time() - 601), dict(requested_at=self.h.clock.time() + 31), dict(requested_by=True), dict(schema=True), dict(expected_current_version="9.0.0"), dict(version="0.9.0")]
        for changes in cases:
            with self.subTest(changes=changes):
                opid, _ = self.h.request(**changes)
                result = self.h.run(opid)
                self.assertEqual(result["phase"], "failed")
        self.assertFalse(self.h.engine.events)

    def test_nonfinite_and_duplicate_json_rejected(self):
        opid, _ = self.h.request()
        path = self.h.config.shared / "requests" / (opid + ".json")
        path.write_text('{"schema":1,"schema":1}')
        self.assertEqual(self.h.run(opid)["error_code"], "update_duplicate_json_key")
        opid, request = self.h.request()
        request["requested_at"] = float("nan")
        path = self.h.config.shared / "requests" / (opid + ".json")
        path.write_text(json.dumps(request))
        self.assertEqual(self.h.run(opid)["error_code"], "update_request_expired")

    def test_invalid_bootstrap_or_private_key_has_no_capability(self):
        with tempfile.TemporaryDirectory(prefix="invalid-bootstrap-key-") as root:
            harness = Harness(root, initialize=False)
            harness.config.public_key.write_bytes(b"invalid-key")
            with self.assertRaises(Exception):
                harness.updater.initialize(compose_config())
            self.assertFalse(harness.updater.store.registration_path.exists())
        self.h.updater.store.trusted_key.write_bytes(b"invalid-key")
        self.assertFalse(self.h.updater.refresh_capability()["ready"])
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "failed")
        self.assertFalse(self.h.engine.events)

    def test_initialized_private_key_snapshot_ignores_later_host_bind_changes(self):
        raw = self.h.key.public_key().public_bytes_raw()
        self.assertEqual(self.h.updater.store.trusted_key.read_bytes(), raw)
        self.h.config.public_key.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
        self.assertEqual(self.h.updater._key(), raw)
        self.h.updater.store.trusted_key.unlink()
        with self.assertRaises(UpdaterError):
            self.h.updater._key()

    def test_signed_source_requires_static_maintenance_protocol_before_build(self):
        invalid_modules = (
            None,
            b"",
            b"def read_maintenance():\n    return {}\n",
            b"MAINTENANCE_PROTOCOL = 2\n",
            b"MAINTENANCE_PROTOCOL = True\n",
            b"MAINTENANCE_PROTOCOL = int('1')\n",
            b"if True:\n    MAINTENANCE_PROTOCOL = 1\n",
            b"MAINTENANCE_PROTOCOL = 1\nMAINTENANCE_PROTOCOL = 1\n",
            b"MAINTENANCE_PROTOCOL = 1 +\n",
        )
        for value in invalid_modules:
            with self.subTest(module=value):
                opid, request = self.h.request()
                digest = signed_fixture(self.h.config.shared / "artifacts" / opid, self.h.key, files={"backend/update_maintenance.py": value})
                request["manifest_sha256"] = digest
                atomic_json(self.h.config.shared / "requests" / (opid + ".json"), request)
                result = self.h.run(opid)
                self.assertEqual(result["phase"], "failed")
                self.assertEqual(result["error_code"], "update_maintenance_protocol_unsupported")
                self.assertEqual(result["version"], "1.1.0")
        self.assertFalse(self.h.engine.events)

    def test_signed_source_protocol_check_never_executes_candidate_module(self):
        opid, request = self.h.request()
        module = b"MAINTENANCE_PROTOCOL = 1\nraise RuntimeError('candidate module must not execute during admission')\n"
        digest = signed_fixture(self.h.config.shared / "artifacts" / opid, self.h.key, files={"backend/update_maintenance.py": module})
        request["manifest_sha256"] = digest
        atomic_json(self.h.config.shared / "requests" / (opid + ".json"), request)
        self.assertEqual(self.h.run(opid)["phase"], "succeeded")
        self.assertIn("build", [event[0] for event in self.h.engine.events])

    def test_tampering_source_and_descriptor_rejected(self):
        for name in ("source.zip", "docker.manifest.json", "docker.manifest.sig"):
            with self.subTest(name=name):
                opid, _ = self.h.request()
                path = self.h.config.shared / "artifacts" / opid / name
                path.write_bytes(path.read_bytes() + b"tampered")
                self.assertEqual(self.h.run(opid)["phase"], "failed")
        self.assertFalse(self.h.engine.events)

    def test_verified_private_snapshot_not_mutable_by_app(self):
        opid, _ = self.h.request()
        self.h.until(opid, "building")
        (self.h.config.shared / "artifacts" / opid / "source.zip").write_bytes(b"changed-after-verification")
        self.assertEqual(self.h.run(opid)["phase"], "succeeded")

    def test_replay_and_forged_public_success(self):
        opid, request = self.h.request()
        self.h.until(opid, "building")
        public = self.h.config.shared / "status/operations" / (opid + ".json")
        atomic_json(public, {"status": "succeeded"})
        self.h.engine.fail = "build"
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(json.loads(public.read_text())["status"], "failed")
        prior = list(self.h.engine.events)
        request["version"] = "9.0.0"
        atomic_json(self.h.config.shared / "requests" / (opid + ".json"), request)
        self.h.restart()
        self.h.updater.tick()
        self.assertEqual(prior, self.h.engine.events)

    def test_build_and_preflight_failure_do_not_stop_old(self):
        for failure in ("build", "preflight", "drain"):
            with self.subTest(failure=failure):
                self.h.engine.fail = failure
                opid, _ = self.h.request()
                result = self.h.run(opid)
                self.assertEqual(result["phase"], "failed")
                self.assertTrue(self.h.engine.containers["a" * 64]["State"]["Running"])
                self.assertFalse(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
                self.assertNotIn("compose_stop", [e[0] for e in self.h.engine.events])

    def test_incompatible_or_unknown_data_version_never_stops_old(self):
        before = copy.deepcopy(self.h.engine.synthetic_data)
        for value in (None, True, 0, -1, "1", 2):
            with self.subTest(data_version=value):
                self.h.engine.candidate_data_version = value
                opid, _ = self.h.request()
                result = self.h.run(opid)
                self.assertEqual(result["phase"], "failed")
                self.assertEqual(result["error_code"], "update_data_backward_incompatible" if value == 2 else "update_data_version_unknown")
                self.assertTrue(self.h.engine.containers["a" * 64]["State"]["Running"])
                self.assertEqual(self.h.engine.synthetic_data, before)
                self.assertFalse({"drain", "compose_stop", "tag", "compose_up"} & {e[0] for e in self.h.engine.events})

    def test_missing_current_data_version_disables_installation(self):
        self.h.engine.images["sha256:" + "1" * 64].pop("update_data_version")
        capability = self.h.updater.refresh_capability()
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["reason"], "update_data_version_unknown")

    def test_incompatible_rollback_hidden_and_rechecked_on_submission(self):
        first, _ = self.h.request("1.1.0")
        first_result = self.h.run(first)
        second, _ = self.h.request("1.2.0")
        second_result = self.h.run(second)
        self.h.engine.images[second_result["candidate"]]["update_data_version"] = 2
        self.assertEqual(self.h.updater.refresh_capability()["rollback_versions"], [])
        prior = len(self.h.engine.events)
        rollback, _ = self.h.request("1.1.0", action="rollback", manifest_sha256=first_result["request"]["manifest_sha256"])
        result = self.h.run(rollback)
        self.assertEqual(result["error_code"], "update_data_backward_incompatible")
        self.assertFalse({"drain", "compose_stop", "tag", "compose_up"} & {e[0] for e in self.h.engine.events[prior:]})

    def test_failure_restores_code_but_never_restores_business_data(self):
        self.h.engine.probe_version_mismatch = True
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], "sha256:" + "1" * 64)
        self.assertIn("write-after-new-start", self.h.engine.synthetic_data["orders"])
        self.assertFalse((self.h.config.private / "backups").exists())
        self.assertTrue(self.h.updater.refresh_capability()["ready"])

    def test_recovery_failure_preserves_maintenance_and_blocks_new_request(self):
        opid, _ = self.h.request()
        self.h.until(opid, "switching")
        self.h.engine.fail = "compose_up"
        self.h.restart()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        self.assertFalse(self.h.updater.refresh_capability()["ready"])
        second, _ = self.h.request("1.2.0")
        self.h.updater.tick()
        self.assertFalse(self.h.updater.store.journal(second).exists())

    def test_unique_same_project_target_required(self):
        second = fixture_container()
        second["Id"] = "d" * 64
        second["Name"] = "/another"
        self.h.engine.containers[second["Id"]] = second
        capability = self.h.updater.refresh_capability()
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["reason"], "update_target_not_unique")
        second["Config"]["Labels"][PROJECT] = "other-project"
        self.assertTrue(self.h.updater.refresh_capability()["ready"])

    def test_unsupported_configuration_refused_before_build(self):
        baseline = fixture_container()
        cases = [("HostConfig", "Privileged", True), ("HostConfig", "NetworkMode", "host"), ("HostConfig", "DeviceRequests", [{}]), ("HostConfig", "UnrecognizedFeature", "set"), ("Config", "User", "root"), ("Config", "Cmd", ["sh"])]
        for group, key, value in cases:
            self.h.engine.containers[baseline["Id"]] = copy.deepcopy(baseline)
            self.h.engine.containers[baseline["Id"]][group][key] = value
            self.assertFalse(self.h.updater.refresh_capability()["ready"])
        self.assertFalse(self.h.engine.events)

    def test_private_volume_and_custom_data_submount_rejected(self):
        target = self.h.engine.containers["a" * 64]
        target["Mounts"].append({"Type": "volume", "Name": "fixture_private", "Source": "/synthetic/private", "Destination": "/leak", "RW": False})
        self.assertEqual(self.h.updater.refresh_capability()["reason"], "update_private_mount_exposed")
        target["Mounts"][-1] = {"Type": "bind", "Source": "/synthetic/private/compose/runtime.json", "Destination": "/leak", "RW": False, "Propagation": "rprivate"}
        self.assertEqual(self.h.updater.refresh_capability()["reason"], "update_private_mount_exposed")
        target["Mounts"][-1] = {"Type": "bind", "Source": "/synthetic/extra", "Destination": "/data/tenants", "RW": False, "Propagation": "rprivate"}
        self.assertEqual(self.h.updater.refresh_capability()["reason"], "update_unsupported_mount_layout")

    def test_drain_ack_rejects_bool_counts_and_wrong_operation(self):
        opid = "d" * 32
        ack = {"schema": 1, "operation_id": opid, "active": True, "ready": True, "active_jobs": 0, "active_workers": 0, "error_code": ""}
        self.assertTrue(valid_drain_ack(ack, opid, ready=True))
        for key, value in (("active_jobs", False), ("schema", True), ("active_workers", -1), ("operation_id", "e" * 32), ("active", False)):
            self.assertFalse(valid_drain_ack({**ack, key: value}, opid, ready=True))

    def test_idle_api_ack_proves_protocol_but_never_drain_readiness(self):
        opid = "0" * 32
        ack = {"schema": 1, "operation_id": opid, "active": False, "ready": False,
               "active_jobs": None, "active_workers": None, "error_code": "update_maintenance_mismatch"}
        self.assertTrue(valid_drain_ack(ack, opid))
        self.assertFalse(valid_drain_ack(ack, opid, ready=True))
        with patch.object(DockerEngine, "_http", return_value=json.dumps(ack)):
            DockerEngine().check_protocol({"Id": "a" * 64}, self.h.config)
        for key, value in (("schema", True), ("operation_id", "d" * 32),
                           ("active", True), ("ready", True), ("active_jobs", False),
                           ("active_workers", -1), ("error_code", ""),
                           ("error_code", "update_drain_unavailable")):
            with self.subTest(field=key, value=value):
                self.assertFalse(valid_drain_ack({**ack, key: value}, opid))
        for key in ("active_jobs", "active_workers"):
            self.assertFalse(valid_drain_ack({k: v for k, v in ack.items() if k != key}, opid))

    def test_missing_maintenance_protocol_no_capability(self):
        self.h.engine.protocol = False
        self.assertEqual(self.h.updater.refresh_capability()["reason"], "update_maintenance_protocol_unavailable")

    def test_runtime_digest_normalizes_legacy_and_structured_mount_transport(self):
        legacy = fixture_container()
        structured = copy.deepcopy(legacy)
        structured["HostConfig"]["Binds"] = None
        structured["HostConfig"]["Mounts"] = []
        for mount in structured["Mounts"]:
            transport = {"Type": mount["Type"], "Source": mount.get("Name") if mount["Type"] == "volume" else mount["Source"], "Target": mount["Destination"], "ReadOnly": not mount["RW"]}
            if mount["Type"] == "bind":
                transport["BindOptions"] = {"Propagation": mount.get("Propagation") or "rprivate", "CreateMountpoint": False}
            else:
                transport["VolumeOptions"] = {"NoCopy": False}
            structured["HostConfig"]["Mounts"].append(transport)
        legacy_before = copy.deepcopy(legacy)
        structured_before = copy.deepcopy(structured)
        self.assertEqual(DockerEngine.runtime_digest(legacy), DockerEngine.runtime_digest(structured))
        self.assertEqual(legacy, legacy_before)
        self.assertEqual(structured, structured_before)

        for field, value in (("Source", "/different/data"), ("RW", False), ("Propagation", "private")):
            changed = copy.deepcopy(legacy)
            changed["Mounts"][0][field] = value
            self.assertNotEqual(DockerEngine.runtime_digest(legacy), DockerEngine.runtime_digest(changed))

        deployment = dict(self.h.updater.deployment)
        advanced = copy.deepcopy(structured)
        advanced["HostConfig"]["Mounts"][0]["BindOptions"]["NonRecursive"] = True
        with self.assertRaises(EngineError) as raised:
            DockerEngine._validate_target_basics(self.h.engine, advanced, deployment)
        self.assertEqual(raised.exception.code, "update_unsupported_mount_options")
        advanced = copy.deepcopy(structured)
        volume = next(mount for mount in advanced["HostConfig"]["Mounts"] if mount["Type"] == "volume")
        volume["VolumeOptions"]["Subpath"] = "nested"
        with self.assertRaises(EngineError) as raised:
            DockerEngine._validate_target_basics(self.h.engine, advanced, deployment)
        self.assertEqual(raised.exception.code, "update_volume_options_unsupported")

    def test_runtime_digest_normalizes_unordered_inspect_collections_without_mutation(self):
        original = fixture_container()
        original["HostConfig"]["PortBindings"]["8096/tcp"].append({"HostIp": "127.0.0.1", "HostPort": "38097"})
        original["HostConfig"]["ExtraHosts"].append("second:127.0.0.3")
        original["HostConfig"]["Ulimits"].append({"Name": "nproc", "Soft": 1024, "Hard": 2048})
        before = copy.deepcopy(original)
        reordered = copy.deepcopy(original)
        reordered["Config"]["Env"].reverse()
        reordered["HostConfig"]["Binds"].reverse()
        reordered["HostConfig"]["ExtraHosts"].reverse()
        reordered["HostConfig"]["Ulimits"].reverse()
        reordered["HostConfig"]["PortBindings"]["8096/tcp"].reverse()
        reordered["Mounts"].reverse()
        reordered["NetworkSettings"]["Networks"] = dict(reversed(list(reordered["NetworkSettings"]["Networks"].items())))
        for endpoint in reordered["NetworkSettings"]["Networks"].values():
            endpoint["Aliases"] = list(reversed(endpoint.get("Aliases") or []))
        self.assertEqual(DockerEngine.runtime_digest(original), DockerEngine.runtime_digest(reordered))
        self.assertEqual(original, before)

        changes = []
        for group, key, value in (
            ("Config", "Env", [*original["Config"]["Env"][:-1], "LITERAL_ENV=changed"]),
            ("HostConfig", "PortBindings", {"8096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "39096"}]}),
        ):
            changed = copy.deepcopy(original)
            changed[group][key] = value
            changes.append(changed)
        changed = copy.deepcopy(original)
        changed["Mounts"][0]["Source"] = "/different/data"
        changes.append(changed)
        command = copy.deepcopy(original)
        command["Config"]["Cmd"] = ["one", "two"]
        reversed_command = copy.deepcopy(command)
        reversed_command["Config"]["Cmd"].reverse()
        self.assertNotEqual(DockerEngine.runtime_digest(command), DockerEngine.runtime_digest(reversed_command))
        dns = copy.deepcopy(original)
        dns["HostConfig"]["Dns"] = ["1.1.1.1", "8.8.8.8"]
        reversed_dns = copy.deepcopy(dns)
        reversed_dns["HostConfig"]["Dns"].reverse()
        self.assertNotEqual(DockerEngine.runtime_digest(dns), DockerEngine.runtime_digest(reversed_dns))
        for changed in changes:
            self.assertNotEqual(DockerEngine.runtime_digest(original), DockerEngine.runtime_digest(changed))

    def test_context_order_reordering_before_stop_is_accepted(self):
        opid, _ = self.h.request()
        self.h.until(opid, "preparing")
        target = self.h.engine.containers["a" * 64]
        target["Config"]["Env"].reverse()
        target["HostConfig"]["Binds"].reverse()
        target["Mounts"].reverse()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "succeeded")

    def test_context_change_before_maintenance_rejected(self):
        opid, _ = self.h.request()
        self.h.until(opid, "preparing")
        self.h.engine.containers["a" * 64]["Config"]["Env"].append("NEW_OVERRIDE=changed")
        result = self.h.run(opid)
        self.assertEqual(result["error_code"], "update_compose_config_changed")
        self.assertNotIn("stop", [e[0] for e in self.h.engine.events])

    def test_project_alias_drift_is_not_overwritten(self):
        opid, _ = self.h.request()
        self.h.until(opid, "preflighting")
        self.h.engine.images["external"] = metadata("3.0.0")
        self.h.engine.alias["fixture-xianyu-saas:managed"] = "external"
        result = self.h.run(opid)
        self.assertEqual(result["error_code"], "update_image_alias_drift")
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], "external")

    def test_drain_context_drift_fails_closed_without_stopping_or_restoring(self):
        changed_memory = 896 * 1024 * 1024
        original_drain = self.h.engine.drain
        def drift(target, operation_id, config):
            original_drain(target, operation_id, config)
            self.h.engine.containers[target["Id"]]["HostConfig"]["Memory"] = changed_memory
        self.h.engine.drain = drift
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["error_code"], "update_compose_config_changed")
        old = self.h.engine.containers["a" * 64]
        self.assertTrue(old["State"]["Running"])
        self.assertEqual(old["HostConfig"]["Memory"], changed_memory)
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        self.assertFalse({"compose_stop", "compose_up", "tag"} & {event[0] for event in self.h.engine.events})

    def test_registered_resource_drift_after_drain_fails_closed_before_stop(self):
        opid, _ = self.h.request()
        self.h.until(opid, "stopping")
        self.h.engine.registration_error = "update_compose_resource_changed"
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["error_code"], "update_compose_resource_changed")
        self.assertTrue(self.h.engine.containers["a" * 64]["State"]["Running"])
        self.assertNotIn("compose_stop", [event[0] for event in self.h.engine.events])
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])

    def test_drain_alias_drift_is_not_retagged_or_stopped(self):
        external = "sha256:" + "9" * 64
        self.h.engine.images[external] = metadata("9.0.0")
        original_drain = self.h.engine.drain
        def drift(target, operation_id, config):
            original_drain(target, operation_id, config)
            self.h.engine.alias["fixture-xianyu-saas:managed"] = external
        self.h.engine.drain = drift
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["error_code"], "update_image_alias_drift")
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], external)
        self.assertTrue(self.h.engine.containers["a" * 64]["State"]["Running"])
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        self.assertFalse({"compose_stop", "compose_up", "tag"} & {event[0] for event in self.h.engine.events})

    def test_verification_alias_drift_is_not_retagged_or_restored(self):
        external = "sha256:" + "8" * 64
        self.h.engine.images[external] = metadata("8.0.0")
        original_verify = self.h.engine.verify
        def drift(container_id, expected, config):
            original_verify(container_id, expected, config)
            if expected["version"] == "1.1.0":
                self.h.engine.alias["fixture-xianyu-saas:managed"] = external
        self.h.engine.verify = drift
        opid, _ = self.h.request()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["error_code"], "update_image_alias_drift")
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], external)
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        names = [event[0] for event in self.h.engine.events]
        self.assertEqual(names.count("compose_up"), 1)
        self.assertEqual(names.count("tag"), 1)

    def test_interrupted_compose_double_container_converges_without_manual_delete(self):
        opid, _ = self.h.request()
        journal = self.h.until(opid, "switching")
        self.h.engine.alias[self.h.updater.deployment["alias"]] = journal["candidate"]
        candidate = self.h.engine.compose_up(self.h.updater.deployment, self.h.updater.registration, self.h.config, opid, self.h.config.private / "logs/setup.log")
        old = fixture_container()
        old["State"]["Running"] = False
        self.h.engine.containers[old["Id"]] = old
        journal.update(compose_up_started=True, alias_updated=True)
        self.h.updater.store.save(journal, self.h.clock.time())
        self.h.restart()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "rolled_back")
        self.assertNotEqual(result["restored_id"], old["Id"])
        self.assertEqual(sum(container["State"]["Running"] for container in self.h.engine.containers.values()), 1)
        self.assertNotIn("/containers/create", json.dumps(self.h.engine.events))
        self.assertNotEqual(candidate["Id"], result["restored_id"])

    def test_missing_target_before_compose_up_is_not_recreated_over_external_change(self):
        opid, _ = self.h.request()
        self.h.until(opid, "switching")
        del self.h.engine.containers["a" * 64]
        self.h.restart()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["recovery_error_code"], "update_container_context_changed")
        self.assertNotIn("compose_up", [event[0] for event in self.h.engine.events])
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])

    def test_recovery_alias_drift_is_not_retagged_or_restored(self):
        external = "sha256:" + "7" * 64
        self.h.engine.images[external] = metadata("7.0.0")
        opid, _ = self.h.request()
        self.h.until(opid, "switching")
        self.h.engine.alias["fixture-xianyu-saas:managed"] = external
        self.h.restart()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "recovery_failed")
        self.assertEqual(result["error_code"], "update_image_alias_drift")
        self.assertEqual(self.h.engine.alias["fixture-xianyu-saas:managed"], external)
        self.assertTrue(json.loads((self.h.config.shared / "status/maintenance.json").read_text())["active"])
        self.assertNotIn("compose_up", [event[0] for event in self.h.engine.events])
        self.assertNotIn("tag", [event[0] for event in self.h.engine.events])

    def test_rollback_capability_only_advertises_older_versions_and_api_accepts(self):
        from types import SimpleNamespace
        import platform_update as protocol
        first, _ = self.h.request("1.1.0")
        first_result = self.h.run(first)
        second, _ = self.h.request("1.3.0")
        self.assertEqual(self.h.run(second)["phase"], "succeeded")
        rollback, _ = self.h.request("1.1.0", action="rollback", manifest_sha256=first_result["request"]["manifest_sha256"])
        self.assertEqual(self.h.run(rollback)["phase"], "rolled_back")
        capability = self.h.updater.refresh_capability()
        self.assertEqual(capability["current_version"], "1.1.0")
        self.assertEqual(capability["rollback_versions"], [])
        key = self.h.key.public_key()
        key_file = SimpleNamespace(parent=Path("/synthetic"), lstat=lambda: SimpleNamespace(st_uid=0))
        with patch.dict(os.environ, {"SAAS_DOCKER_DEPLOYMENT_ID": "fixture:xianyu-saas", "SAAS_DOCKER_UPDATE_ROOT": str(self.h.config.shared)}), \
             patch.object(protocol, "VERSION", "1.1.0"), \
             patch.object(protocol.time, "time", return_value=self.h.clock.time()), \
             patch.object(protocol, "read_trusted_json", return_value=capability), \
             patch.object(protocol, "_trusted_update_directory"), \
             patch.object(protocol, "_public_key_file", return_value=key_file), \
             patch.object(protocol, "load_public_key", return_value=key):
            parsed = protocol.read_docker_capabilities()
        self.assertTrue(parsed["ready"])
        self.assertEqual(parsed["current_version"], "1.1.0")
        self.assertEqual(parsed["rollback_versions"], [])

    def test_manual_rollback_finishes_rolled_back(self):
        first, _ = self.h.request("1.1.0")
        first_result = self.h.run(first)
        second, _ = self.h.request("1.3.0")
        self.assertEqual(self.h.run(second)["phase"], "succeeded")
        rollback, _ = self.h.request("1.1.0", action="rollback", manifest_sha256=first_result["request"]["manifest_sha256"])
        self.assertEqual(self.h.run(rollback)["phase"], "rolled_back")

    def test_manual_rollback_committed_recovery_finishes_rolled_back(self):
        first, _ = self.h.request("1.1.0")
        first_result = self.h.run(first)
        second, _ = self.h.request("1.3.0")
        self.assertEqual(self.h.run(second)["phase"], "succeeded")
        rollback, _ = self.h.request("1.1.0", action="rollback", manifest_sha256=first_result["request"]["manifest_sha256"])
        self.h.until(rollback, "verifying")
        original = self.h.updater.store.maintenance
        def crash(journal, active, now):
            if journal.get("committed") and journal.get("action") == "rollback" and not active:
                raise Crash("rollback-postcommit")
            original(journal, active, now)
        with patch.object(self.h.updater.store, "maintenance", crash), self.assertRaises(Crash):
            self.h.updater.tick()
        self.h.restart()
        result = self.h.run(rollback)
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(self.h.updater.refresh_capability()["current_version"], "1.1.0")

    def test_private_verified_history_only_manual_rollback(self):
        first, _ = self.h.request("1.1.0")
        first_result = self.h.run(first)
        second, _ = self.h.request("1.2.0")
        self.assertEqual(self.h.run(second)["phase"], "succeeded")
        candidate = self.h.updater.refresh_capability()["rollback_versions"]
        self.assertEqual([r["version"] for r in candidate], ["1.1.0"])
        rollback, _ = self.h.request("1.1.0", action="rollback", manifest_sha256=first_result["request"]["manifest_sha256"])
        self.assertEqual(self.h.run(rollback)["phase"], "rolled_back")
        initial, _ = self.h.request("1.0.0", action="rollback")
        self.assertEqual(self.h.run(initial)["error_code"], "update_rollback_not_verified")

    def test_each_phase_restart_and_each_engine_side_effect_crash(self):
        phases = ["verifying_package", "building", "preflighting", "preparing", "stopping", "switching", "verifying"]
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix="phase-crash-") as root:
                h = Harness(root)
                opid, _ = h.request()
                h.until(opid, phase)
                h.restart()
                result = h.run(opid)
                self.assertEqual(result["phase"], "rolled_back" if phase in CRITICAL else "succeeded")
        for point in ("build", "preflight", "retain", "drain", "compose_stop", "tag", "compose_up_before", "compose_up", "verify"):
            with self.subTest(after=point), tempfile.TemporaryDirectory(prefix="effect-crash-") as root:
                h = Harness(root)
                opid, _ = h.request()
                h.engine.crash_after = point
                with self.assertRaises(Crash):
                    h.run(opid)
                h.restart()
                result = h.run(opid)
                self.assertEqual(result["phase"], "succeeded" if point in {"build", "preflight", "retain", "drain"} else "rolled_back")
                self.assertEqual(sum(c["State"]["Running"] for c in h.engine.containers.values()), 1)

    def test_postcommit_crash_finishes_verified_success(self):
        opid, _ = self.h.request()
        self.h.until(opid, "verifying")
        original = self.h.updater.store.maintenance
        def crash(journal, active, now):
            if journal.get("committed") and not active:
                raise Crash("postcommit")
            original(journal, active, now)
        with patch.object(self.h.updater.store, "maintenance", crash), self.assertRaises(Crash):
            self.h.updater.tick()
        self.h.restart()
        self.assertEqual(self.h.run(opid)["phase"], "succeeded")
        self.assertEqual(self.h.updater.refresh_capability()["current_version"], "1.1.0")

    def test_private_registration_keeps_only_target_and_actual_engine_resources(self):
        target = fixture_container()
        deployment = self.h.engine.identity("helper", "xianyu-saas", "xianyu-updater")
        deployment.update(compose_directory=self.h.config.private / "compose", compose_file=self.h.config.private / "compose/runtime.json")
        source = compose_config()
        hashes = iter(("1" * 64, "2" * 64))
        with patch.object(self.h.engine, "compose_version", return_value=COMPOSE_VERSION), \
             patch.object(self.h.engine, "_normalize_compose", side_effect=lambda deployment, value: copy.deepcopy(value)), \
             patch.object(self.h.engine, "_compose_hash", side_effect=lambda deployment, value, service: next(hashes)), \
             patch.object(self.h.engine, "_volume_identity", return_value="3" * 64), \
             patch.object(self.h.engine, "_network_identity", side_effect=("4" * 64, "5" * 64)):
            record, model = DockerEngine.register(self.h.engine, source, deployment, target)
        self.assertEqual(set(model["services"]), {"xianyu-saas"})
        service = model["services"]["xianyu-saas"]
        self.assertNotIn("build", service)
        self.assertNotIn("depends_on", service)
        self.assertNotIn("OTHER_SERVICE_SECRET", json.dumps(model))
        self.assertNotIn("F:/", json.dumps(model))
        expected_model_environment = {**explicit_environment(), "SYNTHETIC_UNSET_DEFAULT": None}
        self.assertEqual(compose_literal(service["environment"]), expected_model_environment)
        mounts = {mount["target"]: mount for mount in service["volumes"]}
        self.assertEqual(mounts["/data"]["source"], "/isolated-synthetic/data")
        self.assertFalse(mounts["/data"]["bind"]["create_host_path"])
        self.assertEqual(model["volumes"][mounts["/updates"]["source"]], {"name": "fixture_ipc", "external": True})
        self.assertEqual({value["name"] for value in model["networks"].values()}, {"fixture_default", "fixture_extra"})
        self.assertEqual(record["target_name"], "fixture-app")
        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["explicit_environment"], explicit_environment())
        self.assertEqual(record["runtime_digest"], DockerEngine.runtime_digest(target, explicit_environment()))

    def test_nondefault_or_foreign_compose_dependencies_are_rejected_at_registration(self):
        target = fixture_container()
        deployment = self.h.engine.identity("helper", "xianyu-saas", "xianyu-updater")
        deployment.update(compose_directory=self.h.config.private / "compose", compose_file=self.h.config.private / "compose/runtime.json")
        cases = []
        for condition in ("service_healthy", "service_completed_successfully"):
            value = compose_config()
            value["services"]["xianyu-saas"]["depends_on"]["xianyu-updater"]["condition"] = condition
            cases.append(value)
        value = compose_config()
        value["services"]["xianyu-saas"]["depends_on"]["xianyu-updater"]["restart"] = True
        cases.append(value)
        value = compose_config()
        value["services"]["database"] = {"image": "fixture-database:local"}
        value["services"]["xianyu-saas"]["depends_on"]["database"] = {"condition": "service_started", "required": True}
        cases.append(value)
        for value in cases:
            with patch.object(self.h.engine, "compose_version", return_value=COMPOSE_VERSION), patch.object(self.h.engine, "_normalize_compose", side_effect=lambda deployment, value: copy.deepcopy(value)):
                with self.assertRaises(EngineError) as raised:
                    DockerEngine.register(self.h.engine, value, deployment, target)
            self.assertEqual(raised.exception.code, "update_compose_configuration_unsupported")
        self.assertFalse(self.h.engine.events)

    def test_public_key_source_cannot_overlap_writable_application_mount(self):
        target = fixture_container()
        key_mount = next(mount for mount in target["Mounts"] if mount["Destination"] == PUBLIC_KEY)
        key_mount["Source"] = "/isolated-synthetic/data/update-signing.pub"
        deployment = self.h.engine.identity("helper", "xianyu-saas", "xianyu-updater")
        deployment["public_key_mount"] = copy.deepcopy(key_mount)
        deployment.update(compose_directory=self.h.config.private / "compose", compose_file=self.h.config.private / "compose/runtime.json")
        with patch.object(self.h.engine, "compose_version", return_value=COMPOSE_VERSION), \
             patch.object(self.h.engine, "_normalize_compose", side_effect=lambda deployment, value: copy.deepcopy(value)), \
             patch.object(self.h.engine, "_compose_hash", return_value="1" * 64):
            with self.assertRaises(EngineError) as raised:
                DockerEngine.register(self.h.engine, compose_config(), deployment, target)
        self.assertEqual(raised.exception.code, "update_public_key_mount_mismatch")

    def test_real_compose_registration_roundtrip_preserves_literals_and_engine_paths(self):
        command = compose_frontend()
        if command is None:
            self.skipTest("fixed Compose 5.5.1 frontend unavailable")

        class OfflineEngine(DockerEngine):
            def __init__(self, target):
                super().__init__(command, os.devnull)
                self.target = target

            def image_id(self, image):
                if image != "fixture-xianyu-saas:managed":
                    raise EngineError("update_image_missing")
                return self.target["Image"]

            def image_environment(self, image):
                if image != self.target["Image"]:
                    raise EngineError("update_image_missing")
                return image_defaults("1.0.0")

            def request(self, method, path, payload=None, *, missing=False):
                name = unquote(path.rsplit("/", 1)[-1])
                if path.startswith("/volumes/"):
                    return {"Name": name, "Driver": "local", "Scope": "local", "Mountpoint": (CONTAINER_ROOT / "var" / "lib" / "docker" / "volumes" / name).as_posix(), "CreatedAt": "synthetic", "Labels": {PROJECT: "fixture"}, "Options": {}}
                if path.startswith("/networks/"):
                    return {"Id": "id-" + name, "Name": name, "Driver": "bridge", "Scope": "local", "Internal": False, "Attachable": False, "Ingress": False, "EnableIPv6": False, "IPAM": {}, "Options": {}, "Labels": {PROJECT: "fixture"}}
                raise AssertionError((method, path))

        target = fixture_container()
        target["HostConfig"]["MemorySwap"] = 2 * target["HostConfig"]["Memory"]
        engine = OfflineEngine(target)
        deployment = {
            "project": "fixture", "service": "xianyu-saas", "updater_service": "xianyu-updater", "compose_directory": self.h.config.private / "compose",
            "public_key_path": PUBLIC_KEY,
            "shared_mount": {"Type": "volume", "Name": "fixture_ipc", "Destination": "/updates", "RW": True},
            "private_mount": {"Type": "volume", "Name": "fixture_private", "Source": "/synthetic/private", "Destination": UPDATER_PRIVATE, "RW": True},
            "public_key_mount": {"Type": "bind", "Source": "/engine/trusted.pub", "Destination": PUBLIC_KEY, "RW": False},
        }
        source = compose_config()
        normalized = engine._normalize_compose(deployment, source)
        target["Config"]["Labels"][CONFIG_HASH] = engine._compose_hash(deployment, normalized, "xianyu-saas")
        record, model = engine.register(source, deployment, target)
        service = model["services"]["xianyu-saas"]
        self.assertEqual(compose_literal(service["environment"])["LITERAL_ENV"], "$VALUE $$ quote=\" 空格 中文\nnext")
        self.assertEqual(next(volume for volume in service["volumes"] if volume["target"] == "/data")["source"], "/isolated-synthetic/data")
        self.assertNotIn("OTHER_SERVICE_SECRET", json.dumps(model))
        self.assertEqual(record["compose_version"], COMPOSE_VERSION)
        self.assertEqual(record["unset_environment"], ["SYNTHETIC_UNSET_DEFAULT"])
        self.assertIsNone(model["services"]["xianyu-saas"]["environment"]["SYNTHETIC_UNSET_DEFAULT"])

        ephemeral = copy.deepcopy(source)
        ephemeral["services"]["xianyu-saas"]["ports"][0]["published"] = "0"
        with self.assertRaises(EngineError) as raised:
            engine.register(ephemeral, deployment, target)
        self.assertEqual(raised.exception.code, "update_ephemeral_ports_unsupported")

        hooked = copy.deepcopy(source)
        hooked["services"]["xianyu-saas"]["pre_start"] = [{"command": ["sh", "-c", "forbidden"]}]
        with self.assertRaises(EngineError) as raised:
            engine.register(hooked, deployment, target)
        self.assertEqual(raised.exception.code, "update_compose_configuration_unsupported")

        dependency_cases = []
        for condition in ("service_healthy", "service_completed_successfully"):
            changed = copy.deepcopy(source)
            changed["services"]["xianyu-saas"]["depends_on"]["xianyu-updater"]["condition"] = condition
            dependency_cases.append(changed)
        changed = copy.deepcopy(source)
        changed["services"]["xianyu-saas"]["depends_on"]["xianyu-updater"]["restart"] = True
        dependency_cases.append(changed)
        changed = copy.deepcopy(source)
        changed["services"]["database"] = {"image": "fixture-database:local"}
        changed["services"]["xianyu-saas"]["depends_on"]["database"] = {"condition": "service_started", "required": True}
        dependency_cases.append(changed)
        for changed in dependency_cases:
            with self.assertRaises(EngineError) as raised:
                engine.register(changed, deployment, target)
            self.assertEqual(raised.exception.code, "update_compose_configuration_unsupported")

        target["HostConfig"]["Memory"] += 128 * 1024 * 1024
        with self.assertRaises(EngineError) as raised:
            engine.register(source, deployment, target)
        self.assertEqual(raised.exception.code, "update_compose_config_mismatch")
        target["HostConfig"]["Memory"] -= 128 * 1024 * 1024

        target["NetworkSettings"]["Networks"]["fixture_extra"]["Aliases"].append("manual-drift")
        with self.assertRaises(EngineError) as raised:
            engine.register(source, deployment, target)
        self.assertEqual(raised.exception.code, "update_compose_config_mismatch")

    def test_fixed_compose_build_transport_does_not_inherit_secrets_or_entitlements(self):
        from types import SimpleNamespace
        deployment = dict(self.h.updater.deployment)
        opid = "e" * 32
        image = "sha256:" + "1" * 64
        self.h.engine.alias["fixture-xianyu-saas:operation-" + opid] = image
        with patch("docker_engine.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            result = DockerEngine.build(self.h.engine, self.h.root, deployment, opid, "b" * 40, self.h.config.private / "logs/test.log")
        self.assertEqual(result, image)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[:2], ["/usr/local/bin/docker", "compose"])
        self.assertIn("build", command)
        self.assertIn("--builder", command)
        self.assertIn("--provenance=false", command)
        self.assertNotIn("--pull", command)
        self.assertEqual(environment["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertNotIn("SAAS_AI_MASTER_KEY", environment)
        self.assertNotIn("--allow", command)
        self.assertNotIn("--network=host", command)
        self.assertFalse((deployment["compose_directory"] / ("build-" + opid + ".json")).exists())

    def test_candidate_runtime_check_does_not_run_data_tasks(self):
        target = fixture_container()
        candidate = {"Config": {"User": target["Config"]["User"], "Entrypoint": target["Config"]["Entrypoint"], "Cmd": target["Config"]["Cmd"], "WorkingDir": target["Config"]["WorkingDir"], "Volumes": {"/data": {}}, "Env": ["NEW_IMAGE_DEFAULT=allowed"]}}
        deployment = dict(self.h.updater.deployment)
        with patch.object(self.h.engine, "request", return_value=candidate), self.assertRaises(EngineError) as raised:
            DockerEngine.preflight(self.h.engine, target, "candidate", deployment)
        self.assertEqual(raised.exception.code, "update_candidate_runtime_incompatible")
        for name in deployment["inherited_config_fields"]:
            candidate["Config"][name] = target["Config"].get(name)
        with patch.object(self.h.engine, "request", return_value=candidate), patch.object(self.h.engine, "_task") as task:
            DockerEngine.preflight(self.h.engine, target, "candidate", deployment)
            task.assert_not_called()

    def test_verifier_tasks_have_no_business_data_mounts(self):
        with patch.object(self.h.engine, "request", side_effect=[None, {"Id": "probe"}, {"StatusCode": 0}]) as request, \
             patch.object(self.h.engine, "start"), patch.object(self.h.engine, "_logs", return_value=b"ok"), \
             patch.object(self.h.engine, "_remove_owned"):
            result = DockerEngine._task(self.h.engine, "helper-image", ["python", "-c", "pass"], self.h.updater.deployment, "probe")
        payload = request.call_args_list[1].args[2]
        self.assertEqual(result, b"ok")
        self.assertEqual(payload["User"], "10001:10001")
        self.assertTrue(payload["HostConfig"]["ReadonlyRootfs"])
        self.assertNotIn("Mounts", payload["HostConfig"])
        self.assertIn("/data", payload["HostConfig"]["Tmpfs"])

    def test_independent_verifier_checks_health_ready_image_version_and_assets(self):
        from types import SimpleNamespace
        expected = metadata("1.0.0", "a" * 40)
        expected["image_id"] = "sha256:" + "1" * 64
        content = {"assets/app.js": b"synthetic-js", "assets/app.css": b"synthetic-css"}
        expected["assets"] = {path: hashlib.sha256(raw).hexdigest() for path, raw in content.items()}
        references = {"/xianyu-saas/" + path + "?v=" + expected["asset_version"]: raw for path, raw in content.items()}
        replies = {"/health": b'{"ok":true}', "/api/ready": b'{"ok":true,"database":"ready"}', "/api/version/public": canonical({"version": "1.0.0", "asset_version": expected["asset_version"]}), "/xianyu-saas/": " ".join(references).encode(), **references}
        config = SimpleNamespace(shared=self.h.config.shared, health_timeout=1)
        def verify(response):
            with patch.object(self.h.engine, "_http", side_effect=lambda cid, path, **kwargs: response[path]), patch("docker_engine.time.monotonic", side_effect=[0, 0, 2]), patch("docker_engine.time.sleep"):
                DockerEngine.verify(self.h.engine, "a" * 64, expected, config)
        verify(replies)
        for path in ("/health", "/api/ready", "/api/version/public", "/xianyu-saas/", *references):
            with self.subTest(path=path), self.assertRaises(EngineError):
                verify({**replies, path: b"corrupt"})
        expected["image_id"] = "sha256:" + "9" * 64
        with self.assertRaises(EngineError):
            verify(replies)

    def test_semver_and_heartbeat_use_clock(self):
        self.assertLess(_version_key("1.0.0-alpha.2"), _version_key("1.0.0-alpha.10"))
        self.assertLess(_version_key("1.0.0-rc.1"), _version_key("1.0.0"))
        first = self.h.updater.refresh_capability()
        self.h.clock.sleep(17)
        self.assertEqual(self.h.updater.publish_capability()["heartbeat_at"], first["heartbeat_at"] + 17)
        self.assertEqual(first["public_key_sha256"], hashlib.sha256(self.h.key.public_key().public_bytes_raw()).hexdigest())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "真实POSIX权限由隔离集成验收验证")
    def test_root_status_and_app_only_request_artifact_ownership(self):
        config = Config(self.h.root / "permissions-shared", self.h.root / "permissions-private", self.h.config.public_key, "helper")
        store = Store(config)
        self.assertEqual(config.shared.stat().st_uid, 0)
        self.assertEqual(store.status.stat().st_uid, 0)
        self.assertEqual((config.shared / "requests").stat().st_uid, 10001)
        self.assertEqual((config.shared / "artifacts").stat().st_uid, 10001)
        self.assertEqual(stat.S_IMODE(config.private.stat().st_mode), 0o700)
        with patch("os.umask"):
            atomic_json(store.status / "probe.json", {"ok": True}, True)
        self.assertEqual(stat.S_IMODE((store.status / "probe.json").stat().st_mode), 0o644)

    def test_claimed_request_does_not_expire_during_long_build(self):
        opid, _ = self.h.request()
        self.h.until(opid, "building")
        self.h.clock.sleep(3600)
        self.h.restart()
        result = self.h.run(opid)
        self.assertEqual(result["phase"], "succeeded")
        self.assertEqual(self.h.updater.publish_capability()["operation_id"], opid)

    def test_lock_is_exclusive(self):
        other = Store(self.h.config)
        with self.h.updater.store.lock():
            with self.assertRaises(UpdaterError):
                with other.lock():
                    self.fail("duplicate executor acquired lock")


class MountContracts(unittest.TestCase):
    def test_named_volume_uses_actual_name(self):
        mount = {"Type": "volume", "Name": "override_business_volume", "Source": "/unusable/daemon/path", "Destination": "/data", "RW": True}
        self.assertEqual(mount_spec(mount, "/live", True)["Source"], "override_business_volume")
        self.assertTrue(mount_spec(mount, "/live", True)["ReadOnly"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

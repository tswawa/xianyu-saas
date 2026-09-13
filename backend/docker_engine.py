"""Restricted Linux Docker/Compose transport.

Only the updater daemon opens the Unix socket. Temporary verifier tasks have
no socket, no business-data mounts and never run the application entrypoint.
The fixed official Compose plugin owns application build/stop/recreate/restore;
this module never reconstructs an application HostConfig by hand.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
ROLE = "io.xianyu.updates.role"
OP_LABEL = "io.xianyu.updates.operation"
API = "/v1.47"
SOCKET = "/var/run/docker.sock"
PRIVATE = "/var/lib/xianyu-updater"
SHARED = "/updates"
PUBLIC_KEY = "/app/update-signing.pub"
COMPOSE_VERSION = "5.5.1"
CONFIG_HASH = "com.docker.compose.config-hash"
MAX_COMPOSE_BYTES = 16 * 1024 * 1024


class EngineError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=600)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def _ident(value: str) -> str:
    return quote(value, safe="")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical(value):
    return (_json(value) + "\n").encode()


def compose_escape(value):
    """Encode already-resolved strings so a later Compose pass stays literal."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [compose_escape(item) for item in value]
    if isinstance(value, dict):
        return {key: compose_escape(item) for key, item in value.items()}
    return value


def compose_literal(value):
    """Return the runtime string represented by canonical Compose $$ escapes."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, list):
        return [compose_literal(item) for item in value]
    if isinstance(value, dict):
        return {key: compose_literal(item) for key, item in value.items()}
    return value


def _identity_digest(value, fields):
    return hashlib.sha256(_canonical({field: value.get(field) for field in fields})).hexdigest()


def _mount_key(mount):
    return (mount.get("Type"), mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source"))


def _source_overlap(left, right):
    sources = (left.get("Source"), right.get("Source"))
    if not all(isinstance(source, str) and PurePosixPath(source).is_absolute() for source in sources):
        return False
    first, second = map(PurePosixPath, sources)
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def mount_spec(mount, destination=None, readonly=None):
    modes = set(filter(None, (mount.get("Mode") or "").split(",")))
    if modes - {"ro", "rw", "private", "rprivate", "cached", "delegated", "consistent"}:
        raise EngineError("update_unsupported_mount_options")
    kind = mount["Type"]
    if kind not in {"bind", "volume"}:
        raise EngineError("update_unsupported_mount")
    result = {"Type": kind, "Source": mount["Name"] if kind == "volume" else mount["Source"], "Target": destination or mount["Destination"], "ReadOnly": not mount.get("RW", False) if readonly is None else readonly}
    consistency = modes & {"cached", "delegated", "consistent"}
    if len(consistency) > 1:
        raise EngineError("update_unsupported_mount_options")
    if consistency:
        result["Consistency"] = next(iter(consistency))
    if kind == "bind":
        propagation = mount.get("Propagation") or "rprivate"
        if propagation not in {"private", "rprivate"}:
            raise EngineError("update_unsupported_mount_propagation")
        result["BindOptions"] = {"Propagation": propagation, "CreateMountpoint": False}
    else:
        result["VolumeOptions"] = {"NoCopy": True}
    return result


def _env(config):
    result = {}
    for value in config.get("Env", []):
        key, sep, val = value.partition("=")
        if not sep or key in result:
            raise EngineError("update_unsupported_environment")
        result[key] = val
    return result


def _data_relative(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or not str(path).startswith("/data/"):
        raise EngineError("update_unsupported_data_layout")
    return str(path.relative_to("/data"))


def _compose_integer(value, *, default=0):
    if value is None:
        return default
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise EngineError("update_compose_config_invalid")


def _restart_policy(value):
    if value in (None, "", "no"):
        return {"Name": "no", "MaximumRetryCount": 0}
    if value in {"always", "unless-stopped"}:
        return {"Name": value, "MaximumRetryCount": 0}
    if isinstance(value, str) and re.fullmatch(r"on-failure(?::[1-9][0-9]*)?", value):
        _, _, count = value.partition(":")
        return {"Name": "on-failure", "MaximumRetryCount": int(count or 0)}
    raise EngineError("update_compose_configuration_unsupported")


def _extra_hosts(values):
    result = []
    for value in values or []:
        if not isinstance(value, str) or not value:
            raise EngineError("update_compose_config_invalid")
        result.append(value.replace("=", ":", 1))
    return sorted(result)


def valid_drain_ack(value, operation_id, *, ready=False):
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1 or value.get("operation_id") != operation_id:
        return False
    if any(type(value.get(name)) is not bool for name in ("active", "ready")) or not isinstance(value.get("error_code"), str):
        return False
    # The idle API does not count writers before maintenance is active. Its
    # explicit mismatch response proves protocol support, never drain readiness.
    if (not ready and not value["active"] and not value["ready"]
            and value["error_code"] == "update_maintenance_mismatch"
            and all(name in value and value[name] is None for name in ("active_jobs", "active_workers"))):
        return True
    if any(type(value.get(name)) is not int or value[name] < 0 for name in ("active_jobs", "active_workers")):
        return False
    return not ready or (value["active"] and value["ready"] and value["active_jobs"] == 0 and value["active_workers"] == 0 and not value["error_code"])


def _endpoint(network, old_id=""):
    if network.get("IPAMConfig") or network.get("Links"):
        raise EngineError("update_unsupported_static_network")
    return {"Aliases": [alias for alias in network.get("Aliases") or [] if alias not in {old_id, old_id[:12]}], "DriverOpts": network.get("DriverOpts") or {}}


# Compose preserves the supported runtime model. We only refuse configurations
# that violate updater isolation or depend on another container/service.
UNSAFE_HOST_FIELDS = frozenset("Privileged AutoRemove VolumesFrom ContainerIDFile CapAdd Devices DeviceRequests DeviceCgroupRules Links CgroupParent Cgroup UsernsMode UTSMode PidMode Sysctls Annotations PublishAllPorts".split())
UNSUPPORTED_SERVICE_FIELDS = frozenset({"configs", "secrets", "develop", "provider", "models", "volumes_from", "credential_spec", "pre_start", "post_start", "pre_stop"})
VOLUME_IDENTITY_FIELDS = ("Name", "Driver", "Scope", "Mountpoint", "CreatedAt", "Labels", "Options")
NETWORK_IDENTITY_FIELDS = ("Id", "Name", "Driver", "Scope", "Internal", "Attachable", "Ingress", "EnableIPv6", "IPAM", "Options", "Labels")
REGISTRATION_FIELDS = frozenset({"schema", "protocol", "compose_version", "project", "service", "alias", "host_config_hash", "runtime_config_hash", "runtime_digest", "model_sha256", "target_name", "explicit_environment", "unset_environment", "inherited_config_fields", "mounts", "volumes", "networks"})


class DockerEngine:
    def __init__(self, compose_command=None, compose_env_file=None):
        self.self_info = None
        self._metadata = {}
        self._compose_version_value = None
        self.compose_command = list(compose_command or ("/usr/local/bin/docker", "compose"))
        self.compose_env_file = compose_env_file or "/dev/null"

    @staticmethod
    def _cli_environment():
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
            "DOCKER_HOST": "unix://" + SOCKET,
            "DOCKER_CONFIG": PRIVATE + "/docker-config",
            "DOCKER_BUILDKIT": "1",
            "COMPOSE_ANSI": "never",
            "COMPOSE_PROGRESS": "plain",
            "COMPOSE_MENU": "false",
        }

    def _compose_prefix(self, deployment, compose_file):
        return [
            *self.compose_command,
            "--ansi", "never",
            "--progress", "plain",
            "--project-name", deployment["project"],
            "--project-directory", str(deployment["compose_directory"]),
            "--env-file", self.compose_env_file,
            "--file", str(compose_file),
        ]

    def _compose_capture(self, deployment, payload, *arguments):
        command = self._compose_prefix(deployment, "-") + list(arguments)
        try:
            result = subprocess.run(
                command,
                input=_canonical(payload),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._cli_environment(),
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EngineError("update_compose_unavailable") from error
        if result.returncode != 0 or result.stderr.strip() or len(result.stdout) > MAX_COMPOSE_BYTES:
            raise EngineError("update_compose_config_invalid")
        return result.stdout

    def _compose_logged(self, deployment, compose_file, arguments, log_path, timeout):
        command = self._compose_prefix(deployment, compose_file) + list(arguments)
        try:
            with log_path.open("ab") as output:
                os.chmod(log_path, 0o600)
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=self._cli_environment(),
                    timeout=timeout,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EngineError("update_compose_command_failed") from error
        if result.returncode != 0:
            raise EngineError("update_compose_command_failed")

    def compose_version(self):
        if self._compose_version_value is None:
            try:
                result = subprocess.run(
                    [*self.compose_command, "version", "--short"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._cli_environment(),
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise EngineError("update_compose_unavailable") from error
            value = result.stdout.decode("ascii", errors="ignore").strip()
            if result.returncode != 0 or result.stderr.strip() or value != COMPOSE_VERSION:
                raise EngineError("update_compose_version_mismatch")
            self._compose_version_value = value
        return self._compose_version_value

    def _normalize_compose(self, deployment, value):
        try:
            normalized = json.loads(self._compose_capture(deployment, value, "config", "--no-path-resolution", "--format", "json"))
        except (UnicodeError, ValueError) as error:
            raise EngineError("update_compose_config_invalid") from error
        if not isinstance(normalized, dict):
            raise EngineError("update_compose_config_invalid")
        return normalized

    def _compose_hash(self, deployment, value, service):
        raw = self._compose_capture(deployment, value, "config", "--no-path-resolution", "--hash", service)
        try:
            name, digest = raw.decode("ascii").strip().split()
        except (UnicodeError, ValueError) as error:
            raise EngineError("update_compose_config_invalid") from error
        if name != service or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EngineError("update_compose_config_invalid")
        return digest

    def request(self, method, path, payload=None, *, missing=False):
        connection = UnixConnection()
        try:
            body = None if payload is None else _json(payload).encode()
            connection.request(method, API + path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(16 * 1024 * 1024 + 1)
            if response.status == 404 and missing:
                return None
            if response.status == 400 and b"client version" in raw and b"too new" in raw:
                raise EngineError("update_engine_api_1_47_required")
            if not 200 <= response.status < 300 or len(raw) > 16 * 1024 * 1024:
                raise EngineError("update_engine_request_failed")
            if not raw:
                return None
            return json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise EngineError("update_engine_unavailable") from error
        finally:
            connection.close()

    def inspect(self, container_id):
        return self.request("GET", "/containers/" + _ident(container_id) + "/json")

    def _list(self, labels):
        query = urlencode({"all": "1", "filters": _json({"label": [key + "=" + value for key, value in labels.items()]})})
        return self.request("GET", "/containers/json?" + query)

    def image_id(self, image):
        info = self.request("GET", "/images/" + _ident(image) + "/json", missing=True)
        if not info:
            raise EngineError("update_image_missing")
        return info["Id"]

    def image_exists(self, image):
        return self.request("GET", "/images/" + _ident(image) + "/json", missing=True) is not None

    def image_environment(self, image):
        info = self.request("GET", "/images/" + _ident(image) + "/json", missing=True)
        if not info or not isinstance(info.get("Config"), dict):
            raise EngineError("update_image_missing")
        return _env(info["Config"])

    @staticmethod
    def expected_environment(image_environment, explicit_environment, unset_environment):
        expected = dict(image_environment)
        expected.update(explicit_environment)
        for name in unset_environment:
            expected.pop(name, None)
        return expected

    def validate_environment(self, target, registration, *, mismatch="update_compose_config_changed"):
        actual = _env(target["Config"])
        expected = self.expected_environment(self.image_environment(target["Image"]), registration["explicit_environment"], registration["unset_environment"])
        if actual != expected:
            raise EngineError(mismatch)
        return actual

    def identity(self, self_id, app_service, updater_service, public_key=PUBLIC_KEY):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self_id) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", app_service) or not PurePosixPath(public_key).is_absolute():
            raise EngineError("update_invalid_deployment_identity")
        info = self.request("GET", "/info")
        if info.get("OSType") != "linux" or info.get("Swarm", {}).get("LocalNodeState") not in (None, "inactive", ""):
            raise EngineError("update_linux_single_compose_required")
        own = self.inspect(self_id)
        labels = own["Config"].get("Labels") or {}
        if labels.get(ROLE) != "updater" or labels.get(SERVICE) != updater_service or not labels.get(PROJECT):
            raise EngineError("update_updater_identity_mismatch")
        peers = self._list({PROJECT: labels[PROJECT], SERVICE: updater_service})
        if len(peers) != 1 or peers[0]["Id"] != own["Id"]:
            raise EngineError("update_unique_updater_required")
        if own["Config"].get("User") not in ("", "0", "0:0", "root", "root:root"):
            raise EngineError("update_root_required")
        mounts = {m["Destination"]: m for m in own["Mounts"]}
        if len(mounts) != len(own["Mounts"]):
            raise EngineError("update_updater_mounts_missing")
        for path in (SHARED, PRIVATE, SOCKET, public_key):
            if path not in mounts:
                raise EngineError("update_updater_mounts_missing")
        if not mounts[SHARED].get("RW") or not mounts[PRIVATE].get("RW") or mounts[SOCKET].get("RW") or mounts[public_key].get("RW"):
            raise EngineError("update_updater_mounts_invalid")
        if _mount_key(mounts[SHARED]) == _mount_key(mounts[PRIVATE]) or _source_overlap(mounts[SHARED], mounts[PRIVATE]) or _mount_key(mounts[public_key]) == _mount_key(mounts[PRIVATE]) or _source_overlap(mounts[public_key], mounts[PRIVATE]):
            raise EngineError("update_private_shared_overlap")
        own_env = _env(own["Config"])
        if own_env.get("SAAS_DOCKER_UPDATE_ROOT", SHARED) != SHARED or own_env.get("SAAS_DOCKER_UPDATER_STATE_DIR", PRIVATE) != PRIVATE or own_env.get("SAAS_UPDATE_PUBLIC_KEY_FILE", PUBLIC_KEY) != public_key:
            raise EngineError("update_standard_helper_layout_required")
        self.self_info = own
        return {
            "project": labels[PROJECT],
            "service": app_service,
            "updater_service": updater_service,
            "self_id": own["Id"],
            "helper_image": own["Image"],
            "private_mount": mounts[PRIVATE],
            "shared_mount": mounts[SHARED],
            "public_key_mount": mounts[public_key],
            "public_key_path": public_key,
        }

    def targets(self, deployment):
        return [self.inspect(row["Id"]) for row in self._list({PROJECT: deployment["project"], SERVICE: deployment["service"]})]

    def _validate_target_basics(self, target, deployment):
        conf, host = target["Config"], target["HostConfig"]
        labels = conf.get("Labels") or {}
        if labels.get(PROJECT) != deployment["project"] or labels.get(SERVICE) != deployment["service"] or labels.get(ROLE) != "app" or labels.get("com.docker.compose.oneoff", "False").lower() != "false":
            raise EngineError("update_target_identity_mismatch")
        if conf.get("User") not in {"10001", "10001:10001", "xianyu", "xianyu:xianyu"}:
            raise EngineError("update_nonroot_app_required")
        if conf.get("Entrypoint") != ["/app/docker/entrypoint.sh"] or conf.get("Cmd") or conf.get("WorkingDir") != "/app":
            raise EngineError("update_custom_command_unsupported")
        if not conf.get("Healthcheck", {}).get("Test") or conf["Healthcheck"]["Test"][0] == "NONE":
            raise EngineError("update_healthcheck_required")
        if any(host.get(name) for name in UNSAFE_HOST_FIELDS):
            raise EngineError("update_unsafe_host_configuration")
        if host.get("IpcMode") not in (None, "", "private") or host.get("NetworkMode") in ("host", "none", "bridge", "default") or str(host.get("NetworkMode", "")).startswith("container:"):
            raise EngineError("update_unsupported_namespaces")
        if host.get("Runtime") not in (None, "", "runc") or host.get("LogConfig", {}).get("Type") not in (None, "", "json-file", "local"):
            raise EngineError("update_unsupported_runtime")
        if conf.get("MacAddress") or conf.get("NetworkDisabled") or conf.get("ArgsEscaped") or conf.get("OnBuild") or conf.get("Tty") or conf.get("OpenStdin"):
            raise EngineError("update_unsupported_container_configuration")
        for key, values in (host.get("PortBindings") or {}).items():
            if not re.fullmatch(r"[0-9]+/(tcp|udp)", key) or not values or any(not str(v.get("HostPort", "")).isdigit() or int(v["HostPort"]) == 0 for v in values):
                raise EngineError("update_ephemeral_ports_unsupported")
        env = _env(conf)
        _data_relative(env.get("SAAS_DB", "/data/saas.db"))
        _data_relative(env.get("SAAS_TENANTS_DIR", "/data/tenants"))
        if env.get("SAAS_DOCKER_UPDATE_ROOT") != SHARED or env.get("SAAS_UPDATE_STATUS_DIR", SHARED + "/status") != SHARED + "/status" or env.get("SAAS_UPDATE_MAINTENANCE_FILE", SHARED + "/status/maintenance.json") != SHARED + "/status/maintenance.json" or env.get("SAAS_UPDATE_PUBLIC_KEY_FILE", deployment["public_key_path"]) != deployment["public_key_path"]:
            raise EngineError("update_shared_protocol_not_configured")
        mounts = {m["Destination"]: m for m in target.get("Mounts", [])}
        required = {"/data", SHARED, deployment["public_key_path"]}
        if len(mounts) != len(target.get("Mounts", [])) or not required <= set(mounts) or not mounts["/data"].get("RW"):
            raise EngineError("update_required_mounts_missing")
        if not mounts[SHARED].get("RW") or _mount_key(mounts[SHARED]) != _mount_key(deployment["shared_mount"]):
            raise EngineError("update_shared_mount_mismatch")
        if mounts[deployment["public_key_path"]].get("RW") or _mount_key(mounts[deployment["public_key_path"]]) != _mount_key(deployment["public_key_mount"]):
            raise EngineError("update_public_key_mount_mismatch")
        for destination, mount in mounts.items():
            mount_spec(mount)
            private_mount = deployment["private_mount"]
            if _mount_key(mount) == _mount_key(private_mount) or _source_overlap(mount, private_mount):
                raise EngineError("update_private_mount_exposed")
            if destination not in required:
                if mount.get("RW") or destination == "/" or any(destination == path or destination.startswith(path + "/") for path in ("/app", "/data", SHARED, "/proc", "/sys", "/dev", "/var/run", PRIVATE)):
                    raise EngineError("update_unsupported_mount_layout")
            if mount["Type"] == "bind" and (mount["Source"] in ("/", "/var", "/var/run", "/run") or "docker.sock" in mount["Source"]):
                raise EngineError("update_docker_socket_exposed")
        for destination in host.get("Tmpfs") or {}:
            if destination not in {"/tmp", "/run"}:
                raise EngineError("update_unsupported_tmpfs")
        for transport in host.get("Mounts") or []:
            if not isinstance(transport, dict) or transport.get("Type") not in {"bind", "volume"}:
                raise EngineError("update_unsupported_mount_options")
            actual = mounts.get(transport.get("Target"))
            source = actual.get("Name") if actual and actual.get("Type") == "volume" else actual.get("Source") if actual else None
            if not actual or transport.get("Type") != actual.get("Type") or transport.get("Source") != source or bool(transport.get("ReadOnly")) != (not actual.get("RW", False)):
                raise EngineError("update_unsupported_mount_options")
            modes = set(filter(None, (actual.get("Mode") or "").split(",")))
            consistency = next(iter(modes & {"cached", "delegated", "consistent"}), "")
            if (transport.get("Consistency") or "") != consistency:
                raise EngineError("update_unsupported_mount_options")
            for name in ("TmpfsOptions", "ImageOptions", "ClusterOptions"):
                if transport.get(name):
                    raise EngineError("update_unsupported_mount_options")
            if transport["Type"] == "bind":
                options = transport.get("BindOptions") or {}
                if not isinstance(options, dict) or (options.get("Propagation") or "rprivate") != (actual.get("Propagation") or "rprivate"):
                    raise EngineError("update_unsupported_mount_options")
                if any(value for name, value in options.items() if name not in {"Propagation", "CreateMountpoint"}):
                    raise EngineError("update_unsupported_mount_options")
            else:
                options = transport.get("VolumeOptions") or {}
                if not isinstance(options, dict) or any(value for value in options.values()):
                    raise EngineError("update_volume_options_unsupported")

    def _validate_compose_runtime(self, service, target):
        """Reject stale labels that no longer describe the live mutable settings."""
        host = target["HostConfig"]
        expected_ports = {}
        for port in service.get("ports") or []:
            if not isinstance(port, dict) or port.get("mode", "ingress") != "ingress":
                raise EngineError("update_compose_configuration_unsupported")
            published = port.get("published")
            protocol = port.get("protocol", "tcp")
            target_port = port.get("target")
            if not isinstance(target_port, int) or protocol not in {"tcp", "udp"} or not isinstance(published, str) or not published.isdigit() or int(published) == 0:
                raise EngineError("update_ephemeral_ports_unsupported")
            host_ip = str(port.get("host_ip") or "")
            if host_ip in {"0.0.0.0", "::"}:
                host_ip = ""
            expected_ports.setdefault(str(target_port) + "/" + protocol, set()).add((host_ip, published))
        actual_ports = {}
        for key, bindings in (host.get("PortBindings") or {}).items():
            for binding in bindings or []:
                host_ip = str(binding.get("HostIp") or "")
                if host_ip in {"0.0.0.0", "::"}:
                    host_ip = ""
                actual_ports.setdefault(key, set()).add((host_ip, str(binding.get("HostPort") or "")))
        if expected_ports != actual_ports:
            raise EngineError("update_compose_config_mismatch")

        for compose_name, host_name in (
            ("mem_limit", "Memory"), ("mem_reservation", "MemoryReservation"),
            ("mem_swappiness", "MemorySwappiness"),
            ("cpu_shares", "CpuShares"), ("cpu_period", "CpuPeriod"),
            ("cpu_quota", "CpuQuota"), ("cpu_rt_period", "CpuRealtimePeriod"),
            ("cpu_rt_runtime", "CpuRealtimeRuntime"), ("pids_limit", "PidsLimit"),
            ("oom_score_adj", "OomScoreAdj"),
        ):
            expected = _compose_integer(service.get(compose_name))
            actual = _compose_integer(host.get(host_name))
            if compose_name == "mem_swappiness" and compose_name not in service and actual in {-1, 0}:
                continue
            if expected != actual:
                raise EngineError("update_compose_config_mismatch")
        memory = _compose_integer(service.get("mem_limit"))
        swap = _compose_integer(service.get("memswap_limit"))
        actual_swap = _compose_integer(host.get("MemorySwap"))
        if swap == 0:
            if actual_swap not in ({0, memory * 2} if memory > 0 else {0}):
                raise EngineError("update_compose_config_mismatch")
        elif swap != actual_swap:
            raise EngineError("update_compose_config_mismatch")
        if str(service.get("cpuset") or "") != str(host.get("CpusetCpus") or "") or host.get("CpusetMems"):
            raise EngineError("update_compose_config_mismatch")
        if bool(service.get("oom_kill_disable", False)) != bool(host.get("OomKillDisable", False)):
            raise EngineError("update_compose_config_mismatch")
        for name in ("BlkioWeight", "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps", "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "IOMaximumIOps", "IOMaximumBandwidth"):
            if host.get(name):
                raise EngineError("update_compose_configuration_unsupported")
        if "shm_size" in service and _compose_integer(service.get("shm_size")) != _compose_integer(host.get("ShmSize")):
            raise EngineError("update_compose_config_mismatch")
        cpus = service.get("cpus")
        expected_nano = 0 if cpus is None else int(float(cpus) * 1_000_000_000)
        if not math.isfinite(float(cpus or 0)) or expected_nano != _compose_integer(host.get("NanoCpus")):
            raise EngineError("update_compose_config_mismatch")
        actual_restart = host.get("RestartPolicy") or {"Name": "no", "MaximumRetryCount": 0}
        actual_restart = {"Name": actual_restart.get("Name") or "no", "MaximumRetryCount": int(actual_restart.get("MaximumRetryCount") or 0)}
        if _restart_policy(service.get("restart")) != actual_restart:
            raise EngineError("update_compose_config_mismatch")
        if bool(service.get("read_only", False)) != bool(host.get("ReadonlyRootfs", False)):
            raise EngineError("update_compose_config_mismatch")
        expected_ulimits = []
        for name, value in (service.get("ulimits") or {}).items():
            limits = {"soft": value, "hard": value} if type(value) is int else value
            if not isinstance(limits, dict):
                raise EngineError("update_compose_config_invalid")
            expected_ulimits.append({"Name": name, "Soft": _compose_integer(limits.get("soft")), "Hard": _compose_integer(limits.get("hard"))})
        if sorted(expected_ulimits, key=lambda item: item["Name"]) != sorted(host.get("Ulimits") or [], key=lambda item: item.get("Name", "")):
            raise EngineError("update_compose_config_mismatch")
        if _extra_hosts(service.get("extra_hosts")) != _extra_hosts(host.get("ExtraHosts")):
            raise EngineError("update_compose_config_mismatch")
        for compose_name, host_name in (("security_opt", "SecurityOpt"), ("cap_add", "CapAdd"), ("cap_drop", "CapDrop"), ("dns", "Dns"), ("dns_search", "DnsSearch"), ("dns_opt", "DnsOptions"), ("group_add", "GroupAdd")):
            if sorted(service.get(compose_name) or []) != sorted(host.get(host_name) or []):
                raise EngineError("update_compose_config_mismatch")
        ipc = service.get("ipc")
        if ipc not in (None, "private") or (ipc == "private" and host.get("IpcMode") != "private"):
            raise EngineError("update_compose_config_mismatch")
        logging = service.get("logging")
        if logging is not None:
            if not isinstance(logging, dict) or logging.get("driver") != (host.get("LogConfig") or {}).get("Type") or (logging.get("options") or {}) != ((host.get("LogConfig") or {}).get("Config") or {}):
                raise EngineError("update_compose_config_mismatch")

    def _volume_identity(self, name):
        volume = self.request("GET", "/volumes/" + _ident(name), missing=True)
        if not volume or volume.get("Name") != name:
            raise EngineError("update_compose_resource_changed")
        return _identity_digest(volume, VOLUME_IDENTITY_FIELDS)

    def _network_identity(self, name, deployment, *, external):
        network = self.request("GET", "/networks/" + _ident(name), missing=True)
        if not network or network.get("Name") != name or network.get("Driver") != "bridge" or network.get("Scope") != "local" or network.get("Ingress"):
            raise EngineError("update_compose_resource_changed")
        if not external and (network.get("Labels") or {}).get(PROJECT) != deployment["project"]:
            raise EngineError("update_compose_resource_changed")
        return _identity_digest(network, NETWORK_IDENTITY_FIELDS)

    @staticmethod
    def _actual_mount_record(mount):
        return {
            "target": mount["Destination"],
            "type": mount["Type"],
            "source": mount.get("Name") if mount["Type"] == "volume" else mount.get("Source"),
            "read_only": not mount.get("RW", False),
        }

    @staticmethod
    def _runtime_mount_record(mount):
        mount_spec(mount)
        record = DockerEngine._actual_mount_record(mount)
        modes = set(filter(None, (mount.get("Mode") or "").split(",")))
        consistency = modes & {"cached", "delegated", "consistent"}
        if consistency:
            record["consistency"] = next(iter(consistency))
        if mount["Type"] == "bind":
            record["propagation"] = mount.get("Propagation") or "rprivate"
        elif mount.get("Driver"):
            record["driver"] = mount["Driver"]
        return record

    def register(self, value, deployment, target):
        """Validate a host-rendered Compose JSON and return a private one-service model."""
        self.compose_version()
        if not isinstance(value, dict) or value.get("name") != deployment["project"] or not isinstance(value.get("services"), dict) or deployment["service"] not in value["services"]:
            raise EngineError("update_compose_config_invalid")
        normalized = self._normalize_compose(deployment, value)
        if normalized.get("name") != deployment["project"] or not isinstance(normalized.get("services"), dict):
            raise EngineError("update_compose_config_invalid")
        service = normalized["services"].get(deployment["service"])
        if not isinstance(service, dict) or UNSUPPORTED_SERVICE_FIELDS & set(service) or "env_file" in service:
            raise EngineError("update_compose_configuration_unsupported")
        semantic_service = compose_literal(copy.deepcopy(service))
        dependencies = semantic_service.get("depends_on") or {}
        if dependencies:
            updater_service = deployment.get("updater_service")
            policy = dependencies.get(updater_service) if set(dependencies) == {updater_service} else None
            if not isinstance(policy, dict) or set(policy) - {"condition", "required", "restart"} or policy.get("condition", "service_started") != "service_started" or policy.get("required", True) is not True or policy.get("restart", False) is not False:
                raise EngineError("update_compose_configuration_unsupported")
        self._validate_target_basics(target, deployment)
        self._validate_compose_runtime(semantic_service, target)
        labels = target["Config"].get("Labels") or {}
        host_hash = self._compose_hash(deployment, normalized, deployment["service"])
        if labels.get(CONFIG_HASH) != host_hash:
            raise EngineError("update_compose_config_mismatch")
        alias = deployment["project"] + "-" + deployment["service"] + ":managed"
        if service.get("image") != alias or self.image_id(alias) != target["Image"]:
            raise EngineError("update_image_alias_drift")
        if (service.get("labels") or {}).get(ROLE) != "app" or service.get("network_mode") or service.get("ipc") not in (None, "private"):
            raise EngineError("update_compose_configuration_unsupported")
        deploy = service.get("deploy") or {}
        if not isinstance(deploy, dict) or set(deploy) - {"replicas"} or deploy.get("replicas") not in (None, 1) or service.get("scale") not in (None, 1):
            raise EngineError("update_compose_configuration_unsupported")

        configured_environment = semantic_service.get("environment") or {}
        if not isinstance(configured_environment, dict) or any(value is not None and not isinstance(value, str) for value in configured_environment.values()):
            raise EngineError("update_compose_config_invalid")
        explicit_environment = {key: value for key, value in configured_environment.items() if value is not None}
        unset_environment = sorted(key for key, value in configured_environment.items() if value is None)
        actual_environment = _env(target["Config"])
        expected_environment = self.expected_environment(self.image_environment(target["Image"]), explicit_environment, unset_environment)
        if actual_environment != expected_environment:
            raise EngineError("update_compose_config_mismatch")
        inherited_config_fields = [
            name for name, compose_name in (("Healthcheck", "healthcheck"), ("StopSignal", "stop_signal"), ("StopTimeout", "stop_grace_period"))
            if compose_name not in semantic_service
        ]
        semantic_service.pop("build", None)
        semantic_service.pop("depends_on", None)
        semantic_service["image"] = alias
        semantic_service["pull_policy"] = "never"
        semantic_service["environment"] = {**explicit_environment, **{key: None for key in unset_environment}}

        configured_mounts = semantic_service.get("volumes") or []
        if not isinstance(configured_mounts, list) or any(not isinstance(item, dict) for item in configured_mounts):
            raise EngineError("update_compose_config_invalid")
        actual_mounts = {mount["Destination"]: mount for mount in target.get("Mounts", [])}
        key_mount = actual_mounts.get(deployment["public_key_path"], {})
        if any(mount.get("RW") and (_mount_key(mount) == _mount_key(key_mount) or _source_overlap(mount, key_mount)) for mount in actual_mounts.values()):
            raise EngineError("update_public_key_mount_mismatch")
        configured_targets = [item.get("target") for item in configured_mounts]
        if len(set(configured_targets)) != len(configured_targets) or set(configured_targets) != set(actual_mounts):
            raise EngineError("update_compose_config_mismatch")
        runtime_mounts = []
        runtime_volumes = {}
        volume_identities = {}
        top_volumes = compose_literal(normalized.get("volumes") or {})
        for index, configured in enumerate(configured_mounts):
            destination = configured.get("target")
            actual = actual_mounts[destination]
            if configured.get("type") != actual.get("Type") or configured.get("type") not in {"bind", "volume"} or bool(configured.get("read_only", False)) != (not actual.get("RW", False)):
                raise EngineError("update_compose_config_mismatch")
            mount_spec(actual)
            runtime = copy.deepcopy(configured)
            runtime["target"] = destination
            if actual["Type"] == "bind":
                source = actual.get("Source")
                if not isinstance(source, str) or not PurePosixPath(source).is_absolute():
                    raise EngineError("update_compose_bind_source_invalid")
                runtime["source"] = source
                options = runtime.setdefault("bind", {})
                if not isinstance(options, dict):
                    raise EngineError("update_compose_configuration_unsupported")
                options["create_host_path"] = False
            else:
                name = actual.get("Name")
                configured_source = configured.get("source")
                top = top_volumes.get(configured_source) if isinstance(configured_source, str) else None
                if not isinstance(name, str) or not name or not isinstance(top, dict) or top.get("name") != name:
                    raise EngineError("update_anonymous_volume_unsupported")
                source = "registered-volume-" + str(index)
                runtime["source"] = source
                runtime_volumes[source] = {"name": name, "external": True}
                volume_identities[name] = self._volume_identity(name)
            if actual.get("RW", False):
                runtime.pop("read_only", None)
            else:
                runtime["read_only"] = True
            runtime_mounts.append(runtime)
        semantic_service["volumes"] = runtime_mounts

        configured_networks = semantic_service.get("networks") or {}
        if not isinstance(configured_networks, dict):
            raise EngineError("update_compose_config_invalid")
        top_networks = compose_literal(normalized.get("networks") or {})
        actual_networks = target.get("NetworkSettings", {}).get("Networks") or {}
        resolved_networks = {}
        for key in configured_networks:
            top = top_networks.get(key)
            if not isinstance(top, dict) or not isinstance(top.get("name"), str):
                raise EngineError("update_compose_config_invalid")
            resolved_networks[key] = top["name"]
        if set(resolved_networks.values()) != set(actual_networks) or len(set(resolved_networks.values())) != len(resolved_networks):
            raise EngineError("update_compose_config_mismatch")
        runtime_networks = {}
        service_networks = {}
        network_identities = {}
        for index, (key, actual_name) in enumerate(resolved_networks.items()):
            endpoint = actual_networks[actual_name]
            actual_endpoint = _endpoint(endpoint, target["Id"])
            top = top_networks[key]
            external = top.get("external") is True
            network_identities[actual_name] = self._network_identity(actual_name, deployment, external=external)
            configured = configured_networks[key] or {}
            if not isinstance(configured, dict):
                raise EngineError("update_compose_config_invalid")
            if any(configured.get(name) for name in ("ipv4_address", "ipv6_address", "mac_address", "link_local_ips")):
                raise EngineError("update_unsupported_static_network")
            expected_aliases = set(configured.get("aliases") or [])
            actual_aliases = set(actual_endpoint["Aliases"])
            automatic_aliases = {deployment["service"], target["Name"].lstrip("/")}
            if not expected_aliases <= actual_aliases or actual_aliases - expected_aliases - automatic_aliases or (configured.get("driver_opts") or {}) != actual_endpoint["DriverOpts"]:
                raise EngineError("update_compose_config_mismatch")
            source = "registered-network-" + str(index)
            runtime_networks[source] = {"name": actual_name, "external": True}
            service_networks[source] = configured
        semantic_service["networks"] = service_networks

        semantic = {
            "name": deployment["project"],
            "services": {deployment["service"]: semantic_service},
            "volumes": runtime_volumes,
            "networks": runtime_networks,
        }
        runtime_model = self._normalize_compose(deployment, compose_escape(semantic))
        rendered_service = runtime_model.get("services", {}).get(deployment["service"], {})
        if compose_literal(rendered_service.get("environment")) != semantic_service["environment"]:
            raise EngineError("update_compose_literal_roundtrip_failed")
        runtime_hash = self._compose_hash(deployment, runtime_model, deployment["service"])
        record = {
            "schema": 2,
            "protocol": 1,
            "compose_version": COMPOSE_VERSION,
            "project": deployment["project"],
            "service": deployment["service"],
            "alias": alias,
            "host_config_hash": host_hash,
            "runtime_config_hash": runtime_hash,
            "runtime_digest": self.runtime_digest(target, explicit_environment),
            "model_sha256": hashlib.sha256(_canonical(runtime_model)).hexdigest(),
            "target_name": target["Name"].lstrip("/"),
            "explicit_environment": explicit_environment,
            "unset_environment": unset_environment,
            "inherited_config_fields": inherited_config_fields,
            "mounts": sorted((self._actual_mount_record(mount) for mount in actual_mounts.values()), key=lambda item: item["target"]),
            "volumes": volume_identities,
            "networks": network_identities,
        }
        return record, runtime_model

    def validate_registration(self, registration, model, deployment):
        if set(registration) != REGISTRATION_FIELDS or registration.get("schema") != 2 or registration.get("protocol") != 1 or registration.get("compose_version") != COMPOSE_VERSION:
            raise EngineError("update_compose_registration_invalid")
        expected_alias = deployment["project"] + "-" + deployment["service"] + ":managed"
        if registration.get("project") != deployment["project"] or registration.get("service") != deployment["service"] or registration.get("alias") != expected_alias:
            raise EngineError("update_compose_registration_invalid")
        for name in ("host_config_hash", "runtime_config_hash", "runtime_digest", "model_sha256"):
            if not isinstance(registration.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", registration[name]):
                raise EngineError("update_compose_registration_invalid")
        service = (model.get("services") or {}).get(deployment["service"], {})
        explicit_environment = registration.get("explicit_environment")
        unset_environment = registration.get("unset_environment")
        if not isinstance(explicit_environment, dict) or any(not isinstance(name, str) or not isinstance(value, str) for name, value in explicit_environment.items()):
            raise EngineError("update_compose_registration_invalid")
        if not isinstance(unset_environment, list) or len(set(unset_environment)) != len(unset_environment) or any(not isinstance(name, str) for name in unset_environment) or set(explicit_environment) & set(unset_environment):
            raise EngineError("update_compose_registration_invalid")
        inherited_config_fields = registration.get("inherited_config_fields")
        if not isinstance(inherited_config_fields, list) or len(set(inherited_config_fields)) != len(inherited_config_fields) or set(inherited_config_fields) - {"Healthcheck", "StopSignal", "StopTimeout"}:
            raise EngineError("update_compose_registration_invalid")
        model_environment = service.get("environment") or {}
        expected_model_environment = {**explicit_environment, **{name: None for name in unset_environment}}
        if hashlib.sha256(_canonical(model)).hexdigest() != registration["model_sha256"] or model.get("name") != deployment["project"] or set(model.get("services") or {}) != {deployment["service"]} or service.get("image") != expected_alias or service.get("pull_policy") != "never" or any(name in service for name in ("build", "env_file", "depends_on")) or compose_literal(model_environment) != expected_model_environment:
            raise EngineError("update_compose_registration_invalid")
        self.compose_version()
        if any(self._volume_identity(name) != digest for name, digest in registration.get("volumes", {}).items()):
            raise EngineError("update_compose_resource_changed")
        if any(self._network_identity(name, deployment, external=True) != digest for name, digest in registration.get("networks", {}).items()):
            raise EngineError("update_compose_resource_changed")

    def validate_target(self, target, deployment, config, registration, *, require_running=True, allowed_hashes=None, expected_images=None):
        self._validate_target_basics(target, deployment)
        if require_running and (not target["State"].get("Running") or target["State"].get("Paused") or target["State"].get("Restarting")):
            raise EngineError("update_target_not_running")
        labels = target["Config"].get("Labels") or {}
        hashes = set(allowed_hashes or (registration["host_config_hash"], registration["runtime_config_hash"]))
        if labels.get(CONFIG_HASH) not in hashes or target["Name"].lstrip("/") != registration["target_name"]:
            raise EngineError("update_compose_config_changed")
        records = sorted((self._actual_mount_record(mount) for mount in target.get("Mounts", [])), key=lambda item: item["target"])
        if records != registration["mounts"] or set(target.get("NetworkSettings", {}).get("Networks") or {}) != set(registration["networks"]):
            raise EngineError("update_compose_resource_changed")
        images = set(expected_images or ())
        if not images:
            images.add(self.image_id(registration["alias"]))
        if target["Image"] not in images:
            raise EngineError("update_image_alias_drift")
        self.validate_environment(target, registration)
        if self.runtime_digest(target, registration["explicit_environment"]) != registration["runtime_digest"]:
            raise EngineError("update_compose_config_changed")

    def _validate_operation_member(self, target, journal, deployment, registration):
        self._validate_target_basics(target, deployment)
        labels = target["Config"].get("Labels") or {}
        if labels.get(CONFIG_HASH) not in {registration["host_config_hash"], registration["runtime_config_hash"]}:
            raise EngineError("update_compose_config_changed")
        records = sorted((self._actual_mount_record(mount) for mount in target.get("Mounts", [])), key=lambda item: item["target"])
        if records != registration["mounts"] or set(target.get("NetworkSettings", {}).get("Networks") or {}) != set(registration["networks"]):
            raise EngineError("update_compose_resource_changed")
        allowed = {journal.get("candidate"), journal.get("old_alias")}
        allowed.discard(None)
        if target["Image"] not in allowed:
            raise EngineError("update_image_alias_drift")
        self.validate_environment(target, registration)
        if self.runtime_digest(target, registration["explicit_environment"]) != registration["runtime_digest"]:
            raise EngineError("update_compose_config_changed")

    def operation_target(self, journal, deployment, registration):
        candidates = self.targets(deployment)
        compose_window = bool(journal.get("compose_up_started") or journal.get("compose_restore_started"))
        if not candidates:
            return None
        if len(candidates) > 1:
            if not compose_window:
                raise EngineError("update_container_context_changed")
            for target in candidates:
                self._validate_operation_member(target, journal, deployment, registration)
            # Let official Compose converge its own interrupted replacement set.
            return None
        target = candidates[0]
        if target["Id"] == journal["old"]["Id"]:
            self.validate_target(target, deployment, None, registration, require_running=False,
                                 expected_images={journal["old_alias"]})
            return target
        try:
            self.validate_target(target, deployment, None, registration, require_running=False, allowed_hashes={registration["runtime_config_hash"]}, expected_images={journal.get("candidate"), journal.get("old_alias")} - {None})
            return target
        except EngineError:
            if not compose_window:
                raise
            self._validate_operation_member(target, journal, deployment, registration)
            return None

    @staticmethod
    def runtime_digest(target, explicit_environment=None):
        source = target["Config"]
        config = {name: copy.deepcopy(source.get(name)) for name in ("Hostname", "Domainname", "User", "Env", "Cmd", "Healthcheck", "WorkingDir", "Entrypoint", "StopSignal", "StopTimeout")}
        if config.get("Hostname") in {target["Id"], target["Id"][:12]}:
            config["Hostname"] = ""
        environment = _env(source)
        if explicit_environment is None:
            config["Env"] = sorted(source.get("Env") or [])
        else:
            config["Env"] = sorted(name + "=" + environment[name] for name in explicit_environment if name in environment)
        host = copy.deepcopy(target["HostConfig"])
        # Binds and HostConfig.Mounts are alternate Docker API transports for the
        # same realized mount set. Advanced options are rejected during registration.
        host.pop("Binds", None)
        host.pop("Mounts", None)
        for name in ("VolumesFrom", "CapAdd", "CapDrop", "ExtraHosts", "GroupAdd", "Links",
                     "SecurityOpt", "Devices", "DeviceCgroupRules", "DeviceRequests", "Ulimits",
                     "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
                     "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "MaskedPaths", "ReadonlyPaths"):
            if isinstance(host.get(name), list):
                host[name] = sorted(host[name], key=_json)
        for bindings in (host.get("PortBindings") or {}).values():
            if isinstance(bindings, list):
                bindings.sort(key=_json)
        mounts = sorted((DockerEngine._runtime_mount_record(mount) for mount in target.get("Mounts", [])), key=lambda item: item["target"])
        networks = {name: _endpoint(endpoint, target["Id"]) for name, endpoint in sorted((target.get("NetworkSettings", {}).get("Networks") or {}).items())}
        for endpoint in networks.values():
            endpoint["Aliases"].sort()
        value = {"Config": config, "HostConfig": host, "Mounts": mounts, "Networks": networks}
        return hashlib.sha256(_canonical(value)).hexdigest()

    def _labels(self, deployment, operation_id, role="task"):
        return {PROJECT: deployment["project"], SERVICE: "xianyu-updater-task", ROLE: role, OP_LABEL: operation_id}

    def _remove_owned(self, container_id, deployment, operation_id):
        current = self.request("GET", "/containers/" + _ident(container_id) + "/json", missing=True)
        if current is None:
            return
        labels = current["Config"].get("Labels") or {}
        if labels.get(PROJECT) != deployment["project"] or labels.get(OP_LABEL) != operation_id or labels.get(ROLE) != "task":
            raise EngineError("update_resource_identity_mismatch")
        # Never remove a volume. Task containers override image /data VOLUME.
        self.request("DELETE", "/containers/" + _ident(container_id) + "?force=1&v=0")

    def _logs(self, container_id):
        connection = UnixConnection()
        try:
            connection.request("GET", API + "/containers/" + _ident(container_id) + "/logs?stdout=1&stderr=0")
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 4 * 1024 * 1024:
                raise EngineError("update_task_output_invalid")
            output = bytearray()
            while raw:
                if len(raw) < 8:
                    raise EngineError("update_task_output_invalid")
                length = struct.unpack(">I", raw[4:8])[0]
                if length > len(raw) - 8:
                    raise EngineError("update_task_output_invalid")
                if raw[0] == 1:
                    output.extend(raw[8:8 + length])
                raw = raw[8 + length:]
            return bytes(output)
        finally:
            connection.close()

    def _task(self, image, command, deployment, operation_id, *, network="none"):
        name = deployment["project"] + "-update-task-" + hashlib.sha256((operation_id + _json(command)).encode()).hexdigest()[:20]
        # A crash can leave only this exact labelled task. Retrying discards it safely.
        existing = self.request("GET", "/containers/" + _ident(name) + "/json", missing=True)
        if existing:
            self._remove_owned(existing["Id"], deployment, operation_id)
        host = {"NetworkMode": network, "ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 64, "Memory": 512 * 1024 * 1024, "NanoCpus": 1_000_000_000, "LogConfig": {"Type": "json-file", "Config": {"max-size": "4m", "max-file": "1"}}, "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m,mode=1777", "/data": "rw,nosuid,nodev,size=1m,mode=1777"}, "RestartPolicy": {"Name": "no"}}
        payload = {"Image": image, "Entrypoint": command[:1], "Cmd": command[1:], "User": "10001:10001", "Env": [], "WorkingDir": "/", "Labels": self._labels(deployment, operation_id), "HostConfig": host}
        container_id = self.request("POST", "/containers/create?" + urlencode({"name": name}), payload)["Id"]
        try:
            self.start(container_id)
            result = self.request("POST", "/containers/" + _ident(container_id) + "/wait?condition=not-running")
            if result.get("StatusCode") != 0:
                raise EngineError("update_isolated_task_failed")
            return self._logs(container_id)
        finally:
            self._remove_owned(container_id, deployment, operation_id)

    def _deployment_for_probe(self):
        if not self.self_info:
            raise EngineError("update_identity_required")
        return {"project": self.self_info["Config"]["Labels"][PROJECT], "helper_image": self.self_info["Image"]}

    def metadata(self, image):
        image_id = self.image_id(image)
        if image_id not in self._metadata:
            code = "import sys,json,hashlib,os;sys.path.insert(0,'/app/backend');import version;from pathlib import Path;m=version.version_payload();m['update_data_version']=getattr(version,'UPDATE_DATA_VERSION',None);m['uid']=os.getuid();m['assets']={p:hashlib.sha256((Path('/app/frontend')/p).read_bytes()).hexdigest() for p in ['assets/app.js','assets/app.css']};print(json.dumps(m))"
            raw = self._task(image_id, ["/app/backend/.venv/bin/python", "-I", "-c", code], self._deployment_for_probe(), "metadata")
            try:
                result = json.loads(raw)
                if result["uid"] != 10001 or not result.get("asset_version"):
                    raise ValueError()
            except (ValueError, KeyError) as error:
                raise EngineError("update_image_metadata_invalid") from error
            result["image_id"] = image_id
            self._metadata[image_id] = result
        return copy.deepcopy(self._metadata[image_id])

    def build(self, root, deployment, operation_id, commit, log_path):
        tag = deployment["project"] + "-" + deployment["service"] + ":operation-" + operation_id
        compose_file = deployment["compose_directory"] / ("build-" + operation_id + ".json")
        model = compose_escape({
            "name": deployment["project"],
            "services": {
                deployment["service"]: {
                    "image": tag,
                    "pull_policy": "never",
                    "build": {
                        "context": str(root),
                        "dockerfile": str(root / "Dockerfile"),
                        "target": "runtime",
                        "pull": False,
                        "args": {"SAAS_BUILD_COMMIT": commit, "SAAS_BUILD_DIRTY": "false"},
                    },
                }
            },
        })
        try:
            compose_file.write_bytes(_canonical(model))
            os.chmod(compose_file, 0o600)
            self._compose_logged(
                deployment,
                compose_file,
                ["build", "--builder", "default", "--provenance=false", deployment["service"]],
                log_path,
                3600,
            )
        except EngineError as error:
            raise EngineError("update_build_failed") from error
        except OSError as error:
            raise EngineError("update_build_failed") from error
        finally:
            compose_file.unlink(missing_ok=True)
        return self.image_id(tag)

    def _http(self, container_id, path, *, web=False):
        target = self.inspect(container_id)
        env = _env(target["Config"])
        port = env.get("SAAS_DEV_WEB_PORT" if web else "SAAS_DEV_API_PORT", "4173" if web else "8096")
        if not port.isdigit() or not 1 <= int(port) <= 65535 or not path.startswith("/"):
            raise EngineError("update_invalid_health_endpoint")
        code = "import urllib.request,base64;op=urllib.request.build_opener(urllib.request.ProxyHandler({}),type('NoRedirect',(urllib.request.HTTPRedirectHandler,),{'redirect_request':lambda *a:None})());r=op.open(" + repr("http://127.0.0.1:" + port + path) + ",timeout=5);assert r.status==200;b=r.read(4*1024*1024+1);assert len(b)<=4*1024*1024;print(base64.b64encode(b).decode())"
        raw = self._task(self.self_info["Image"], ["/usr/local/bin/python", "-I", "-c", code], self._deployment_for_probe(), "http-probe", network="container:" + container_id)
        import base64
        try:
            return base64.b64decode(raw.strip(), validate=True)
        except ValueError as error:
            raise EngineError("update_health_invalid") from error

    def check_protocol(self, target, config):
        try:
            response = json.loads(self._http(target["Id"], "/internal/v1/update/drain?operation_id=" + "0" * 32))
            if not valid_drain_ack(response, "0" * 32):
                raise ValueError()
        except Exception as error:
            raise EngineError("update_maintenance_protocol_unavailable") from error

    def drain(self, target, operation_id, config):
        deadline = time.monotonic() + config.drain_timeout
        while time.monotonic() < deadline:
            try:
                result = json.loads(self._http(target["Id"], "/internal/v1/update/drain?operation_id=" + operation_id))
                if valid_drain_ack(result, operation_id, ready=True):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise EngineError("update_drain_timeout")

    def verify(self, container_id, metadata, config):
        deadline = time.monotonic() + config.health_timeout
        while time.monotonic() < deadline:
            try:
                target = self.inspect(container_id)
                if target["Image"] != metadata["image_id"] or not target["State"].get("Running") or target["State"].get("Health", {}).get("Status") != "healthy":
                    raise ValueError()
                maintenance_path = config.shared / "status/maintenance.json"
                maintenance = json.loads(maintenance_path.read_text(encoding="utf-8")) if maintenance_path.exists() else {}
                if maintenance.get("active") is True:
                    operation_id = maintenance["operation_id"]
                    ack = json.loads(self._http(container_id, "/internal/v1/update/drain?operation_id=" + operation_id))
                    if not valid_drain_ack(ack, operation_id, ready=True):
                        raise ValueError()
                health = json.loads(self._http(container_id, "/health"))
                ready = json.loads(self._http(container_id, "/api/ready"))
                version = json.loads(self._http(container_id, "/api/version/public"))
                if health.get("ok") is not True or ready.get("database") != "ready" or ready.get("ok") is not True or version.get("version") != metadata["version"] or version.get("asset_version") != metadata["asset_version"]:
                    raise ValueError()
                index = self._http(container_id, "/xianyu-saas/", web=True).decode("utf-8")
                for path, digest in metadata["assets"].items():
                    reference = "/xianyu-saas/" + path + "?v=" + metadata["asset_version"]
                    if reference not in index or hashlib.sha256(self._http(container_id, reference, web=True)).hexdigest() != digest:
                        raise ValueError()
                return
            except Exception:
                time.sleep(1)
        raise EngineError("update_independent_verification_failed")

    def preflight(self, target, candidate, deployment):
        """Check image compatibility without mounting or copying business data."""
        candidate_config = self.request("GET", "/images/" + _ident(candidate) + "/json")["Config"]
        _env(candidate_config)
        if candidate_config.get("User") not in {"10001", "10001:10001", "xianyu", "xianyu:xianyu"} or any(candidate_config.get(name) != target["Config"].get(name) for name in ("Entrypoint", "Cmd", "WorkingDir")) or any(candidate_config.get(name) != target["Config"].get(name) for name in deployment.get("inherited_config_fields", ())) or set(candidate_config.get("Volumes") or {}) - {m["Destination"] for m in target["Mounts"]}:
            raise EngineError("update_candidate_runtime_incompatible")

    def stop(self, target, deployment, operation_id, log_path):
        self._compose_logged(
            deployment,
            deployment["compose_file"],
            ["stop", "--timeout", "60", deployment["service"]],
            log_path,
            120,
        )
        current = self.request("GET", "/containers/" + _ident(target["Id"]) + "/json", missing=True)
        if current is None or current["State"].get("Running"):
            raise EngineError("update_stop_failed")

    def start(self, container_id):
        if not self.inspect(container_id)["State"].get("Running"):
            self.request("POST", "/containers/" + _ident(container_id) + "/start")

    def compose_up(self, deployment, registration, config, operation_id, log_path):
        self._compose_logged(
            deployment,
            deployment["compose_file"],
            [
                "up", "--detach", "--force-recreate", "--no-deps", "--no-build",
                "--wait", "--wait-timeout", str(max(10, int(config.health_timeout))),
                "--pull", "never", deployment["service"],
            ],
            log_path,
            max(120, int(config.health_timeout) + 60),
        )
        targets = self.targets(deployment)
        if len(targets) != 1:
            raise EngineError("update_target_not_unique")
        target = targets[0]
        self.validate_target(
            target,
            deployment,
            config,
            registration,
            allowed_hashes={registration["runtime_config_hash"]},
        )
        return target

    def retain(self, image, alias):
        if self.image_exists(alias):
            if self.image_id(alias) != image:
                raise EngineError("update_recovery_alias_conflict")
            return
        self.tag(image, alias)

    def tag(self, image, alias):
        repo, tag = alias.rsplit(":", 1)
        self.request("POST", "/images/" + _ident(image) + "/tag?" + urlencode({"repo": repo, "tag": tag}))
        if self.image_id(alias) != image:
            raise EngineError("update_alias_verification_failed")

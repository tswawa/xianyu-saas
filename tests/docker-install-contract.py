#!/usr/bin/env python3
"""Contract for the single-command Docker installation entry point.

Static assertions always run.  Behavioral scenarios require a POSIX bash and
drive the real ``deploy/docker-install.sh`` against a fake ``docker``/``git``
on PATH: no Docker daemon, network, image build or host mutation is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/docker-install.sh"
SOURCE = INSTALLER.read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEPLOYMENT = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
COMPOSE_BASE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE_UPDATES = (ROOT / "docker-compose.updates.yml").read_text(encoding="utf-8")

TEST_KEY = "-----BEGIN PUBLIC KEY-----\nsynthetic-installer-contract-key\n-----END PUBLIC KEY-----\n"

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""Deterministic fake docker CLI for deploy/docker-install.sh contract tests."""
import fcntl
import hashlib
import json
import os
import shutil
import sys


STATE = os.environ["FAKE_DOCKER_STATE"]
# Consume the pipeline before locking, so the config producer can finish.
INITIALIZE_INPUT = sys.stdin.buffer.read() if "docker_updater.py" in sys.argv else b""


def load():
    with open(STATE, encoding="utf-8") as source:
        return json.load(source)


def save(state):
    with open(STATE, "w", encoding="utf-8") as target:
        json.dump(state, target)


def format_value(argv):
    for index, value in enumerate(argv):
        if value == "--format":
            return argv[index + 1]
    return ""


def main():
    argv = sys.argv[1:]
    state = load()
    state.setdefault("calls", []).append({
        "argv": argv,
        "project": os.environ.get("COMPOSE_PROJECT_NAME", ""),
        "env_file": os.environ.get("SAAS_ENV_FILE", ""),
        "key": os.environ.get("SAAS_UPDATE_PUBLIC_KEY_HOST_FILE", ""),
        "commit": os.environ.get("SAAS_BUILD_COMMIT", ""),
        "dirty": os.environ.get("SAAS_BUILD_DIRTY", ""),
    })
    save(state)

    if argv[:2] == ["compose", "version"]:
        print("5.5.1")
        return 0
    if argv[:2] == ["context", "show"]:
        print("default")
        return 0
    if argv[:2] == ["context", "inspect"]:
        print(state.get("endpoint", "unix:///var/run/docker.sock"))
        return 0
    if argv[:1] == ["info"]:
        print("linux")
        return 0
    if argv[:1] == ["ps"]:
        if any(value.startswith("label=com.docker.compose.project.working_dir=") for value in argv):
            for name in state.get("working_dir_projects", []):
                print(name)
            return 0
        if any("io.xianyu.updates.role=updater" in value for value in argv):
            for row in state.get("updater_rows", []):
                print(row)
            return 0
        if any(value.startswith("label=com.docker.compose.project=") for value in argv):
            service_filter = ""
            for value in argv:
                if value.startswith("label=com.docker.compose.service="):
                    service_filter = value.removeprefix("label=com.docker.compose.service=")
            fmt = format_value(argv)
            for entry in state.get("project_containers", []):
                service, role, workdir = entry[:3]
                oneoff = entry[3] if len(entry) > 3 else ""
                if service_filter and service != service_filter:
                    continue
                if "service" in fmt:
                    print("\x1f".join(("id-" + service, service, role, workdir, oneoff)))
                else:
                    print("id-" + service)
            return 0
        return 0
    if argv[:2] == ["image", "inspect"]:
        return 0 if argv[2] in state.get("images", []) else 1
    if argv[:2] == ["volume", "inspect"]:
        return 0 if argv[2] in state.get("volumes", []) else 1
    if argv[:1] == ["start"]:
        state["containers_running"] = True
        save(state)
        return 0
    if argv[:1] == ["inspect"]:
        fmt = format_value(argv)
        if "Mounts" in fmt:
            if state.get("mounted_key_staged"):
                print(os.path.join(os.getcwd(), ".local", "docker-install", "update-signing.pub"))
            else:
                print(state.get("mounted_key", ""))
        elif "State.Running" in fmt:
            print("true" if state.get("containers_running") else "false")
        else:
            print(state.get("health", "healthy"))
        return 0
    if argv[:1] == ["run"]:
        mounts = {}
        command = []
        index = 1
        while index < len(argv):
            value = argv[index]
            if value == "--mount":
                spec = dict(part.split("=", 1) for part in argv[index + 1].split(",") if "=" in part)
                mounts[spec.get("target", "")] = spec.get("source", "")
                index += 2
            elif value == "--rm":
                index += 1
            elif value in ("--user", "--entrypoint"):
                index += 2
            else:
                command = argv[index:]
                break
        script = ""
        positional = []
        if "-c" in command:
            at = command.index("-c")
            script = command[at + 1]
            positional = command[at + 3:]
        if "/trust" in mounts and "cp" in script:
            source_dir = mounts["/seed"]
            target_dir = mounts["/trust"]
            source_name = positional[0].rsplit("/", 1)[-1] if positional else ""
            target_name = positional[-1] if positional else ""
            shutil.copyfile(os.path.join(source_dir, source_name), os.path.join(target_dir, target_name))
            os.chmod(os.path.join(target_dir, target_name), 0o644)
            state["staged"] = True
        if "/data" in mounts:
            state["prepared_data"] = True
        save(state)
        return 0

    if argv[:1] != ["compose"]:
        sys.stderr.write("fake docker: unsupported command\n")
        return 2

    index = 1
    while index < len(argv):
        value = argv[index]
        if value in ("--project-directory", "-p", "--env-file", "-f"):
            index += 2
        else:
            break
    if index >= len(argv):
        return 1
    sub = argv[index]
    rest = argv[index + 1:]

    if sub == "config":
        if "--format" in rest and "json" in rest:
            print(json.dumps({"name": state.get("project", "xianyu-saas"), "services": {"xianyu-saas": {}, "xianyu-updater": {}}}))
        else:
            print("{}")
        return 0
    if sub == "build":
        project = state.get("project", "xianyu-saas")
        overlay = any(str(value).endswith("docker-compose.updates.yml") for value in argv)
        state["images"] = (
            [project + "-xianyu-saas:managed", project + "-xianyu-updater:local"]
            if overlay else ["xianyu-saas:local"]
        )
        save(state)
        return 0
    if sub == "up":
        state["containers_running"] = True
        if not state.get("project_containers"):
            state["project_containers"] = [["xianyu-saas", "app", ""], ["xianyu-updater", "updater", ""]]
        save(state)
        return 0
    if sub == "exec":
        while rest and rest[0].startswith("-"):
            if rest[0] in ("-e",):
                rest = rest[2:]
            else:
                rest = rest[1:]
        command = rest[1:]
        joined = " ".join(command)
        if "docker-deployment.json" in joined:
            print("registered" if state.get("registered") else "unregistered")
            return 0
        if "docker_updater.py" in joined:
            payload = INITIALIZE_INPUT
            state["initialize_input"] = hashlib.sha256(payload).hexdigest()
            if state.get("initialize_busy_forever"):
                save(state)
                sys.stdout.write('{"error_code":"update_executor_busy","ok":false}\n')
                return 2
            if state.get("initialize_busy_once"):
                state["initialize_busy_once"] = False
                save(state)
                sys.stdout.write('{"error_code":"update_executor_busy","ok":false}\n')
                return 2
            if state.get("initialize_error"):
                save(state)
                sys.stdout.write('{"error_code":"' + state["initialize_error"] + '","ok":false}\n')
                return 2
            state["registered"] = True
            save(state)
            sys.stdout.write('{"compose_version":"5.5.1","deployment_id":"xianyu-saas:xianyu-saas","ok":true,"protocol":1,"schema":1}\n')
            return 0
        if "update_capabilities" in joined:
            sequence = state.get("acceptance_sequence")
            if sequence:
                value = sequence.pop(0)
                save(state)
            else:
                value = state.get("acceptance", "READY")
            print(value)
            return 0
        return 1
    return 1


if __name__ == "__main__":
    # config and exec run concurrently in the installer's pipe; keep their
    # fake state and call history coherent without changing production code.
    with open(STATE + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raise SystemExit(main())
'''

FAKE_GIT = r'''#!/usr/bin/env python3
"""Deterministic fake git for installer build-provenance tests."""
import os
import sys


MODE = os.environ.get("FAKE_GIT", "none")
ARGV = sys.argv[1:]


def main():
    if "--git-dir" in ARGV:
        return 0 if MODE != "none" else 128
    if "--verify" in ARGV:
        if MODE == "none":
            return 128
        print("a" * 40)
        return 0
    if "status" in ARGV:
        if MODE == "none":
            return 128
        if MODE == "dirty":
            print(" M backend/app.py")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def assert_static_contract() -> None:
    # One ordinary entry point that renders base + updates in the required order
    # and wires the chosen env file into both interpolation and service env_file.
    assert "--project-directory" in SOURCE
    assert '-f "$root/docker-compose.yml"' in SOURCE
    assert '-f "$root/docker-compose.updates.yml"' in SOURCE
    compose_block = SOURCE.split("compose=(", 1)[1].split("export COMPOSE_PROJECT_NAME", 1)[0]
    assert compose_block.index('-f "$root/docker-compose.yml"') < compose_block.index('-f "$root/docker-compose.updates.yml"')
    assert 'COMPOSE_PROJECT_NAME="$project"' in SOURCE
    assert 'SAAS_ENV_FILE="$env_file"' in SOURCE
    assert "builtin=true" in SOURCE
    assert "SAAS_APP_CODE_DIR: /data/app-code" in COMPOSE_BASE
    assert SOURCE.index('export SAAS_ENV_FILE="$env_file"') < SOURCE.index('"${compose[@]}" config')
    assert 'SAAS_UPDATE_PUBLIC_KEY_HOST_FILE="$key_path"' in SOURCE
    assert "deploy/update-signing.pub" in SOURCE
    assert ".local/docker-install" in SOURCE
    assert "set -Eeuo pipefail" in SOURCE
    assert "DOCKER_HOST" in SOURCE and "DOCKER_CONTEXT" in SOURCE
    assert "docker start" in SOURCE

    # Existing config is copied only when missing; new data dirs are prepared
    # without recursion; managed reruns require existing data.
    assert 'if [[ ! -e "$env_file" ]]' in SOURCE
    assert 'ls -A "$data_dir"' in SOURCE
    assert 'chown "$1:$2" /data' in SOURCE
    assert "chown -R" not in SOURCE and "chown -r" not in SOURCE

    # One-time registration reuses the documented root-only initialize path
    # through a single consistent compose array and retries executor contention.
    assert "config --format json" in SOURCE
    assert SOURCE.index("config --format json") < SOURCE.index("docker_updater.py initialize")
    assert "update_executor_busy" in SOURCE
    assert "docker-deployment.json" in SOURCE
    assert "update_compose_initialization_conflict" in SOURCE

    # Acceptance is the real application-side readiness function, not /health.
    assert "update_capabilities" in SOURCE
    assert "platform_update" in SOURCE
    assert "State.Health" in SOURCE

    # No destructive or remote-trust behavior in the entry point.
    for forbidden in ("compose down", "down -v", "prune", "remove-orphans", "renew-anon-volumes", "--pull", "curl", "wget"):
        assert forbidden not in SOURCE, forbidden

    # Docs route new users through the single entry point and describe the
    # limited rerun semantics.
    assert "deploy/docker-install.sh" in README
    assert "deploy/docker-install.sh" in DEPLOYMENT

    # The service env_file is parameterized so --env-file also reaches the app.
    assert "${SAAS_ENV_FILE:-config/saas.env}" in COMPOSE_BASE
    assert "./data:/data" in COMPOSE_BASE

    # The overlay still carries the IPC, key mount and managed alias.
    for needle in ("target: /updates", "target: /app/update-signing.pub", ":managed"):
        assert needle in COMPOSE_UPDATES, needle
    assert "SAAS_DOCKER_UPDATE_ROOT: /updates" in COMPOSE_UPDATES


def write_fake_bin(directory: Path) -> None:
    for name, content in (("docker", FAKE_DOCKER), ("git", FAKE_GIT)):
        target = directory / name
        target.write_text(content.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1), encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def preinstall_env(project: Path) -> None:
    (project / "config/saas.env").write_text("SAAS_ENV=production\n", encoding="utf-8")


def preinstall_key(project: Path) -> None:
    target = project / ".local/docker-install"
    target.mkdir(parents=True, exist_ok=True)
    (target / "update-signing.pub").write_text(TEST_KEY, encoding="utf-8")


def preinstall_data(project: Path) -> None:
    data = project / "data"
    data.mkdir(exist_ok=True)
    (data / "saas.db").write_bytes(b"synthetic-existing-data")


def prepare_existing_installation(project: Path) -> None:
    preinstall_env(project)
    preinstall_key(project)
    preinstall_data(project)


def run_installer(state: dict, arguments=(), environment=None, prepare=None):
    with tempfile.TemporaryDirectory(prefix="xianyu-docker-install-") as temporary:
        workspace = Path(temporary)
        project = workspace / "xianyu-saas"
        (project / "config").mkdir(parents=True)
        (project / "deploy").mkdir()
        (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (project / "docker-compose.updates.yml").write_text("services: {}\n", encoding="utf-8")
        (project / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (project / "config/saas.env.docker.example").write_text("SAAS_ENV=production\n", encoding="utf-8")
        (project / "deploy/update-signing.pub").write_text(TEST_KEY, encoding="utf-8")
        (project / "deploy/docker-install.sh").write_text(SOURCE, encoding="utf-8")

        fake_bin = workspace / "fake-bin"
        fake_bin.mkdir()
        write_fake_bin(fake_bin)

        state_path = workspace / "docker-state.json"
        state.setdefault("project", "xianyu-saas")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if prepare is not None:
            prepare(project)
        resolved_arguments = arguments(project) if callable(arguments) else arguments

        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["FAKE_DOCKER_STATE"] = str(state_path)
        env.pop("SAAS_UPDATE_PUBLIC_KEY_HOST_FILE", None)
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
        env.update(environment or {})

        process = subprocess.run(
            ["bash", str(project / "deploy/docker-install.sh"), *resolved_arguments],
            cwd=str(project),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        result = json.loads(state_path.read_text(encoding="utf-8"))
        return process, result


def subcommand_of(call: dict) -> str:
    argv = call["argv"]
    if not argv:
        return ""
    if argv[0] != "compose":
        return argv[0]
    index = 1
    while index < len(argv):
        if argv[index] in ("--project-directory", "-p", "--env-file", "-f"):
            index += 2
        else:
            return argv[index]
    return ""


def indexes_of(state: dict, subcommand: str) -> list[int]:
    return [index for index, call in enumerate(state.get("calls", [])) if subcommand_of(call) == subcommand]


def indexes_containing(state: dict, needle: str) -> list[int]:
    return [index for index, call in enumerate(state.get("calls", [])) if needle in " ".join(call["argv"])]


def has_call(state: dict, needle: str) -> bool:
    return bool(indexes_containing(state, needle))


def first_index(state: dict, subcommand: str) -> int:
    found = indexes_of(state, subcommand)
    assert found, "no docker subcommand " + subcommand
    return found[0]


def first_needle(state: dict, needle: str) -> int:
    found = indexes_containing(state, needle)
    assert found, "no docker call containing " + needle
    return found[0]


def compose_options(call: dict) -> list[str]:
    argv = call["argv"]
    options = []
    index = 1
    while index < len(argv):
        value = argv[index]
        if value in ("--project-directory", "-p", "--env-file", "-f"):
            options += argv[index:index + 2]
            index += 2
        else:
            break
    return options


def assert_no_mutation(state) -> None:
    assert not indexes_of(state, "build"), "must not build"
    assert not indexes_of(state, "up"), "must not compose up"
    assert not indexes_of(state, "start"), "must not start containers"
    assert not has_call(state, "target=/seed"), "must not stage the trust key"
    assert not has_call(state, "target=/data"), "must not prepare data"


def assert_fresh_install(process, state) -> None:
    assert process.returncode == 0, process.stderr + process.stdout
    assert "网页升级已就绪" in process.stdout

    preflight = first_index(state, "config")
    build = first_index(state, "build")
    prepare_data = first_needle(state, "target=/data")
    start = first_index(state, "up")
    acceptance = first_needle(state, "update_capabilities")
    assert preflight < build < prepare_data < start < acceptance
    assert not has_call(state, "target=/seed"), "built-in path must not stage a host key"
    assert not has_call(state, "docker_updater.py"), "built-in path must not run sidecar registration"
    assert not has_call(state, "docker-deployment.json")

    build_call = state["calls"][build]
    assert build_call["project"] == "xianyu-saas"
    assert build_call["dirty"] == "unknown"
    assert build_call["commit"] == ""
    assert build_call["env_file"].endswith("config/saas.env")
    assert not state.get("staged")
    assert state.get("prepared_data") is True


def assert_builtin_managed(process, state) -> None:
    assert process.returncode == 0, process.stderr + process.stdout
    assert "网页升级已就绪" in process.stdout
    assert indexes_of(state, "start"), "stopped built-in install must start the existing container"
    assert not indexes_of(state, "up")
    assert not indexes_of(state, "build"), "built-in rerun must not rebuild"
    assert not has_call(state, "target=/seed")
    assert not has_call(state, "target=/data")
    assert not has_call(state, "docker_updater.py")
    assert has_call(state, "update_capabilities")


def assert_managed_ready(process, state) -> None:
    assert process.returncode == 0, process.stderr + process.stdout
    assert "网页升级已就绪" in process.stdout
    assert "只校验并启动既有容器" in process.stdout
    assert_no_mutation(state)
    assert not has_call(state, "docker_updater.py"), "registered rerun must not re-register"
    assert has_call(state, "docker-deployment.json")
    assert has_call(state, "update_capabilities")


def assert_not_ready(process, state) -> None:
    assert process.returncode != 0
    assert "网页升级已就绪" not in process.stdout
    assert "update_compose_config_changed" in process.stderr


def assert_key_conflict(process, state) -> None:
    assert process.returncode != 0
    assert "不一致" in process.stderr
    assert_no_mutation(state)


def behavioral_contract() -> bool:
    if os.name != "posix" or shutil.which("bash") is None:
        return False

    def fresh_state() -> dict:
        return {
            "images": [],
            "volumes": [],
            "project_containers": [],
            "containers_running": False,
            "registered": False,
            "acceptance": "READY",
        }

    def managed_state() -> dict:
        state = fresh_state()
        state.update(
            project_containers=[
                ["xianyu-saas", "app", ""],
                ["xianyu-updater", "updater", ""],
                ["xianyu-updater-task", "task", ""],
                ["xianyu-saas", "app", "", "True"],
            ],
            containers_running=True,
            images=["xianyu-saas-xianyu-saas:managed", "xianyu-saas-xianyu-updater:local"],
            volumes=["xianyu-saas_updater-private"],
            registered=True,
            mounted_key_staged=True,
        )
        return state

    process, state = run_installer(fresh_state())
    assert_fresh_install(process, state)

    # A stopped built-in install starts the existing container without rebuilding
    # or touching keys/data and reports readiness through the same capability.
    def builtin_managed_state() -> dict:
        state = fresh_state()
        state.update(project_containers=[["xianyu-saas", "", ""]], containers_running=True,
                     images=["xianyu-saas:local"])
        return state

    builtin_stopped = builtin_managed_state()
    builtin_stopped["containers_running"] = False
    process, state = run_installer(builtin_stopped, prepare=prepare_existing_installation)
    assert_builtin_managed(process, state)

    # --env-file must also reach the service env_file through SAAS_ENV_FILE.
    def custom_env(project: Path) -> None:
        (project / "config/custom.env").write_text("SAAS_ENV=production\n", encoding="utf-8")

    process, state = run_installer(
        fresh_state(),
        arguments=lambda project: ("--env-file", str(project / "config/custom.env")),
        prepare=custom_env,
    )
    assert process.returncode == 0, process.stderr + process.stdout
    custom_build = state["calls"][first_index(state, "build")]
    assert custom_build["env_file"].endswith("config/custom.env")
    assert str(state["calls"][first_index(state, "build")]["argv"]).find("custom.env") >= 0

    # A rejected capability must never be reported as a successful install.
    not_ready_state = managed_state()
    not_ready_state["acceptance"] = "PENDING:update_compose_config_changed"
    process, state = run_installer(not_ready_state, arguments=("--timeout", "3"),
                                   prepare=prepare_existing_installation)
    assert_not_ready(process, state)

    # Ready managed rerun: no build, no staging, no data change, no recreate,
    # no re-registration; only validation/start of the existing deployment.
    process, state = run_installer(managed_state(), prepare=prepare_existing_installation)
    assert_managed_ready(process, state)

    # Both services emit the same host project label. A web-updated app is
    # subsequently owned by the updater's private Compose working directory.
    updated = managed_state()
    updated["working_dir_projects"] = ["xianyu-saas", "xianyu-saas"]
    updated["project_containers"][0][2] = "/var/lib/xianyu-updater/compose"
    process, state = run_installer(updated, prepare=prepare_existing_installation)
    assert_managed_ready(process, state)

    # Stopped managed installation: docker start preserves the original
    # containers; compose up is never used.
    stopped_state = managed_state()
    stopped_state["containers_running"] = False
    process, state = run_installer(stopped_state, prepare=prepare_existing_installation)
    assert process.returncode == 0, process.stderr + process.stdout
    assert indexes_of(state, "start"), "stopped managed containers must be started via docker start"
    assert not indexes_of(state, "up"), "managed rerun must not recreate containers"
    assert not indexes_of(state, "build")
    assert not has_call(state, "target=/seed") and not has_call(state, "target=/data")

    # Missing app container: explicit failure before any staging or mutation.
    missing_app = managed_state()
    missing_app["project_containers"] = [["xianyu-updater", "updater", ""]]
    process, state = run_installer(missing_app, prepare=prepare_existing_installation)
    assert process.returncode != 0
    assert "缺少应用容器" in process.stderr
    assert_no_mutation(state)

    # Missing trust key on an established install: refused, never restaged.
    def env_and_data(project: Path) -> None:
        preinstall_env(project)
        preinstall_data(project)

    process, state = run_installer(managed_state(), prepare=env_and_data)
    assert process.returncode != 0
    assert "公钥文件缺失" in process.stderr
    assert_no_mutation(state)

    # Empty data on an established install: refused, never replaced.
    def empty_data(project: Path) -> None:
        preinstall_env(project)
        preinstall_key(project)
        (project / "data").mkdir()

    process, state = run_installer(managed_state(), prepare=empty_data)
    assert process.returncode != 0
    assert "数据目录为空" in process.stderr
    assert_no_mutation(state)

    def missing_database(project: Path) -> None:
        prepare_existing_installation(project)
        (project / "data/saas.db").unlink()
        (project / "data/tenants").mkdir()

    process, state = run_installer(managed_state(), prepare=missing_database)
    assert process.returncode != 0 and "数据库缺失" in process.stderr
    assert_no_mutation(state)

    # Established install without an env file: refused, never replaced with defaults.
    def env_only_key_data(project: Path) -> None:
        preinstall_key(project)
        preinstall_data(project)

    process, state = run_installer(managed_state(), prepare=env_only_key_data)
    assert process.returncode != 0
    assert "缺少环境文件" in process.stderr
    assert_no_mutation(state)

    # Established install without registration: refused, never reinitialized.
    unregistered = managed_state()
    unregistered["registered"] = False
    process, state = run_installer(unregistered, prepare=prepare_existing_installation)
    assert process.returncode != 0
    assert "拒绝在既有状态下重新初始化" in process.stderr
    assert not has_call(state, "docker_updater.py")

    # Duplicate managed containers are refused instead of guessing an ID.
    duplicate = managed_state()
    duplicate["project_containers"].append(["xianyu-saas", "app", ""])
    process, state = run_installer(duplicate, prepare=prepare_existing_installation)
    assert process.returncode != 0
    assert "多个应用容器" in process.stderr
    assert_no_mutation(state)

    # Existing unmanaged containers are refused before any mutation.
    unmanaged = fresh_state()
    unmanaged["project_containers"] = [["xianyu-saas", "", ""]]
    unmanaged["volumes"] = ["xianyu-saas_updater-private"]
    process, state = run_installer(unmanaged)
    assert process.returncode != 0
    assert "未接入独立更新器的应用容器" in process.stderr
    assert_no_mutation(state)

    foreign = fresh_state()
    foreign["project_containers"] = [["migrate-service", "", ""]]
    process, state = run_installer(foreign)
    assert process.returncode != 0
    assert "已被服务" in process.stderr
    assert_no_mutation(state)

    # Working directories with spaces are compared without word splitting.
    other_root = fresh_state()
    other_root["project_containers"] = [["xianyu-saas", "app", "/srv/my deployment"], ["xianyu-updater", "updater", "/srv/my deployment"]]
    process, state = run_installer(other_root)
    assert process.returncode != 0
    assert "/srv/my deployment" in process.stderr
    assert_no_mutation(state)

    # Remote Docker contexts are refused before host filesystem assumptions.
    process, state = run_installer(fresh_state(), environment={"DOCKER_HOST": "tcp://remote.example:2375"})
    assert process.returncode != 0
    assert "DOCKER_HOST" in process.stderr
    assert not state.get("calls")

    remote = fresh_state()
    remote["endpoint"] = "ssh://remote.example"
    process, state = run_installer(remote)
    assert process.returncode != 0 and "远程上下文" in process.stderr
    assert_no_mutation(state)

    # A dangling env symlink must not be followed by the config copy.
    def dangling_env(project: Path) -> None:
        (project / "config/saas.env").symlink_to(project / "config/missing.env")

    process, state = run_installer(fresh_state(), prepare=dangling_env)
    assert process.returncode != 0
    assert "符号链接" in process.stderr
    assert_no_mutation(state)

    # A symlinked .local must be rejected before privileged key staging.
    def linked_local(project: Path) -> None:
        (project / ".local").symlink_to(project / "elsewhere")

    process, state = run_installer(fresh_state(), prepare=linked_local)
    assert process.returncode != 0
    assert "符号链接" in process.stderr
    assert_no_mutation(state)

    def dangling_key(project: Path) -> None:
        (project / ".local/docker-install").mkdir(parents=True)
        (project / ".local/docker-install/update-signing.pub").symlink_to(project / "missing-key")

    process, state = run_installer(fresh_state(), prepare=dangling_key)
    assert process.returncode != 0 and "符号链接" in process.stderr
    assert_no_mutation(state)

    # Staged trust key drift is refused before any build or mutation.
    def mismatched_key(project: Path) -> None:
        preinstall_key(project)
        (project / ".local/docker-install/update-signing.pub").write_text("different-key\n", encoding="utf-8")

    process, state = run_installer(fresh_state(), prepare=mismatched_key)
    assert_key_conflict(process, state)

    # Existing sidecars with lost registration fail without reinitializing it.
    busy_state = managed_state()
    busy_state["registered"] = False
    busy_state["initialize_busy_forever"] = True
    process, state = run_installer(busy_state, arguments=("--timeout", "2"),
                                   prepare=prepare_existing_installation)
    assert process.returncode != 0
    assert "缺少更新器登记" in process.stderr, process.stderr + process.stdout
    assert not indexes_containing(state, "docker_updater.py")
    assert "网页升级已就绪" not in process.stdout

    busy_once = managed_state()
    busy_once["registered"] = False
    busy_once["initialize_busy_once"] = True
    process, state = run_installer(busy_once, prepare=prepare_existing_installation)
    assert process.returncode != 0, process.stderr + process.stdout
    assert not indexes_containing(state, "docker_updater.py")
    assert state.get("registered") is False

    # Build provenance reports the real repository state and never claims a
    # clean release for a dirty tree.
    process, state = run_installer(fresh_state(), environment={"FAKE_GIT": "clean"})
    assert process.returncode == 0, process.stderr + process.stdout
    assert state["calls"][first_index(state, "build")]["dirty"] == "false"
    assert state["calls"][first_index(state, "build")]["commit"] == "a" * 40

    process, state = run_installer(fresh_state(), environment={"FAKE_GIT": "dirty"})
    assert process.returncode == 0, process.stderr + process.stdout
    assert state["calls"][first_index(state, "build")]["dirty"] == "true"

    # The acceptance loop waits for readiness instead of failing on the first
    # pending capability.
    transition_state = fresh_state()
    transition_state["acceptance_sequence"] = ["PENDING:update_initializing", "READY"]
    process, state = run_installer(transition_state, arguments=("--timeout", "30"))
    assert process.returncode == 0, process.stderr + process.stdout
    assert len(indexes_containing(state, "update_capabilities")) >= 2

    return True


def main() -> None:
    assert_static_contract()
    if behavioral_contract():
        print("docker-install contract: static entry-point guards and fake-docker install scenarios passed")
    else:
        print("docker-install contract: static entry-point guards passed (behavioral scenarios skipped: POSIX bash unavailable)")


if __name__ == "__main__":
    main()

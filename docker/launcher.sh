#!/usr/bin/env bash
# Built-in file-update supervisor launcher.
#
# Owns the application processes (API, consumer and optionally the Docker web
# server), serves a heartbeat to the application, and consumes signed switch
# requests from the writable code store: stop -> atomically switch the code
# directory -> start -> HTTP health + target version check -> roll back and
# restart the previous code on failure.  Business data and the trusted public
# key are never inside the writable store.
#
# Environment contract (set by docker/entrypoint.sh or the systemd unit):
#   SAAS_APP_CODE_DIR           writable code store (required)
#   SAAS_LAUNCH_BASE_ROOT       immutable base with backend/ worker/ frontend/
#   SAAS_LAUNCH_PYTHON          API/consumer interpreter
#   SAAS_LAUNCH_PYTHONPATH_EXTRA  immutable site-packages directories (optional)
#   SAAS_LAUNCH_WORKER_PYTHON   worker interpreter (defaults to launch python)
#   SAAS_LAUNCH_WORKER_PYTHONPATH_EXTRA  worker site-packages (optional)
#   SAAS_LAUNCH_API_PORT / SAAS_LAUNCH_WEB_PORT
#   SAAS_LAUNCH_WEB             1 to run the bundled Node web server
#   SAAS_LAUNCH_CONSUMER        1 to run the job consumer
#   SAAS_LAUNCH_CONFLICT_UNITS  space separated systemd units that must be inactive
#   SAAS_LAUNCH_API_ARGS        extra uvicorn arguments
set -Eeuo pipefail

code_store="${SAAS_APP_CODE_DIR:?SAAS_APP_CODE_DIR is required}"
base_root="${SAAS_LAUNCH_BASE_ROOT:-/app}"
launch_python="${SAAS_LAUNCH_PYTHON:-$base_root/backend/.venv/bin/python}"
worker_python="${SAAS_LAUNCH_WORKER_PYTHON:-$launch_python}"
api_port="${SAAS_LAUNCH_API_PORT:-8096}"
web_port="${SAAS_LAUNCH_WEB_PORT:-4173}"
run_web="${SAAS_LAUNCH_WEB:-0}"
run_consumer="${SAAS_LAUNCH_CONSUMER:-1}"
pythonpath_extra="${SAAS_LAUNCH_PYTHONPATH_EXTRA:-}"
worker_pythonpath_extra="${SAAS_LAUNCH_WORKER_PYTHONPATH_EXTRA:-}"
conflict_units="${SAAS_LAUNCH_CONFLICT_UNITS:-}"
api_args="${SAAS_LAUNCH_API_ARGS:---proxy-headers --forwarded-allow-ips=127.0.0.1 --limit-concurrency 100 --backlog 128}"
health_timeout="${SAAS_LAUNCH_HEALTH_TIMEOUT:-180}"

heartbeat="$code_store/launcher.json"
request_file="$code_store/switch.json"
result_file="$code_store/result.json"
state_file="$code_store/switch-state.json"

umask 077
install -d -m 0700 "$code_store" "$code_store/releases"

code_root="$base_root"
active_version=""
pids=()
names=()
switching=0

join_path() {
  if [[ -n "$2" ]]; then
    printf '%s:%s' "$1" "$2"
  else
    printf '%s' "$1"
  fi
}

resolve_code_root() {
  local link="$code_store/current"
  if [[ -L "$link" && -f "$link/backend/app.py" && -f "$link/frontend/index.html" ]]; then
    code_root="$(cd "$link" && pwd -P)"
  else
    code_root="$base_root"
  fi
}

code_version() {
  local root="$1"
  (cd "$root"; PYTHONPATH="$(join_path "$root/backend" "$pythonpath_extra")" "$launch_python" -c \
    'import version; print(version.VERSION)') 2>/dev/null || printf 'unknown'
}

write_heartbeat() {
  local ready="$1" reason="$2"
  printf '{"schema":1,"pid":%d,"code_root":"%s","active_version":"%s","ready":%s,"reason":"%s","heartbeat_at":%d}\n' \
    "$$" "$code_root" "$active_version" "$ready" "$reason" "$(date +%s)" > "$heartbeat.tmp"
  mv -f "$heartbeat.tmp" "$heartbeat"
}

write_result() {
  local operation_id="$1" action="$2" version="$3" status="$4" error_code="$5"
  "$launch_python" - "$result_file" "$operation_id" "$action" "$version" "$status" "$error_code" "$active_version" <<'PY'
import json, os, sys, time

target, operation_id, action, version, status, error_code, current = sys.argv[1:8]
payload = {
    "schema": 1, "operation_id": operation_id, "action": action, "version": version,
    "current_version": current, "status": status, "phase": status,
    "error_code": error_code, "updated_at": time.time(),
}
temporary = target + ".tmp"
with open(temporary, "w", encoding="utf-8") as stream:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
os.replace(temporary, target)
PY
  rm -f "$request_file"
  rm -f "$state_file"
}

recover_pending_switch() {
  [[ -f "$state_file" ]] || return 0
  local previous version operation_id action
  local -a recovered
  mapfile -t recovered < <("$launch_python" - "$state_file" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    payload = {}
for key in ("previous", "version", "operation_id", "action"):
    print(payload.get(key, ""))
PY
)
  previous="${recovered[0]:-}"
  version="${recovered[1]:-}"
  operation_id="${recovered[2]:-}"
  action="${recovered[3]:-}"
  if [[ -n "$previous" ]]; then
    local temporary="$code_store/.current.recover"
    rm -f "$temporary"
    ln -s "$previous" "$temporary"
    mv -T "$temporary" "$code_store/current"
  else
    rm -f "$code_store/current"
  fi
  resolve_code_root
  active_version="$(code_version "$code_root")"
  if [[ -n "$operation_id" ]]; then
    write_result "$operation_id" "${action:-apply}" "$version" "rolled_back" "update_interrupted"
  else
    clear_state
  fi
}

start() {
  local name="$1"
  shift
  "$@" &
  pids+=("$!")
  names+=("$name")
}

stop_all() {
  local pid
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  local deadline=$(( $(date +%s) + 60 ))
  for pid in "${pids[@]:-}"; do
    while kill -0 "$pid" 2>/dev/null; do
      if (( $(date +%s) >= deadline )); then
        kill -KILL "$pid" 2>/dev/null || true
        break
      fi
      sleep 0.5
    done
    wait "$pid" 2>/dev/null || true
  done
  pids=()
  names=()
}

start_children() {
  local root="$1"
  local backend_path worker_path
  backend_path="$(join_path "$root/backend" "$pythonpath_extra")"
  worker_path="$(join_path "$root/worker" "$worker_pythonpath_extra")"
  # Native systemd starts in the base backend directory; it must not shadow
  # the new release's modules when Python resolves its current directory.
  cd "$root"
  start api env PYTHONPATH="$backend_path" SAAS_CURRENT_ROOT="$root" \
    SAAS_BOT_ROOT="$root/worker" SAAS_BOT_PYTHON="$worker_python" SAAS_BOT_PYTHONPATH="$worker_path" \
    "$launch_python" -m uvicorn app:app --app-dir "$root/backend" --host 0.0.0.0 --port "$api_port" $api_args
  if [[ "$run_consumer" == "1" ]]; then
    start consumer env PYTHONPATH="$backend_path" SAAS_CURRENT_ROOT="$root" \
      SAAS_BOT_ROOT="$root/worker" SAAS_BOT_PYTHON="$worker_python" SAAS_BOT_PYTHONPATH="$worker_path" \
      "$launch_python" -m job_consumer
  fi
  if [[ "$run_web" == "1" ]]; then
    start web env SAAS_DEV_API_ORIGIN="http://127.0.0.1:$api_port" SAAS_DEV_WEB_PORT="$web_port" \
      SAAS_DEV_WEB_HOST=0.0.0.0 SAAS_DEV_WEB_ROOT="$root" \
      node "$base_root/scripts/dev-server.mjs"
  fi
}

all_alive() {
  local pid
  for pid in "${pids[@]:-}"; do
    kill -0 "$pid" 2>/dev/null || return 1
  done
  return 0
}

port_free() {
  "$launch_python" - "$api_port" <<'PY'
import socket, sys
probe = socket.socket()
probe.settimeout(0.5)
try:
    probe.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)
finally:
    probe.close()
sys.exit(1)
PY
}

health_ready() {
  local version="$1" deadline=$(( $(date +%s) + health_timeout ))
  while (( $(date +%s) < deadline )); do
    if all_alive && "$launch_python" - "$api_port" "$version" <<'PY'
import json, sys, urllib.request

port, version = sys.argv[1], sys.argv[2]
def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.loads(response.read())
assert get("/health").get("ok") is True
assert get("/api/ready").get("database") == "ready"
assert get("/api/version/public").get("version") == version
PY
    then
      return 0
    fi
    sleep 2
  done
  return 1
}

write_state() {
  local operation_id="$1" action="$2" version="$3" previous="$4" phase="$5"
  printf '{"schema":1,"operation_id":"%s","action":"%s","version":"%s","previous":"%s","phase":"%s","pid":%d,"updated_at":%d}\n' \
    "$operation_id" "$action" "$version" "$previous" "$phase" "$$" "$(date +%s)" > "$state_file.tmp"
  mv -f "$state_file.tmp" "$state_file"
}

clear_state() {
  rm -f "$state_file"
}

conflict_check() {
  local unit
  command -v systemctl >/dev/null 2>&1 || return 0
  for unit in $conflict_units; do
    if systemctl is-active --quiet "$unit"; then
      return 1
    fi
  done
  return 0
}

snapshot_previous() {
  local link="$code_store/current"
  previous_target=""
  if [[ -L "$link" ]]; then
    previous_target="$(readlink "$link" || true)"
  fi
}

switch_to() {
  local version="$1"
  local temporary="$code_store/.current.switch"
  rm -f "$temporary"
  ln -s "releases/$version" "$temporary"
  mv -T "$temporary" "$code_store/current"
}

restore_previous() {
  if [[ -n "$previous_target" ]]; then
    local temporary="$code_store/.current.restore"
    rm -f "$temporary"
    ln -s "$previous_target" "$temporary"
    mv -T "$temporary" "$code_store/current"
  else
    rm -f "$code_store/current"
  fi
}

process_switch() {
  [[ -f "$request_file" ]] || return 0
  local operation_id action version candidate error_code
  read -r operation_id action version candidate < <("$launch_python" - "$request_file" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    payload = {}
print(payload.get("operation_id", ""), payload.get("action", ""), payload.get("version", ""), payload.get("candidate_path", ""))
PY
)
  if [[ -z "$operation_id" || -z "$version" ]]; then
    rm -f "$request_file"
    return 0
  fi
  local target="$code_store/releases/$version"
  if [[ "$action" == "apply" && ( ! -d "$target" || -L "$target" || ! -f "$target/backend/app.py" || ! -f "$target/frontend/index.html" ) ]]; then
    write_result "$operation_id" "$action" "$version" "failed" "update_candidate_invalid"
    return 0
  fi
  resolve_code_root
  if [[ "$action" == "apply" && "$active_version" == "$version" ]]; then
    write_result "$operation_id" "$action" "$version" "succeeded" ""
    return 0
  fi
  snapshot_previous
  switching=1
  write_state "$operation_id" "$action" "$version" "$previous_target" "stopping"
  write_heartbeat false "update_switching"
  stop_all
  local deadline=$(( $(date +%s) + 60 ))
  while ! port_free; do
    if (( $(date +%s) >= deadline )); then
      start_children "$code_root"
      switching=0
      active_version="$(code_version "$code_root")"
      write_heartbeat true ""
      write_result "$operation_id" "$action" "$version" "failed" "update_port_busy"
      return 0
    fi
    sleep 1
  done
  switch_to "$version"
  write_state "$operation_id" "$action" "$version" "$previous_target" "switched"
  resolve_code_root
  start_children "$code_root"
  if health_ready "$version"; then
    active_version="$version"
    switching=0
    write_heartbeat true ""
    write_result "$operation_id" "$action" "$version" "succeeded" ""
  else
    error_code="update_verification_failed"
    stop_all
    restore_previous
    resolve_code_root
    start_children "$code_root"
    if ! health_ready "$active_version"; then
      write_heartbeat false "update_rollback_failed"
      write_result "$operation_id" "$action" "$version" "failed" "update_rollback_failed"
      stop_all
      exit 1
    fi
    active_version="$(code_version "$code_root")"
    switching=0
    write_heartbeat true ""
    write_result "$operation_id" "$action" "$version" "rolled_back" "$error_code"
  fi
}

recover_pending_switch
resolve_code_root
if ! conflict_check; then
  write_heartbeat false "update_launcher_conflict"
  printf 'file-updater launcher: conflicting legacy services are active\n' >&2
  exit 1
fi
active_version="$(code_version "$code_root")"
start_children "$code_root"
if ! health_ready "$active_version"; then
  write_heartbeat false "update_launcher_unhealthy"
  printf 'file-updater launcher: application did not become ready\n' >&2
  stop_all
  exit 1
fi
write_heartbeat true ""
process_switch

trap 'printf "\n[launcher] stopping\n"; stop_all; exit 0' INT TERM

while :; do
  if [[ -f "$request_file" ]]; then
    process_switch
    continue
  fi
  write_heartbeat true ""
  if [[ "$switching" == "0" ]]; then
    for index in "${!pids[@]}"; do
      if ! kill -0 "${pids[$index]}" 2>/dev/null; then
        status=0
        wait "${pids[$index]}" || status=$?
        printf '[launcher] %s exited with %s\n' "${names[$index]}" "$status" >&2
        stop_all
        exit "$status"
      fi
    done
  fi
  sleep 2
done

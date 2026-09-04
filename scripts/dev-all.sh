#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -f config/saas.env ]]; then
  printf '%s\n' '缺少 config/saas.env，请先运行 scripts/bootstrap-dev.sh。' >&2
  exit 1
fi

for required in node setsid; do
  if ! command -v "$required" >/dev/null 2>&1; then
    printf '缺少命令：%s\n' "$required" >&2
    exit 1
  fi
done

for required_file in backend/.venv/bin/uvicorn backend/.venv/bin/python; do
  if [[ ! -x "$required_file" ]]; then
    printf '缺少可执行文件：%s\n' "$required_file" >&2
    printf '%s\n' '请先运行 scripts/bootstrap-dev.sh。' >&2
    exit 1
  fi
done

set -a
source config/saas.env
set +a

api_port="${SAAS_DEV_API_PORT:-8096}"
web_port="${SAAS_DEV_WEB_PORT:-4173}"
run_id="$(date +%Y%m%d-%H%M%S)"
log_dir="$project_root/.local/dev-logs/$run_id"
umask 077
mkdir -p "$log_dir"
install -d -m 0700 .local .local/tenants

service_names=()
service_pids=()
output_pids=()

start_service() {
  local name="$1"
  local log_file="$log_dir/$name.log"
  shift
  : > "$log_file"
  service_names+=("$name")
  printf '[dev] 启动 %-8s 日志：%s\n' "$name" "$log_file"
  setsid --wait "$@" >"$log_file" 2>&1 &
  service_pids+=("$!")
  tail -n 0 -F "$log_file" 2>/dev/null | while IFS= read -r line; do
    printf '[%s] %s\n' "$name" "$line"
  done &
  output_pids+=("$!")
}

stop_services() {
  local pid
  for pid in "${service_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${output_pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${service_pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  for pid in "${output_pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  printf '\n[dev] 正在停止 API、consumer 和 Web...\n'
  stop_services
  printf '[dev] 日志已保留：%s\n' "$log_dir"
  exit "$status"
}
trap cleanup EXIT INT TERM

start_service api ./scripts/dev-api.sh
start_service consumer ./scripts/dev-consumer.sh
start_service web env SAAS_DEV_API_ORIGIN="http://127.0.0.1:$api_port" SAAS_DEV_WEB_PORT="$web_port" node scripts/dev-server.mjs

printf '\n'
printf '%s\n' '[dev] xianyu-saas 本地开发环境已启动'
printf '      工作台：http://127.0.0.1:%s/xianyu-saas/\n' "$web_port"
printf '      API 健康检查：http://127.0.0.1:%s/health\n' "$api_port"
printf '      consumer：后台运行（无 HTTP 地址）\n'
printf '      日志目录：%s\n' "$log_dir"
printf '%s\n' '[dev] 按 Ctrl+C 会同时停止全部服务。'
printf '\n'

while :; do
  for index in "${!service_pids[@]}"; do
    if ! kill -0 "${service_pids[$index]}" 2>/dev/null; then
      status=0
      wait "${service_pids[$index]}" || status=$?
      printf '[dev] %s 已退出，状态码：%s\n' "${service_names[$index]}" "$status" >&2
      exit "$status"
    fi
  done
  sleep 0.5
done

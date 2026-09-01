#!/usr/bin/env bash
# 容器入口：按需生成 AI 主密钥，然后并行启动控制面 API、任务消费者与静态工作台。
# 任一服务退出即整体退出，交由容器编排重启。
set -Eeuo pipefail

app_root="${SAAS_APP_ROOT:-/app}"
cd "$app_root"

api_port="${SAAS_DEV_API_PORT:-8096}"
web_port="${SAAS_DEV_WEB_PORT:-4173}"
state_dir="$(dirname "${SAAS_DB:-/data/saas.db}")"

umask 077
install -d -m 0700 "$state_dir" "${SAAS_TENANTS_DIR:-/data/tenants}"

# 生产部署应显式提供 SAAS_AI_MASTER_KEY；此处仅为容器首次启动兜底，
# 密钥落在数据卷内，重建容器不会作废已加密的模型凭据。
if [[ -z "${SAAS_AI_MASTER_KEY:-}" ]]; then
  key_file="$state_dir/ai-master-key"
  SAAS_AI_MASTER_KEY="$(PYTHONPATH="$app_root/backend" backend/.venv/bin/python - \
    "$key_file" "${SAAS_TENANTS_DIR:-/data/tenants}" <<'PY'
import sys
from pathlib import Path

from ai_customer_service import AIServiceError, ensure_development_master_key

try:
    print(ensure_development_master_key(Path(sys.argv[1]), Path(sys.argv[2])), end="")
except AIServiceError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
PY
  )"
  export SAAS_AI_MASTER_KEY
fi

pids=()
names=()

start() {
  local name="$1"
  shift
  "$@" &
  pids+=("$!")
  names+=("$name")
  printf '[%s] 已启动\n' "$name"
}

stop_all() {
  local pid
  for pid in "${pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap 'printf "\n[entrypoint] 收到停止信号\n"; stop_all; exit 0' INT TERM

start api env PYTHONPATH="$app_root/backend" \
  backend/.venv/bin/python -m uvicorn app:app \
    --app-dir backend --host 0.0.0.0 --port "$api_port"

start consumer env PYTHONPATH="$app_root/backend" \
  backend/.venv/bin/python -m job_consumer

start web env SAAS_DEV_API_ORIGIN="http://127.0.0.1:$api_port" \
  SAAS_DEV_WEB_PORT="$web_port" SAAS_DEV_WEB_HOST=0.0.0.0 \
  node scripts/dev-server.mjs

printf '[entrypoint] 工作台 :%s，API :%s\n' "$web_port" "$api_port"

while :; do
  for index in "${!pids[@]}"; do
    if ! kill -0 "${pids[$index]}" 2>/dev/null; then
      status=0
      wait "${pids[$index]}" || status=$?
      printf '[entrypoint] %s 已退出，状态码 %s\n' "${names[$index]}" "$status" >&2
      stop_all
      exit "$status"
    fi
  done
  sleep 1
done

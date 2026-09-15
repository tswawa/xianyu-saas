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

# 内置文件更新器：入口只准备主密钥，进程监督与应用版本切换交给随镜像分发的
# launcher。代码存储在可写数据卷内，镜像 /app 的虚拟环境与公钥保持不可变。
export SAAS_APP_CODE_DIR="${SAAS_APP_CODE_DIR:-/data/app-code}"
export SAAS_LAUNCH_BASE_ROOT="$app_root"
export SAAS_LAUNCH_PYTHON="$app_root/backend/.venv/bin/python"
export SAAS_LAUNCH_WORKER_PYTHON="$app_root/worker/.venv/bin/python"
export SAAS_LAUNCH_API_PORT="$api_port"
export SAAS_LAUNCH_WEB_PORT="$web_port"
export SAAS_LAUNCH_WEB=1
export SAAS_LAUNCH_CONSUMER=1

printf '[entrypoint] launcher: 工作台 :%s，API :%s，代码存储 %s\n' "$web_port" "$api_port" "$SAAS_APP_CODE_DIR"
exec "$app_root/docker/launcher.sh"

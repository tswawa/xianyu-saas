#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -f config/saas.env ]]; then
  echo "缺少 config/saas.env，请先运行 scripts/bootstrap-dev.sh。" >&2
  exit 1
fi

set -a
source config/saas.env
set +a

install -d -m 0700 .local .local/tenants
if [[ -z "${SAAS_AI_MASTER_KEY:-}" ]]; then
  SAAS_AI_MASTER_KEY="$(PYTHONPATH="$project_root/backend${PYTHONPATH:+:$PYTHONPATH}" \
    backend/.venv/bin/python - "$project_root/.local/ai-master-key" "$project_root/.local/tenants" <<'PY'
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
exec backend/.venv/bin/python -m uvicorn app:app \
  --app-dir backend \
  --host 127.0.0.1 \
  --port "${SAAS_DEV_API_PORT:-8096}" \
  --reload

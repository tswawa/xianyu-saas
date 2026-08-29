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
PYTHONPATH="$project_root/backend${PYTHONPATH:+:$PYTHONPATH}" \
  exec backend/.venv/bin/python -m job_consumer

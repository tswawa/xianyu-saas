#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root/worker"

if [[ ! -f .env ]]; then
  echo "缺少 worker/.env，请先运行 scripts/bootstrap-dev.sh。" >&2
  exit 1
fi

set -a
source .env
set +a

install -d -m 0700 runtime-data
exec .venv/bin/python main.py

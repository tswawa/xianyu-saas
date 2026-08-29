#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -x backend/.venv/bin/python || ! -x worker/.venv/bin/python ]]; then
  echo "缺少 Python 虚拟环境，请先运行 scripts/bootstrap-dev.sh。" >&2
  exit 1
fi
if [[ ! -d node_modules/playwright ]]; then
  echo "缺少 Node 依赖，请先运行 npm ci。" >&2
  exit 1
fi

npm run test:repository
npm run test:syntax
npm run test:api
npm run test:p0
npm run test:ai
npm run test:consumer
npm run test:async
npm run test:recovery
npm run test:auto-worker
npm run test:api-lock
npm run test:runtime-cas
npm run test:storage
npm run test:isolation
npm run test:inbox
npm run test:manual-reply
npm run test:analytics
npm run test:product-batch
npm run test:templates-cards
npm run test:deploy
npm run test:connector
npm run test:worker
npm run test:ui

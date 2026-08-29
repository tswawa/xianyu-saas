#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

for command in python3 node npm; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少命令: $command" >&2
    exit 1
  fi
done

python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install --disable-pip-version-check -r backend/requirements-dev.txt

python3 -m venv worker/.venv
worker/.venv/bin/python -m pip install --disable-pip-version-check -r worker/requirements.txt

npm ci --ignore-scripts

install -d -m 0700 .local .local/tenants worker/runtime-data
if [[ ! -f config/saas.env ]]; then
  install -m 0600 config/saas.env.example config/saas.env
fi
if [[ ! -f worker/.env ]]; then
  install -m 0600 worker/.env.example worker/.env
fi

echo "开发依赖已安装。先编辑 config/saas.env 和 worker/.env，再按 docs/NEW_UBUNTU_HANDOFF.md 启动。"
echo "首次运行 UI 测试前执行: npx playwright install --with-deps chromium"

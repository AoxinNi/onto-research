#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' '请先创建 .venv 并安装项目依赖。' >&2
  exit 1
fi
exec .venv/bin/python -m workbench.server

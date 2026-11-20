#!/usr/bin/env bash
# The local workbench in a browser tab: the same server, address and data as the desktop app, so both show the
# same projects.
#
#   ./start-workbench.sh             start it and open http://127.0.0.1:8766 (just open it when already running,
#                                    e.g. started by the desktop app)
#   WORKBENCH_OPEN=0 ./start-workbench.sh   do not open a browser
#
# Data lives in the app's data folder (macOS: ~/Library/Application Support/onto-research; ONTO_DATA_DIR moves it):
# the embedded PostgreSQL in pg/, data source keys in .env, project snapshots in projects/. To use another database,
# set DATABASE_URL in the environment (or ENV_FILE=.env to read it from this checkout's .env).
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [[ ! -x $PY ]]; then
  printf '%s\n' "请先创建 .venv 并安装项目依赖：python3 -m venv .venv && .venv/bin/python -m pip install -e '.[local,dev]'" >&2
  exit 1
fi

HOST=${WORKBENCH_HOST:-127.0.0.1}
PORT=${WORKBENCH_PORT:-8766}
URL="http://$HOST:$PORT/"

open_page() {
  [[ ${WORKBENCH_OPEN:-1} == 1 ]] || return 0
  if command -v open >/dev/null; then open "$URL"; else printf '打开 %s\n' "$URL"; fi
}

if curl -sf -o /dev/null --max-time 2 "$URL"; then
  printf '工作台已在运行：%s\n' "$URL"
  open_page
  exit 0
fi

export WORKBENCH_HOST=$HOST WORKBENCH_PORT=$PORT
export WORKBENCH_SNAPSHOTS=${WORKBENCH_SNAPSHOTS:-1}
if [[ -z ${DATABASE_URL:-} ]]; then
  export ONTO_LOCAL_DB=${ONTO_LOCAL_DB:-1}
fi
if [[ -z ${ENV_FILE:-} ]]; then
  ENV_FILE=$("$PY" -c 'from ontology.localdb import data_dir; print(data_dir() / ".env")')
  export ENV_FILE
fi

# Open the page once the server answers (the first start creates the database, 10–20 seconds).
(
  for _ in $(seq 1 180); do
    if curl -sf -o /dev/null --max-time 1 "$URL"; then open_page; exit 0; fi
    sleep 0.5
  done
) &

printf '工作台：%s（数据：%s）\n' "$URL" "$(dirname "$ENV_FILE")"
exec "$PY" -m workbench.server

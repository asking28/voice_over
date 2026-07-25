#!/usr/bin/env bash
# Start the revoice web app on http://127.0.0.1:$REVOICE_PORT (default 8010).
set -euo pipefail
cd "$(dirname "$0")"

command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg not found — brew install ffmpeg" >&2; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "creating .venv …"
  uv venv --python 3.13
  uv pip install -q fastapi "uvicorn[standard]" python-dotenv
fi

PORT="${REVOICE_PORT:-8010}"
echo "revoice → http://127.0.0.1:${PORT}"
exec .venv/bin/python -m uvicorn revoice.server:app --host 127.0.0.1 --port "${PORT}" "$@"

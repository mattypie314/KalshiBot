#!/usr/bin/env bash
# Start the original campaign desk + scheduler on this Pi.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$HOME/.kalshi/env" ]]; then
  set -a
  # ~/.kalshi/env uses `export KEY=value`. Do not feed it to systemd EnvironmentFile.
  # shellcheck disable=SC1091
  source "$HOME/.kalshi/env"
  set +a
fi

export KALSHI_LIVE="${KALSHI_LIVE:-1}"
export PYTHONUNBUFFERED=1
exec "$ROOT/.venv/bin/python" -m kalshibot serve --host 0.0.0.0 --port 8000

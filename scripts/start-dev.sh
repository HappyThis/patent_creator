#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
BACKEND_DIR="${REPO_ROOT}/backend"
FRONTEND_DIR="${REPO_ROOT}/frontend"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  source "${REPO_ROOT}/.env"
  set +a
fi

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
VITE_API_BASE_URL="${VITE_API_BASE_URL:-http://${BACKEND_HOST}:${BACKEND_PORT}}"

BACKEND_PYTHON="${BACKEND_DIR}/.venv/bin/python"

if [[ ! -x "${BACKEND_PYTHON}" ]]; then
  echo "Backend Python not found: ${BACKEND_PYTHON}"
  echo "Please set up the backend virtualenv first."
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is not installed or not in PATH."
  exit 1
fi

backend_pid=""
frontend_pid=""

cleanup() {
  local exit_code=$?

  if [[ -n "${backend_pid}" ]] && kill -0 "${backend_pid}" >/dev/null 2>&1; then
    kill "${backend_pid}" >/dev/null 2>&1 || true
  fi

  if [[ -n "${frontend_pid}" ]] && kill -0 "${frontend_pid}" >/dev/null 2>&1; then
    kill "${frontend_pid}" >/dev/null 2>&1 || true
  fi

  wait >/dev/null 2>&1 || true
  exit "${exit_code}"
}

trap cleanup INT TERM EXIT

echo "Starting backend on http://${BACKEND_HOST}:${BACKEND_PORT}"
(
  cd "${BACKEND_DIR}"
  exec "${BACKEND_PYTHON}" -m uvicorn app.api.app:app --reload --host "${BACKEND_HOST}" --port "${BACKEND_PORT}"
) &
backend_pid=$!

echo "Starting frontend on http://${FRONTEND_HOST}:${FRONTEND_PORT}"
(
  cd "${FRONTEND_DIR}"
  exec npm run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}"
) &
frontend_pid=$!

echo ""
echo "Patent Creator dev stack is starting..."
echo "Frontend: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Press Ctrl+C to stop both processes."
echo ""

while true; do
  if ! kill -0 "${backend_pid}" >/dev/null 2>&1; then
    echo "Backend process exited."
    exit 1
  fi

  if ! kill -0 "${frontend_pid}" >/dev/null 2>&1; then
    echo "Frontend process exited."
    exit 1
  fi

  sleep 1
done

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
PATENT_CREATOR_CORS_ALLOW_ORIGINS="${PATENT_CREATOR_CORS_ALLOW_ORIGINS:-http://${FRONTEND_HOST}:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}}"
PATENT_CREATOR_DRAWIO_EMBED_URL="${PATENT_CREATOR_DRAWIO_EMBED_URL:-http://127.0.0.1:8081/}"
export VITE_API_BASE_URL
export PATENT_CREATOR_CORS_ALLOW_ORIGINS
export PATENT_CREATOR_DRAWIO_EMBED_URL

BACKEND_PYTHON="${BACKEND_DIR}/.venv/bin/python"
DRAWIO_COMPOSE_FILE="${REPO_ROOT}/compose.drawio.yaml"
DRAWIO_LOCAL_URL="http://127.0.0.1:8081/"

prepend_path() {
  local dir="$1"

  if [[ -d "${dir}" ]] && [[ ":${PATH}:" != *":${dir}:"* ]]; then
    export PATH="${dir}:${PATH}"
  fi
}

load_nvm() {
  export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"

  if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
    # shellcheck disable=SC1091
    source "${NVM_DIR}/nvm.sh"
  fi
}

install_uv_if_missing() {
  prepend_path "${HOME}/.local/bin"
  prepend_path "${HOME}/.cargo/bin"

  if command -v uv >/dev/null 2>&1; then
    return
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "uv is not installed and curl is not available to install it."
    exit 1
  fi

  echo "uv not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  prepend_path "${HOME}/.local/bin"
  prepend_path "${HOME}/.cargo/bin"

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installation completed, but uv is still not available in PATH."
    echo "Open a new terminal or add ~/.local/bin to PATH, then run this script again."
    exit 1
  fi
}

install_node_if_missing() {
  load_nvm

  if command -v npm >/dev/null 2>&1; then
    return
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "npm is not installed and curl is not available to install Node.js."
    exit 1
  fi

  if [[ ! -s "${NVM_DIR}/nvm.sh" ]]; then
    echo "npm not found. Installing nvm..."
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
  fi

  load_nvm

  if ! command -v nvm >/dev/null 2>&1; then
    echo "nvm installation completed, but nvm is not available in this shell."
    echo "Open a new terminal, then run this script again."
    exit 1
  fi

  echo "Installing Node.js LTS..."
  nvm install --lts
  nvm use --lts

  if ! command -v npm >/dev/null 2>&1; then
    echo "Node.js installation completed, but npm is still not available in PATH."
    exit 1
  fi
}

sync_backend() {
  install_uv_if_missing

  echo "Syncing backend dependencies..."
  (
    cd "${BACKEND_DIR}"
    uv sync
  )

  if [[ ! -x "${BACKEND_PYTHON}" ]]; then
    echo "Backend Python not found after uv sync: ${BACKEND_PYTHON}"
    exit 1
  fi
}

sync_frontend() {
  install_node_if_missing

  local node_modules="${FRONTEND_DIR}/node_modules"
  local package_json="${FRONTEND_DIR}/package.json"
  local package_lock="${FRONTEND_DIR}/package-lock.json"

  if [[ ! -d "${node_modules}" ]] || [[ "${package_json}" -nt "${node_modules}" ]] || [[ "${package_lock}" -nt "${node_modules}" ]]; then
    echo "Installing frontend dependencies..."
    (
      cd "${FRONTEND_DIR}"
      npm install
    )
  fi
}

install_playwright_chromium() {
  echo "Ensuring Playwright Chromium is installed..."
  (
    cd "${FRONTEND_DIR}"
    npm exec -- playwright install chromium
  )
}

uses_bundled_drawio() {
  case "${PATENT_CREATOR_DRAWIO_EMBED_URL}" in
    "http://127.0.0.1:8081"|"http://127.0.0.1:8081/"*|"http://127.0.0.1:8081?"*|\
    "http://localhost:8081"|"http://localhost:8081/"*|"http://localhost:8081?"*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

drawio_is_ready() {
  "${BACKEND_PYTHON}" -c '
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    if response.status >= 400:
        raise SystemExit(1)
' "${DRAWIO_LOCAL_URL}" >/dev/null 2>&1
}

docker_daemon_is_ready() {
  "${BACKEND_PYTHON}" -c '
import subprocess

try:
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)

raise SystemExit(result.returncode)
' >/dev/null 2>&1
}

start_drawio() {
  if ! uses_bundled_drawio; then
    echo "Using externally managed Draw.io: ${PATENT_CREATOR_DRAWIO_EMBED_URL}"
    return
  fi

  if drawio_is_ready; then
    echo "Draw.io is already running on ${DRAWIO_LOCAL_URL}"
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required to start the local Draw.io service."
    echo "Install and start Docker Desktop, then run this script again."
    exit 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required to start the local Draw.io service."
    exit 1
  fi

  if ! docker_daemon_is_ready; then
    echo "Docker is installed, but the Docker daemon is not running."
    echo "Start Docker Desktop, then run this script again."
    exit 1
  fi

  echo "Starting local Draw.io on ${DRAWIO_LOCAL_URL}"
  if ! docker compose -f "${DRAWIO_COMPOSE_FILE}" up -d; then
    echo "Failed to start the local Draw.io service."
    exit 1
  fi

  local attempt
  for attempt in {1..60}; do
    if drawio_is_ready; then
      echo "Draw.io is ready."
      return
    fi
    sleep 1
  done

  docker compose -f "${DRAWIO_COMPOSE_FILE}" ps || true
  echo "Draw.io did not become ready within 60 seconds."
  exit 1
}

sync_backend
start_drawio
sync_frontend
install_playwright_chromium
echo "Checking Draw.io rendering with the canonical smoke fixture..."
"${BACKEND_PYTHON}" "${REPO_ROOT}/scripts/drawio_render_preflight.py" \
  --drawio-url "${PATENT_CREATOR_DRAWIO_EMBED_URL}"

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
  exec npm run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}" --strictPort
) &
frontend_pid=$!

echo ""
echo "Patent Creator dev stack is starting..."
echo "Frontend: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
if uses_bundled_drawio; then
  echo "Draw.io:  ${DRAWIO_LOCAL_URL}"
fi
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

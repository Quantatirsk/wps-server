#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-192.168.6.228}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/wps-api-service}"
GIT_URL="${GIT_URL:-https://github.com/Quantatirsk/wps-server.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
IMAGE_NAME="${IMAGE_NAME:-wps-api-service:latest}"
PUBLIC_PORT="${PUBLIC_PORT:-18000}"
WORKER_COUNT="${WORKER_COUNT:-8}"
DISPATCHER_TIMEOUT_SECONDS="${DISPATCHER_TIMEOUT_SECONDS:-180}"
REMOTE_PASSWORD="${REMOTE_PASSWORD:-}"

if [[ -z "${REMOTE_PASSWORD}" ]]; then
  read -r -s -p "SSH password for ${REMOTE_USER}@${REMOTE_HOST}: " REMOTE_PASSWORD
  echo
fi

worker_urls=()
container_names=(wps-api-service)
for index in $(seq 1 "${WORKER_COUNT}"); do
  worker_urls+=("http://wps-worker-${index}:8000")
  container_names+=("wps-worker-${index}")
done
WPS_BATCH_WORKER_URLS="$(IFS=,; echo "${worker_urls[*]}")"

remote_script="$(mktemp)"
trap 'rm -f "${remote_script}"' EXIT

{
  printf 'set -euo pipefail\n'
  printf 'mkdir -p %q\n' "${REMOTE_DIR}"
  printf 'if [[ ! -d %q ]]; then git clone --branch %q %q %q; fi\n' "${REMOTE_DIR}/.git" "${GIT_BRANCH}" "${GIT_URL}" "${REMOTE_DIR}"
  printf 'cd %q\n' "${REMOTE_DIR}"
  printf 'git fetch --all --prune\n'
  printf 'git reset --hard %q\n' "origin/${GIT_BRANCH}"
  for name in "${container_names[@]}"; do
    printf 'docker rm -f %q >/dev/null 2>&1 || true\n' "${name}"
  done
  printf 'docker image rm -f %q >/dev/null 2>&1 || true\n' "${IMAGE_NAME}"
  printf 'docker image prune -f >/dev/null 2>&1 || true\n'
  printf 'docker network create wps-cluster >/dev/null 2>&1 || true\n'
  printf 'docker build -t %q .\n' "${IMAGE_NAME}"
  for index in $(seq 1 "${WORKER_COUNT}"); do
    printf 'docker run -d --restart unless-stopped --name %q --network wps-cluster -e WPS_BATCH_WORKER_URLS= -e WPS_DISPATCHER_REQUEST_TIMEOUT_SECONDS=%q %q >/dev/null\n' "wps-worker-${index}" "${DISPATCHER_TIMEOUT_SECONDS}" "${IMAGE_NAME}"
  done
  printf 'docker run -d --restart unless-stopped --name wps-api-service --network wps-cluster -p %q:8000 -e WPS_BATCH_WORKER_URLS=%q -e WPS_DISPATCHER_REQUEST_TIMEOUT_SECONDS=%q %q >/dev/null\n' "${PUBLIC_PORT}" "${WPS_BATCH_WORKER_URLS}" "${DISPATCHER_TIMEOUT_SECONDS}" "${IMAGE_NAME}"
  printf 'sleep 5\n'
  printf 'printf "\\n== containers ==\\n"\n'
  printf 'docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}" | grep -E "^NAMES|^wps-api-service|^wps-worker-"\n'
  printf 'printf "\\n== memory ==\\n"\n'
  printf 'free -h\n'
  printf 'printf "\\n== readyz ==\\n"\n'
  printf 'curl -fsS http://127.0.0.1:%q/api/v1/readyz\n' "${PUBLIC_PORT}"
  printf 'printf "\\n"\n'
} > "${remote_script}"

expect <<EXPECT_EOF
set timeout -1
spawn sh -lc {cat "$remote_script" | ssh -o StrictHostKeyChecking=no -p ${REMOTE_PORT} ${REMOTE_USER}@${REMOTE_HOST} 'bash -s'}
expect {
  "*yes/no*" {
    send "yes\r"
    exp_continue
  }
  "*password:*" {
    send "${REMOTE_PASSWORD}\r"
    exp_continue
  }
  eof
}
EXPECT_EOF

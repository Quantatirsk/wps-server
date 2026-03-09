#!/usr/bin/env bash
set -euo pipefail

worker_count="${WPS_WORKER_COUNT:-1}"
worker_base_url="${WPS_BATCH_WORKER_BASE_URL:-}"

if [[ -z "${WPS_BATCH_WORKER_URLS:-}" ]]; then
  if [[ -z "${worker_base_url}" ]]; then
    echo "WPS_BATCH_WORKER_BASE_URL is required when WPS_BATCH_WORKER_URLS is empty" >&2
    exit 1
  fi

  if ! [[ "${worker_count}" =~ ^[0-9]+$ ]] || [[ "${worker_count}" -lt 1 ]]; then
    echo "WPS_WORKER_COUNT must be an integer greater than 0" >&2
    exit 1
  fi

  worker_urls=()
  for _ in $(seq 1 "${worker_count}"); do
    worker_urls+=("${worker_base_url%/}")
  done
  export WPS_BATCH_WORKER_URLS
  WPS_BATCH_WORKER_URLS="$(IFS=,; echo "${worker_urls[*]}")"
fi

exec /entrypoint.sh "$@"

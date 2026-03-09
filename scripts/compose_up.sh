#!/usr/bin/env bash
set -euo pipefail

compose_file="docker/docker-compose.yml"
worker_count="${WPS_WORKER_COUNT:-8}"
image_name="${WPS_IMAGE:-quantatrisk/wps-api-service:latest}"

if ! [[ "${worker_count}" =~ ^[0-9]+$ ]] || [[ "${worker_count}" -lt 1 ]]; then
  echo "WPS_WORKER_COUNT must be an integer greater than 0" >&2
  exit 1
fi

if ! docker image inspect "${image_name}" >/dev/null 2>&1; then
  echo "Image not found: ${image_name}" >&2
  echo "Build it first with ./scripts/build_image.sh" >&2
  exit 1
fi

echo "Starting cluster with ${worker_count} workers using ${image_name}"
exec docker compose -f "${compose_file}" up -d --scale wps-worker="${worker_count}"

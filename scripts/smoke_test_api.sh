#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
INPUT_FILE="${1:-}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/wps-api-smoke}"

mkdir -p "${OUTPUT_DIR}"

printf '\n[1/3] healthz\n'
curl -fsS "${BASE_URL}/api/v1/healthz"
printf '\n\n[2/3] readyz\n'
curl -fsS "${BASE_URL}/api/v1/readyz"
printf '\n'

if [[ -z "${INPUT_FILE}" ]]; then
  printf '\n[3/3] skip convert (no input file provided)\n'
  exit 0
fi

OUTPUT_FILE="${OUTPUT_DIR}/$(basename "${INPUT_FILE%.*}").pdf"
printf '\n[3/3] convert %s\n' "${INPUT_FILE}"
curl -fsS -X POST \
  -F "file=@${INPUT_FILE}" \
  "${BASE_URL}/api/v1/convert-to-pdf" \
  --output "${OUTPUT_FILE}"

printf 'saved pdf to %s\n' "${OUTPUT_FILE}"

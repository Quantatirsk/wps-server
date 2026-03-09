#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/benchmark_batch.sh [options]

Options:
  --url URL                  Full batch endpoint URL.
                             Default: http://127.0.0.1:18000/api/v1/convert-to-pdf/batch
  --ready-url URL            Optional readiness endpoint URL.
                             Default: http://127.0.0.1:18000/api/v1/readyz
  --source-file FILE         One source document to duplicate into a batch set.
  --input-dir DIR            Directory containing prebuilt input files.
  --batch-size N             Number of documents per batch. Default: 12
  --batches N                Number of batch requests to execute. Default: 4
  --concurrency N            Number of concurrent batch requests. Default: 1
  --workdir DIR              Working directory for generated inputs and outputs.
                             Default: /tmp/wps-api-benchmark
  --curl-max-time SECONDS    curl --max-time value per request. Default: 0
  --keep-artifacts           Keep generated outputs after the run.
  --help                     Show this message.

Examples:
  ./scripts/benchmark_batch.sh \
    --url http://192.168.6.146:18000/api/v1/convert-to-pdf/batch \
    --source-file /path/to/sample.docx \
    --batch-size 12 \
    --batches 4 \
    --concurrency 4

  ./scripts/benchmark_batch.sh \
    --url http://127.0.0.1:18000/api/v1/convert-to-pdf/batch \
    --input-dir /data/docx-set \
    --batch-size 8 \
    --batches 10 \
    --concurrency 2
EOF
}

batch_url="http://192.168.6.146:18000/api/v1/convert-to-pdf/batch"
ready_url="http://192.168.6.146:18000/api/v1/readyz"
source_file=""
input_dir=""
batch_size=12
batches=2
concurrency=2
workdir="/tmp/wps-api-benchmark"
curl_max_time=0
keep_artifacts=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      batch_url="$2"
      shift 2
      ;;
    --ready-url)
      ready_url="$2"
      shift 2
      ;;
    --source-file)
      source_file="$2"
      shift 2
      ;;
    --input-dir)
      input_dir="$2"
      shift 2
      ;;
    --batch-size)
      batch_size="$2"
      shift 2
      ;;
    --batches)
      batches="$2"
      shift 2
      ;;
    --concurrency)
      concurrency="$2"
      shift 2
      ;;
    --workdir)
      workdir="$2"
      shift 2
      ;;
    --curl-max-time)
      curl_max_time="$2"
      shift 2
      ;;
    --keep-artifacts)
      keep_artifacts=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_positive_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -lt 1 ]]; then
    echo "$name must be a positive integer" >&2
    exit 1
  fi
}

require_non_negative_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$name must be a non-negative integer" >&2
    exit 1
  fi
}

require_positive_int "--batch-size" "$batch_size"
require_positive_int "--batches" "$batches"
require_positive_int "--concurrency" "$concurrency"
require_non_negative_int "--curl-max-time" "$curl_max_time"

if [[ -n "$source_file" && -n "$input_dir" ]]; then
  echo "Use either --source-file or --input-dir, not both" >&2
  exit 1
fi

if [[ -z "$source_file" && -z "$input_dir" ]]; then
  echo "One of --source-file or --input-dir is required" >&2
  exit 1
fi

if [[ -n "$source_file" && ! -f "$source_file" ]]; then
  echo "Source file not found: $source_file" >&2
  exit 1
fi

if [[ -n "$input_dir" && ! -d "$input_dir" ]]; then
  echo "Input directory not found: $input_dir" >&2
  exit 1
fi

mkdir -p "$workdir"
input_workdir="$workdir/inputs"
output_workdir="$workdir/outputs"
result_tsv="$workdir/results.tsv"
rm -rf "$input_workdir" "$output_workdir"
mkdir -p "$input_workdir" "$output_workdir"
: >"$result_tsv"

cleanup() {
  if [[ "$keep_artifacts" -eq 0 ]]; then
    rm -rf "$workdir"
  fi
}
trap cleanup EXIT

wait_ready() {
  if [[ -z "$ready_url" ]]; then
    return 0
  fi

  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS --max-time 5 "$ready_url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "Ready check failed: $ready_url" >&2
  exit 1
}

prepare_inputs_from_source() {
  local suffix
  suffix="$(python3 - <<PY
from pathlib import Path
print(Path("$source_file").suffix.lower())
PY
)"
  if [[ -z "$suffix" ]]; then
    echo "Source file must have an extension" >&2
    exit 1
  fi

  local index
  for index in $(seq 1 "$batch_size"); do
    cp "$source_file" "$input_workdir/input_$(printf '%03d' "$index")$suffix"
  done
}

prepare_inputs_from_dir() {
  local available_files=()
  while IFS= read -r -d '' file; do
    available_files+=("$file")
  done < <(find "$input_dir" -maxdepth 1 -type f \( \
      -name '*.doc' -o -name '*.docx' -o -name '*.ppt' -o -name '*.pptx' -o -name '*.xls' -o -name '*.xlsx' \
    \) -print0 | sort -z)

  if [[ "${#available_files[@]}" -lt "$batch_size" ]]; then
    echo "Input directory must contain at least $batch_size supported files" >&2
    exit 1
  fi

  local index=1
  local file
  for file in "${available_files[@]:0:$batch_size}"; do
    local extension=""
    if [[ "$file" == *.* ]]; then
      extension=".${file##*.}"
    fi
    cp "$file" "$input_workdir/input_$(printf '%03d' "$index")${extension}"
    index=$((index + 1))
  done
}

if [[ -n "$source_file" ]]; then
  prepare_inputs_from_source
else
  prepare_inputs_from_dir
fi

input_files=()
while IFS= read -r file; do
  input_files+=("$file")
done < <(find "$input_workdir" -maxdepth 1 -type f | sort)

if [[ "${#input_files[@]}" -ne "$batch_size" ]]; then
  echo "Prepared input file count mismatch: expected $batch_size got ${#input_files[@]}" >&2
  exit 1
fi

run_one_batch() {
  local batch_index="$1"
  local output_zip="$output_workdir/batch_${batch_index}.zip"
  local curl_output
  local curl_cmd=(
    curl
    -sS
    -o "$output_zip"
    -w '%{http_code} %{time_total}'
    -X POST
  )

  if [[ "$curl_max_time" -gt 0 ]]; then
    curl_cmd+=(--max-time "$curl_max_time")
  fi

  local file
  for file in "${input_files[@]}"; do
    curl_cmd+=(-F "files=@${file}")
  done
  curl_cmd+=("$batch_url")

  curl_output="$("${curl_cmd[@]}")"
  local http_code
  local elapsed
  http_code="${curl_output%% *}"
  elapsed="${curl_output##* }"

  if [[ "$http_code" != "200" ]]; then
    echo -e "${batch_index}\tfailed\t${http_code}\t${elapsed}\t0" >>"$result_tsv"
    return 1
  fi

  local file_size
  file_size="$(wc -c <"$output_zip" | tr -d ' ')"
  echo -e "${batch_index}\tsucceeded\t${http_code}\t${elapsed}\t${file_size}" >>"$result_tsv"
}

export batch_url
export output_workdir
export result_tsv
export curl_max_time
export batch_size
export -f run_one_batch

input_file_list="$workdir/input_files.txt"
printf '%s\n' "${input_files[@]}" >"$input_file_list"
export input_file_list

run_one_batch_wrapper() {
  input_files=()
  while IFS= read -r file; do
    input_files+=("$file")
  done <"$input_file_list"
  run_one_batch "$1"
}
export -f run_one_batch_wrapper

wait_ready

started_epoch="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
started_monotonic="$(python3 - <<'PY'
import time
print(time.perf_counter())
PY
)"

seq 1 "$batches" | xargs -I{} -P "$concurrency" bash -lc 'run_one_batch_wrapper "$@"' _ {}

ended_monotonic="$(python3 - <<'PY'
import time
print(time.perf_counter())
PY
)"

export started_epoch
export started_monotonic
export ended_monotonic
export concurrency
export batches
export batch_url
export batch_size

python3 - <<PY
import os
from pathlib import Path

result_path = Path("$result_tsv")
rows = []
for line in result_path.read_text(encoding="utf-8").splitlines():
    batch_index, status, http_code, elapsed, file_size = line.split("\\t")
    rows.append(
        {
            "batch_index": int(batch_index),
            "status": status,
            "http_code": int(http_code),
            "elapsed": float(elapsed),
            "file_size": int(file_size),
        }
    )

rows.sort(key=lambda item: item["batch_index"])
successful_rows = [row for row in rows if row["status"] == "succeeded"]
failed_rows = [row for row in rows if row["status"] != "succeeded"]
total_elapsed = float(os.environ["ended_monotonic"]) - float(os.environ["started_monotonic"])
total_docs = len(successful_rows) * int("$batch_size")

print("Benchmark Summary")
print(f"  Started At: {os.environ['started_epoch']}")
print(f"  Batch URL: {os.environ['batch_url']}")
print(f"  Batch Size: {os.environ['batch_size']}")
print(f"  Batches: {os.environ['batches']}")
print(f"  Concurrency: {os.environ['concurrency']}")
print(f"  Successful Batches: {len(successful_rows)}")
print(f"  Failed Batches: {len(failed_rows)}")
print(f"  Total Wall Time: {total_elapsed:.3f}s")
if successful_rows:
    average_batch = sum(row["elapsed"] for row in successful_rows) / len(successful_rows)
    print(f"  Average Batch Time: {average_batch:.3f}s")
    print(f"  Total Docs: {total_docs}")
    print(f"  Docs Per Second: {total_docs / total_elapsed:.3f}")

print("")
print("Batch Results")
for row in rows:
    print(
        f"  Batch {row['batch_index']:>3}: "
        f"status={row['status']} http={row['http_code']} "
        f"time={row['elapsed']:.3f}s size={row['file_size']}"
    )

if failed_rows:
    raise SystemExit(1)
PY

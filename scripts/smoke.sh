#!/usr/bin/env bash
set -euo pipefail

base='http://127.0.0.1:8000'

job=$(curl --connect-timeout 2 --max-time 5 -fsS "$base/execute" \
  -H 'content-type: application/json' \
  -d '{"language":"python","source":"print(2 + 40)"}')

echo "$job"
job_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<< "$job")

for _ in $(seq 1 100); do
  result=$(curl --connect-timeout 2 --max-time 5 -fsS "$base/jobs/$job_id")
  echo "$result"
  status=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<< "$result")
  case "$status" in
    succeeded)
      python3 -c '
import json, sys
result = json.load(sys.stdin)
assert result["exit_code"] == 0, result
assert result["stdout"] == "42\n", result
' <<< "$result"
      echo "smoke test passed"
      exit 0
      ;;
    failed|timed_out|oom_killed)
      echo "smoke test failed: $result" >&2
      exit 1
      ;;
  esac
  sleep 0.1
done

echo "job did not finish" >&2
exit 1

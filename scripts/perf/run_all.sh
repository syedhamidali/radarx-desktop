#!/bin/sh
# Full frontend benchmark for one build, written to $OUT/<label>_*.json.
#
#   PY=/path/to/python-with-playwright ENGINE_PORT=25611 FILE=/data/KTLX...gz FILE2=/data/KGSP...nc \
#   PLAIN_URL=http://127.0.0.1:4175/ PERF_URL=http://127.0.0.1:4174/ OUT=/tmp/perf \
#   sh scripts/perf/run_all.sh after
#
# PLAIN_URL serves the normal `npm run build` output (startup numbers), PERF_URL the build from
# `npx vite build -c scripts/perf/vite.perf.config.ts` (it adds window.__xr for driving the app).
set -e
LABEL=${1:-run}
OUT=${OUT:-.}
D=$(dirname "$0")
for CPU in 1 4; do
  $PY "$D/measure_startup.py" --url "$PLAIN_URL" --cpu $CPU --runs ${RUNS:-11} --label "$LABEL" \
      --engine-port "$ENGINE_PORT" --json "$OUT/${LABEL}_startup_cpu$CPU.json" | tail -1
  $PY "$D/measure_runtime.py" --url "$PERF_URL" --engine-port "$ENGINE_PORT" --file "$FILE" \
      --only frames --cpu $CPU --seconds 8 --json "$OUT/${LABEL}_frames_cpu$CPU.json" > /dev/null
done
$PY "$D/measure_runtime.py" --url "$PERF_URL" --engine-port "$ENGINE_PORT" --file "$FILE" --file2 "$FILE2" \
    --only leak --cycles ${CYCLES:-20} --json "$OUT/${LABEL}_leak.json" > /dev/null
echo "done: $OUT/${LABEL}_*.json"

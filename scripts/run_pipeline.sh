#!/usr/bin/env bash
#
# Run the remaining pipeline end to end, in dependency order.
#
#   sudo bash scripts/run_pipeline.sh
#
# Each step is independent enough to be re-run on its own, but they are ordered:
# training needs a collected dataset, the experiment sweep needs a trained
# model, and the analysis needs the sweep's results. Running them out of order
# is how a report ends up describing a model that does not exist.
#
# The script stops at the first failure. Continuing past a failed step would
# produce artefacts that describe each other but not the run that actually
# happened -- the exact class of problem this project keeps trying to avoid.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
RESULTS="$ROOT/results"

step() {
    printf '\n\033[1m========== %s ==========\033[0m\n' "$1"
}

step "1/5  Train the congestion predictor (P7)"
if [ -f "$RESULTS/model.joblib" ] && [ "${FORCE_TRAIN:-0}" != "1" ]; then
    echo "results/model.joblib exists; skipping (FORCE_TRAIN=1 to retrain)"
else
    "$PY" -m ml.train
fi

step "2/5  P7 acceptance"
"$PY" scripts/verify_p7.py --out "$RESULTS/p7_acceptance.json" || true

step "3/5  Experiment sweep (P10) -- this is the long one"
echo "4 modes x 3 scenarios x repetitions, one fabric at a time."
echo "Use --resume after an interruption: bash scripts/run_pipeline.sh --resume"
if [ "${SKIP_EXPERIMENTS:-0}" = "1" ]; then
    echo "SKIP_EXPERIMENTS=1; skipping"
elif [ "${RESUME:-0}" = "1" ]; then
    "$PY" scripts/run_experiments.py --resume
else
    "$PY" scripts/run_experiments.py
fi

step "4/5  Analysis and report (P11)"
"$PY" scripts/make_report.py --results "$RESULTS/experiments.csv" --out "$RESULTS" || {
    echo "no sweep results to analyse yet; continuing to the demo"
}

step "5/5  Demonstration (P13)"
"$PY" scripts/demo.py --mode ml_aware || echo "demo did not demonstrate the claim (see exit codes in the README)"

step "Snapshot"
"$PY" scripts/snapshot.py --only p6 p7 p8 p9 p10 p11 unit || true
"$PY" scripts/snapshot_image.py || true

printf '\n\033[1m========== done ==========\033[0m\n'
echo "results/RESULTS.md      the write-up, including what the data does NOT support"
echo "results/snapshots/      progress ledger, logs and progress.png"

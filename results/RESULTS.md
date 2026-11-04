# Experiment results

Generated from `results/experiments.csv` with 46 run(s) across 4 mode(s) and 3 scenario(s).

Modes: `ecmp`, `ml_aware`, `reactive_threshold`, `static_shortest`.

## How to read this

Each cell is `mean ± population sd` across repetitions. A mode marked
*\** is the lowest (or highest) mean for that metric, and that alone is
not a claim of superiority -- read the verdict column. `separated from
best` means the 95% intervals do not overlap; `overlapping with best`
means the data does not distinguish them.

## Did each run test what it claims?

**46 run(s) did not run under their labelled mode.** Rows affected are excluded from any head-to-head claim below, because comparing a real mode against a fallback measures the fallback:

| Mode | Runs | As labelled | Degraded | Unknown |
|---|---|---|---|---|
| `ecmp` | 12 | 0 | 0 | 12 |
| `ml_aware` | 11 | 0 | 0 | 11 |
| `reactive_threshold` | 11 | 0 | 0 | 11 |
| `static_shortest` | 12 | 0 | 0 | 12 |

Affected runs: `ecmp__baseline__r0`, `ecmp__baseline__r1`, `ecmp__baseline__r2`, `ecmp__baseline__r3`, `ecmp__hot_sink__r0`, `ecmp__hot_sink__r1`, `ecmp__hot_sink__r2`, `ecmp__hot_sink__r3`, `ecmp__ramp_bursty__r0`, `ecmp__ramp_bursty__r1`, `ecmp__ramp_bursty__r2`, `ecmp__ramp_bursty__r3`, `ml_aware__baseline__r0`, `ml_aware__baseline__r1`, `ml_aware__baseline__r2`, `ml_aware__baseline__r3`, `ml_aware__hot_sink__r0`, `ml_aware__hot_sink__r1`, `ml_aware__hot_sink__r2`, `ml_aware__ramp_bursty__r0` ...

## Per-mode summary

| Mode | Scenario | n | Latency mean (ms) | Congestion (s) | Recovery (s) | Reroutes |
|---|---|---|---|---|---|---|
| `ecmp` | baseline | 4 | 11.91 ± 0.81 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `ecmp` | hot_sink | 4 | 12.93 ± 0.61 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `ecmp` | ramp_bursty | 4 | 14.19 ± 0.92 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `ml_aware` | baseline | 4 | 12.66 ± 0.92 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 |
| `ml_aware` | hot_sink | 3 | 13.03 ± 1.04 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `ml_aware` | ramp_bursty | 4 | 14.24 ± 0.89 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `reactive_threshold` | baseline | 4 | 12.65 ± 1.06 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 |
| `reactive_threshold` | hot_sink | 3 | 12.61 ± 0.98 | 0.0 ± 0.0 | 2.0 ± 0.0 | 0.0 ± 0.0 |
| `reactive_threshold` | ramp_bursty | 4 | 13.98 ± 0.80 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 |
| `static_shortest` | baseline | 4 | 12.67 ± 0.44 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |
| `static_shortest` | hot_sink | 4 | 12.78 ± 0.26 | 0.5 ± 0.8 | -- | 0.0 ± 0.0 |
| `static_shortest` | ramp_bursty | 4 | 13.55 ± 1.51 | 0.0 ± 0.0 | -- | 0.0 ± 0.0 |

## Scenario comparison

### baseline

**Latency mean (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` **\*** | 11.91 ± 0.81 | 4 | best |
| `ml_aware` | 12.66 ± 0.92 | 4 | overlapping with best |
| `reactive_threshold` | 12.65 ± 1.06 | 4 | overlapping with best |
| `static_shortest` | 12.67 ± 0.44 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Latency p95 (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` **\*** | 14.51 ± 0.93 | 4 | best |
| `ml_aware` | 15.12 ± 0.97 | 4 | overlapping with best |
| `reactive_threshold` | 14.99 ± 1.22 | 4 | overlapping with best |
| `static_shortest` | 15.51 ± 1.31 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Throughput (Mbps)** (higher is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.65 ± 0.03 | 4 | overlapping with best |
| `ml_aware` | 0.63 ± 0.02 | 4 | overlapping with best |
| `reactive_threshold` | 0.61 ± 0.03 | 4 | overlapping with best |
| `static_shortest` **\*** | 0.66 ± 0.02 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Loss (%)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.01 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.01 ± 0.00 | 4 | overlapping with best |
| `reactive_threshold` **\*** | 0.01 ± 0.01 | 4 | best |
| `static_shortest` | 0.01 ± 0.00 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Util balance (sd)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.07 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.06 ± 0.00 | 4 | overlapping with best |
| `reactive_threshold` **\*** | 0.06 ± 0.00 | 4 | best |
| `static_shortest` | 0.07 ± 0.00 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Congestion (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.00 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.00 ± 0.00 | 4 | overlapping with best |
| `reactive_threshold` | 0.00 ± 0.00 | 4 | overlapping with best |
| `static_shortest` **\*** | 0.00 ± 0.00 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Recovery (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ml_aware` | 0.00 ± 0.00 | 1 | insufficient repetitions |
| `reactive_threshold` **\*** | 0.00 ± 0.00 | 1 | insufficient repetitions |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

### hot_sink

**Latency mean (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 12.93 ± 0.61 | 4 | overlapping with best |
| `ml_aware` | 13.03 ± 1.04 | 3 | overlapping with best |
| `reactive_threshold` **\*** | 12.61 ± 0.98 | 3 | best |
| `static_shortest` | 12.78 ± 0.26 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Latency p95 (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 15.79 ± 0.67 | 4 | overlapping with best |
| `ml_aware` | 16.32 ± 1.82 | 3 | overlapping with best |
| `reactive_threshold` **\*** | 15.32 ± 0.79 | 3 | best |
| `static_shortest` | 15.55 ± 0.16 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Throughput (Mbps)** (higher is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.00 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.00 ± 0.00 | 3 | overlapping with best |
| `reactive_threshold` | 0.00 ± 0.00 | 3 | overlapping with best |
| `static_shortest` **\*** | 0.00 ± 0.00 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Loss (%)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` **\*** | 0.00 ± 0.00 | 4 | best |
| `ml_aware` | 0.00 ± 0.00 | 3 | overlapping with best |
| `reactive_threshold` | 0.05 ± 0.08 | 3 | overlapping with best |
| `static_shortest` | 0.12 ± 0.20 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Util balance (sd)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.00 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.00 ± 0.00 | 3 | overlapping with best |
| `reactive_threshold` | 0.00 ± 0.00 | 3 | overlapping with best |
| `static_shortest` **\*** | 0.00 ± 0.00 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Congestion (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` **\*** | 0.00 ± 0.00 | 4 | best |
| `ml_aware` | 0.00 ± 0.00 | 3 | overlapping with best |
| `reactive_threshold` | 0.00 ± 0.00 | 3 | overlapping with best |
| `static_shortest` | 0.46 ± 0.80 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Recovery (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `reactive_threshold` **\*** | 2.01 ± 0.00 | 1 | insufficient repetitions |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

### ramp_bursty

**Latency mean (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 14.19 ± 0.92 | 4 | overlapping with best |
| `ml_aware` | 14.24 ± 0.89 | 4 | overlapping with best |
| `reactive_threshold` | 13.98 ± 0.80 | 4 | overlapping with best |
| `static_shortest` **\*** | 13.55 ± 1.51 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Latency p95 (ms)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 15.82 ± 1.47 | 4 | overlapping with best |
| `ml_aware` | 15.87 ± 0.87 | 4 | overlapping with best |
| `reactive_threshold` | 15.59 ± 0.80 | 4 | overlapping with best |
| `static_shortest` **\*** | 15.00 ± 1.35 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Throughput (Mbps)** (higher is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 1.72 ± 0.00 | 4 | separated from best |
| `ml_aware` **\*** | 1.73 ± 0.00 | 4 | best |
| `reactive_threshold` | 1.72 ± 0.00 | 4 | separated from best |
| `static_shortest` | 1.72 ± 0.00 | 4 | separated from best |

`ml_aware` is separated from at least one other mode on this metric (non-overlapping 95% intervals).

**Loss (%)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` **\*** | 0.81 ± 0.37 | 4 | best |
| `ml_aware` | 1.11 ± 0.32 | 4 | overlapping with best |
| `reactive_threshold` | 0.86 ± 0.13 | 4 | overlapping with best |
| `static_shortest` | 0.94 ± 0.18 | 4 | overlapping with best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Util balance (sd)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.17 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.17 ± 0.00 | 4 | separated from best |
| `reactive_threshold` | 0.17 ± 0.00 | 4 | overlapping with best |
| `static_shortest` **\*** | 0.17 ± 0.00 | 4 | best |

`static_shortest` is separated from at least one other mode on this metric (non-overlapping 95% intervals).

**Congestion (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `ecmp` | 0.00 ± 0.00 | 4 | overlapping with best |
| `ml_aware` | 0.00 ± 0.00 | 4 | overlapping with best |
| `reactive_threshold` | 0.00 ± 0.00 | 4 | overlapping with best |
| `static_shortest` **\*** | 0.00 ± 0.00 | 4 | best |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

**Recovery (s)** (lower is better)

| Mode | mean ± sd | n | Verdict |
|---|---|---|---|
| `reactive_threshold` **\*** | 0.00 ± 0.00 | 1 | insufficient repetitions |

**No mode is separated from the best on this metric.** The ranking above is not supported by the data at this repetition count; it is shown only so the raw numbers are visible.

## What the data supports

Supported by the data:

- **ramp_bursty / Throughput (Mbps)**: `ml_aware` is separated from at least one alternative.
- **ramp_bursty / Util balance (sd)**: `static_shortest` is separated from at least one alternative.

**Not** supported by the data:

- **baseline / Latency mean (ms)**: no separation; `ecmp` has the lowest mean but the data does not distinguish it.
- **baseline / Latency p95 (ms)**: no separation; `ecmp` has the lowest mean but the data does not distinguish it.
- **baseline / Throughput (Mbps)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **baseline / Loss (%)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **baseline / Util balance (sd)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **baseline / Congestion (s)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **baseline / Recovery (s)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **hot_sink / Latency mean (ms)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **hot_sink / Latency p95 (ms)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **hot_sink / Throughput (Mbps)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **hot_sink / Loss (%)**: no separation; `ecmp` has the lowest mean but the data does not distinguish it.
- **hot_sink / Util balance (sd)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **hot_sink / Congestion (s)**: no separation; `ecmp` has the lowest mean but the data does not distinguish it.
- **hot_sink / Recovery (s)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.
- **ramp_bursty / Latency mean (ms)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **ramp_bursty / Latency p95 (ms)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **ramp_bursty / Loss (%)**: no separation; `ecmp` has the lowest mean but the data does not distinguish it.
- **ramp_bursty / Congestion (s)**: no separation; `static_shortest` has the lowest mean but the data does not distinguish it.
- **ramp_bursty / Recovery (s)**: no separation; `reactive_threshold` has the lowest mean but the data does not distinguish it.

Increasing `experiments.repetitions` is the only way to turn the second list into the first.


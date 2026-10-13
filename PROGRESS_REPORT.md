# Project Progress Report

**Project:** AI-Driven SDN Traffic Optimization and Congestion Avoidance<br>
**Report date:** 13 October 2026<br>
**Repository:** `/home/rahul/sdn_project`<br>
**Latest commit:** `adc6eb1` — *Train on real data; find a duplicate-measurement bug and a leakage-prone split*<br>
**Note:** the traffic-generator rewrite in §5.3 is implemented but **not yet committed** — it lands with the verification in step 1.

---

## 1. The goal, stated so it can be judged

The project is a **closed-loop SDN control system: Measure → Predict → Decide → Reroute → Verify**.

A Ryu controller measures per-link telemetry, an ML model predicts *future*
congestion, and that probability becomes one term of a path cost function. The
model never chooses a path itself — it informs a cost function that a
deterministic, provably-safe rerouting policy acts on.

**The claim worth defending is not accuracy. It is *lead time*.**

> A model that reports 98% accuracy but warns you two seconds before a link
> fails is operationally useless. A model with lower accuracy that warns thirty
> seconds ahead lets the controller reroute traffic *before* anyone notices.

Everything below is organised around that. Where a number looks good but is not
evidence of the claim, this report says so rather than banking it.

### Success criteria

| No. | Criterion | How it is judged |
|---|---|---|
| 1 | Loop-safe controller, zero packet loss | P3 acceptance: pingall, broadcast-storm check, flow inspection |
| 2 | Real telemetry, not synthesised | P5 acceptance against a live fabric |
| 3 | A labelled dataset with both classes | P6 acceptance: positive rate 25–60% |
| 4 | A predictor whose lead time exceeds the guard band | Lead time > `label_gap_s`, detection over most episodes |
| 5 | Rerouting that does not flap | Improvement ratio + cooldown + dwell, each mutation-tested |
| 6 | Four routing modes measurably different | P10 sweep + P11 verdicts |
| 7 | No claim stronger than the evidence | P11 must report what the data does *not* support |

---

## 2. Where we are — one table, no spin

Two different numbers, because they mean different things. Reporting only the
flattering one would defeat the purpose of the report.

| Measure | Value |
|---|---|
| Code written | ~95% — all 13 phases implemented |
| **Phases verified by live acceptance run** | **7 of 13** |
| Unit tests | **599 passing**, hermetic (no root, no fabric, no dataset) |
| Commits | 19 |
| Recorded acceptance checks passing | 783 |
| Lines of code (src + tests) | ~23,000 |

### Phase status

| Phase | Scope | Code | Verified live |
|---|---|---|---|
| P1 | Environment, pinned deps, Makefile | done | **13/13** |
| P2 | Multipath topology, path isolation | done | **22/22** |
| P3 | OF1.3 controller + ARP proxy | done | **17/17** |
| P4 | Live LLDP topology discovery | done | **18/18** |
| P5 | Telemetry (rate, latency, loss, CSV) | done | **11/11** |
| P6 | Dataset generation + future labelling | done | **27/27** |
| P7 | ML training + evaluation | done | **16/17 — 1 failure, see §5** |
| P8 | Online inference | done | 28/28 unit-verified |
| P9 | Cost, paths, reroute ordering | done | 145/145 unit-verified |
| P10 | Experiment harness | done | 16/16 harness-verified |
| P11 | Analysis + report | done | 32/32 unit-verified |
| P12 | FastAPI + JWT + React | done | **27/27** (parked in `future/`) |
| P13 | Demonstration | done | not yet run live |

**Live-verified** means the phase's own acceptance script ran against a real
Mininet fabric and printed its own `SUMMARY: n passed, m failed`. Nothing below
P6 is claimed verified until that has happened — see `results/snapshots/`.

---

## 3. Evidence: snapshots, not assertions

Claims in a project like this rot. A later change breaks an earlier phase while
the README still says green. So every claim above is backed by a machine-readable
ledger.

### Latest snapshot (`results/snapshots/MANIFEST.json`)

| Target | Status | Passed | Failed |
|---|---|---|---|
| p6 — Dataset labelling | **pass** | 27 | 0 |
| p7 — ML training | **fail** | 16 | 1 |
| p8 — Online inference | **pass** | 28 | 0 |
| p9 — Cost/paths/reroute | **pass** | 145 | 0 |
| p10 — Experiment harness | **pass** | 16 | 0 |
| p11 — Analysis restraint | **pass** | 32 | 0 |
| unit — Unit suite | **pass** | 519 | 0 |

### How the snapshot system resists self-deception

Three deliberate properties, each with a test:

1. **A non-zero exit is authoritative, *and* any reported failure count is
   re-checked.** A harness that lies in its exit code cannot read green.
2. **A target whose verification script does not exist reports `planned`**
   ("not built"), rather than being dropped. P10 showing as unbuilt is honest;
   P10 silently vanishing from the catalogue is not.
3. **The progress picture cannot overstate.** `make snapshot-image` renders
   `results/snapshots/progress.png`: a tile per phase, and *an unverified phase
   is never rendered as green*. Unverified phases are grey and read `NOT RUN`.

> This caught two real bugs in our own work the moment it was switched on — see §6.

### Artefacts

| File | Contents |
|---|---|
| `results/snapshots/PROGRESS.md` | Human-readable table (phase, pass/fail, timestamp, commit) |
| `results/snapshots/MANIFEST.json` | Machine-readable ledger |
| `results/snapshots/HISTORY.jsonl` | One record per snapshot run — makes *growth* visible |
| `results/snapshots/progress.png` | Progress picture |
| `results/snapshots/logs/*.log` | Full raw output, timestamped |

---

## 4. The dataset (P6) — collected and verified

Collection completed: **25 runs × 90 s**, five traffic patterns, real Mininet
fabric.

| Property | Value |
|---|---|
| Telemetry rows | **9,400** |
| Collection runs | 25 |
| Links | 8 (four diamond paths × two directions) |
| Samples per link-run | 46–48 (median 47) |
| Valid horizon | 8,192 (87%) |
| **Positive class** | **1,708 (20.8%)** |
| Patterns | ramp, bursty_onoff, poisson, tcp_udp_mix, elephant_mice (~1,880 rows each) |

P6 acceptance: **27/27 passing.**

### Why "valid horizon 87%" is correct, not a defect

A label requires observing the *future* — congestion within
`[t + 2s, t + 12s]`. For windows at the end of a run, that future never happens,
so they are excluded rather than labelled negative. Labelling them 0 would teach
the model that "I stopped watching" means "everything is fine", which is precisely
the failure mode P8's staleness guards exist to prevent.

---

## 5. The model (P7) — trained, and what it actually means

Trained on real collected data. Results:

| Metric | Value |
|---|---|
| Winner | Random Forest |
| **PR-AUC** | **0.9847** |
| Precision | 0.9388 |
| Recall | 0.9684 |
| F1 | 0.9534 |
| Inference p95 | 33.6 ms (budget 50 ms) — fits |
| Evaluation protocol | `run_grouped` (holds out whole runs) |
| Chronological protocol, for contrast | 0.9856 |
| **Lead time** | **2.0 s — only 20% of episodes detected** |

**The accuracy is real. The lead time is not good enough, and §5.1 explains
exactly why.**

### 5.1 The lead-time problem, diagnosed to root cause

Lead time of 2.0 s is exactly `label_gap_s`, the minimum the labelling scheme
allows, and 80% of congestion episodes are missed entirely.

We chased this rather than accepting or tuning it away. Alternatives ruled out by
measurement, not assumption:

| Hypothesis | Test | Result |
|---|---|---|
| Threshold miscalibrated | Swept t = 0.1…0.5 | **Ruled out.** F1 is *best* at 0.5 (0.956); recall 0.97, precision 0.94 |
| Leakage inflating the score | Compared chronological vs run-grouped | **Ruled out.** 0.9847 vs 0.9856 — agree closely |
| Horizon too short | Swept 10→60 s | **Ruled out.** Longer horizons made detection *worse*; 60 s gave the best PR-AUC with **zero** detections |
| **The traffic itself is unpredictable** | Read the raw rate profile | **Confirmed — this is the cause** |

The raw link-rate profile for the `ramp` pattern:

```
[0, 1, 2, 2, 2, 2, 1, 0, 0, 0, 0, 2, 6, 6, 6, 6, 4, 0, 0, 0, 0, 3, 10, 10, 10, 10, 8, ...]
```

**The "ramp" is a square wave.** Offered load steps from idle to saturated inside
a single 2-second sample, then returns to zero. It never ramps 2→4→6→8→10→12 as
intended, and never holds a level.

When load switches on like a light switch, **there is nothing to predict.** The
model can only react — and 2 seconds is the physical maximum for an abrupt
onset. The 0.985 PR-AUC is measuring "is this link busy right now", not future
congestion. Feature importance agrees:

| Feature | Importance |
|---|---|
| `utilization__mean` | 17.7% |
| `tx_packet_rate_pps__mean` | 16.1% |
| `tx_rate_mbps__mean` | 15.2% |
| `utilization__std` | 10.8% |

Nearly all present-load. Nothing anticipatory.

**So the honest reading of the current numbers: the pipeline works end to end,
and the prediction task it is pointed at is trivially easy. That is a result
about the data, not about the method.**

### 5.2 Root cause in the code

`traffic/generate.py` started **one iperf3 process per ramp step**:

```python
while elapsed < duration_s:
    h1.cmd(f"{iperf} -u -c 10.0.0.4 -b {rate}M -t {step_dur} -p 5201 ... &")
    time_sleep(step_dur)
```

Two unavoidable consequences on a real fabric:

1. **Mininet's `node.cmd()` costs several seconds** (it spawns a shell and a
   pty), so whole steps are skipped — a profile of 2,4,6,8,10,12 arrived at the
   switches as 2,6,10.
2. **Each flow exits when its own `-t` expires.** If the next step starts even
   slightly late, there is a gap at zero — which is exactly the square wave.

### 5.3 The fix (implemented, not yet verified against a fabric)

Load is now delivered as **overlapping long-lived flows whose aggregate follows
the profile**. Each flow is started once and never re-launched; adding a flow
raises the rate, letting one expire lowers it, and nothing in between drops to
zero.

The unit of load is 2 Mbps. Unit *i* starts at the first step needing more than
*i* units and expires at the last step still needing it:

```
ScheduledFlow(start_s=0.0,  port=5201, rate=2.0, life=110s)
ScheduledFlow(start_s=10.0, port=5202, rate=2.0, life=90s)
ScheduledFlow(start_s=20.0, port=5203, rate=2.0, life=70s)
ScheduledFlow(start_s=30.0, port=5204, rate=2.0, life=50s)
ScheduledFlow(start_s=40.0, port=5205, rate=2.0, life=30s)
ScheduledFlow(start_s=50.0, port=5206, rate=2.0, life=10s)
```

Resulting aggregate — continuous, symmetric, never zero:

| t (s) | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Mbps | 2 | 4 | 6 | 8 | 10 | **12** | 10 | 8 | 6 | 4 | 2 |

`poisson` was broken the same way and is fixed identically (continuous
fluctuating background, minimum 2.2 Mbps, never zero). `bursty_onoff` was
already correct — an on/off square wave is what that pattern is *for* — and is
left alone.

**A secondary change we intend to make:** the collector should *assert the shape
of the traffic it received*. A "ramp" run whose profile is not monotonic should be
a failed run, not data to train on. Right now 25 runs of square waves were
accepted without comment; this was found only by reading raw rates by hand.

---

## 6. Bugs found and fixed

Every item below was found by measurement, diagnosed to root cause, and covered
by a test. This is the part of the report worth reading carefully.

### 6.1 A silent controller deadlock (the most serious)

**Symptom:** telemetry stopped ~24 s into every 90 s collection run — 13 samples
per link where ~45 were expected. No error, no traceback, no stall warning. The
controller was alive but inert.

**Root cause:** every long-lived controller spawn used
`stdout=subprocess.PIPE` with nothing reading it. A pipe buffer is finite
(~64 KiB on Linux). Once the controller filled it, it blocked in `write()` — and
because Ryu's event loop runs in that same process, the controller stopped
dispatching events entirely.

**Evidence:** the controller logs captured from the completed 30-run sweep were
**79,023 and 80,377 bytes** — just past the 65,536-byte default buffer, matching
the 24 s cutoff exactly.

**Fix:** `common.proc.ControllerLog` redirects a child's output to a file. A file
never applies back-pressure, so the child cannot block, and the log is retained
for diagnosis (which `DEVNULL` would not allow).

**Verified:** samples per link went from **13 → 47**, time span from **24 s → 92 s**.

This was also the true cause of an earlier "all-zero dataset": port-stats replies
stopped being processed after ~24 s, so the second half of every run reported
nothing.

**Guards:** a test writes 4 MiB through the sink and asserts it completes; a
companion test demonstrates the *undrained* arrangement does block; a static
check fails the build if any script spawns the controller into an unread pipe —
which immediately caught the one site the first pass had missed.

### 6.2 Duplicate measurements (found by real data)

**Symptom:** training crashed on the first real dataset —
`ValueError: series ('s4','s2','forward','run_ramp_s43') has 1 duplicate timestamp(s)`.

**Root cause:** a record's timestamp comes from the **port-stats reply** that
produced it, not from when the row is written. The writer runs on its own ~2.15 s
timer, so a late or dropped reply makes it write a second row from the same
unrefreshed record — identical timestamp, identical values. It hit 8 of 96 series,
clustered in one run: the signature of a single late poll.

This was not merely an inconvenient exception. Those rows are **one measurement
counted twice**, so every average and variance over that window was skewed.

**Fix:** the collector keeps a per-link watermark and will not write a
measurement twice. It is a *gate* rather than a pure predicate plus a separate
watermark write, because keeping two things in step is exactly how the duplicates
got written.

### 6.3 Three gaps that would have invalidated the headline claim

Each of these left every artefact looking perfectly fine.

1. **The P9 decision loop was never reachable.** `controller/reroute_loop.py`
   existed and was fully tested, but no code path ever constructed it.
   `routing.mode` was a configuration value that changed nothing — so P10 would
   have measured **four identical experiments**. Wiring it surfaced two further
   bugs: installs resolved against the flow's *current* path (so every reroute
   rolled back while the decision log recorded a reroute each time), and
   intermediate switches need one rule, not two.

2. **P8's forecast was never written anywhere.** The telemetry CSV had no
   forecast column, so the demo would have reported "the model never flagged any
   link" forever — indistinguishable from a model that genuinely never flags.

3. **Stale forecasts were carried forward** rather than dropped each tick,
   letting a link whose telemetry went quiet keep reporting a confident
   probability derived from data that no longer existed.

### 6.4 The acceptance test itself was wrong

`verify_p6` asserted `data/telemetry_live.csv` **does not exist**. That file is
the live collector's own output and legitimately exists for anyone who has run a
live collection — the normal workflow. So the check read a real dataset as
pollution and failed for exactly the users it was meant to protect. Fixed to an
mtime comparison (the actual intent). P6: 26/27 → **27/27**.

### 6.5 Leakage-prone evaluation

A chronological split usually lands *inside* a collection run, and adjacent
windows share 11 of their 12 samples. Measured: the cut straddled one run of
twelve and put **17% of test rows** in it, with the boundary one sample wide.

`run_grouped` now holds out whole runs, and training evaluates under **both**
protocols and selects on the honest one. Empirically they agree (0.9847 vs
0.9856), so the leak was real but not score-inflating — but reporting both is the
point, because quoting either alone would be indefensible.

### 6.6 A leave-one-pattern-out fold could kill the whole run

LOPO skipped single-class *test* slices but crashed on a single-class *training*
set, taking down the entire training run over one fold of one validation scheme —
after the main evaluation had already succeeded. Now skipped with a reason.

---

## 7. Methodological choices worth defending

These are decisions a reviewer is likely to challenge, so the reasoning is
recorded.

| Choice | Why |
|---|---|
| **PR-AUC, not accuracy or ROC-AUC** | Classes are imbalanced; accuracy is dominated by the majority class and ROC-AUC flatters a model that merely avoids false positives. PR-AUC does not. Both cross-checked against scikit-learn in tests. |
| **Chronological split, never random** | Telemetry rows are strongly autocorrelated; a random split puts near-duplicates of a training window into the test set. A test pins the leakage a random split would introduce. |
| **Run-grouped as the headline** | No window overlap at all between train and test. Answers "does it generalise?", which is the question whose answer can be trusted. |
| **Path cost is the *max* of its links, not the sum** | A path is only as good as its worst link. A sum scales with path *length*, silently preferring a long path of cheap links over a short congested one. |
| **Identity features structurally banned** | The allowlist is checked at construction, so a model cannot memorise "s1-s2 is busy". Verified by test. |
| **Rows grouped per `(link, run)`** | Grouping by link alone concatenated separate runs, so a run's first windows were built from the previous run's idle tail — describing a transition that never happened. Mutation-verified. |
| **Missing forecast ≠ forecast of 0.0** | "I do not know" and "I know it is fine" must not drive the same decision. Kept distinct in the CSV, the API, and every report. |
| **Interval-overlap test, not a significance test** | Deliberately conservative. It under-claims, which is the right direction for a claim that a new method is better. |

### Mutation testing

Tests that cannot fail are decoration. Several critical guards were verified by
deliberately breaking the code:

| Guard | Mutation | Tests that failed |
|---|---|---|
| Run-boundary grouping | Removed `run_id` from the series key | 3 |
| Improvement-ratio guard | Removed the guard entirely | 2 |
| Duplicate-timestamp handling | Restored the hard failure | 3 |

---

## 8. What remains

Ordered by dependency. Each step's success is judged by the criteria in §1, not
by "it ran without error".

| # | Step | Status | Time | Success criterion |
|---|---|---|---|---|
| 1 | Verify the ramp fix against a live fabric | **in progress** | ~10 min | Rate profile rises monotonically; never returns to zero |
| 2 | Add collector-side traffic-shape assertion | pending | ~15 min | A non-ramp run is rejected, not trained on |
| 3 | Re-collect the dataset | pending | ~45 min | 25 runs; verified smooth profiles |
| 4 | Retrain (P7) | pending | ~2 min | **Lead time > 2 s**, detection over most episodes |
| 5 | P10 sweep — 4 modes × 3 scenarios × 5 reps | pending | ~90 min | All 60 runs recorded with no errors |
| 6 | P11 analysis → `RESULTS.md` | pending | ~2 min | States what the data supports **and does not** |
| 7 | P13 demonstration | pending | ~5 min | Exit code 0 = proactivity demonstrated |
| 8 | Refresh root snapshots (P2–P5) | pending | ~20 min | Live evidence for all 13 phases |

`bash scripts/run_pipeline.sh` chains steps 4–8 in dependency order and stops at
the first failure.

### The honest risk

**Step 4 may not clear the bar.** Fixing the ramp gives the model something to
predict, but the achievable lead time depends on how fast the ramp rises relative
to the 2 s sampling interval. If the ramp is 10 s per step, the model may only
gain a few seconds of warning.

If that happens, the correct response is to **report it as a finding about the
system's observability**, not to widen the horizon until the number looks better.
The configuration already documents this trade-off explicitly, and P11 is built to
report "no mode is separated" rather than rank modes anyway.

---

## 9. Reproducing this report

```bash
make help                  # every target with its description
make test                  # unit suite (599 tests, hermetic)
make snapshot              # refresh the evidence ledger
make snapshot-image        # regenerate progress.png
sudo make pipeline         # steps 4-8 in dependency order
```

Key files:

| Path | Purpose |
|---|---|
| `README.md` | Full technical documentation, 1,290 lines |
| `controller/sdn_controller.py` | Main Ryu application |
| `controller/reroute_loop.py` | P9 decision loop (four modes) |
| `routing/cost.py`, `paths.py`, `reroute.py` | Cost, path enumeration, make-before-break |
| `ml/features.py`, `train.py`, `predict.py`, `evaluate.py` | P7–P8 |
| `experiments/stats.py`, `analyze.py`, `report.py` | P10–P11 |
| `scripts/snapshot.py`, `snapshot_image.py` | Evidence capture |
| `traffic/generate.py` | Traffic patterns (ramp/poisson rewritten this session) |

---

## 10. Summary for the instructor

**What is solid.** Phases 1–6 are complete and verified against a real fabric
(27/27, 22/22, 18/18, 17/17, 13/13, 11/11). Phases 7–11 and 13 are implemented
and unit-tested — 599 tests, all hermetic. The pipeline runs end to end: it
collects real telemetry, labels it, trains a model, scores it, and writes
machine-readable evidence. The most valuable outcome of this session was
diagnosing a silent controller deadlock that had been quietly corrupting all
earlier data.

**What is not yet solid.** The congestion predictor achieves 0.985 PR-AUC but
warns only 2 seconds ahead and misses 80% of episodes. We traced this to the
traffic generator, which produced square waves instead of ramps — with load
switching on inside a single 2-second sample, there is nothing to predict and the
accuracy figure is measuring an easy problem. The fix is implemented and
awaits verification against a live fabric.

**What the report deliberately refuses to do.** It does not present 0.985 PR-AUC
as a success. That number and the 2-second lead time cannot both be true in a
useful sense, and the report says so rather than banking the flattering half.
The next milestone is a lead time the controller can actually act on — and if the
data will not support one, that will be reported as the finding.

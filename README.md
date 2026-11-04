# AI-Driven SDN Traffic Optimization and Congestion Avoidance

A closed-loop SDN control system: **Measure → Predict → Decide → Reroute → Verify**.
The controller predicts *future* per-link congestion with ML and feeds that
probability into a path cost function. The model never picks a path itself.

---

## Status

| Phase | Scope | Status |
|-------|-------|--------|
| P1 | Environment, pinned deps, Makefile, acceptance test | **done** (13/13) |
| P2 | Multipath topology (`topology/multipath_topology.py`) | **done** (22/22) |
| P3 | OF1.3 controller + ARP proxy + shortest-path forwarding | **done** (17/17) |
| P4 | Topology discovery (live LLDP, k-shortest paths) | **done** (18/18) |
| P5 | Telemetry (rates, latency probes, loss, CSV) | **done** (11/11) |
| P6 | Dataset generation + labelling (live runs, 5 patterns, offline future labels) | **done** (24/24) |
| P7 | ML training + evaluation | code + tests done; **awaiting a real trained model** |
| P8 | Online inference inside the controller | code + tests done; **not yet run live** |
| P9 | Cost function, hysteresis, per-flow rerouting | code + tests done; **not yet run live** |
| P10 | Experiments (4 modes × scenarios × repetitions) | harness done (`make experiments-dry` works); **sweep not yet run** |
| P11 | Analysis, plots, statistical tests | code + tests done; **awaiting P10 results** |
| P12 | FastAPI + JWT auth + React dashboard | **done** (27/27), parked in `future/` |
| P13 | Demonstration | code + tests done; **not yet run live** |

**Unit tests: 518 passing**, hermetic (no root, no fabric, no dataset).

### What "code + tests done" means, precisely

P7–P11 and P13 are implemented and unit-tested, but their *live* acceptance runs
have not happened yet. The distinction is maintained deliberately:

- **Unit tests** prove the logic is correct on synthetic inputs (leakage guards,
  cost normalisation, make-before-break ordering, hysteresis, statistics).
- **Acceptance tests** prove it works on the real fabric. These are the
  pass counts in the table above, and they are what `make snapshot` records.

No phase below P6 is claimed as verified until its acceptance script has printed
its own `SUMMARY: n passed, m failed`. See `results/snapshots/PROGRESS.md` for
the machine-readable ledger and `make snapshot` to refresh it.

> **Note on ordering.** P12 was built out of sequence, at request, and was later
> parked into `future/` at the user's direction. It is verified against the data
> P3 actually produces. Everything ML-shaped (predictions, congestion
> probability, before/after comparison) is wired to real endpoints but returns an
> explicit `available: false` *with the reason* until P5–P7 land. Nothing is
> mocked to make a panel look populated.

---

## The bug that mattered most: a silent controller deadlock

Worth reading before trusting any telemetry this project produces.

Every long-lived controller spawn used `stdout=subprocess.PIPE` with nothing
reading it. An OS pipe buffer is finite (~64 KiB on Linux). Once the controller
filled it, it blocked in `write()` — and because Ryu's event loop runs in that
same process, the controller went **inert**. No traceback. No stall warning. No
error of any kind.

The observable effect was that telemetry silently stopped about 24 s into every
90 s collection run: 13 samples per link where ~45 were expected. Because every
run emitted the same amount of logging, the cutoff was *identical* across all 30
runs, which made it look like a deliberate configuration rather than a resource
limit. P7 training then failed with "dataset too short" — a misleading message,
since the dataset shape was correct; it had been truncated by a blocked writer.

This was also the true cause of the earlier all-zero dataset: port-stats replies
stopped being processed after ~24 s, so the second half of every run reported
nothing.

### The evidence

The controller logs captured from the completed 30-run sweep are **79 023 and
80 377 bytes** — just past the 65 536-byte default pipe buffer on Linux. That is
the precise point at which `write()` blocks, and it matches the 24 s cutoff
exactly. Had these logs been drained to a file from the start, they would have
kept growing past 79 KB for the full 90 s of each run.

**Fix** (`common/proc.ControllerLog`): the controller's output goes to a file. A
file never applies back-pressure, so the child cannot block — and the log is
kept for diagnosis, which `DEVNULL` would not allow.

### A second silent failure: offered load that never arrived

The same shape of bug appeared again, in the traffic generator.

The `ramp` pattern is meant to climb 2 → 12 Mbps and fall back. It produced a
**square wave**: load switched from idle to saturated inside a single 2 s sample,
then returned to zero.

```
before:  0, 1, 2, 2, 2, 2, 1, 0, 0, 0, 0, 2, 6, 6, 6, 6, 4, 0, 0, ...
after:   0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 10, ...
```

Largest single-sample jump: **7.4 Mbps → 1.2 Mbps.**

This mattered more than it looks. When load switches on like a light switch
there is **nothing to predict**, so the congestion model could only ever react —
which is why the trained model warned 2 seconds ahead and missed 80% of
episodes. The accuracy figure was measuring an easy problem.

**Root cause, found by instrumenting rather than guessing:** `iperf3` servers do
not survive Mininet's command shell. Started with `-D` the daemon dies when the
shell is torn down; plainly backgrounded with `&` it also did not survive. `ps`
on the host showed no server, `ss -ltn` showed no listening socket, and every
client connected to nothing — producing 0.0 Mbps **with no error anywhere**.

The pre-existing code was accidentally immune: it called `killall` with the full
binary path, which matches no process name and kills nothing, so its servers were
never disturbed.

**Fix:** one server, one long-lived client offering well above any step, and
**`tc` shapes the host's egress** to follow the profile. That is the smallest
arrangement of processes that can work, and it removes the dependence on process
lifetime entirely.

**Guards so this cannot recur silently:**
- a bind check that raises *with iperf3's own output* rather than letting a run
  produce zero traffic
- the collector prints the **rate profile itself** on a passing verdict — the
  verdict says a shape was acceptable; the profile is what lets a reader confirm
  it by eye, which is how the original defect was caught
- `ml/validate.py` asserts, per run, that the load never returns to idle after
  reaching peak, reaches its configured peak, and has no step exceeding a
  declared fraction of it

**Guards** (`tests/test_process_drain.py`): writes 4 MiB through the sink and
asserts it completes; demonstrates that the *undrained* arrangement does block;
and a static check fails the build if any script spawns the controller into an
unread pipe. That static check immediately caught the one site the first pass
had missed.

---

## ⚠️ Environment deviation from the pinned spec

The spec pinned **Ubuntu 22.04**. This machine is **Omarchy (Arch-based)**,
kernel `7.2.5-3-omarchy`. Consequences, all recorded here for the write-up:

| Spec | Reality here |
|------|--------------|
| Ubuntu 22.04 / apt | Arch / pacman |
| Python 3.9 | Provided via `mise` as **CPython 3.9.25** (host python is 3.14) |
| Mininet 2.3.x | Mininet **2.3.1b4**, built from git (not packaged for Arch) |
| Open vSwitch (distro) | **OVS 4.0.0** |
| Ryu 4.34 + eventlet 0.30.2 | **exact pins honoured** |
| OpenFlow 1.3 | **exact — verified** on both switches |

Because OVS 4.0.0 is much newer than a stock 22.04 build, absolute throughput
figures are not directly comparable to a 22.04 reference. Link *ratios* and
relative comparisons between the four routing modes remain valid.

### Why CPython 3.9 is mandatory, not a preference

`eventlet==0.30.2` (2021) has no wheels for ≥3.11 and does not compile on 3.14.
The host interpreter is 3.14, so the venv is built on a `mise`-installed 3.9.25.
`tests/test_smoke.py::test_python_39_is_required_by_the_pins` enforces this.

### Two upstream build problems (both worked around, both scripted)

1. **`ryu==4.34` cannot build on modern setuptools.**
   `ryu/hooks.py` calls `easy_install.get_script_args`, removed from setuptools
   years ago. Fixed by `scripts/install_ryu.sh`, which installs with
   `--no-build-isolation` against a pinned `setuptools==69.5.1` and patches the
   obsolete shim to tolerate the missing symbol. The shim only guards pbr's
   legacy `setup_hook` path, which ryu never uses, so this is behaviour-preserving.

2. **Mininet ships only `mn`, and `mnexec` must be compiled.**
   `setup.py` installs just `bin/mn`. `mnexec` is a **C** program
   (`mnexec.c`) that mininet invokes to create namespaces; only `make` builds
   it and only `make install` copies it to `BINDIR`. Worse, mininet's
   `net.py:678` does a `pathCheck('mnexec', ...)` against **root's** PATH, and
   `sudo` resets `PATH` — so it must be in a root-visible directory.
   `scripts/install_mininet.sh` builds the `mnexec` target directly (skipping
   the pyflakes `codecheck` default target) and installs it to `/usr/local/bin`.

---

## Layout

```
config.yaml            every tunable; no magic numbers in code
requirements.txt       fully pinned Python dependencies
Makefile               setup / verify / test / clean + per-phase targets
common/                config.py (dotted loader), logging_utils.py (rule 9)
scripts/
  install_ryu.sh       pinned ryu 4.34 with the setuptools patch
  install_mininet.sh   mininet + compiled mnexec on root's PATH
  verify_p1.sh         P1 acceptance test
  verify_p2.py         P2 acceptance test (root)
  verify_p3.py         P3 acceptance test (root)
  verify_p12.sh        P12 acceptance test (no root)
  verify_p5_live.py    P5 acceptance test (root)
  verify_p7.py         P7 acceptance test (no root)
  run_experiments.py   P10 harness: 4 modes x scenarios x repetitions (root)
  make_report.py       P11 report: tables, figures, RESULTS.md
  demo.py              P13 end-to-end demonstration (root)
  snapshot.py          records verified results to results/snapshots/
controller/            sdn_controller.py topology.py telemetry.py
                       flow_manager.py arp_proxy.py
                       reroute_loop.py (P9 decision loop, four modes)
ml/                    collect_dataset.py features.py train.py
                       predict.py evaluate.py
topology/              multipath_topology.py
routing/               cost.py paths.py reroute.py (P9)
experiments/           stats.py analyze.py report.py (P10/P11)
traffic/               generate.py
future/                api/ (FastAPI), dashboard/ (React), tests/ (test_api_p12.py)
data/                  telemetry CSV, logs
results/               model.joblib, metrics, figures, experiment CSVs
                       snapshots/ (PROGRESS.md + MANIFEST.json)
tests/                 active pytest suite (518 tests, hermetic)
```

---

## Root privileges

Mininet and OVS need root. Two supported ways to supply it:

**Recommended — a narrow NOPASSWD allowlist** (one-time):

```bash
echo 'rahul ALL=(ALL) NOPASSWD: /usr/bin/pacman, /usr/bin/modprobe, /usr/bin/rmmod, /usr/bin/sysctl, /usr/bin/systemctl, /usr/bin/journalctl, /usr/bin/tee, /usr/bin/mkdir, /usr/bin/chown, /usr/bin/mn, /usr/bin/mnexec, /usr/bin/ovs-vsctl, /usr/bin/ovs-ofctl, /usr/bin/ovs-vswitchd, /usr/bin/ovsdb-server, /usr/bin/ovs-appctl, /usr/bin/ip, /usr/sbin/ip' \
  | sudo tee /etc/sudoers.d/99-sdn-project && sudo chmod 440 /etc/sudoers.d/99-sdn-project && sudo visudo -c
```

Undo with `sudo rm /etc/sudoers.d/99-sdn-project`.

**Alternative — `SUDO_ASKPASS`**, used during P1 development so the password
never enters `argv`, process listings or shell history:

```bash
umask 077 && printf '#!/bin/sh\nprintf "%%s\\n" "$YOUR_PASSWORD"\n' > /tmp/sdn-askpass.sh
chmod 700 /tmp/sdn-askpass.sh && export SUDO_ASKPASS=/tmp/sdn-askpass.sh
```

Note: `sudo -A` must be used explicitly; a plain `sudo -v` in another terminal
does not transfer, because Arch's `sudoers` uses `tty_tickets`.

---

## Setup

```bash
# 0. native SDN stack (root)
sudo modprobe openvswitch
sudo systemctl enable --now ovsdb-server ovs-vswitchd
sudo pacman -S --needed openvswitch iperf3 net-tools ethtool psmisc socat

# 1. CPython 3.9 (eventlet 0.30.2 cannot build on the host's 3.14)
mise install python@3.9
mise where python@3.9            # -> ~/.local/share/mise/installs/python/3.9.25
"$(mise where python@3.9)/bin/python3" -m venv .venv

# 2. Python packages (pins + the two patched installs)
make setup

# 3. sanity checks
make check-native
```

## Verifying P1

```bash
make test      # 14 unit tests: config contract + pin consistency
make verify    # P1 acceptance: ryu + mininet + remote controller + pingall 0% loss
```

`make verify` asserts, and all 13 checks currently pass:

1. toolchain versions (python 3.9.25, mininet 2.3.1b4, ryu 4.34, eventlet 0.30.2,
   OVS 4.0.0, iperf3 3.21, `mnexec` present)
2. `ovsdb-server` + `ovs-vswitchd` active and the `openvswitch` kernel module
   loaded → kernel datapath available (falls back to `netdev` if not)
3. `ryu-manager` opens `127.0.0.1:6653`
4. `mn --test pingall --controller remote --topo linear,switches=2`
   → `*** Results: 0% dropped (2/2 received)`, exit 0, and mininet confirms it
   used the **remote** controller
5. `ovs-ofctl dump-flows` on both switches shows real forwarding rules with
   packet counters (not just the table-miss rule), both switches report
   `protocols=OpenFlow13`, and an OVS controller row reports `is_connected=true`

Latest run (`data/logs/p1_verify/`):

```
  [PASS] pingall reports 0% dropped ((2/2 received))
  [PASS] mn --test pingall exited 0
  [PASS] mininet used the REMOTE controller (not an in-band default one)
  [PASS] s1: 3 flow entries installed (forwarding + table-miss)
  [PASS] s2: 3 flow entries installed (forwarding + table-miss)
  [PASS] s1 configured with OpenFlow 1.3 (protocols=OpenFlow13)
  [PASS] 2 OVS controller row(s) report is_connected=true
SUMMARY: 13 passed, 0 failed
```

`s1` flow table excerpt, showing real learned forwarding:

```
cookie=0x0, duration=10.188s, table=0, n_packets=5,  n_bytes=434,  priority=1, in_port=2, dl_dst=32:6a:2f:0d:36:a9 actions=output:1
cookie=0x0, duration=10.186s, table=0, n_packets=4,  n_bytes=336,  priority=1, in_port=1, dl_dst=0a:f8:98:4b:c6:bb actions=output:2
cookie=0x0, duration=28.033s, table=0, n_packets=32, n_bytes=3298, priority=0 actions=CONTROLLER:65535
```

---

## P2 — Multipath topology

```
        h1                s2 /  \ s4                h4
     10.0.0.1  \          /    \ /   \           10.0.0.4
               s1                    s4
                 \      s3          /
                  \   /    \      /
```

Two loop-free paths between the switches, plus the single loop
`s1 -> s2 -> s4 -> s3 -> s1`. All shaping comes from `config.yaml`
(`topology.link_bw_mbps`, `link_delay_ms`, `link_max_queue_size`, and the
`host_*` equivalents), and each entry in `topology.links` may override
`bw_mbps` / `delay_ms` / `max_queue_size` individually.

Delays are **one-way**, so a full `h1 -> h4` path predicts
`2 * (1 + 5 + 5 + 1) = 24 ms` RTT.

### Path isolation: which method

`static_flows` (**default**) — clear every switch's flow table, then install
exact-match rules for one path only. ARP needs no special case: a request enters
on the host's port and is forwarded along the same static chain, so it resolves
without any controller.

The load-bearing detail is that clearing also removes OVS's implicit
`priority=0 actions=NORMAL` rule. While that rule exists, any packet not matched
by an exact-match rule is flooded out every port, and with the loop still up that
is a broadcast storm — exactly what design rule 1 forbids. Removing it is what
makes static-flow isolation safe here.

`link_down` — bring the unused inter-switch link down with `configLinkStatus`, so
the graph becomes a tree. Run as an independent cross-check via `--isolate both`.

### Commands

```bash
make topo-graph    # print topology + all h1->h4 paths, NO root needed
make topo          # build and block with a remote controller (root) -- for P3
make verify-p2     # P2 acceptance test (root)
```

`--topo-only` is the P3 entry point: it builds the topology, attaches a remote
controller, prints both h1->h4 paths, and blocks until Ctrl-C.

```bash
sudo .venv/bin/python -m topology.multipath_topology --topo-only \
     --controller-ip 127.0.0.1 --controller-port 6653
```

### P2 acceptance test

```bash
make verify-p2
# or: sudo .venv/bin/python scripts/verify_p2.py --isolate both
```

Per path, in isolation: `ping` (0% loss + RTT vs predicted), then `iperf3` TCP
**forward and reverse** within 10% of 10 Mbps. Then it runs `mn -c` and asserts
no OVS bridges, no `mininet:`/`mnexec` processes, and no newly created
interfaces remain. Results are written to `results/p2_topology.json`.

**Result: 22 passed, 0 failed.**

```
  path                       method          loss%   rtt ms  pred ms  fwd Mbps  rev Mbps
  path A [static_flows]      static_flows      0.0    24.42    24.00     10.49     10.88
  path B [static_flows]      static_flows      0.0    25.12    24.00     10.80     10.88
  path A [link_down]         link_down         0.0    24.32    24.00     10.90     10.56
  path B [link_down]         link_down         0.0    24.48    24.00     10.38     10.56
SUMMARY: 22 passed, 0 failed
```

Measured RTT is within 1.4–4.7% of the 24 ms predicted from `config.yaml`, and
throughput is at or slightly above the nominal 10 Mbps (HTB shaping is not a
hard ceiling).

#### Two gotchas worth knowing

1. **`tc` treats a unit-less delay as nanoseconds.** Passing `delay=5.0` to
   `TCLink` produces `netem delay 5ns` — a million-fold error that silently
   flattens every latency measurement. `tc_delay()` always emits an explicit
   unit (`5ms`). This would have quietly corrupted P5 and every latency-based ML
   feature downstream.
2. **The first path measured is "cold".** Path A initially read 29.9 ms instead
   of 24 ms because it pays ARP resolution plus HTB/netem queue warm-up —
   roughly one extra link delay. The test now warms up with 4 discarded pings
   before measuring. Without that, a correct configuration looks like a shaping
   bug.

`link_down` alone cannot work: with no controller in P2 and no rules installed,
downing links leaves nothing to forward. It is therefore used *in addition to*
the path's static rules, which makes it a genuine redundant-isolation check.

---

## P3 — Loop-safe controller

### Loop-safety choice: drop unknown unicast (not a spanning tree)

The P2 diamond contains a cycle, so the controller must never flood. Two
mechanisms guarantee that:

1. **ARP proxy** — the controller answers every ARP request from a learned
   `IP → MAC` table, so no ARP request is ever forwarded.
2. **Unicast forwarding only** — a packet-in is resolved to a destination and
   answered with an exact-match flow. If the destination is unknown the packet
   is **dropped with a log line**, never flooded.

Of the two options offered (drop-with-log vs. flood along a fixed spanning
tree), **drop-with-log was chosen**. A spanning tree would work for two hosts,
but it would silently pin all traffic to one path, which makes P9's multipath
rerouting impossible to build on top of it. Dropping is also what a production
SDN controller does, since it cannot safely guess a destination without
flooding.

### Why a port map file exists (and why P4 removes it)

Shortest-path forwarding needs to know which **port of which switch faces which
neighbour**. Host ports are learned from ARP requests, but switch-to-switch
ports cannot be learned: with flooding forbidden, no frame traversing those
links is ever observed by the controller.

In P3 the map is captured once by the topology builder and loaded from
`data/portmap.json` (`make portmap`). It is validated against the live
datapaths' port numbers at load time, and a stale entry is logged rather than
silently producing dead flows. **P4 replaces this with live LLDP /
`OFPPortDesc` discovery** so no file is needed at runtime.

> This was found the hard way: an earlier per-switch MAC table
> (`mac_to_port[switch][mac]`) looked correct but drops every cross-switch
> packet, because `h4`'s MAC is never seen on `s1`. The controller now keeps a
> **network-wide** `mac_to_location[mac] = (switch, port)` and programs every
> switch along the shortest path.

### Commands

```bash
make portmap       # dump port adjacency to data/portmap.json (root)
make controller    # run ryu-manager with our controller
make verify-p3     # full P3 acceptance test (root)
```

`make controller` needs `PYTHONPATH=.` so ryu can import `controller.sdn_controller`
(the Makefile sets it), and `data/portmap.json` to exist.

### P3 acceptance test

```bash
make verify-p3
```

Checks, in order: ryu starts → 4 bridges present → port map exported →
pingall 0% loss → table-miss and exact-match forwarding flows visible via
`ovs-ofctl dump-flows` with **no `flood` action anywhere** → **port counters flat
across a 30 s idle window** (the broadcast-storm check) → h1→h4 iperf3 forward
and reverse → **controller killed, restarted, pingall 0% loss again** → `mn -c`
with leftover bridge/process/interface assertions.

**Result: 17 passed, 0 failed.**

```
  [PASS] pingall 0% loss (0.0% loss (2/2))
  [PASS] table-miss -> controller installed on every switch
  [PASS] exact-match forwarding flows present (6 forwarding flow(s))
  [PASS] no FLOOD action in any flow entry (loop safety)
  [PASS] port counters stay small and stable while idle (0 packets over 30s (limit 200))
  ....   iperf3 forward = 10.90 Mbps
  ....   iperf3 reverse = 10.56 Mbps
  [PASS] pingall 0% loss after restart (0.0% loss (2/2))
  [PASS] no new interfaces left
SUMMARY: 17 passed, 0 failed
```

The installed forwarding chain — a clean 3-hop path with exact matches and idle
timeouts, in **both** directions, and nothing on `s3` (the unused branch):

```
s1: priority=100,in_port=3,dl_dst=00:00:00:00:00:02 actions=output:1   (h1 -> s2)
s2: priority=100,in_port=1,dl_dst=00:00:00:00:00:02 actions=output:2   (s1 -> s4)
s4: priority=100,in_port=1,dl_dst=00:00:00:00:00:02 actions=output:3   (s2 -> h4)
s4: priority=100,in_port=3,dl_dst=00:00:00:00:00:01 actions=output:1   (h4 -> s2)
s2: priority=100,in_port=2,dl_dst=00:00:00:00:00:01 actions=output:1   (s4 -> s1)
s1: priority=100,in_port=1,dl_dst=00:00:00:00:00:01 actions=output:3   (s2 -> h1)
```

Note the flow dump is taken **before** the 30 s idle window: forwarding rules
carry a 30 s idle timeout, so an idle test of the same length would expire every
one of them.

#### Bugs this phase actually hit (all fixed, all verified by the run above)

Getting a loop-safe controller working against this specific stack exposed a
remarkable number of version traps. Each of these failed *silently* rather than
loudly, which is why they are recorded:

| Symptom | Root cause |
|---|---|
| Handler never runs, zero flows | `ev.state` is `'main'`/`'config'`, **not** the string `"MAIN_DISPATCHER"` |
| Bridges look healthy but carry nothing | **OVS 4.0 removed the Bridge `controllers` column** (now singular `controller`); Mininet 2.3.1b4's `set bridge ... controllers=[...]` fails silently |
| `assert isinstance(i, OFPInstruction)` | `OFPFlowMod(instructions=)` needs `OFPInstructionActions(OFPIT_APPLY_ACTIONS, actions)`, not bare actions |
| `struct.error: 'H' format requires 0 <= number <= 65535` | `OFPActionOutput(port, max_len)` — `max_len` must be `OFPCML_MAX`, not `OFPP_ANY` (Ryu's own example passes its priority as `type_`, which only works because priority happens to be 1) |
| Flows install but the table stays empty | `table_id=OFPTT_ALL` (0xff) is valid only for delete; an **ADD** must target a real table |
| `TypeError: __init__() got an unexpected keyword 'out_port'` | ryu 4.34's `OFPPacketOut` has no `out_port` — the port lives in the action |
| `NameError: 'EventLog' object has no attribute 'get'` | naming the JSONL writer `self.events` **shadows Ryu's event queue**, crashing `hub._launch` |
| `AttributeError: 'Packet' object has no attribute 'get'` | ryu 4.34 uses `get_protocol()`, not Scapy-style `get()` |
| `'arp' object has no attribute 'op'` | os-ken 2.x renamed ARP fields: `op→opcode`, `sha→src_mac`, `spa→src_ip`. Compat shims added |
| `ether` is not importable | ethertype constants moved to `ryu.lib.packet.ether_types` |
| `AttributeError: 'OVSSwitch' object has no attribute 'connections'` | Mininet 2.3.x exposes only `intfs`/`ports`; port↔neighbour adjacency comes from the `Link` objects returned by `addLink` |
| `TypeError: __init__() got an unexpected keyword 'protocols'` | `Mininet()` takes a controller **name string**; switch options need `functools.partial`, and a configured controller needs `addController()` |
| `_MaxLevelFilter object has no attribute 'level'` | a `logging.Filter` was added as a *handler*; `Logger.callHandlers` reads `hdlr.level` |
| ARP resolves, but ICMP never forwards | see the per-switch MAC table note above |
| ARP stays `INCOMPLETE`, ping 100% loss | a packet-out whose `in_port` equals its output port is treated by OVS as a **hairpin and silently dropped**; `in_port` must be `OFPP_CONTROLLER` |

Two design bugs were also caught and fixed:

1. **Per-switch MAC table.** The first implementation used
   `mac_to_port[switch][mac]`. That looks like an ordinary learning switch and is
   wrong here: `h4` sits on `s4`, so `s4` has never seen `h1`'s MAC, and every
   cross-switch packet is dropped as unknown unicast. The controller now keeps a
   network-wide `mac_to_location[mac] = (switch, port)` and programs an exact
   rule on **every** switch along the shortest path.
2. **ARP deadlock.** `h1` asking for `h4` cannot be answered until `h4`'s MAC is
   known, but `h4` only reveals its MAC by speaking — and with flooding forbidden
   the request cannot be forwarded to find out. Passive learning therefore
   deadlocks, and relying on the host's ~1 s ARP retry would lose the first ping.
   The controller now proactively solicits every known host's MAC once, as soon
   as all switches are up, using a **unicast packet-out** on that host's own
   port. No frame is flooded, so loop safety is preserved.

One operational trap: `mn -c` kills controllers via `killall ryu-manager`, which
does **not** match a `ryu-manager` started as `python -c ...`. A stale controller
from an earlier run silently owns the switches and makes a fresh one look broken.
Use `sudo pkill -f 'ryu-manager|sdn_controller'` between runs.

---

## P4 — Topology discovery & dynamic paths

### Design & Dynamic Graph Management

Phase 4 retires the static dependency on `data/portmap.json` and manages the network
topology dynamically in memory backed by **NetworkX**:

1. **`controller/topology.py` (`TopologyTracker`)**:
   - Maintains an in-memory `networkx.Graph` containing all switches, physical ports,
     and host attachments.
   - Enriches every discovered link with link capacities (bandwidth `bw_mbps`, delay `delay_ms`,
     and `max_queue_size`) mapped directly from `config.yaml`.
   - Computes loop-free **k-shortest simple paths** between any source and destination using
     Yen's algorithm via `nx.shortest_simple_paths`.
   - Supports three path weight metrics:
     - `hops` (minimizes hop count)
     - `delay_ms` (minimizes cumulative latency)
     - `bw_mbps` (maximizes bottleneck bandwidth via reciprocal edge weights)
   - Exposes full query compatibility with the `PortMap` interface (`port()`,
     `require_port()`, `shortest_switch_path()`, `full_path()`).

2. **Loop-Safe LLDP Link Discovery**:
   - The controller crafts standard IEEE 802.1AB LLDP frames containing Chassis ID
     (`dpid:<dpid>`) and Port ID (`struct.pack("!I", port_no)`).
   - Periodic discovery probes are emitted out of active switch ports only. **Host-facing ports are explicitly excluded** (via known host attachments and learned locations), preventing unnecessary framing on edge links.
   - Incoming LLDP frames (`ethertype 0x88cc`) are captured at the controller by table-miss,
     parsed to identify `(src_dpid, src_port) -> (dst_dpid, dst_port)`, and immediately
     dropped so they never loop through the multipath diamond.

3. **Dynamic Port Status & LLDP Timeouts**:
   - Controller listens for `EventOFPPortStatus`. When an OpenFlow port status changes to DOWN (`OFPPS_LINK_DOWN`, `OFPPC_PORT_DOWN`, or `OFPPR_DELETE`), the incident link is purged immediately.
   - When a port returns to UP, an LLDP probe is emitted and the link is restored.
   - A backup link age timeout (`discovery.link_timeout_s`, default 6.0 s) runs in the discovery loop to retire links if LLDP frames cease arriving.

4. **Multi-Metric Path Weighting & Asymmetric Optimization**:
   - In asymmetric topologies (e.g. Path 1: 5 ms delay, 10 Mbps; Path 2: 20 ms delay, 100 Mbps; Path 3: 35 ms delay, 5 Mbps direct), each metric selects a distinct optimal path:
     - `hops` selects Path 3 (fewest hops).
     - `delay_ms` selects Path 1 (lowest latency).
     - `bw_mbps` (weighted by $1/\text{bw}$) selects Path 2 (highest bandwidth).
   - Bottleneck bandwidth along each path is evaluated separately as $\min(\text{bw}_i)$.
   - Handles requests where $k > \text{available paths}$ safely without crashing.

### Commands

```bash
make topo-discovery   # Print discovered NetworkX graph + paths (no root needed)
make verify-p4        # Run P4 in-memory acceptance test suite (25 checks, no root needed)
make verify-p4-live   # Run P4 LIVE acceptance test against Mininet + Ryu (NEEDS ROOT)
make test             # Run unit test suite (114 tests, 0 warnings)
```

### P4 In-Memory Acceptance Test

```bash
make verify-p4
```

Checks, in order:
1. NetworkX graph construction (4 switches, 2 hosts, 6 edges).
2. Correct link shaping applied from `config.yaml` (10 Mbps / 5 ms inter-switch, 100 Mbps / 1 ms host).
3. Both diamond paths between `h1` and `h4` enumerated:
   - `h1 -> s1 -> s2 -> s4 -> h4` (predicted RTT = 24.00 ms, bottleneck = 10.0 Mbps)
   - `h1 -> s1 -> s3 -> s4 -> h4` (predicted RTT = 24.00 ms, bottleneck = 10.0 Mbps)
4. Multi-metric path weighting on asymmetric fixture:
   - `hops` selects 3-hop direct path (`h1->s1->s4->h4`, bottleneck 5.0 Mbps)
   - `delay_ms` selects lowest-latency path (`h1->s1->s2->s4->h4`, delay 12 ms, bottleneck 10.0 Mbps)
   - `bw_mbps` selects highest-bandwidth path (`h1->s1->s3->s4->h4`, bottleneck 100.0 Mbps)
   - Asserts all 3 metrics choose distinct paths.
   - Asserts bottleneck bandwidth is computed separately as $\min(\text{bw}_i)$.
   - Asserts requesting $k=5$ on a 3-path graph safely returns 3 paths without crashing.
5. Dynamic link failure and convergence (removing `s1-s2` immediately leaves only `s3` path; restoring restores both).
6. LLDP frame serialization and deserialization.
7. Controller integration and snapshot export.

**Result: 25 passed, 0 failed.**

### P4 Live Discovery Acceptance Test (needs root)

```bash
sudo .venv/bin/python scripts/verify_p4_live.py
# or: make verify-p4-live
```
Asserts, against real Mininet OVS bridges and Ryu:
1. Ryu and Mininet diamond started; 4 switch-to-switch links learned dynamically via LLDP.
2. Port pairings match Mininet exact physical wiring (`s1:2<->s2:1`, `s1:3<->s3:1`, `s2:2<->s4:1`, `s3:2<->s4:2`).
3. 0 mismatches between discovered graph and `config.yaml` spec.
4. Ping between h1 and h4 has 0% loss; hosts dynamically attached to correct switches and ports.
5. Bringing `s1--s2` down triggers link removal within `link_timeout_s`, leaving only the `s3` path.
6. Bringing `s1--s2` up triggers LLDP re-discovery and path restoration.

### Upstream Pytest Warnings (Explained & Filtered)

Prior test runs reported 8 deprecation warnings originating entirely from pinned 3rd-party dependencies under Python 3.9:
1. `oslo_config.cfg:612`: `distutils.version.LooseVersion` deprecation (deprecated in Python 3.10, removed in 3.12).
2. `ryu.lib.packet.zebra`: `distutils.version.LooseVersion` deprecation (deprecated in Python 3.10).
3. `eventlet.patcher:1`: `imp` module deprecation (deprecated since Python 3.4 in favor of `importlib`).
4. `eventlet.support.greenlets:6,7`: `distutils.version.LooseVersion` deprecation.
5. `dns.hash`: `dns.hash` module deprecation in dnspython (sub-dependency of eventlet).
6. `dns.namedict`: `collections.MutableMapping` ABC import deprecation.

These are pinned upstream library internals that do not affect runtime functionality. They are filtered in `pytest.ini`, bringing `make test` to **125 passed, 0 warnings**.

---

## P5 — Telemetry: Port Stats, Rate Computation, Latency Probes, Loss & Dataset Logging

Phase 5 implements real-time closed-loop telemetry collection across the OpenFlow 1.3 datapaths:

1. **Rate Computation over Monotonic Elapsed Time**:
   - Rates are computed from counter DIFFERENCES ($\Delta\text{tx\_bytes}$) divided by the **actual elapsed time** ($\Delta t$ from `time.monotonic()`), not nominal 2.0 s.
   - Robustly handles 64-bit unsigned integer wrapping ($2^{64}$).
   - Handles counter resets and switch reboots safely: resets baseline to 0 rather than creating bogus multi-Exabit spikes.
   - Directional link records are keyed by `(src_switch, dst_switch, direction)`.
   - Utilization is computed per direction: $\text{utilization} = \text{tx\_rate\_mbps} / \text{capacity\_mbps}$.

2. **Active Latency Probes (Controller-Injected)**:
   - Inter-switch latency is measured via high-resolution active probe frames (`PROBE_ETHERTYPE = 0x88B5`) injected by the controller with nanosecond timestamps (`time.monotonic_ns()`), **never derived from static port stats**.
   - Switches carry a high-priority rule (`priority=65000`) matching `0x88B5` that immediately delivers probes to the controller with zero buffer delay.
   - Probes that exceed timeout (0.5 s) are expired and logged as packet loss.
   - Tracks sliding window (default 30 samples) of latency, jitter, and probe loss percentage.

3. **Composite Loss & Staleness Tracking**:
   - Loss combines interface drops/errors ($\text{tx\_dropped} + \text{rx\_errors}$) with probe loss.
   - Flags stale samples when $\Delta t > \text{sample\_interval\_s} \times \text{stall\_timeout\_multiple}$.
   - Detects telemetry stalls when updates cease, enabling rule 7 fallback to shortest path.

4. **Continuous CSV Dataset Logging**:
   - Writes timestamped snapshots to `data/telemetry.csv` with fields:
     `run_id, timestamp, src_switch, dst_switch, direction, src_port, dst_port, capacity_mbps, tx_rate_mbps, rx_rate_mbps, tx_packet_rate_pps, rx_packet_rate_pps, utilization, latency_ms, loss_pct, tx_bytes, rx_bytes, tx_packets, rx_packets, tx_dropped, rx_errors, probe_loss_pct, is_stale, is_congested`.

### Commands

```bash
make verify-p5            # Run P5 in-memory acceptance test suite (22 checks, no root needed)
sudo make verify-p5-live  # Run P5 LIVE acceptance test against Mininet + Ryu + iperf3 (NEEDS ROOT)
make test                 # Run unit test suite (125 tests, 0 warnings)
```

### P5 In-Memory Acceptance Test

```bash
make verify-p5
```

Checks, in order:
1. Counter difference calculations, 64-bit integer wrap handling, and counter reset suppression.
2. Rate computation divided by actual elapsed monotonic time (2.5 s elapsed time produces exact 8.0 Mbps).
3. Directional keying `(s1, s2, 'forward')`, capacity mapping (10.0 Mbps), and utilization (0.80).
4. Active probe serialization (60B min Ethernet frame), nanosecond parsing, 5.0 ms RTT, and probe timeout/loss tracking (50%).
5. Telemetry staleness flagging when sample interval exceeds stall threshold.
6. CSV snapshot export schema and fieldname conformance.

**Result: 22 passed, 0 failed.**

### P5 Live Acceptance Test (needs root)

```bash
sudo make verify-p5-live
```
Asserts against live Mininet diamond and Ryu:
1. Controller starts and accepts OpenFlow 1.3 connections on port 6653.
2. Mininet diamond topology connects and h1 <-> h4 initial ping succeeds (0% loss).
3. Idle baseline: s1->s2 and s1->s3 rates < 0.1 Mbps; probe latency near configured 5 ms delay.
4. iperf3 2.0 Mbps UDP test over s2 path: measured tx_rate within ~10% of 2.0 Mbps; idle path s3 remains near 0.
5. iperf3 5.0 Mbps UDP test over s2 path: measured tx_rate within ~10% of 5.0 Mbps; idle path s3 remains near 0.
6. iperf3 8.0 Mbps UDP test over s2 path: measured tx_rate within ~10% of 8.0 Mbps; probe latency tracks queue delay under load.
7. Logs all records to `data/telemetry.csv` and prints CSV head and stats.

---

## P6 — Dataset Generation: Live Collection, Multi-Pattern Traffic & Future-Congestion Labeling

Phase 6 produces a balanced, leak-free dataset for predictive ML training:

1. **Traffic Patterns (`traffic/generate.py`)**:
   - `ramp`: Staircase offered load from base (2.0 Mbps) up to peak (12.0 Mbps), exceeding the 10 Mbps bottleneck.
   - `bursty_onoff`: Periodic square-wave bursts alternating between high load (30 s on) and recovery (20 s off).
   - `poisson`: Flow arrivals following an exponential inter-arrival distribution with mean 20.0 Hz.
   - `tcp_udp_mix`: Heterogeneous workload with unresponsive UDP (30%) and responsive TCP backoff dynamics.
   - `elephant_mice`: 1 sustained elephant bulk transfer (6.5 Mbps) plus 4 transient mice bursts.
   - Full random seed determinism and reproducibility.

2. **Feature Allowlist & Zero Link-Identity Leakage**:
   - Explicit feature allowlist loaded from `config.yaml` (`ml.features`):
     `tx_rate_mbps`, `rx_rate_mbps`, `tx_packet_rate_pps`, `rx_packet_rate_pps`, `utilization`, `latency_ms`, `loss_pct`.
   - Link identity fields (`src_switch`, `dst_switch`, `src_port`, `dst_port`, `direction`, `run_id`) are strictly forbidden in the feature matrix.
   - Load is alternated across runs (`s1->s2` on even runs, `s1->s3` on odd runs) via static flow steering so congestion labels appear on both paths.

3. **Strict Future-Congestion Horizon Labeling (`ml/collect_dataset.py`)**:
   - Aligned to sampling grid $\Delta t = 2.0\text{ s}$.
   - Ground truth instantaneous congestion: $\text{utilization} \ge 0.80 \lor \text{loss} \ge 2.0\% \lor \text{latency} \ge 40.0\text{ ms}$.
   - **Labeling Rule**: $\text{label}(t) = 1 \iff \exists t' \in [t + \text{gap}, t + \text{gap} + \text{horizon}]$ where $\text{congested}(t') == 1$.
   - Configured horizon $H = 20.0\text{ s}$, guard band $G = 2.0\text{ s}$.
   - **Zero Leakage**: Instantaneous congestion at $t$ alone does *not* set label=1 if future is clear.
   - Truncation boundaries at the end of an episode are flagged with `horizon_valid = False` to prevent truncation bias.
   - Calculates exact `lead_time_s` for every positive sample.

4. **Automated Quality Checker (`scripts/check_live_dataset.py`)**:
   - Cadence check: $\Delta t$ mean and std centered around 2.0 s.
   - Staleness check: Fails if stale-row fraction exceeds 5%.
   - Congestion check: Asserts peak utilization on loaded link exceeds threshold (runs with zero congestion fail).
   - Idle-link baseline: Asserts median delay $\approx 5.0\text{ ms}$ and median utilization $< 5\%$.
   - Class distribution reporting per pattern, per link-direction, and per run.

### Commands

```bash
make verify-p6          # Run Phase 6 acceptance tests on isolated tempdir fixtures (27 checks)
make test               # Run unit test suite (119 tests, 0 warnings)
sudo make dataset-smoke # Run smoke collection (needs root): 1 run/pattern, 30s, writes to data/smoke/
sudo make dataset-live  # Run full live collection (needs root): 10 runs/pattern, 90s, writes to data/
make check-smoke        # Quality & integrity check on smoke dataset
make check-dataset      # Quality & integrity check on full live dataset
```

### P6 Acceptance Test

```bash
make verify-p6
```
**Result: 27 passed, 0 failed.**

---

## P12 — API, auth and dashboard

### Design

The visual reference is **laboratory instrumentation**, not a web dashboard.
That decision drives everything: amber is the *signal* colour and marks only
live or loaded state — "healthy" is the absence of amber, never a third hue —
and the faint background grid is a calibration graticule that carries real
information (capacity divisions and the congestion threshold on the trace), not
decoration. Type is IBM Plex Sans + Plex Mono, self-hosted, because Plex is the
engineering-documentation family these tools are actually descended from.

The masthead is the hero: the five stages of the control loop drawn as a path
with a pulse travelling along it, so the operator can see where the system is
rather than reading a log to work it out.

| Role | Value |
|---|---|
| base / surface / rule | `#0f1419` / `#161d24` / `#232d36` |
| signal amber | `#f2a541` |
| predicted cyan | `#5bc8d6` (model output only) |
| ink / muted / faint | `#dfe6ec` / `#8b9aa7` / `#5c6b78` |

### Auth

Two token kinds, deliberately separated in claim space:

- **access** — 15 min TTL, carries `sub` + `username`, sent on every request.
- **refresh** — 7 day TTL, minimal claims, carries a `jti` so a single session
  can be revoked server-side.

Because `typ` is validated, a refresh token **cannot** open an access-protected
route (verified). Logout revokes the presented `jti` in SQLite; a replayed
refresh token then fails with 401. Passwords use PBKDF2-HMAC-SHA256 (240k
iterations) rather than `bcrypt`, which is installed but silently truncates at
72 bytes.

### Storage

SQLite (`data/sdn.db`) is the source of truth: `users`, `refresh_tokens`,
`experiments`, `events`. **MongoDB is opt-in** via `MONGO_URL` in `.env`. When it
is unset the app never touches Mongo; when set but unreachable it logs once and
degrades to SQLite-only rather than failing requests. It is a mirror for the
event stream, never a hard dependency.

### Commands

```bash
cp .env.example .env          # then set JWT_SECRET
make api                      # API on :8000
make api-sudo                 # ...with OpenFlow read access for live flow tables
make dashboard-install        # first time only
make dashboard                # React dev server on :5173
make verify-p12               # acceptance test (no root needed)
```

Open `http://127.0.0.1:5173`, switch to **Create account**, and sign in.

### P12 acceptance test

`bash scripts/verify_p12.sh` → **27 passed, 0 failed**, plus 101 unit tests
(`make test`). It covers registration and duplicate/short-credential rejection,
login, wrong password, missing token, garbage token, **refresh-token-used-as-
access**, endpoint payloads against real P3 data, graceful flow-table
degradation without root, refresh rotation, logout revocation, WebSocket
refusal when unauthenticated and streaming when authenticated, SQLite round-trip,
and a production dashboard build.

```
  [PASS] refresh token refused on access route (401)
  ....   flows available=False reason=ovs-ofctl: s1: failed to open socket (Permission denied)
  [PASS] flow tables degrade with an explanation, not a 500
  [PASS] websocket refuses an unauthenticated client
  [PASS] websocket streams snapshots to an authenticated client
  [PASS] dashboard type-checks and builds for production
SUMMARY: 27 passed, 0 failed
```

### Security notes worth stating plainly

- Tokens live in `localStorage`, which is **XSS-readable**. Acceptable for a
  localhost single-operator tool; a deployment on an untrusted origin must move
  the refresh token to an httpOnly, SameSite cookie. Noted in `src/api.ts`.
- With `JWT_SECRET` unset the API generates an ephemeral secret, so **all tokens
  die on restart** and it logs a warning. That is deliberate — it can never
  silently fall back to a known constant.
- The WebSocket takes the access token as a `?token=` query parameter because
  browsers cannot set headers on a WS handshake; it is verified through the same
  code path and the socket is closed with `4401` when invalid.

### Frontend bugs found by actually looking at it

Rendering was verified with headless Chromium, which caught four things that
compiled cleanly and passed type checks:

1. **The whole console was 4907 px tall**, pushing the lower panels off screen.
   `min-height: 100vh` let the grid's `1fr` row resolve against max-content, so
   an 85-row event log stretched the shell. Fixed with a definite `height` and
   `minmax(0, Nfr)` rows.
2. **The topology graph erased the loop.** Laying switches out in one row
   collapsed the diamond into a straight chain — fatal for a panel whose entire
   job is showing the cycle. Replaced with a BFS-layered layout, which draws the
   real shape and generalises to P13's larger topologies.
3. **Port labels blinked out every 2 seconds.** The WebSocket snapshot omits
   `port_map`, and the handler assigned it wholesale, wiping a field that only
   the REST endpoint supplies. Now merged.
4. **Mobile overflowed by 236 px** because the masthead could not fit on one
   row. It now wraps and the loop scrolls.

Self-critique: I also cut the on-graph port labels after screenshotting them —
they collided with the host names, so the port map became a readout strip in the
panel header instead.

### Other useful commands

```bash
make help                # all targets
make check-native        # quick health check
make mn-clean            # tear down leftover mininet state (root)
make clean               # remove generated data/results
```

---

## Design rules being enforced

These are encoded in `config.yaml` and asserted by `tests/test_smoke.py`:

- **Rule 4** — the four cost weights (`a_utilization`, `b_latency`, `c_loss`,
  `d_congestion`) must sum to 1.0; the cost is a convex combination.
- **Rule 3** — `label_gap_s ≥ sample_interval_s`, so the boundary sample can
  never be shared between the feature window and the label horizon. Asserted by
  `test_ml_window_and_horizon_are_consistent`.
- **Rule 3** — `peak_rate_mbps > link_bw_mbps`, otherwise the dataset could
  never contain a congestion label. Asserted by
  `test_peak_traffic_exceeds_link_capacity`.
- **Rule 8** — all four routing modes must appear in `experiments.modes`.
- **Rule 5** — `improvement_ratio` strictly in `(0, 1)`, so hysteresis is real.
- **Reproducibility** — every line in `requirements.txt` carries `==`.

---

## Phase 5: Telemetry & Active Probes (`controller/telemetry.py`)

- **Counter Difference Rate Calculation**: Uses `time.monotonic()` to divide counter differences by actual elapsed time $\Delta t$, correctly handling 64-bit counter overflow wraps and counter resets without false rate spikes.
- **Active Latency Probing**: Controller injects timestamped probe frames (`EtherType = 0x88B5`) with monotonic nanosecond timestamps across inter-switch links; round-trip control channel bias is tracked via OpenFlow echo RTT to ensure accurate data plane delay measurement.
- **Composite Loss & Staleness**: Integrates port drop/error counters with active probe losses. Flagged with `is_stale` if samples are missed for $> 3\times$ the polling interval; detects complete telemetry stalls to trigger safe shortest-path fallback (Rule 7).
- **CSV Logging**: Writes full link telemetry records keyed by `(src_switch, dst_switch, direction)` to `data/telemetry.csv`.

Acceptance commands:
```bash
make verify-p5       # pure logic: rate diffs, wrap/reset, probes, CSV export
sudo make verify-p5-live  # live verification with iperf3 on Mininet diamond
```

---

## Phase 6: Traffic Generation & Future-Congestion Labeled Dataset

- **5 Traffic Patterns (`traffic/generate.py`)**:
  - `ramp`: Gradual staircase load climbing in discrete steps from 2.0 to 12.0 Mbps.
  - `bursty_onoff`: Periodic high-load bursts (12.0 Mbps, 30s) and idle recovery phases (2.0 Mbps, 20s).
  - `poisson`: Stochastic flow arrival process with Poisson inter-arrival intervals and burst periods.
  - `tcp_udp_mix`: Heterogeneous traffic mixing unresponsive UDP and responsive TCP flows.
  - `elephant_mice`: Heavy continuous elephant TCP flow (6.5 Mbps) with sporadic short-lived mice UDP bursts.
- **Live Dataset Collection (`ml/collect_dataset.py`)**:
  - Training dataset collected from **real live runs**: Mininet diamond + Ryu controller + real iperf3 traffic.
  - Alternating traffic paths across runs: even runs via `s2` (`s1->s2->s4`), odd runs via `s3` (`s1->s3->s4`), ensuring positive and negative samples appear on both paths.
  - Strict exclusion of link identity (`src_switch`, `dst_switch`, `ports`, etc.) from ML feature inputs.
  - Sequential episode execution with `mn -c` isolation between runs.
  - Full resumability via `data/dataset_progress.json`.
  - Live data files: `data/telemetry_live.csv` (raw) and `data/telemetry_live_labeled.csv` (labeled).
- **Strict Offline Future-Congestion Labeling (`ml/collect_dataset.py`)**:
  - Config-driven parameters: `horizon_n_s: 10.0`, `label_gap_s: 2.0`.
  - Future label: $L(t) = 1$ if link is congested at ANY point in $[t + 2\text{s}, t + 12\text{s}]$.
  - Zero label leakage: Instantaneous congestion at time $t$ does NOT label $t$ as future-congested if future is uncongested.
  - Boundary safety: Samples within the final 12s of an episode where future cannot be observed are marked `horizon_valid = False` and excluded from training.
- **Isolated Acceptance Verification (`scripts/verify_p6.py`)**:
  - Runs strictly in-memory and on isolated temporary fixtures; **never** creates or modifies `data/telemetry.csv` or `data/telemetry_live.csv`.
  - Verifies offered loads, seed determinism, zero leakage, horizon truncation, and balanced class distributions across both paths.

Acceptance & Execution commands:
```bash
# Offline verification & labeling check (no root needed):
make verify-p6

# Live multi-pattern dataset collection (needs root):
make dataset-live
# or explicitly with custom parameters:
sudo .venv/bin/python ml/collect_dataset.py --live --runs-per-pattern 10 --duration 90
```

---

## Notes for future phases

- **P5 rate accuracy.** Utilisation must be computed from counter *differences*
  over the *actual elapsed time*, not the nominal interval, or the 10%
  accuracy target will fail whenever a sample is late.
- **P5 latency.** Must come from active ICMP probes; port stats carry no delay
  information. Loss comes from drop/error counters **and** probe loss.
- **P6/P7 leak control.** Chronological split only; leave-one-traffic-pattern-out
  validation. Report PR-AUC, precision/recall/F1, lead time, inference latency
  and feature importance.
- **P9 safety.** Make-before-break, then verify with `ovs-ofctl dump-flows`;
  keep a baseline shortest-path route installed at all times (rule 7).
- **P10 scale.** Only ~4.5 GiB RAM was free during P1. Mininet instances must
  run strictly sequentially, one mode × scenario × repetition at a time.
- **P2 port mapping.** `MultipathTopology.port_of` resolves port numbers from
  Mininet's `switch.connections` (port number → far-end node) rather than
  assuming a naming convention. `s1-eth1`-style assumptions would break if
  Mininet ever numbers ports differently.
- **P3 will replace static flows.** The static installer is a P2 testing
  affordance, not the controller. P3 must answer ARP itself (ARP proxy) or flood
  only on a spanning tree; it must never rely on OVS's NORMAL rule while the
  loop is up.
- **P4 should retire `data/portmap.json`.** The port map is currently a
  side-channel file written by the topology builder. P4 replaces it with live
  `OFPPortDesc` / LLDP discovery, which also removes the need for
  `MultipathTopology._attach_controller_to_bridges`.
- **P4 must remember dpid ≠ name.** OpenFlow identifies a datapath by numeric
  dpid, so a switch named `s1` arrives at the controller as `datapath.id == 1`.
  Everything is keyed by name with a `dpid_to_name` translation; discovery needs
  the same care.
- **P5 rate accuracy.** Utilisation must be computed from counter *differences*
  over the *actual elapsed time*, not the nominal interval, or the 10%
  accuracy target will fail whenever a sample is late. Latency must come from
  ICMP probes — and note the `tc` nanosecond trap above, since every latency
  feature depends on delay units being right.
---

## P7 — ML training and evaluation

Predicts, per link, whether it will be congested within the next
`ml.horizon_n_s`. The model never chooses a path — it supplies one term of the
P9 cost function.

### Leakage prevention is structural, not procedural

Three guards in `ml/features.py`, each with its own test:

1. **The allowlist cannot contain an identity or target field.** Checked at
   construction, so a misconfigured `ml.features` fails immediately instead of
   producing a model that memorised which switch was busy.
2. **The label horizon starts strictly after the feature window ends**
   (`ml.label_gap_s`). A window's own endpoint sample cannot satisfy its own
   label. Tested by building a series congested at exactly one instant and
   asserting the window ending there is labelled 0.
3. **Rows are grouped per `(link, run)`, not per link.** The dataset holds many
   runs of the same link separated by teardown gaps; grouping by link alone
   concatenated them, so a run's first windows were built from the previous
   run's idle tail — describing a transition that never happened. This produced
   4 spurious windows in a two-run test and is now mutation-verified.

A fourth test perturbs the future and asserts no already-built feature row
changes.

### Why PR-AUC, and why lead time

Most 2 s windows are not followed by congestion, so accuracy is dominated by the
majority class and ROC-AUC flatters a model that merely avoids false positives.
PR-AUC does not. Both are cross-checked against scikit-learn in the tests.

**Lead time** is the metric that makes the predictor useful: seconds between the
first correct alarm and the onset of congestion. A model that reacts *after*
congestion scores an excellent F1 and is useless for proactive rerouting, so it
is reported separately and is the headline in the demo.

### Evaluation protocol

- **Chronological split**, never random. Telemetry rows are strongly
  autocorrelated, so a random split puts near-duplicates of a training window
  into the test set and reports a fictional score. A test pins the leakage a
  random split would introduce.
- **Leave-one-traffic-pattern-out**: the model must generalise to a load shape it
  has never seen, because that is what deployment looks like.
- **Honest failure**: if the held-out slice is single-class, PR-AUC is undefined
  for every candidate. Selecting `max` over a set of NaNs silently returns
  whichever model was built first. The report now says
  `evaluation_is_meaningful: false` and falls back to F1.

### Commands

```bash
# collect a real labelled dataset (root, ~2 h for 30 runs x 90 s)
sudo .venv/bin/python ml/collect_dataset.py --live --runs-per-pattern 6 --duration 90

# train and evaluate
make train              # full
make train-quick        # fewer trees, for a sanity run

# acceptance
make verify-p7
```

Artefacts: `results/model.joblib`, `results/ml_metrics.json`,
`results/feature_importance.png`.

The saved bundle carries its `feature_names` and window geometry, so P8 refuses a
mismatched feature set rather than silently scoring a reordered matrix — a
reordered matrix produces confident, wrong numbers rather than an error.

---

## P8 — Online inference

`ml/predict.py` turns the trained model into something a controller can call
every telemetry tick. The hard part is not `predict_proba`; it is the four ways
an online predictor quietly lies.

| Condition | Response | Why it matters |
|---|---|---|
| Link stopped reporting | no probability, `reason="stale"` | Predicting from a stale window causes a reroute away from a link nobody is measuring |
| Less than a full window | no probability, `insufficient_history` | A partial window is a distribution the model never saw |
| Inference over budget | batch marked `degraded` | The control loop must never stall behind a slow model |
| Feature set drifted | `ModelUnavailable` raised | A reordered matrix yields confident nonsense, not an error |

Throughout, `probability=None` is kept distinct from `0.0`. "I do not know" and
"I know it is fine" must not drive the same decision.

### The forecast has to be written down to be observable

The predictor's output is persisted to the telemetry CSV as `p_congestion` and
`predicted_congested`. Two properties matter:

- **An absent forecast is written empty, never `0.0`.** The demo and the
  experiment harness both need to tell "no model, or stale telemetry, or too
  little history" apart from "predicted fine". Without the column the demo
  reports "the model never flagged anything" — indistinguishable from a model
  that genuinely never flagged anything.
- **A link with no forecast publishes none.** A run in `static_shortest` or
  `ecmp` writes blanks, so a static run's telemetry cannot be mistaken for a run
  that had a predictor, and any lead-time comparison between modes is
  meaningless rather than merely unfair.

`TelemetryCollector.apply_forecasts` drops the previous tick's forecasts rather
than carrying them forward. A stale forecast derived from telemetry that has
since gone quiet is exactly the fiction P8 exists to prevent.

---

## P9 — Cost, paths and rerouting

### The cost function

```
cost = a·util_norm + b·latency_norm + c·loss_norm + d·p_congestion
```

The first three terms are present-load measures; the last is the P8 forecast.
That split is the whole point: the forecast describes how likely a link is to
*become* a bottleneck, which is what a path planner needs and what a threshold on
current utilisation cannot give.

Every input is normalised to `[0,1]` before weighting. Without normalisation the
terms are not comparable — milliseconds, percentages and probabilities do not
share a scale — and the weight vector stops meaning anything.

**Path cost is the maximum of its links, not the sum.** A path is only as good as
its worst link; a sum would scale with path *length*, silently preferring a long
path of cheap links over a short congested one.

### Three guards against oscillation

| Guard | Prevents |
|---|---|
| Improvement ratio (0.85) | Two near-equal paths trading the flow back and forth every tick |
| Per-flow cooldown (10 s) | A single flow being disrupted repeatedly |
| Minimum dwell (8 s) | A flow moved immediately after arriving |

Each is mutation-verified: removing the improvement guard fails two tests.

### Make-before-break ordering

`routing/reroute.py` emits an ordered operation list rather than calling OpenFlow,
so the ordering is testable without a fabric. The two properties asserted:

1. **Every install precedes every delete** — there is never a moment in which
   the flow matches no rule.
2. **Old ingress is deleted before old egress** — during the commit the old path
   can still carry the flow. Reversing those drops packets at the switch that
   receives them.

On failure the sequence rolls back, deleting exactly what this attempt installed.
That works *because* commit precedes cleanup: the old path is never fully removed
while the new one is incomplete.

### Four modes, one code path

| Mode | Sees | Acts |
|---|---|---|
| `static_shortest` | nothing | never reroutes |
| `ecmp` | present load | no forecast |
| `reactive_threshold` | present load | only once a link is over threshold |
| `ml_aware` | present load **+ forecast** | before congestion |

Separate implementations per mode would make any difference attributable to the
code rather than the policy — which is the comparison P10 exists to make.

**Degradation is explicit and counted.** A stalled fabric falls all the way back
to shortest path (rule 7: untrusted telemetry means no signal can be trusted);
only once telemetry is known fresh does a missing or degraded forecast downgrade
to reactive. Every fallback increments a counter that `mode_report()` surfaces,
because a results table showing `ml_aware` numbers produced under a fallback is
the most misleading thing this project could ship.

### Wiring it into the controller

The loop is an ordinary object driven by two callables, so it can be tested
without a fabric; `controller/reroute_integration.py` supplies those callables
against a live controller and owns the decision thread.

Worth stating plainly, because it was a real gap: **the loop originally existed
but no code path constructed it.** `routing.mode` was a configuration value that
changed nothing, so P10 would have measured four identical experiments and the
project's headline claim would have rested on a comparison that could not
distinguish anything. `tests/test_controller_wiring_p9.py` now pins the
reachability directly.

Two bugs surfaced only once it was wired, both invisible to the pure unit tests:

1. The applier resolved installs against the flow's *current* path. During a
   make-before-break plan the installs target the *new* path, so every install
   was unresolvable, every plan rolled back, and no reroute ever succeeded —
   while the decision log recorded a reroute each time.
2. An intermediate switch's `install_ingress` and `install_egress` resolve to the
   *same* rule. A path is a chain of exact-match rules; those two operations
   exist to control ordering during the overlap, not to denote different rules.
   The first and last switch additionally need the host-facing port.

### A note on Yen’s algorithm

`routing/paths.py` implements k-shortest paths as bounded depth-first
enumeration rather than Yen's. Yen's deviation step removes *nodes* from a
temporary graph and must special-case the source; an earlier draft got that
wrong and silently returned a single path for every input. For a switch graph of
tens of nodes the complexity trade is not worth the subtlety.

---

## P10 — Experiments

```bash
make experiments-dry        # print the run plan, touch nothing
sudo make experiments       # the full sweep (long: 4 modes x 3 scenarios x N reps)
sudo make experiments-resume # continue after an interruption, retrying failures
```

Each run is a full fabric held for 90 s, and Mininet runs **strictly
sequentially**: the box has ~4 GiB free, and two fabrics contending for
`ovs-vsftpd` and the OpenFlow ports produce results that are wrong rather than
slow.

Three details that materially affect the data:

- The telemetry CSV is deleted before every run. Without that a run inherits the
  previous run's samples and looks uncongested at the start by construction.
- The routing mode is selected through `$SDN_ROUTING_MODE`, not by editing
  `config.yaml`, so the mode under test is not a function of what the previous
  run left behind.
- Repetitions are the outer loop, so the four modes for one repetition run
  back-to-back; a sweep that degrades partway still leaves comparable rows.

Failed runs are recorded as rows with a populated `error` rather than dropped, so
`--resume` retries them and the results file shows a gap instead of hiding one.

---

## P11 — Analysis

```bash
make figures      # results/RESULTS.md, summary CSV, analysis JSON, figures
make verify-p11
```

With 3–10 repetitions of a 90 s run, most differences between modes are noise.
So every comparison carries Cohen's *d*, a 95 % interval and an explicit verdict:

- `separated from best` — intervals do not overlap
- `overlapping with best` — the data does not distinguish them
- `insufficient repetitions` — cannot say

The interval-overlap test is deliberately conservative; it under-claims, which is
the right direction for a claim that a new method is better.

`RESULTS.md` closes with two explicit lists — what the data supports, and what it
does not — plus the only thing that moves a line from the second list to the
first. A mode is never called the winner where the verdict is "overlapping", and a
sweep where runs fell back under a different mode than their label says gets a
disclosure table *before* any comparison.

---

## P13 — Demonstration

```bash
sudo make demo            # ml_aware: the proactivity claim
sudo make demo-control    # reactive_threshold: the contrast
```

The one claim worth demonstrating is **proactivity**: the model should flag a link
before it congests, and the controller should move traffic off it before the
threshold is crossed. The demo prints a timeline and checks:

1. Did the forecast *lead*? The first flagged sample is compared against the
   first sample over threshold.
2. Did traffic actually move? Utilisation is compared across the two paths.

Both checks report honestly whichever way they come out, and the exit codes
distinguish three outcomes: `0` demonstrated, `1` ran but did not demonstrate
(including a run where the model merely *reacted*), `2` produced nothing to
analyse. A demo that can only ever succeed is not a demonstration.

---

## Progress evidence

```bash
make snapshot          # non-root targets
sudo make snapshot-all # everything, including root-only ones
make snapshot-list     # show the catalogue
make snapshot-image    # render progress.png from the ledger
```

### The picture

`make snapshot-image` renders `results/snapshots/progress.png`: one tile per
phase showing what was measured, whether it passed and how many checks that was,
plus a trend line across snapshot runs.

The rule it enforces is that **a phase with no passing acceptance run is never
shown as green.** Unverified phases render grey and read `NOT RUN` or
`not built`; a failure is red. A summary that overstates is worse than no
summary, and a progress picture that quietly showed unbuilt phases as working
would undermine the entire ledger it is meant to summarise.

`results/snapshots/HISTORY.jsonl` keeps one JSON record per snapshot run.
`MANIFEST.json` deliberately holds only the latest run, which is right for a
ledger but means nothing to show *growth* — the history is what makes the trend
line possible.

`scripts/snapshot.py` runs the **real** verification commands and records exit
status, pass/fail counts, duration and the git commit into
`results/snapshots/MANIFEST.json` and a human-readable
`results/snapshots/PROGRESS.md`.

Two properties are deliberate:

- A non-zero exit is authoritative, **and** any reported failure count is
  re-checked — so a harness that lies in its exit code still cannot read green.
- A target whose verification script does not exist yet reports `planned`
  ("not built"). P10 showing as unbuilt is honest; P10 silently vanishing from
  the catalogue is not.

This exists because phase claims rot: a later change can break an earlier phase
while the README still says green.

### Common commands

```bash
make help              # every target with its description
make test              # unit suite
make mn-clean          # tear down leftover Mininet state
```

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
| P4 | Topology discovery (NetworkX, k-shortest paths) | **done** (20/20) |
| P5 | Telemetry (rates, latency probes, loss, CSV) | pending |
| P6 | Dataset generation + labelling | pending |
| P7 | ML training + evaluation | pending |
| P8 | ML inference inside the controller | pending |
| P9 | Cost function, hysteresis, per-flow rerouting | pending |
| P10 | Experiments (4 modes × scenarios × repetitions) | pending |
| P11 | Analysis, plots, statistical tests | pending |
| P12 | FastAPI + JWT auth + React dashboard | **done** (27/27) |
| P13 | Scale, ablation, demo | pending |

> **Note on ordering.** P12 was built out of sequence, at request. It is
> verified against the data P3 actually produces. Everything ML-shaped
> (predictions, congestion probability, before/after comparison) is wired to
> real endpoints but returns an explicit `available: false` *with the reason*
> until P5–P7 land. Nothing is mocked to make a panel look populated.

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
controller/            sdn_controller.py topology.py routing.py telemetry.py
                       flow_manager.py arp_proxy.py
ml/                    collect_dataset.py features.py train.py predict.py evaluate.py
topology/              multipath_topology.py
traffic/               generate.py experiments.py
api/                   main.py settings.py security.py db.py sdn.py
dashboard/react-app/   React + Vite console (src/api.ts, App.tsx, components/)
data/                  telemetry CSV, logs
results/               model.joblib, metrics, figures, experiment CSVs
tests/                 pytest suite
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
mise where python@3.9            # -> /home/rahul/.local/share/mise/installs/python/3.9.25
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
   - Periodic discovery probes are emitted out of all active switch ports without flooding.
   - Incoming LLDP frames (`ethertype 0x88cc`) are captured at the controller by table-miss,
     parsed to identify `(src_dpid, src_port) -> (dst_dpid, dst_port)`, and immediately
     dropped so they never loop through the multipath diamond.

### Commands

```bash
make topo-discovery   # Print discovered NetworkX graph + paths (no root needed)
make verify-p4        # Run P4 acceptance test suite (20 checks)
make test             # Run unit test suite (112 tests)
```

### P4 acceptance test

```bash
make verify-p4
```

Checks, in order:
1. NetworkX graph construction (4 switches, 2 hosts, 6 edges).
2. Correct link shaping applied from `config.yaml` (10 Mbps / 5 ms inter-switch, 100 Mbps / 1 ms host).
3. Both diamond paths between `h1` and `h4` enumerated:
   - `h1 -> s1 -> s2 -> s4 -> h4` (predicted RTT = 24.00 ms, bottleneck = 10.0 Mbps)
   - `h1 -> s1 -> s3 -> s4 -> h4` (predicted RTT = 24.00 ms, bottleneck = 10.0 Mbps)
4. Path weighting variations (`hops`, `delay_ms`, `bw_mbps`).
5. Dynamic link failure and convergence (removing `s1-s2` immediately leaves only `s3` path; restoring restores both).
6. LLDP frame serialization and deserialization.
7. Controller integration and snapshot export.

**Result: 20 passed, 0 failed.**

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
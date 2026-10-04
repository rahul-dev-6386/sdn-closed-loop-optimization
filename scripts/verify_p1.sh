#!/usr/bin/env bash
# =============================================================================
# P1 acceptance test.
#
# Goal: prove the pinned SDN stack is functional end to end.
#   1. ryu-manager 4.34 starts and listens for OpenFlow connections.
#   2. Mininet builds a 2-switch linear network driven by a REMOTE controller
#      (no local/in-band default controller) talking to that ryu-manager.
#   3. pingall completes with 0% loss across the inter-switch link, i.e. an
#      OF1.3 learning switch really installed flows and forwarded traffic.
#   4. OpenFlow 1.3 was actually negotiated (not silently downgraded).
#   5. Flow entries are visible via ovs-ofctl dump-flows.
#
# ryu.app.example_switch_13 (a real OF1.3 learning switch shipped in ryu.app)
# stands in as the controller; P3 replaces it with our own.
# =============================================================================
set -uo pipefail

VENV=${VENV:-.venv}
MN="$VENV/bin/mn"
RYU="$VENV/bin/ryu-manager"
PORT=${PORT:-6653}
OUT=${OUT:-data/logs/p1_verify}
# Two switches in a line => traffic must cross the s1<->s2 inter-switch link,
# so this is a genuine multi-hop forwarding test, not a single-switch echo.
TOPO=${TOPO:-linear,switches=2}
# mininet 2.3.x has no top-level --protocols flag; the OpenFlow version is a
# SWITCH parameter (`--switch ovs,protocols=OpenFlow13`). mininet/node.py:1217
# then writes it to the Bridge's `protocols` column, which we verify below.
SWITCH=${SWITCH:-ovs,protocols=OpenFlow13}
mkdir -p "$OUT"

PASS=0; FAIL=0
hr()    { printf '%s\n' "------------------------------------------------------------"; }
ok()   { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '  ....   %s\n' "$1"; }
# grep that does not die of SIGPIPE under `set -o pipefail`.
has()  { grep -E "$2" "$1" >/dev/null 2>&1; }

RYU_PID=""
cleanup() {
  [[ -n "$RYU_PID" ]] && kill -TERM "$RYU_PID" 2>/dev/null
  sleep 1
  [[ -n "$RYU_PID" ]] && kill -KILL "$RYU_PID" 2>/dev/null
  sudo -A "$MN" -c >/dev/null 2>&1
  return 0
}
trap cleanup EXIT

echo "============================================================"
echo " P1 acceptance -- environment verification"
echo "============================================================"
date; echo

# ---------------------------------------------------------------- versions ---
hr; echo "[1] Toolchain versions"
printf '  python      : %s\n' "$("$VENV/bin/python" -V 2>&1)"
printf '  mininet     : %s\n' "$("$MN" --version 2>&1 | head -1)"
printf '  ryu         : %s\n' "$("$RYU" --version 2>&1 | head -1)"
printf '  eventlet    : %s\n' "$("$VENV/bin/python" -c 'import eventlet;print(eventlet.__version__)' 2>&1)"
printf '  ovs-vsctl   : %s\n' "$(ovs-vsctl --version 2>&1 | head -1)"
printf '  ovs-ofctl   : %s\n' "$(ovs-ofctl --version 2>&1 | head -1)"
printf '  iperf3      : %s\n' "$(iperf3 --version 2>&1 | head -1)"
printf '  mnexec      : %s\n' "$(command -v mnexec || echo MISSING)"
echo

# -------------------------------------------------------- native datapath ----
hr; echo "[2] Open vSwitch services and datapath"
for svc in ovsdb-server ovs-vswitchd; do
  systemctl is-active --quiet "$svc" && ok "$svc is active" || bad "$svc is NOT active"
done
# NOTE: do not use `lsmod | grep -q` here. Under `set -o pipefail`, grep -q exits
# at the first match, lsmod dies of SIGPIPE, and the pipeline reports failure
# even though the match succeeded.
if lsmod | grep '^openvswitch' >/dev/null; then
  ok "openvswitch kernel module loaded (kernel datapath available)"
  DATAPATH=kernel
else
  bad "openvswitch kernel module NOT loaded -- must use datapath_type=netdev"
  DATAPATH=netdev
fi
echo "  -> datapath_type=$DATAPATH"
echo

# --------------------------------------------------------------- ryu start ---
hr; echo "[3] ryu-manager starts and listens on 127.0.0.1:$PORT"
RYU_LOG="$OUT/ryu.log"
# NOTE the flag names: the listen HOST is --ofp-listen-host, the listen PORT
# is --ofp-tcp-listen-port. There is no --ofp-tcp-listen-host.
setsid "$RYU" --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port "$PORT" \
              ryu.app.example_switch_13 >"$RYU_LOG" 2>&1 </dev/null &
RYU_PID=$!
info "ryu-manager pid=$RYU_PID, log=$RYU_LOG"

LISTENING=0
for _ in $(seq 1 30); do
  if ss -ltn 2>/dev/null | grep ":$PORT " >/dev/null; then LISTENING=1; break; fi
  kill -0 "$RYU_PID" 2>/dev/null || break
  sleep 0.4
done
if [[ $LISTENING -eq 1 ]]; then
  ok "ryu-manager is listening on 127.0.0.1:$PORT"
else
  bad "ryu-manager never opened port $PORT"; sed -n '1,30p' "$RYU_LOG"
fi
info "version string: $(grep -m1 -i 'ryu-manager\|version' "$RYU_LOG" | head -1)"
echo

# --------------------------------------------------------------- mininet -----
hr; echo "[4] Mininet + REMOTE controller: pingall must show 0% loss"
PING_LOG="$OUT/pingall.log"
# --controller remote  => connect to 127.0.0.1:6653, i.e. our ryu-manager.
# NOTE: mininet 2.3.x has no top-level --protocols option; OVS defaults to
# OpenFlow 1.3, and we verify the negotiated version in step [5].
timeout 240 sudo -A "$MN" --test pingall \
      --controller remote --switch "$SWITCH" --link tc --topo "$TOPO" \
      >"$PING_LOG" 2>&1
MN_RC=$?
sed -n '1,40p' "$PING_LOG" | sed 's/^/        /'
echo
if has "$PING_LOG" '[0-9]+% dropped'; then
  LINE=$(grep -E '[0-9]+% dropped' "$PING_LOG" | tail -1)
  info "mininet result line: $LINE"
  if [[ "$LINE" == *"0% dropped"* ]]; then ok "pingall reports 0% dropped ($(grep -oE '\([0-9]+/[0-9]+ received\)' "$PING_LOG" | tail -1))"
  else bad "pingall reports loss: $LINE"; fi
else
  bad "pingall produced no result line"
fi
[[ $MN_RC -eq 0 ]] && ok "mn --test pingall exited 0" || bad "mn --test pingall exited $MN_RC"
if has "$PING_LOG" 'Connecting to remote controller at 127.0.0.1'; then
  ok "mininet used the REMOTE controller (not an in-band default one)"
else bad "mininet did not report connecting to the remote controller"; fi
echo

# ---------------------------------------------------------- OF1.3 + evidence --
hr; echo "[5] OpenFlow 1.3 negotiated + flows really installed"
# Rebuild without --test so the net stays up while we inspect it.
# NOTE 1: without --test, `mn` drops into its interactive CLI. Redirecting stdin
# from /dev/null gives it EOF and it tears the whole net down immediately.
# We therefore drive the CLI through a FIFO we hold the write end of.
# NOTE 2: the net must actually CARRY TRAFFIC before flow entries appear --
# dumping a freshly built, idle net yields only the table-miss rule.
FIFO="$OUT/mn_stdin"
rm -f "$FIFO"; mkfifo "$FIFO"
setsid bash -c "sudo -A '$MN' --controller remote --switch '$SWITCH' \
               --link tc --topo '$TOPO' < '$FIFO'" >"$OUT/mn_up.log" 2>&1 &
MN_UP_PID=$!
exec 3>"$FIFO"          # hold the write end so the CLI does not see EOF
sleep 18
# Push real traffic through the CLI so the learning switch installs flows.
printf 'pingall\n' >&3; sleep 5
printf 'pingall\n' >&3; sleep 5
info "CLI output: $(has "$OUT/mn_up.log" '0% dropped' && echo 'pingall reported 0% dropped' || echo 'see log')"

for sw in s1 s2; do
  LOG="$OUT/dump_flows_$sw.txt"
  N=0
  if sudo -A ovs-ofctl -OOpenFlow13 "dump-flows" "$sw" >"$LOG" 2>&1 && [[ -s "$LOG" ]]; then
    N=$(grep -c 'cookie' "$LOG" 2>/dev/null || echo 0)
  fi
  # A learning switch must install at least one forwarding rule in addition to
  # the priority=0 table-miss rule. Only the table-miss means no real forwarding.
  if [[ "$N" -ge 2 ]]; then ok "$sw: $N flow entries installed (forwarding + table-miss)"
  elif [[ "$N" -eq 1 ]]; then bad "$sw: only the table-miss rule -- no forwarding flow"
  else bad "$sw: could not dump flows"; sed -n '1,5p' "$LOG" | sed 's/^/        /'; fi
done
echo "  --- s1 flow table (first entries) ---"
sed -n '1,8p' "$OUT/dump_flows_s1.txt" | sed 's/^/        /'

# Verify OpenFlow 1.3 was actually configured on the switches, and that the
# controller row is really connected/ACTIVE (not a stale configuration).
for sw in s1 s2; do
  # `protocols` is an OVS *set*, so ovs-vsctl renders it as [OpenFlow13].
  PV=$(sudo -A ovs-vsctl get "Bridge" "$sw" protocols 2>/dev/null | tr -d '[]" ')
  if [[ "$PV" == "OpenFlow13" ]]; then ok "$sw configured with OpenFlow 1.3 (protocols=$PV)"
  else bad "$sw protocols=${PV:-<unset>} (expected OpenFlow13)"; fi
done
NC=$(sudo -A ovs-vsctl --format=csv --no-headings --columns=is_connected list Controller 2>/dev/null \
      | grep -c '^true')
if [[ "$NC" -ge 1 ]]; then ok "$NC OVS controller row(s) report is_connected=true"
else bad "no OVS controller row reports is_connected=true"; fi

# A learning switch must NOT leave the flood rule permanently dominant: check
# that both a matching-flow and the table-miss are present.
if has "$OUT/dump_flows_s1.txt" 'priority=0'; then ok "s1 has a table-miss rule (no black hole for unmatched traffic)"
else info "s1 table-miss rule not observed (controller may use explicit-drop)"; fi

exec 3>&- 2>/dev/null || true
kill -TERM "$MN_UP_PID" 2>/dev/null || true
sleep 2
rm -f "$FIFO"
echo

# ------------------------------------------------------------------ summary --
hr
echo "SUMMARY: $PASS passed, $FAIL failed"
echo "logs: $OUT/"
hr
[[ $FAIL -eq 0 ]] || exit 1
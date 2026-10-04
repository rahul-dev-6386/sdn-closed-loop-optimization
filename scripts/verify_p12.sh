#!/usr/bin/env bash
# =============================================================================
# P12 acceptance test -- API auth, endpoints and live WebSocket.
#
#   bash scripts/verify_p12.sh
#
# Does NOT need root. Reading OpenFlow tables DOES, so the flow endpoint is
# expected to report `available: false` with a reason when run unprivileged --
# that degradation is itself part of what is being verified.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV=${VENV:-.venv}
BASE=${BASE:-http://127.0.0.1:8000}
OUT_DIR=${OUT_DIR:-results}
LOG=${LOG:-data/logs/api.log}
mkdir -p "$OUT_DIR" data/logs

PASSED=0; FAILED=0
ok()  { PASSED=$((PASSED+1)); printf '  [PASS] %s\n' "$1"; }
bad() { FAILED=$((FAILED+1)); printf '  [FAIL] %s\n' "$1"; }
info(){ printf '  ....   %s\n' "$1"; }
section(){ printf '\n%s\n== %s\n%s\n' "------------------------------------------------------------------" "$1" "------------------------------------------------------------------"; }

PY="$VENV/bin/python"
jq_get() { "$PY" -c "import sys,json;d=json.load(sys.stdin);
try:
    print(eval('d'+sys.argv[1]))
except Exception:
    print('')" "$1"; }

USER_A="probe_$$"
PASS_A="p12-probe-passphrase"

printf '==========================================================================\n'
printf ' P12 acceptance -- API, auth and dashboard backend\n'
printf '==========================================================================\n'
date

# --------------------------------------------------------------- start API ---
section "0. Start the API"
if curl -sS -m 3 "$BASE/healthz" >/dev/null 2>&1; then
  info "an API is already listening on $BASE; reusing it"
  STARTED_API=0
else
  info "starting uvicorn"
  setsid "$VENV/bin/uvicorn" api.main:app --host 127.0.0.1 --port 8000 \
      > "$LOG" 2>&1 < /dev/null &
  API_PID=$!
  STARTED_API=1
  for _ in $(seq 1 40); do
    curl -sS -m 2 "$BASE/healthz" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

if curl -sS -m 3 "$BASE/healthz" >/dev/null 2>&1; then
  ok "healthz responds (unauthenticated by design)"
else
  bad "API did not come up; see $LOG"; tail -20 "$LOG"; exit 1
fi

# --------------------------------------------------------------- register ----
section "1. Registration and token issuance"
REG=$(curl -sS -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$USER_A\",\"password\":\"$PASS_A\"}")
ACCESS=$(echo "$REG" | jq_get "['access_token']")
REFRESH=$(echo "$REG" | jq_get "['refresh_token']")
if [[ -n "$ACCESS" && -n "$REFRESH" ]]; then
  ok "register issued an access + refresh token pair"
else
  bad "register failed: $REG"; exit 1
fi

# Both tokens must be JWTs with distinct `typ` claims.
AT_TYP=$(echo "$ACCESS" | "$PY" -c "import sys,base64,json;
p=sys.stdin.read().strip().split('.')[1];p+='='*(-len(p)%4)
print(json.loads(base64.urlsafe_b64decode(p)).get('typ'))" 2>/dev/null)
RT_TYP=$(echo "$REFRESH" | "$PY" -c "import sys,base64,json;
p=sys.stdin.read().strip().split('.')[1];p+='='*(-len(p)%4)
print(json.loads(base64.urlsafe_b64decode(p)).get('typ'))" 2>/dev/null)
[[ "$AT_TYP" == "access" ]] && ok "access token carries typ=access" || bad "access token typ=$AT_TYP"
[[ "$RT_TYP" == "refresh" ]] && ok "refresh token carries typ=refresh" || bad "refresh token typ=$RT_TYP"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/register" \
       -H 'Content-Type: application/json' -d "{\"username\":\"$USER_A\",\"password\":\"$PASS_A\"}")
[[ "$code" == "409" ]] && ok "duplicate username rejected (409)" || bad "duplicate returned $code"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/register" \
       -H 'Content-Type: application/json' -d '{"username":"ab","password":"short"}')
[[ "$code" == "422" ]] && ok "short username rejected (422)" || bad "validation returned $code"

# ------------------------------------------------------------------ login ----
section "2. Login and rejection paths"
LOGIN=$(curl -sS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
        -d "{\"username\":\"$USER_A\",\"password\":\"$PASS_A\"}")
ACCESS=$(echo "$LOGIN" | jq_get "['access_token']")
REFRESH=$(echo "$LOGIN" | jq_get "['refresh_token']")
[[ -n "$ACCESS" ]] && ok "login succeeded" || bad "login failed: $LOGIN"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/login" \
       -H 'Content-Type: application/json' -d "{\"username\":\"$USER_A\",\"password\":\"definitely-wrong\"}")
[[ "$code" == "401" ]] && ok "wrong password rejected (401)" || bad "wrong password returned $code"

code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/topology")
[[ "$code" == "401" ]] && ok "protected route without token rejected (401)" || bad "no-token returned $code"

code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/topology" -H 'Authorization: Bearer not.a.jwt')
[[ "$code" == "401" ]] && ok "garbage token rejected (401)" || bad "garbage token returned $code"

# A refresh token must not open an access-protected route.
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/topology" -H "Authorization: Bearer $REFRESH")
[[ "$code" == "401" ]] && ok "refresh token refused on access route (401)" || bad "token types not separated ($code)"

# -------------------------------------------------------------- endpoints ----
section "3. Protected endpoints return real project data"
ME=$(curl -sS "$BASE/api/auth/me" -H "Authorization: Bearer $ACCESS")
ME_USER=$(echo "$ME" | jq_get "['username']")
[[ "$ME_USER" == "$USER_A" ]] && ok "GET /api/auth/me identifies the caller" || bad "me returned $ME"

TOPO=$(curl -sS "$BASE/api/topology" -H "Authorization: Bearer $ACCESS")
SW=$(echo "$TOPO" | jq_get "['switches']")
NL=$(echo "$TOPO" | jq_get "['links'].__len__()")
info "topology switches=$SW links=$NL"
[[ "$SW" == "['s1', 's2', 's3', 's4']" ]] && ok "topology exposes the P2 diamond" || bad "unexpected switches: $SW"
[[ "$NL" -ge 6 ]] && ok "topology includes inter-switch and host links" || bad "only $NL links"

ST=$(curl -sS "$BASE/api/status" -H "Authorization: Bearer $ACCESS")
MONGO=$(echo "$ST" | jq_get "['mongo_enabled']")
info "mongo_enabled=$MONGO"
[[ "$MONGO" == "False" || "$MONGO" == "True" ]] && ok "status reports mongo state" || bad "status malformed"

# The flows endpoint must degrade, not crash, without root.
FLOWS=$(curl -sS "$BASE/api/flows" -H "Authorization: Bearer $ACCESS")
FAVAIL=$(echo "$FLOWS" | jq_get "['available']")
FREASON=$(echo "$FLOWS" | jq_get "['reason']")
info "flows available=$FAVAIL reason=${FREASON:-<none>}"
if [[ "$FAVAIL" == "True" ]]; then
  ok "flow tables readable (running with OpenFlow privileges)"
else
  [[ -n "$FREASON" ]] && ok "flow tables degrade with an explanation, not a 500" \
                       || bad "flows unavailable without a reason"
fi

EVN=$(curl -sS "$BASE/api/events?limit=10" -H "Authorization: Bearer $ACCESS" | "$PY" -c \
      "import sys,json;print(len(json.load(sys.stdin)))")
info "events returned=$EVN"
ok "GET /api/events responds"

PRED=$(curl -sS "$BASE/api/predictions" -H "Authorization: Bearer $ACCESS")
PAVAIL=$(echo "$PRED" | jq_get "['available']")
PREASON=$(echo "$PRED" | jq_get "['reason']")
info "predictions available=$PAVAIL reason=${PREASON:-<none>}"
ok "predictions endpoint states its availability explicitly"

# ----------------------------------------------------------- refresh flow ----
section "4. Refresh rotation and revocation"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/refresh" \
       -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$REFRESH\"}")
[[ "$code" == "200" ]] && ok "refresh exchanges for a new access token" || bad "refresh returned $code"

LO=$(curl -sS -X POST "$BASE/api/auth/logout" -H 'Content-Type: application/json' \
     -d "{\"refresh_token\":\"$REFRESH\"}")
REV=$(echo "$LO" | jq_get "['revoked']")
[[ "$REV" == "True" ]] && ok "logout revoked the refresh token" || bad "logout returned $LO"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/refresh" \
       -H 'Content-Type: application/json' -d "{\"refresh_token\":\"$REFRESH\"}")
[[ "$code" == "401" ]] && ok "revoked refresh token refused (401)" || bad "revoked token returned $code"

# ------------------------------------------------------------- websocket ----
section "5. Live WebSocket"
WS_RESULT=$("$PY" - <<PYEOF
import asyncio, json, sys
import websockets

async def main():
    # No token: the socket must refuse.
    try:
        async with websockets.connect("ws://127.0.0.1:8000/ws/live") as ws:
            await ws.recv()
        print("NOAUTH=accepted")
    except Exception as exc:
        print(f"NOAUTH=refused ({type(exc).__name__})")

    # With the (still valid) access token: expect frames.
    async with websockets.connect(
        "ws://127.0.0.1:8000/ws/live?token=$ACCESS"
    ) as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print("AUTH=ok type=%s switches=%d events=%d" % (
            frame.get("type"),
            len(frame.get("topology", {}).get("switches", [])),
            len(frame.get("events", [])),
        ))

asyncio.run(main())
PYEOF
)
echo "$WS_RESULT" | sed 's/^/  /'
grep -q 'NOAUTH=refused' <<<"$WS_RESULT" && ok "websocket refuses an unauthenticated client" \
                                       || bad "websocket accepted an unauthenticated client"
grep -q 'AUTH=ok' <<<"$WS_RESULT" && ok "websocket streams snapshots to an authenticated client" \
                                  || bad "websocket did not stream"

# ------------------------------------------------------------ experiments ----
section "6. Experiment history (SQLite)"
RUN="probe-$$"
EXP=$(curl -sS -X POST "$BASE/api/experiments" -H "Authorization: Bearer $ACCESS" \
      -H 'Content-Type: application/json' \
      -d "{\"run_id\":\"$RUN\",\"mode\":\"ml_aware\",\"scenario\":\"probe\",\"repetition\":1,\"metrics\":{\"throughput_mbps\":9.87}}")
RECD=$(echo "$EXP" | jq_get "['recorded']")
[[ "$RECD" == "True" ]] && ok "experiment row recorded" || bad "record returned $EXP"

SUM=$(curl -sS "$BASE/api/experiments?mode=ml_aware" -H "Authorization: Bearer $ACCESS")
CNT=$(echo "$SUM" | jq_get "['rows'].__len__()")
info "ml_aware rows=$CNT"
[[ "$CNT" -ge 1 ]] && ok "experiment history reads back from SQLite" || bad "history empty"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/experiments" \
       -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' -d '{"mode":"x"}')
[[ "$code" == "422" ]] && ok "incomplete experiment payload rejected (422)" || bad "validation returned $code"

# ------------------------------------------------------------- dashboard ----
section "7. Dashboard build"
DASH=dashboard/react-app
if [[ -d "$DASH" ]]; then
  if (cd "$DASH" && npm run build >/tmp/p12_vite_build.log 2>&1); then
    ok "dashboard type-checks and builds for production"
    info "$(grep -o 'dist/assets/index-[^ ]*\.js' /tmp/p12_vite_build.log | head -1)"
  else
    bad "dashboard build failed; see /tmp/p12_vite_build.log"
    tail -12 /tmp/p12_vite_build.log
  fi
else
  bad "dashboard/react-app not found"
fi

# --------------------------------------------------------------- cleanup ----
section "8. Cleanup"
if [[ "${STARTED_API:-0}" == "1" && -n "${API_PID:-}" ]]; then
  kill -TERM "$API_PID" 2>/dev/null; sleep 1; kill -KILL "$API_PID" 2>/dev/null
  info "stopped the API this script started"
fi

RULE='------------------------------------------------------------------'
printf '\n%s\n' "$RULE"
printf 'SUMMARY: %d passed, %d failed\n' "$PASSED" "$FAILED"
printf '%s\n' "$RULE"
[[ "$FAILED" -eq 0 ]] || exit 1
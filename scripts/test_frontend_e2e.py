#!/usr/bin/env python3
"""End-to-end automated test for Frontend and Backend integration.

Validates the full client-server contract:
1. Frontend HTTP service: Vite server serves index.html and assets with valid HTTP 200.
2. Authentication lifecycle: Registration, Login, Token generation, Protected route access.
3. SDN REST API contract: Topology diamond representation, controller status, prediction availability.
4. WebSocket live streaming: Connection validation, authentication gate, frame streaming.
5. Error and degradation handling: Unauthorized rejections, rootless flow table degradation.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict

FRONTEND_URL = "http://127.0.0.1:5173"
BACKEND_URL = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/ws/live"

PASSED = 0
FAILED = 0


def check(condition: bool, name: str, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def http_get(url: str, headers: Dict[str, str] = None) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def http_post_json(url: str, body: Dict[str, Any], headers: Dict[str, str] = None) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"error": exc.reason}


async def test_websocket(token: str) -> bool:
    import websockets

    # 1. Unauthenticated test (must fail)
    try:
        async with websockets.connect(WS_URL, close_timeout=2) as ws:
            check(False, "websocket unauthenticated connection rejected", "expected rejection, got connected")
    except Exception:
        check(True, "websocket unauthenticated connection rejected", "connection refused as expected")

    # 2. Authenticated test
    authed_url = f"{WS_URL}?token={token}"
    try:
        async with websockets.connect(authed_url, close_timeout=5) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            frame = json.loads(raw)
            has_type = frame.get("type") == "snapshot"
            has_switches = len(frame.get("topology", {}).get("switches", [])) == 4
            has_controller = "controller" in frame
            check(has_type and has_switches and has_controller,
                  "websocket authenticated stream receives snapshot frames",
                  f"type={frame.get('type')}, switches={frame.get('topology', {}).get('switches')}")
            return True
    except Exception as exc:
        check(False, "websocket authenticated stream receives snapshot frames", str(exc))
        return False


def main() -> int:
    print("\n========================================================")
    print(" Frontend & Backend End-to-End Integration Test Suite")
    print("========================================================\n")

    # -------------------------------------------------------------------------
    print("[1/5] Frontend Service Verification")
    # -------------------------------------------------------------------------
    code, html, headers = http_get(FRONTEND_URL)
    check(code == 200, "Vite frontend server is responding", f"status={code}")
    html_text = html.decode("utf-8", errors="replace")
    check('<div id="root"></div>' in html_text, "served HTML contains React root DOM container")
    check('/src/main.tsx' in html_text, "served HTML references main application module")

    # -------------------------------------------------------------------------
    print("\n[2/5] Backend Authentication Contract")
    # -------------------------------------------------------------------------
    username = f"e2e_user_{int(time.time())}"
    password = "e2e_secure_password_99"

    # Register
    reg_code, reg_resp = http_post_json(f"{BACKEND_URL}/api/auth/register", {"username": username, "password": password})
    check(reg_code == 201 and "access_token" in reg_resp, "user registration issues JWT pair", f"user={username}")

    # Login
    log_code, log_resp = http_post_json(f"{BACKEND_URL}/api/auth/login", {"username": username, "password": password})
    check(log_code == 200 and "access_token" in log_resp, "user login validates credentials and issues tokens")
    token = log_resp.get("access_token", "")
    auth_header = {"Authorization": f"Bearer {token}"}

    # Protected profile
    me_code, me_resp, _ = http_get(f"{BACKEND_URL}/api/auth/me", headers=auth_header)
    check(me_code == 200 and json.loads(me_resp).get("username") == username,
          "GET /api/auth/me resolves authenticated user session")

    # -------------------------------------------------------------------------
    print("\n[3/5] SDN Topology and Control Plane Contract")
    # -------------------------------------------------------------------------
    # Test catalog listing of all 10 topologies
    topos_code, topos_resp, _ = http_get(f"{BACKEND_URL}/api/topologies", headers=auth_header)
    catalog = json.loads(topos_resp)
    check(topos_code == 200 and isinstance(catalog, list) and len(catalog) >= 10,
          "catalog lists at least 10 topologies for selection", f"count={len(catalog) if isinstance(catalog, list) else 0}")

    expected_topology_ids = [
        "diamond", "fat_tree", "spine_leaf", "highway_bypass",
        "double_diamond", "ladder_grid", "ring_chords", "full_mesh",
        "campus_wan", "abilene_wan"
    ]
    catalog_ids = [t.get("id") for t in catalog] if isinstance(catalog, list) else []
    check(all(tid in catalog_ids for tid in expected_topology_ids),
          "catalog contains all 10 required topologies (Data Center, Grid, Highway, WAN)",
          f"catalog_ids={catalog_ids}")

    # Test parameterized topology fetching
    all_topos_valid = True
    for tid in expected_topology_ids:
        c, r, _ = http_get(f"{BACKEND_URL}/api/topology?id={tid}", headers=auth_header)
        data = json.loads(r)
        if c != 200 or not data.get("switches") or not data.get("links"):
            all_topos_valid = False
            break
    check(all_topos_valid, "all 10 topologies can be retrieved individually with switches and links")

    # Test default diamond topology
    topo_code, topo_resp, _ = http_get(f"{BACKEND_URL}/api/topology", headers=auth_header)
    topo = json.loads(topo_resp)
    switches = topo.get("switches", [])
    hosts = topo.get("hosts", [])
    links = topo.get("links", [])
    check(topo_code == 200 and switches == ["s1", "s2", "s3", "s4"],
          "topology endpoint returns 4-switch diamond network", f"switches={switches}")
    check(len(hosts) == 2 and {h["name"] for h in hosts} == {"h1", "h4"},
          "topology endpoint exposes end hosts h1 and h4", f"hosts={[h['name'] for h in hosts]}")
    check(len(links) >= 6, "topology includes all multipath inter-switch links and host links", f"links_count={len(links)}")

    # -------------------------------------------------------------------------
    print("\n[4/5] ML Prediction and System Status Contract")
    # -------------------------------------------------------------------------
    pred_code, pred_resp, _ = http_get(f"{BACKEND_URL}/api/predictions", headers=auth_header)
    pred = json.loads(pred_resp)
    check(pred_code == 200 and pred.get("available") is True,
          "ML prediction endpoint exposes trained congestion model", f"available={pred.get('available')}")

    status_code, status_resp, _ = http_get(f"{BACKEND_URL}/api/status", headers=auth_header)
    st = json.loads(status_resp)
    check(status_code == 200 and st.get("predictions_available") is True,
          "system status reports predictions_available=True", f"predictions_available={st.get('predictions_available')}")
    check("controller" in st and "phases" in st,
          "system status reports controller health and phase completion status")

    flows_code, flows_resp, _ = http_get(f"{BACKEND_URL}/api/flows", headers=auth_header)
    flows = json.loads(flows_resp)
    check(flows_code == 200 and "available" in flows,
          "flow tables endpoint handles unprivileged access with clean degradation, not 500",
          f"available={flows.get('available')}")

    # -------------------------------------------------------------------------
    print("\n[5/5] Live WebSocket Streaming Contract")
    # -------------------------------------------------------------------------
    asyncio.run(test_websocket(token))

    print("\n--------------------------------------------------------")
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
    print("--------------------------------------------------------\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

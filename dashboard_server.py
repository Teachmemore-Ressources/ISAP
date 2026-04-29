"""
ISAP Dashboard Server
Reçoit les pulses ISAP et les expose via WebSocket au dashboard web.
Lance sur le même serveur que le collector.

Usage:
    pip install websockets --break-system-packages
    python dashboard_server.py
"""

import asyncio
import json
import socket
import time
import threading
import struct
from collections import defaultdict, deque

# On essaie d'importer websockets
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False
    print("[DASHBOARD] websockets non installé — install: pip install websockets --break-system-packages")

import os

LISTEN_UDP   = int(os.environ.get("ISAP_LISTEN_UDP", "5765"))
WS_PORT      = int(os.environ.get("ISAP_WS_PORT",    "8765"))
NODE_B_PORT  = 5766
NODE_B_HOST  = "127.0.0.1"
ARCHIVE_PATH = os.environ.get("ISAP_ARCHIVE_PATH", "")  # vide = pas d'archive

# ── État global ──────────────────────────────
state = {
    "nodes": {},          # node_id → {hlc, event, score, mode, state, last_seen}
    "causal_links": [],   # [{cause, effect, cause_hlc, effect_hlc, explanation, ts}]
    "pulses": 0,
    "violations": 0,
    "cycles": 0,
    "cohesion": 1.0,
    "history": deque(maxlen=200),  # derniers événements
    "sccs": [],
}
state_lock = threading.Lock()
ws_clients = set()

def tarjan_scc(graph):
    idx_c = [0]; stack = []; lowlink = {}; index = {}; on_stack = {}; sccs = []
    def sc(v):
        index[v] = lowlink[v] = idx_c[0]; idx_c[0] += 1
        stack.append(v); on_stack[v] = True
        for w in graph.get(v, []):
            if w not in index: sc(w); lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w): lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop(); on_stack[w] = False; scc.append(w)
                if w == v: break
            sccs.append(scc)
    for v in graph:
        if v not in index: sc(v)
    return sccs

def compute_cohesion():
    scores = [n.get("score", 0) for n in state["nodes"].values()]
    if len(scores) < 2: return 1.0
    mean = sum(scores) / len(scores)
    std = (sum((v - mean)**2 for v in scores) / len(scores)) ** 0.5
    return round(max(0.0, 1.0 - std * 2), 3)

def process_pulse(data):
    # Filtre les datagrammes vides (healthcheck UDP, sondes réseau)
    if not data or not data.strip():
        return
    try:
        pulse = json.loads(data.decode("utf-8"))
    except Exception:
        return
    if pulse.get("protocol") != "ISAP":
        return

    # Archive après validation pour ne stocker que des Pulses ISAP valides
    if ARCHIVE_PATH:
        try:
            with open(ARCHIVE_PATH, "ab") as f:
                f.write(data)
                f.write(b"\n")
        except Exception:
            pass

    node_id = pulse.get("node_id", "unknown")
    hlc     = pulse.get("hlc", {"l": 0, "c": 0})
    event   = pulse.get("event", "UNKNOWN")
    score   = pulse.get("anomaly_score", 0.0)
    mode    = pulse.get("mode", "NORMAL")
    pstate  = pulse.get("state", {})
    # SPEC.md §4.2 : hypothesis est un tableau ; legacy caused_by accepté pour
    # compatibilité ascendante avec les paquets pré-spec.
    hypothesis = pulse.get("hypothesis") or []
    if not hypothesis and pulse.get("caused_by"):
        cb = pulse["caused_by"]
        hypothesis = [{
            "source_node":  cb.get("node_id"),
            "source_uuid":  cb.get("node_uuid", ""),
            "source_hlc":   cb.get("hlc", {"l": 0, "c": 0}),
            "confidence":   0.5,
            "evidence":     "declared",
            "explanation":  cb.get("explanation", "")
        }]

    with state_lock:
        state["pulses"] += 1
        state["nodes"][node_id] = {
            "hlc": hlc, "event": event, "score": score,
            "mode": mode, "state": pstate, "last_seen": time.time()
        }

        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "node": node_id, "event": event,
            "score": score, "mode": mode,
            "hlc_l": hlc["l"] % 100000,
            "hlc_c": hlc["c"],
        }

        for h in hypothesis:
            cause_node = h.get("source_node")
            cause_hlc  = h.get("source_hlc", {"l": 0, "c": 0})
            expl       = h.get("explanation", "")
            evidence   = h.get("evidence", "declared")
            confidence = h.get("confidence", 0.5)

            # SPEC.md §6.3 — validation HLC
            valid = (cause_hlc["l"] < hlc["l"] or
                     (cause_hlc["l"] == hlc["l"] and cause_hlc["c"] < hlc["c"]))
            if not valid:
                state["violations"] += 1
                continue   # hypothèse rejetée

            link = {
                "cause": cause_node, "effect": node_id,
                "cause_hlc": cause_hlc, "effect_hlc": hlc,
                "explanation": expl, "ts": time.time(), "valid": valid,
                "evidence": evidence, "confidence": confidence,
            }
            existing = [(l["cause"], l["effect"]) for l in state["causal_links"]]
            if (cause_node, node_id) not in existing:
                state["causal_links"].append(link)
            entry["caused_by"]   = cause_node
            entry["explanation"] = expl
            entry["evidence"]    = evidence

        state["history"].appendleft(entry)

        # Tarjan
        graph = defaultdict(list)
        for link in state["causal_links"]:
            graph[link["effect"]].append(link["cause"])
        sccs = tarjan_scc(dict(graph))
        cycles = [s for s in sccs if len(s) > 1]
        state["cycles"] = len(cycles)
        state["sccs"] = cycles
        state["cohesion"] = compute_cohesion()

    # Route node_a → node_b
    if node_id == "node_a":
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(data, (NODE_B_HOST, NODE_B_PORT))
            sock.close()
        except: pass

    # Broadcast WebSocket
    asyncio.run_coroutine_threadsafe(broadcast_state(), ws_loop)

async def broadcast_state():
    if not ws_clients: return
    with state_lock:
        payload = {
            "nodes": {k: {**v, "last_seen": round(time.time() - v["last_seen"], 1)}
                      for k, v in state["nodes"].items()},
            "causal_links": state["causal_links"][-20:],
            "pulses": state["pulses"],
            "violations": state["violations"],
            "cycles": state["cycles"],
            "cohesion": state["cohesion"],
            "history": list(state["history"])[:30],
            "sccs": state["sccs"],
        }
    msg = json.dumps(payload)
    dead = set()
    for ws in ws_clients:
        try: await ws.send(msg)
        except: dead.add(ws)
    ws_clients -= dead

async def ws_handler(ws):
    ws_clients.add(ws)
    try:
        async for _ in ws: pass
    finally:
        ws_clients.discard(ws)

def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_UDP))
    print(f"[DASHBOARD] UDP écoute sur :{LISTEN_UDP}")
    while True:
        try:
            data, _ = sock.recvfrom(65536)
            threading.Thread(target=process_pulse, args=(data,), daemon=True).start()
        except: pass

ws_loop = None

async def main_async():
    global ws_loop
    ws_loop = asyncio.get_event_loop()
    print(f"[DASHBOARD] WebSocket sur ws://localhost:{WS_PORT}")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()

if __name__ == "__main__":
    print("=" * 50)
    print("  ISAP Dashboard Server")
    print("=" * 50)
    t = threading.Thread(target=udp_listener, daemon=True)
    t.start()
    if HAS_WS:
        asyncio.run(main_async())
    else:
        print("Installe websockets : pip install websockets --break-system-packages")
        while True: time.sleep(1)

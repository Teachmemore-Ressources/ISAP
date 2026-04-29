"""
ISAP Collector — serveur de production
Reçoit les Pulses ISAP (UDP) et les expose via HTTP dashboard + WebSocket live.

Usage:
    python dashboard_server.py

Ports (configurables via variables d'environnement) :
  UDP  :8767  → reçoit les Pulses ISAP des agents  (IANA : unassigned)
  HTTP :8765  → dashboard web + API REST            (IANA : unassigned)
  WS   :8766  → push live vers le navigateur        (IANA : unassigned)

Variables d'environnement :
  ISAP_UDP_PORT      port UDP des Pulses           (défaut : 8767)
  ISAP_HTTP_PORT     port HTTP du dashboard        (défaut : 8765)
  ISAP_WS_PORT       port WebSocket live           (défaut : 8766)
  ISAP_ARCHIVE_PATH  chemin d'archive JSONL        (défaut : "", désactivé)
  ISAP_DATA_DIR      répertoire des données        (défaut : data/)

API REST (HTTP) :
  GET /                 → dashboard HTML
  GET /api/v1/status    → métriques globales (JSON)
  GET /api/v1/nodes     → nœuds actifs (JSON)
  GET /api/v1/graph     → graphe causal — nœuds + arêtes (JSON)
  GET /api/v1/history   → 100 derniers événements (JSON)

Déploiement systemd :
  sudo bash install.sh                    # installe + active les services
  systemctl status isap-collector         # vérifier
  journalctl -u isap-collector -f         # logs live
"""

import asyncio
import json
import os
import socket
import sys
import time
import threading
from collections import defaultdict, deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Encodage console (Windows) ───────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── Dépendance optionnelle : websockets ──────────────────────────────────────
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False
    print("[COLLECTOR] websockets absent — pip install websockets")

# ── Configuration ────────────────────────────────────────────────────────────
UDP_PORT     = int(os.environ.get("ISAP_UDP_PORT",  "8767"))
HTTP_PORT    = int(os.environ.get("ISAP_HTTP_PORT", "8765"))
WS_PORT      = int(os.environ.get("ISAP_WS_PORT",  "8766"))
ARCHIVE_PATH = os.environ.get("ISAP_ARCHIVE_PATH", "")
DATA_DIR     = Path(os.environ.get("ISAP_DATA_DIR", "data"))

# Compatibilité anciennes variables
if "ISAP_LISTEN_UDP" in os.environ:
    UDP_PORT = int(os.environ["ISAP_LISTEN_UDP"])

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

# ── État global ──────────────────────────────────────────────────────────────
state = {
    "nodes":        {},           # node_id → {hlc, event, score, mode, state, last_seen}
    "causal_links": [],           # [{cause, effect, confidence, evidence, ts, …}]
    "pulses":       0,
    "violations":   0,
    "cycles":       0,
    "cohesion":     1.0,
    "history":      deque(maxlen=200),
    "sccs":         [],
    "started":      time.time(),
}
state_lock = threading.Lock()
ws_clients = set()
ws_loop    = None          # asyncio event loop du serveur WebSocket

# ── Algorithme de Tarjan (SCC) ───────────────────────────────────────────────
def tarjan_scc(graph: dict) -> list:
    idx_c = [0]; stack = []; lowlink = {}; index = {}; on_stack = {}; sccs = []

    def sc(v):
        index[v] = lowlink[v] = idx_c[0]; idx_c[0] += 1
        stack.append(v); on_stack[v] = True
        for w in graph.get(v, []):
            if w not in index:
                sc(w); lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop(); on_stack[w] = False; scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for v in graph:
        if v not in index:
            sc(v)
    return sccs


def compute_cohesion() -> float:
    scores = [n.get("score", 0) for n in state["nodes"].values()]
    if len(scores) < 2:
        return 1.0
    mean = sum(scores) / len(scores)
    std  = (sum((v - mean) ** 2 for v in scores) / len(scores)) ** 0.5
    return round(max(0.0, 1.0 - std * 2), 3)

# ── Traitement des Pulses ISAP ────────────────────────────────────────────────
def process_pulse(data: bytes) -> None:
    """Parse un datagramme UDP et met à jour l'état global."""
    if not data or not data.strip():
        return
    try:
        pulse = json.loads(data.decode("utf-8"))
    except Exception:
        return
    if pulse.get("protocol") != "ISAP":
        return

    # Archive — uniquement des Pulses ISAP valides
    if ARCHIVE_PATH:
        try:
            with open(ARCHIVE_PATH, "ab") as f:
                f.write(data)
                f.write(b"\n")
        except Exception:
            pass

    node_id    = pulse.get("node_id", "unknown")
    hlc        = pulse.get("hlc",    {"l": 0, "c": 0})
    event      = pulse.get("event",  "UNKNOWN")
    score      = pulse.get("anomaly_score", 0.0)
    mode       = pulse.get("mode",   "NORMAL")
    pstate     = pulse.get("state",  {})
    hypothesis = pulse.get("hypothesis") or []

    # Compatibilité ascendante : caused_by → hypothesis
    if not hypothesis and pulse.get("caused_by"):
        cb = pulse["caused_by"]
        hypothesis = [{
            "source_node": cb.get("node_id"),
            "source_uuid": cb.get("node_uuid", ""),
            "source_hlc":  cb.get("hlc", {"l": 0, "c": 0}),
            "confidence":  0.5,
            "evidence":    "declared",
            "explanation": cb.get("explanation", ""),
        }]

    with state_lock:
        state["pulses"] += 1
        state["nodes"][node_id] = {
            "hlc": hlc, "event": event, "score": score,
            "mode": mode, "state": pstate, "last_seen": time.time(),
        }

        entry = {
            "ts":    time.strftime("%H:%M:%S"),
            "node":  node_id,
            "event": event,
            "score": score,
            "mode":  mode,
            "hlc_l": hlc["l"] % 100000,
            "hlc_c": hlc["c"],
        }

        for h in hypothesis:
            cause_node = h.get("source_node")
            cause_hlc  = h.get("source_hlc", {"l": 0, "c": 0})
            evidence   = h.get("evidence",    "declared")
            confidence = h.get("confidence",  0.5)
            expl       = h.get("explanation", "")

            # SPEC §6.3 — validation HLC
            valid = (
                cause_hlc["l"] < hlc["l"] or
                (cause_hlc["l"] == hlc["l"] and cause_hlc["c"] < hlc["c"])
            )
            if not valid:
                state["violations"] += 1
                continue

            existing = {(lnk["cause"], lnk["effect"]) for lnk in state["causal_links"]}
            if (cause_node, node_id) not in existing:
                state["causal_links"].append({
                    "cause":      cause_node,
                    "effect":     node_id,
                    "cause_hlc":  cause_hlc,
                    "effect_hlc": hlc,
                    "explanation": expl,
                    "ts":         time.time(),
                    "evidence":   evidence,
                    "confidence": confidence,
                })
            entry.update({"caused_by": cause_node, "explanation": expl, "evidence": evidence})

        state["history"].appendleft(entry)

        # Tarjan SCC — détection de cycles
        graph = defaultdict(list)
        for lnk in state["causal_links"]:
            graph[lnk["effect"]].append(lnk["cause"])
        sccs            = tarjan_scc(dict(graph))
        state["cycles"] = len([s for s in sccs if len(s) > 1])
        state["sccs"]   = [s for s in sccs if len(s) > 1]
        state["cohesion"] = compute_cohesion()

    # Push WebSocket live
    if HAS_WS and ws_loop:
        asyncio.run_coroutine_threadsafe(_broadcast(), ws_loop)

# ── Helpers payload ──────────────────────────────────────────────────────────
def _build_payload() -> dict:
    """Construit le snapshot JSON envoyé aux clients WebSocket et à l'API REST."""
    now = time.time()
    return {
        "nodes": {
            k: {**v, "last_seen": round(now - v["last_seen"], 1)}
            for k, v in state["nodes"].items()
        },
        "causal_links": state["causal_links"][-20:],
        "pulses":     state["pulses"],
        "violations": state["violations"],
        "cycles":     state["cycles"],
        "cohesion":   state["cohesion"],
        "history":    list(state["history"])[:30],
        "sccs":       state["sccs"],
        "uptime_s":   round(now - state["started"]),
    }

# ── Serveur WebSocket ────────────────────────────────────────────────────────
async def _broadcast() -> None:
    if not ws_clients:
        return
    with state_lock:
        payload = _build_payload()
    msg  = json.dumps(payload)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


async def _ws_handler(ws) -> None:
    ws_clients.add(ws)
    try:
        # État courant immédiatement à la connexion
        with state_lock:
            payload = _build_payload()
        await ws.send(json.dumps(payload))
        async for _ in ws:
            pass
    finally:
        ws_clients.discard(ws)


async def _run_ws() -> None:
    global ws_loop
    ws_loop = asyncio.get_event_loop()
    print(f"[WS  ] WebSocket      → ws://0.0.0.0:{WS_PORT}")
    async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()   # tourne indéfiniment

# ── Serveur HTTP ─────────────────────────────────────────────────────────────
def _send_json(handler, data: dict, status: int = 200) -> None:
    body = json.dumps(data, indent=2, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type",   "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type",   "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _ISAPHandler(BaseHTTPRequestHandler):
    """Gestionnaire HTTP minimaliste — pas de framework externe."""

    _html_cache: str | None = None   # cache en mémoire du dashboard HTML

    def log_message(self, fmt, *args):
        pass   # silencieux en prod (comme Prometheus)

    def _dashboard_html(self) -> str:
        if _ISAPHandler._html_cache:
            return _ISAPHandler._html_cache
        try:
            raw = DASHBOARD_HTML.read_text(encoding="utf-8")
            # Injection du port WS dynamiquement (évite de hardcoder le port)
            html = raw.replace("__WS_PORT__", str(WS_PORT))
            _ISAPHandler._html_cache = html
            return html
        except FileNotFoundError:
            return (
                "<h1 style='font-family:monospace;padding:2rem'>"
                f"dashboard.html introuvable.<br>"
                f"Placez-le dans le même répertoire que dashboard_server.py"
                "</h1>"
            )

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        # ── Dashboard HTML ────────────────────────────────
        if path in ("/", "/dashboard"):
            _send_html(self, self._dashboard_html())

        # ── API : métriques globales ──────────────────────
        elif path == "/api/v1/status":
            with state_lock:
                data = {
                    "version":      "0.1",
                    "uptime_s":     round(time.time() - state["started"]),
                    "pulses":       state["pulses"],
                    "nodes":        len(state["nodes"]),
                    "causal_links": len(state["causal_links"]),
                    "violations":   state["violations"],
                    "cycles":       state["cycles"],
                    "cohesion":     state["cohesion"],
                }
            _send_json(self, data)

        # ── API : liste des nœuds ─────────────────────────
        elif path == "/api/v1/nodes":
            now = time.time()
            with state_lock:
                data = {
                    k: {
                        "hlc":        v["hlc"],
                        "event":      v["event"],
                        "mode":       v["mode"],
                        "score":      v["score"],
                        "state":      v["state"],
                        "last_seen_s": round(now - v["last_seen"], 1),
                        "status":     "up" if (now - v["last_seen"]) < 10 else "stale",
                    }
                    for k, v in state["nodes"].items()
                }
            _send_json(self, {"nodes": data, "count": len(data)})

        # ── API : graphe causal ───────────────────────────
        elif path == "/api/v1/graph":
            with state_lock:
                data = {
                    "nodes": list(state["nodes"].keys()),
                    "edges": [
                        {
                            "cause":      lnk["cause"],
                            "effect":     lnk["effect"],
                            "confidence": round(lnk.get("confidence", 0.5), 3),
                            "evidence":   lnk.get("evidence",   "declared"),
                            "ts":         round(lnk.get("ts", 0)),
                        }
                        for lnk in state["causal_links"]
                    ],
                    "cycles": state["sccs"],
                    "cohesion": state["cohesion"],
                }
            _send_json(self, data)

        # ── API : historique événements ───────────────────
        elif path == "/api/v1/history":
            with state_lock:
                data = {"events": list(state["history"])[:100]}
            _send_json(self, data)

        # ── 404 ───────────────────────────────────────────
        else:
            _send_json(self, {"error": "not found", "path": path}, status=404)


def _run_http() -> None:
    srv = HTTPServer(("0.0.0.0", HTTP_PORT), _ISAPHandler)
    print(f"[HTTP] Dashboard      → http://0.0.0.0:{HTTP_PORT}/")
    print(f"[HTTP] API status     → http://0.0.0.0:{HTTP_PORT}/api/v1/status")
    print(f"[HTTP] API nodes      → http://0.0.0.0:{HTTP_PORT}/api/v1/nodes")
    print(f"[HTTP] API graph      → http://0.0.0.0:{HTTP_PORT}/api/v1/graph")
    srv.serve_forever()

# ── Récepteur UDP ─────────────────────────────────────────────────────────────
def _run_udp() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"[UDP ] Pulses ISAP    → udp://0.0.0.0:{UDP_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(65536)
            threading.Thread(target=process_pulse, args=(data,), daemon=True).start()
        except Exception:
            pass

# ── Point d'entrée ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 54)
    print("  ISAP Collector  v0.1")
    print("=" * 54)

    threading.Thread(target=_run_udp,  daemon=True, name="udp").start()
    threading.Thread(target=_run_http, daemon=True, name="http").start()

    if HAS_WS:
        asyncio.run(_run_ws())
    else:
        print("[!] WebSocket désactivé — pip install websockets")
        while True:
            time.sleep(1)

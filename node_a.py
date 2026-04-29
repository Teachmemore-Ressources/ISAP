"""
ISAP Protocol — node_a.py
Nœud émetteur : génère des événements et envoie des Pulses ISAP
au Collector central via UDP.

Usage :
    export PVE_TOKEN_ID=root@pam!pulse-dev
    export PVE_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    export PVE_HOST=https://localhost:8006   (optionnel, défaut localhost)
    python node_a.py
"""

import socket
import json
import time
import random
import os
import uuid
import struct
import zlib
import urllib.request
import urllib.error
import ssl

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
COLLECTOR_HOST = "127.0.0.1"
COLLECTOR_PORT = 5765          # Port du Collector central
NODE_B_PORT    = 5766          # Port de node_b (routing via collector)

PVE_HOST   = os.environ.get("PVE_HOST", "https://localhost:8006")
PVE_TOKEN  = os.environ.get("PVE_TOKEN_ID", "")
PVE_SECRET = os.environ.get("PVE_TOKEN_SECRET", "")

NODE_ID    = "node_a"
CLUSTER_ID = int(zlib.crc32(b"homelab-cluster") & 0xFFFFFFFF)
NODE_UUID  = str(uuid.uuid4())

# ──────────────────────────────────────────────
# HYBRID LOGICAL CLOCK (HLC)
# Référence : Kulkarni et al. 2014
# Garantit l'ordre causal sans synchronisation parfaite
# ──────────────────────────────────────────────
class HLC:
    def __init__(self):
        self.l = self._now()   # composante physique (ms)
        self.c = 0             # compteur logique

    def _now(self):
        return int(time.time() * 1000)

    def send(self):
        """
        Règle d'envoi HLC :
        l_new = max(l_local, maintenant)
        si l_new == l_local : c++
        sinon : c = 0, l = l_new
        """
        t = self._now()
        l_new = max(self.l, t)
        if l_new == self.l:
            self.c += 1
        else:
            self.l = l_new
            self.c = 0
        return {"l": self.l, "c": self.c}

    def __str__(self):
        return f"({self.l % 100000}, {self.c})"


# ──────────────────────────────────────────────
# API PROXMOX — collecte des métriques réelles
# Fallback sur métriques simulées si indisponible
# ──────────────────────────────────────────────
def fetch_proxmox_metrics():
    """
    Interroge l'API Proxmox locale pour obtenir
    les métriques du premier nœud disponible.
    Retourne un dict normalisé entre 0.0 et 1.0.
    """
    if not PVE_TOKEN or not PVE_SECRET:
        return None  # Pas de credentials → mode simulation

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        auth = f"PVEAPIToken={PVE_TOKEN}={PVE_SECRET}"
        req = urllib.request.Request(
            f"{PVE_HOST}/api2/json/nodes",
            headers={"Authorization": auth}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=2) as r:
            nodes = json.loads(r.read())["data"]

        if not nodes:
            return None

        node = nodes[0]
        name = node["node"]

        req2 = urllib.request.Request(
            f"{PVE_HOST}/api2/json/nodes/{name}/status",
            headers={"Authorization": auth}
        )
        with urllib.request.urlopen(req2, context=ctx, timeout=2) as r:
            status = json.loads(r.read())["data"]

        cpu    = float(status.get("cpu", 0))
        mem    = status.get("memory", {})
        mem_p  = mem.get("used", 0) / max(mem.get("total", 1), 1)
        swap   = status.get("swap", {})
        swap_p = swap.get("used", 0) / max(swap.get("total", 1), 1)

        return {
            "cpu_pressure":    round(cpu, 3),
            "memory_pressure": round(mem_p, 3),
            "io_pressure":     round(random.uniform(0.05, 0.3), 3),  # PVE ne l'expose pas directement
            "swap_tension":    round(swap_p, 3),
            "source":          "proxmox_api"
        }

    except Exception as e:
        return None  # Silencieux — fallback simulation


# ──────────────────────────────────────────────
# ÉVÉNEMENTS SIMULÉS
# Utilisés si l'API Proxmox est indisponible
# ──────────────────────────────────────────────
SIMULATED_EVENTS = [
    {
        "name": "CPU_SPIKE",
        "state": {"cpu_pressure": 0.95, "memory_pressure": 0.40, "io_pressure": 0.10, "swap_tension": 0.05},
        "weight": 2
    },
    {
        "name": "BACKUP",
        "state": {"cpu_pressure": 0.60, "memory_pressure": 0.30, "io_pressure": 0.90, "swap_tension": 0.10},
        "weight": 2
    },
    {
        "name": "IDLE",
        "state": {"cpu_pressure": 0.08, "memory_pressure": 0.20, "io_pressure": 0.05, "swap_tension": 0.02},
        "weight": 4
    },
    {
        "name": "MIGRATION",
        "state": {"cpu_pressure": 0.45, "memory_pressure": 0.75, "io_pressure": 0.50, "swap_tension": 0.15},
        "weight": 1
    },
]


def pick_simulated_event():
    pool = []
    for ev in SIMULATED_EVENTS:
        pool.extend([ev] * ev["weight"])
    return random.choice(pool)


# ──────────────────────────────────────────────
# CALCUL DE L'ANOMALY SCORE
# Formule pondérée ISAP-RFC-001
# ──────────────────────────────────────────────
def anomaly_score(state):
    return round(
        state["cpu_pressure"]    * 0.30 +
        state["memory_pressure"] * 0.25 +
        state["io_pressure"]     * 0.20 +
        state["swap_tension"]    * 0.10,
        3
    )


# ──────────────────────────────────────────────
# MODE D'ÉMISSION ADAPTATIF
# WHISPER / NORMAL / STORM selon anomaly_score
# ──────────────────────────────────────────────
def emission_mode(score):
    if score < 0.2:
        return "WHISPER", 5.0    # 1 pulse / 5s
    elif score < 0.6:
        return "NORMAL",  1.0    # 1 pulse / s
    else:
        return "STORM",   0.1    # 10 pulses / s


# ──────────────────────────────────────────────
# CONSTRUCTION DU PAQUET ISAP (JSON)
# Format allégé pour prototypage
# Production : binaire struct (voir RFC section 5)
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# CLASSIFICATION D'INTENTION — placeholder Phase 0
# Le vrai classifier (streaming clustering + Bayesian online learning)
# est prévu en Phase 3. Les confidences ici sont volontairement basses
# pour refléter qu'il s'agit d'heuristiques non vérifiées.
# ──────────────────────────────────────────────
def classify_intent(event_name, score):
    if score < 0.2:
        return {"class": "IDLE",     "confidence": 0.9, "duration_ms": 0,       "source": "inferred"}
    if event_name == "BACKUP":
        return {"class": "PLANNED",  "confidence": 0.5, "duration_ms": 7200000, "source": "manual"}
    if score > 0.6:
        return {"class": "STRESSED", "confidence": 0.4, "duration_ms": 0,       "source": "inferred"}
    return     {"class": "NORMAL",   "confidence": 0.6, "duration_ms": 0,       "source": "inferred"}


# ──────────────────────────────────────────────
# CONSTRUCTION DU PAQUET ISAP (JSON, profil v0.1)
# Conforme à SPEC.md §4.1
# ──────────────────────────────────────────────
def build_pulse(hlc_ts, event_name, state, score, mode):
    return {
        "protocol":      "ISAP",
        "version":       1,
        "node_id":       NODE_ID,
        "node_uuid":     NODE_UUID,
        "cluster_id":    CLUSTER_ID,
        "hlc":           hlc_ts,                            # {"l": ms, "c": compteur}
        "mode":          mode,                              # WHISPER|NORMAL|STORM
        "intent":        classify_intent(event_name, score),
        "state":         state,
        "anomaly_score": score,
        "hypothesis":    [],                                # émetteur : aucune cause déclarée
        "event":         event_name,                        # extension informative (debug)
        "ts_iso":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }


# ──────────────────────────────────────────────
# ENVOI UDP
# ──────────────────────────────────────────────
def send_udp(sock, data, host, port):
    payload = json.dumps(data).encode("utf-8")
    sock.sendto(payload, (host, port))
    return len(payload)


# ──────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ──────────────────────────────────────────────
def main():
    hlc  = HLC()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("=" * 55)
    print("  ISAP Protocol — node_a  (émetteur)")
    print("=" * 55)
    print(f"  Node ID   : {NODE_ID}")
    print(f"  Node UUID : {NODE_UUID[:18]}...")
    print(f"  Cluster   : {CLUSTER_ID:#010x}")
    print(f"  Collector : {COLLECTOR_HOST}:{COLLECTOR_PORT}")
    print(f"  PVE API   : {'activée' if PVE_TOKEN else 'désactivée (simulation)'}")
    print("=" * 55)
    print()

    pulse_count = 0

    while True:
        # 1. Collecter les métriques (API ou simulation)
        real = fetch_proxmox_metrics()
        if real:
            event_name = "PVE_REAL"
            state = {
                "cpu_pressure":    real["cpu_pressure"],
                "memory_pressure": real["memory_pressure"],
                "io_pressure":     real["io_pressure"],
                "swap_tension":    real["swap_tension"],
            }
        else:
            ev = pick_simulated_event()
            event_name = ev["name"]
            # Ajoute un peu de bruit pour simuler la réalité
            state = {k: min(1.0, round(v + random.uniform(-0.05, 0.05), 3))
                     for k, v in ev["state"].items()}

        # 2. Calculer l'anomaly score
        score = anomaly_score(state)

        # 3. Déterminer le mode d'émission
        mode, interval = emission_mode(score)

        # 4. Horodater avec HLC
        hlc_ts = hlc.send()

        # 5. Construire et envoyer le paquet
        pulse = build_pulse(hlc_ts, event_name, state, score, mode)
        size  = send_udp(sock, pulse, COLLECTOR_HOST, COLLECTOR_PORT)
        pulse_count += 1

        # 6. Log console
        mode_colors = {"WHISPER": "💤", "NORMAL": "🟢", "STORM": "🔴"}
        icon = mode_colors.get(mode, "•")
        print(
            f"[NODE_A] #{pulse_count:04d} {icon} {mode:<7} "
            f"HLC={hlc} "
            f"EVENT={event_name:<16} "
            f"AS={score:.2f} "
            f"cpu={state['cpu_pressure']:.2f} "
            f"mem={state['memory_pressure']:.2f} "
            f"io={state['io_pressure']:.2f} "
            f"| {size}B"
        )

        # 7. Attendre selon le mode adaptatif
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[NODE_A] Arrêt propre.")

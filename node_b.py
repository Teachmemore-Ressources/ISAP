"""
ISAP Protocol — node_b.py
Nœud réactif : reçoit les Pulses de node_a via le Collector,
réagit causalement et renvoie ses propres Pulses.

Usage :
    python node_b.py
"""

import socket
import json
import time
import random
import uuid
import zlib
import threading

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
LISTEN_HOST    = "0.0.0.0"
LISTEN_PORT    = 5766          # node_b écoute ici (routing depuis collector)

COLLECTOR_HOST = "127.0.0.1"
COLLECTOR_PORT = 5765          # Renvoie les pulses au Collector

NODE_ID    = "node_b"
CLUSTER_ID = int(zlib.crc32(b"homelab-cluster") & 0xFFFFFFFF)
NODE_UUID  = str(uuid.uuid4())


# ──────────────────────────────────────────────
# HYBRID LOGICAL CLOCK — règle de réception
# Plus complexe que send() : synchronise avec
# l'horloge de l'expéditeur
# ──────────────────────────────────────────────
class HLC:
    def __init__(self):
        self.l = self._now()
        self.c = 0
        self._lock = threading.Lock()

    def _now(self):
        return int(time.time() * 1000)

    def send(self):
        with self._lock:
            t = self._now()
            l_new = max(self.l, t)
            if l_new == self.l:
                self.c += 1
            else:
                self.l = l_new
                self.c = 0
            return {"l": self.l, "c": self.c}

    def receive(self, msg_l, msg_c):
        """
        Règle de réception HLC :
        Synchronise l'horloge locale avec le message reçu
        tout en préservant l'ordre causal.
        """
        with self._lock:
            t = self._now()
            l_new = max(self.l, msg_l, t)

            if l_new == self.l == msg_l:
                self.c = max(self.c, msg_c) + 1
            elif l_new == self.l:
                self.c = self.c + 1
            elif l_new == msg_l:
                self.c = msg_c + 1
            else:
                self.c = 0

            self.l = l_new
            return {"l": self.l, "c": self.c}

    def compare(self, other_l, other_c):
        """
        Retourne True si self < other (self précède causalement other)
        """
        return self.l < other_l or (self.l == other_l and self.c < other_c)

    def __str__(self):
        return f"({self.l % 100000}, {self.c})"


# ──────────────────────────────────────────────
# RÉACTIONS CAUSALES
# Chaque événement entrant déclenche une réponse
# après un délai simulant la propagation réelle
# ──────────────────────────────────────────────
REACTIONS = {
    "CPU_SPIKE": {
        "response_event": "MEMORY_PRESSURE",
        "delay_s": 1.0,
        "state": {"cpu_pressure": 0.30, "memory_pressure": 0.85, "io_pressure": 0.10, "swap_tension": 0.20},
        "explanation": "CPU élevé sur node_a → pressure mémoire sur node_b (VM migrée)"
    },
    "BACKUP": {
        "response_event": "IO_SATURATION",
        "delay_s": 2.0,
        "state": {"cpu_pressure": 0.20, "memory_pressure": 0.30, "io_pressure": 0.92, "swap_tension": 0.05},
        "explanation": "Backup node_a → saturation IO réseau node_b"
    },
    "MIGRATION": {
        "response_event": "MEMORY_SPIKE",
        "delay_s": 0.5,
        "state": {"cpu_pressure": 0.50, "memory_pressure": 0.88, "io_pressure": 0.60, "swap_tension": 0.30},
        "explanation": "Migration VM depuis node_a → pic RAM sur node_b"
    },
    "PVE_REAL": {
        "response_event": "LOAD_PROPAGATION",
        "delay_s": 1.5,
        "state": {"cpu_pressure": 0.40, "memory_pressure": 0.55, "io_pressure": 0.35, "swap_tension": 0.10},
        "explanation": "Propagation de charge depuis nœud Proxmox réel"
    },
}


def anomaly_score(state):
    return round(
        state["cpu_pressure"]    * 0.30 +
        state["memory_pressure"] * 0.25 +
        state["io_pressure"]     * 0.20 +
        state["swap_tension"]    * 0.10,
        3
    )


# ──────────────────────────────────────────────
# CLASSIFICATION D'INTENTION — placeholder Phase 0
# Voir SPEC.md §4.3 — confidences basses car non vérifié
# ──────────────────────────────────────────────
def classify_intent(event_name, score):
    if score < 0.2:
        return {"class": "IDLE",     "confidence": 0.9, "duration_ms": 0, "source": "inferred"}
    if score > 0.6:
        return {"class": "STRESSED", "confidence": 0.4, "duration_ms": 0, "source": "inferred"}
    return     {"class": "NORMAL",   "confidence": 0.6, "duration_ms": 0, "source": "inferred"}


def emission_mode(score):
    if score < 0.2:
        return "WHISPER"
    elif score < 0.6:
        return "NORMAL"
    else:
        return "STORM"


# ──────────────────────────────────────────────
# ENVOI UDP
# ──────────────────────────────────────────────
def send_udp(sock, data, host, port):
    payload = json.dumps(data).encode("utf-8")
    sock.sendto(payload, (host, port))
    return len(payload)


# ──────────────────────────────────────────────
# TRAITEMENT D'UN PULSE ENTRANT
# Exécuté dans un thread séparé pour ne pas
# bloquer la réception
# ──────────────────────────────────────────────
def handle_pulse(raw_data, hlc, sock, pulse_count):
    try:
        pulse = json.loads(raw_data.decode("utf-8"))
    except Exception:
        print("[NODE_B] Paquet invalide reçu, ignoré.")
        return

    # Vérification du protocole
    if pulse.get("protocol") != "ISAP":
        return

    sender_id  = pulse.get("node_id", "unknown")
    event      = pulse.get("event", "IDLE")
    sender_hlc = pulse.get("hlc", {"l": 0, "c": 0})

    # Synchronisation HLC avec le message reçu
    recv_hlc = hlc.receive(sender_hlc["l"], sender_hlc["c"])

    print(
        f"[NODE_B] RECV from={sender_id} "
        f"EVENT={event:<18} "
        f"sender_HLC=({sender_hlc['l'] % 100000}, {sender_hlc['c']}) "
        f"local_HLC={hlc}"
    )

    # Vérification ordre causal HLC
    # sender_HLC doit être STRICTEMENT antérieur au HLC local après réception
    causal_ok = (
        sender_hlc["l"] < recv_hlc["l"] or
        (sender_hlc["l"] == recv_hlc["l"] and sender_hlc["c"] < recv_hlc["c"])
    )
    if causal_ok:
        print(f"[NODE_B] ✓ Causal order OK  {sender_id}→{NODE_ID} prouvé par HLC")
    else:
        print(f"[NODE_B] ✗ CAUSAL VIOLATION — HLC order incorrect")

    # Chercher une réaction définie
    reaction = REACTIONS.get(event)
    if not reaction:
        # Événement sans réaction définie → IDLE silencieux
        return

    # Délai causal simulé
    time.sleep(reaction["delay_s"])

    # Ajouter du bruit réaliste
    state = {k: min(1.0, round(v + random.uniform(-0.03, 0.03), 3))
             for k, v in reaction["state"].items()}

    score = anomaly_score(state)
    mode  = emission_mode(score)
    ts    = hlc.send()

    # Construire le Pulse de réponse, conforme SPEC.md §4.1 + §4.2
    # Note honnête : evidence="declared" — node_b sait juste qu'il a reçu un
    # paquet, ce n'est pas une preuve causale. Une vraie inférence (corrélation
    # ou Granger) doit être produite côté Collector, pas côté nœud.
    response = {
        "protocol":      "ISAP",
        "version":       1,
        "node_id":       NODE_ID,
        "node_uuid":     NODE_UUID,
        "cluster_id":    CLUSTER_ID,
        "hlc":           ts,
        "mode":          mode,
        "intent":        classify_intent(reaction["response_event"], score),
        "state":         state,
        "anomaly_score": score,
        "hypothesis": [{
            "source_node":  sender_id,
            "source_uuid":  pulse.get("node_uuid", ""),
            "source_hlc":   sender_hlc,
            "confidence":   0.5,                                        # déclarée, non inférée
            "evidence":     "declared",                                 # SPEC.md §4.2
            "delay_ms":     int(reaction["delay_s"] * 1000),
            "scc_group":    None,
            "explanation":  reaction["explanation"]
        }],
        "event":         reaction["response_event"],                    # extension informative
        "ts_iso":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    size = send_udp(sock, response, COLLECTOR_HOST, COLLECTOR_PORT)
    pulse_count[0] += 1

    mode_icons = {"WHISPER": "💤", "NORMAL": "🟢", "STORM": "🔴"}
    icon = mode_icons.get(mode, "•")

    print(
        f"[NODE_B] SEND #{pulse_count[0]:04d} {icon} {mode:<7} "
        f"HLC={hlc} "
        f"EVENT={reaction['response_event']:<18} "
        f"AS={score:.2f} "
        f"caused_by={sender_id} "
        f"| {size}B"
    )
    print(f"         → {reaction['explanation']}")


# ──────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ──────────────────────────────────────────────
def main():
    hlc  = HLC()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_HOST, LISTEN_PORT))

    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    pulse_count = [0]  # mutable pour passage par référence

    print("=" * 55)
    print("  ISAP Protocol — node_b  (réactif)")
    print("=" * 55)
    print(f"  Node ID   : {NODE_ID}")
    print(f"  Node UUID : {NODE_UUID[:18]}...")
    print(f"  Écoute    : {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Collector : {COLLECTOR_HOST}:{COLLECTOR_PORT}")
    print("=" * 55)
    print()

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            # Traitement dans un thread séparé
            t = threading.Thread(
                target=handle_pulse,
                args=(data, hlc, send_sock, pulse_count),
                daemon=True
            )
            t.start()
        except Exception as e:
            print(f"[NODE_B] Erreur réception : {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[NODE_B] Arrêt propre.")

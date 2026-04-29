"""
ISAP — agent.py (Phase 2)
Agent passif unifié, remplace node_a.py et node_b.py.

Différences vs Phase 0/1 :
- Lit son identité via les variables d'environnement (Docker-friendly).
- Mesure les métriques système RÉELLES via psutil (pas de simulation).
- N'émet PAS d'hypothèses causales déclarées : la causalité est inférée
  par le collecteur (`evidence: inferred`), conformément à SPEC.md §4.2.

Variables d'environnement :
  ISAP_NODE_ID         identifiant lisible              (def: hostname)
  ISAP_COLLECTOR_HOST  adresse du collecteur            (def: collector)
  ISAP_COLLECTOR_PORT  port UDP du collecteur           (def: 5765)
  ISAP_INTERVAL_S      cadence en mode NORMAL           (def: 1.0)
  ISAP_CLUSTER_ID      nom logique du cluster           (def: isap-phase2)
"""

import json
import os
import random
import signal
import socket
import sys
import time
import uuid
import zlib

try:
    import psutil
except ImportError:
    print("[AGENT] psutil requis : pip install psutil", file=sys.stderr)
    sys.exit(1)

# ──────────────────────────────────────────────
# CONFIG via env
# ──────────────────────────────────────────────
NODE_ID         = os.environ.get("ISAP_NODE_ID", socket.gethostname())
COLLECTOR_HOST  = os.environ.get("ISAP_COLLECTOR_HOST", "collector")
COLLECTOR_PORT  = int(os.environ.get("ISAP_COLLECTOR_PORT", "8767"))
INTERVAL_S      = float(os.environ.get("ISAP_INTERVAL_S",   "1.0"))   # NORMAL cadence
WHISPER_S       = float(os.environ.get("ISAP_WHISPER_S",    "5.0"))   # idle cadence (SPEC §7)
STORM_S         = float(os.environ.get("ISAP_STORM_S",      "0.1"))   # critical cadence
CLUSTER_NAME    = os.environ.get("ISAP_CLUSTER_ID", "isap-phase2")
CLUSTER_ID      = int(zlib.crc32(CLUSTER_NAME.encode()) & 0xFFFFFFFF)
NODE_UUID       = str(uuid.uuid4())


# ──────────────────────────────────────────────
# HLC (Kulkarni et al. 2014 — voir SPEC.md §5)
# ──────────────────────────────────────────────
class HLC:
    def __init__(self):
        self.l = self._now()
        self.c = 0

    def _now(self):
        return int(time.time() * 1000)

    def send(self):
        t = self._now()
        l_new = max(self.l, t)
        if l_new == self.l:
            self.c += 1
        else:
            self.l = l_new
            self.c = 0
        return {"l": self.l, "c": self.c}


# ──────────────────────────────────────────────
# MÉTRIQUES SYSTÈME RÉELLES — par conteneur via cgroup v2
# ──────────────────────────────────────────────
# Sur Docker Desktop / WSL2, psutil lit /proc/stat du LinuxKit VM, qui est
# PARTAGÉ entre tous les conteneurs → tous voient les mêmes valeurs CPU/mem.
# C'est un confounder structurel qui rend la causalité indétectable.
#
# La solution propre : lire les métriques cgroup v2 du conteneur lui-même
# via /sys/fs/cgroup/{cpu.stat, memory.current, io.stat}. Chaque conteneur
# a alors sa propre vue, isolée des autres.
#
# Réseau : psutil.net_io_counters() lit eth0 du conteneur, déjà isolé.
class MetricsSampler:
    CGROUP_ROOT = "/sys/fs/cgroup"
    IO_BASELINE = 200_000_000     # 200 MB/s pour normaliser io_pressure

    def __init__(self, net_max_bytes_per_sec: float = 125_000_000):
        self.has_cgroup = os.path.exists(f"{self.CGROUP_ROOT}/cpu.stat")
        self.cpu_count  = self._cpu_quota_count() if self.has_cgroup else (os.cpu_count() or 1)
        self.mem_limit  = self._memory_limit()    if self.has_cgroup else psutil.virtual_memory().total

        psutil.cpu_percent(interval=None)         # prime psutil fallback
        self.last_cpu_us = self._read_cpu_us()    if self.has_cgroup else None
        self.last_io_b   = self._read_io_bytes()  if self.has_cgroup else (0, 0)
        self.last_net    = psutil.net_io_counters()
        self.last_t      = time.time()
        self.net_max     = net_max_bytes_per_sec

    # ── helpers cgroup v2 ────────────────────────
    def _cpu_quota_count(self) -> float:
        try:
            with open(f"{self.CGROUP_ROOT}/cpu.max") as f:
                quota, period = f.read().split()
                if quota == "max":
                    return float(os.cpu_count() or 1)
                return max(1.0, int(quota) / int(period))
        except Exception:
            return float(os.cpu_count() or 1)

    def _memory_limit(self) -> int:
        try:
            with open(f"{self.CGROUP_ROOT}/memory.max") as f:
                v = f.read().strip()
            if v == "max":
                return psutil.virtual_memory().total
            return int(v)
        except Exception:
            return psutil.virtual_memory().total

    def _read_cpu_us(self):
        try:
            with open(f"{self.CGROUP_ROOT}/cpu.stat") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except Exception:
            return None

    def _read_memory(self) -> float:
        try:
            with open(f"{self.CGROUP_ROOT}/memory.current") as f:
                cur = int(f.read().strip())
            return cur / max(1, self.mem_limit)
        except Exception:
            return 0.0

    def _read_io_bytes(self):
        r = w = 0
        try:
            with open(f"{self.CGROUP_ROOT}/io.stat") as f:
                for line in f:
                    for p in line.split()[1:]:
                        if   p.startswith("rbytes="): r += int(p.split("=", 1)[1])
                        elif p.startswith("wbytes="): w += int(p.split("=", 1)[1])
        except Exception:
            pass
        return (r, w)

    # ── sample() ────────────────────────────────
    def sample(self):
        now = time.time()
        dt  = max(0.001, now - self.last_t)
        self.last_t = now

        # CPU : cgroup usage_usec en delta, normalisé par nb cœurs alloués
        if self.has_cgroup:
            cur_us = self._read_cpu_us()
            if cur_us is not None and self.last_cpu_us is not None:
                delta_s = max(0, cur_us - self.last_cpu_us) / 1_000_000.0
                cpu_p = max(0.0, min(1.0, (delta_s / dt) / self.cpu_count))
            else:
                cpu_p = 0.0
            self.last_cpu_us = cur_us
        else:
            cpu_p = psutil.cpu_percent(interval=None) / 100.0

        # Mémoire : cgroup memory.current / memory.max
        mem_p = self._read_memory() if self.has_cgroup else (psutil.virtual_memory().percent / 100.0)

        # IO : cgroup io.stat rbytes+wbytes en delta / baseline 200 MB/s
        if self.has_cgroup:
            cur_io = self._read_io_bytes()
            io_diff = max(0, (cur_io[0] - self.last_io_b[0]) +
                             (cur_io[1] - self.last_io_b[1]))
            io_p = max(0.0, min(1.0, (io_diff / dt) / self.IO_BASELINE))
            self.last_io_b = cur_io
        else:
            io_p = 0.0

        # Swap : info host (cgroup swap pas toujours dispo sous WSL2)
        sw_p = psutil.swap_memory().percent / 100.0

        # Net : psutil net_io (par conteneur, eth0)
        net = psutil.net_io_counters()
        if net and self.last_net:
            bytes_diff = ((net.bytes_sent + net.bytes_recv) -
                          (self.last_net.bytes_sent + self.last_net.bytes_recv))
            net_p = max(0.0, min(1.0, (bytes_diff / dt) / self.net_max))
        else:
            net_p = 0.0
        self.last_net = net

        return {
            "cpu_pressure":    round(cpu_p, 4),
            "memory_pressure": round(mem_p, 4),
            "io_pressure":     round(io_p,  4),
            "swap_tension":    round(sw_p,  4),
            "net_tension":     round(net_p, 4),
        }


# ──────────────────────────────────────────────
# ANOMALY / EVENT / INTENT / MODE
# ──────────────────────────────────────────────
def anomaly_score(state: dict) -> float:
    return round(
        state["cpu_pressure"]    * 0.30 +
        state["memory_pressure"] * 0.25 +
        state["io_pressure"]     * 0.20 +
        state["swap_tension"]    * 0.10 +
        state["net_tension"]     * 0.15,
        3
    )

def classify_event(state: dict, score: float) -> str:
    """Décrit ce qui se passe réellement sur ce nœud — visible dans le dashboard."""
    cpu = state["cpu_pressure"]
    mem = state["memory_pressure"]
    io  = state["io_pressure"]
    sw  = state["swap_tension"]
    net = state["net_tension"]

    if score < 0.05:
        return "idle"

    # Dominant axis — identifie la ressource sous pression
    axes = {
        "cpu_loaded":    cpu,
        "mem_loaded":    mem,
        "io_loaded":     io,
        "swap_pressure": sw,
        "net_saturated": net,
    }
    dominant, dominant_val = max(axes.items(), key=lambda x: x[1])

    if dominant_val < 0.10:
        return "idle"

    if score > 0.6:
        return f"critical_{dominant}"   # ex: "critical_cpu_loaded"

    if dominant_val > 0.4:
        return dominant                 # ex: "cpu_loaded"

    return "normal"

def classify_intent(state: dict, score: float) -> dict:
    """Intent SPEC §4.3 — basé sur l'axe dominant, pas juste le score global."""
    cpu = state["cpu_pressure"]
    mem = state["memory_pressure"]
    io  = state["io_pressure"]

    if score < 0.05:
        return {"class": "IDLE", "confidence": 0.95, "duration_ms": 0, "source": "inferred"}

    if score > 0.6:
        # Identifie la cause dominante
        dominant = max(
            [("CPU",    cpu),
             ("MEMORY", mem),
             ("IO",     io)],
            key=lambda x: x[1]
        )[0]
        return {
            "class":       "STRESSED",
            "confidence":  round(min(0.9, score), 2),
            "duration_ms": 0,
            "source":      "inferred",
            "dominant":    dominant,    # champ extra — aide le collecteur
        }

    # Charge modérée — quel axe ?
    if cpu > mem and cpu > io:
        intent_class = "NORMAL"         # traitement actif
    elif mem > 0.5:
        intent_class = "STRESSED"       # mémoire haute = potentiellement planifié
    else:
        intent_class = "NORMAL"

    return {"class": intent_class, "confidence": 0.7, "duration_ms": 0, "source": "inferred"}

def emission_mode(state: dict, score: float) -> str:
    m = max(state.values()) if state else 0.0
    if m > 0.5 or score > 0.5: return "STORM"
    if m > 0.1 or score > 0.2: return "NORMAL"
    return "WHISPER"


# ──────────────────────────────────────────────
# PULSE (SPEC.md §4.1)
# ──────────────────────────────────────────────
def build_pulse(hlc_ts: dict, state: dict, score: float, mode: str) -> dict:
    return {
        "protocol":      "ISAP",
        "version":       1,
        "node_id":       NODE_ID,
        "node_uuid":     NODE_UUID,
        "cluster_id":    CLUSTER_ID,
        "hlc":           hlc_ts,
        "mode":          mode,
        "event":         classify_event(state, score),
        "intent":        classify_intent(state, score),
        "state":         state,
        "anomaly_score": score,
        "hypothesis":    [],   # les hypothèses sont injectées par causal_infer + push
        "ts_iso":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ──────────────────────────────────────────────
# MAIN LOOP avec arrêt propre
# ──────────────────────────────────────────────
RUNNING = True

def stop_handler(signum, _frame):
    global RUNNING
    print(f"[{NODE_ID}] signal {signum} → arrêt propre")
    RUNNING = False

signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT,  stop_handler)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hlc = HLC()
    sampler = MetricsSampler()

    print("=" * 60, flush=True)
    print(f"  ISAP agent — Phase 2 (passive observer)", flush=True)
    print(f"  Node ID    : {NODE_ID}", flush=True)
    print(f"  Cluster    : {CLUSTER_NAME} ({CLUSTER_ID:#010x})", flush=True)
    print(f"  Collector  : {COLLECTOR_HOST}:{COLLECTOR_PORT}", flush=True)
    print(f"  Interval   : {INTERVAL_S}s", flush=True)
    print("=" * 60, flush=True)

    time.sleep(0.5)   # priming psutil
    sampler.sample()

    pulse_count = 0
    while RUNNING:
        state = sampler.sample()
        score = anomaly_score(state)
        mode  = emission_mode(state, score)
        hlc_ts = hlc.send()
        pulse  = build_pulse(hlc_ts, state, score, mode)
        payload = json.dumps(pulse, separators=(",", ":")).encode("utf-8")

        try:
            sock.sendto(payload, (COLLECTOR_HOST, COLLECTOR_PORT))
            pulse_count += 1
        except Exception as e:
            print(f"[{NODE_ID}] send error: {e}", flush=True)

        # log toutes les ~10 émissions ou en STORM
        if pulse_count % 10 == 1 or mode == "STORM":
            print(f"[{NODE_ID}] #{pulse_count:05d} {mode:<7} "
                  f"AS={score:.2f} cpu={state['cpu_pressure']:.2f} "
                  f"mem={state['memory_pressure']:.2f} "
                  f"io={state['io_pressure']:.2f} "
                  f"net={state['net_tension']:.2f}", flush=True)

        # Cadence selon SPEC.md §7 (override par env pour les bancs).
        # Jitter ±20% : casse la périodicité parfaite qui crée des artefacts
        # de cross-correlation entre agents synchronisés (cf. Phase 2 report).
        interval = {"WHISPER": WHISPER_S, "NORMAL": INTERVAL_S, "STORM": STORM_S}[mode]
        interval *= 1.0 + random.uniform(-0.20, 0.20)
        time.sleep(interval)

    print(f"[{NODE_ID}] stopped after {pulse_count} pulses", flush=True)
    sock.close()


if __name__ == "__main__":
    main()

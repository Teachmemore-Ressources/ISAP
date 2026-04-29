"""
ISAP — scenario_runner.py (Phase 2)
Pilote des workloads RÉELS dans les conteneurs Docker via `docker exec`
et logge la vérité terrain (qui a fait quoi, quand).

Sortie : data/phase2_truth.json
Format compatible avec causal_infer_phase2.py pour calcul precision/recall.

Usage :
  python scenario_runner.py                      # tous les scénarios
  python scenario_runner.py --scenario cpu_a     # un seul
  python scenario_runner.py --duration 30        # raccourci pour tests rapides
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List

# Windows console : UTF-8 pour les caractères ⏳ ✅ etc.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ──────────────────────────────────────────────
# Détection du binaire docker (PATH ou chemin Windows par défaut)
# ──────────────────────────────────────────────
DOCKER = shutil.which("docker") or os.environ.get("DOCKER_BIN") \
         or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if not os.path.exists(DOCKER) and not shutil.which(DOCKER):
    print(f"[runner] docker introuvable : {DOCKER}", file=sys.stderr)
    sys.exit(2)

DEFAULT_OUT = "data/phase2_truth.json"

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)


def docker_exec(container: str, cmd: List[str], detach: bool = True,
                stream: bool = False) -> subprocess.CompletedProcess:
    args = [DOCKER, "exec"]
    if detach: args.append("-d")
    args.append(container)
    args.extend(cmd)
    return subprocess.run(args, capture_output=not stream, text=True)


def docker_exec_kill(container: str, pattern: str):
    subprocess.run([DOCKER, "exec", container, "pkill", "-9", "-f", pattern],
                   capture_output=True, text=True)


def wait(s: float, label: str = ""):
    if label: print(f"   ⏳ {s:>4.1f}s  {label}", flush=True)
    time.sleep(s)


# ──────────────────────────────────────────────
# SCÉNARIOS
# Chacun retourne {name, events: [{source_node, kind, t_start_ms, t_end_ms, expected_metric}]}
# ──────────────────────────────────────────────
def scenario_baseline(duration_s: float):
    name = "baseline_idle"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    wait(duration_s, "idle (no workload, baseline noise)")
    return {"name": name, "events": [], "expected_edges": []}


def scenario_cpu_burn_a(duration_s: float):
    """Avec cgroups isolés, AUCUNE propagation cross-conteneur attendue."""
    name = "cpu_burn_agent_a"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    t0 = now_ms()
    docker_exec("isap_agent_a",
                ["stress-ng", "--cpu", "4", "--timeout", f"{int(duration_s)}s",
                 "--metrics-brief"])
    wait(duration_s + 3, "cpu burn on agent_a")
    t1 = now_ms()
    return {"name": name,
            "events": [{"source_node": "agent_a", "kind": "cpu_burn",
                        "t_start_ms": t0, "t_end_ms": t1,
                        "expected_metric": "cpu_pressure"}],
            "expected_edges": []}     # cgroups → pas de noisy neighbor


def scenario_io_burn_b(duration_s: float):
    name = "io_burn_agent_b"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    t0 = now_ms()
    docker_exec("isap_agent_b", [
        "sh", "-c",
        f"fio --name=randrw --rw=randrw --bs=4k --size=256M --numjobs=4 "
        f"--time_based --runtime={int(duration_s)} --filename=/tmp/fio.dat "
        f"--direct=0 > /tmp/fio.log 2>&1"
    ])
    wait(duration_s + 3, "io burn on agent_b")
    t1 = now_ms()
    return {"name": name,
            "events": [{"source_node": "agent_b", "kind": "io_burn",
                        "t_start_ms": t0, "t_end_ms": t1,
                        "expected_metric": "io_pressure"}],
            "expected_edges": []}


def scenario_mem_pressure_c(duration_s: float):
    name = "mem_pressure_agent_c"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    t0 = now_ms()
    docker_exec("isap_agent_c",
                ["stress-ng", "--vm", "2", "--vm-bytes", "200M",
                 "--timeout", f"{int(duration_s)}s"])
    wait(duration_s + 3, "memory pressure on agent_c")
    t1 = now_ms()
    return {"name": name,
            "events": [{"source_node": "agent_c", "kind": "mem_pressure",
                        "t_start_ms": t0, "t_end_ms": t1,
                        "expected_metric": "memory_pressure"}],
            "expected_edges": []}


def scenario_iperf_a_to_b(duration_s: float):
    """A inonde B en réseau — vrai lien causal réseau A→B sur net_tension."""
    name = "iperf_a_to_b"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    docker_exec("isap_agent_b",
                ["sh", "-c", "iperf3 -s -1 -p 5201 > /tmp/iperf_s.log 2>&1"])
    time.sleep(1.0)
    t0 = now_ms()
    docker_exec("isap_agent_a", [
        "sh", "-c",
        f"iperf3 -c agent_b -p 5201 -t {int(duration_s)} -P 4 "
        f"> /tmp/iperf_c.log 2>&1"
    ])
    wait(duration_s + 3, "iperf3 A → B")
    t1 = now_ms()
    return {"name": name,
            "events": [
                {"source_node": "agent_a", "kind": "iperf_client",
                 "t_start_ms": t0, "t_end_ms": t1, "expected_metric": "net_tension"},
                {"source_node": "agent_b", "kind": "iperf_server",
                 "t_start_ms": t0, "t_end_ms": t1, "expected_metric": "net_tension"},
            ],
            "expected_edges": [["agent_a", "agent_b"]]}


def scenario_combined(duration_s: float):
    """A burn CPU + B burn IO simultanément — chacun isolé, pas de lien."""
    name = "combined_a_cpu_b_io"
    print(f"\n=== {name} ({duration_s}s) ===", flush=True)
    t0 = now_ms()
    docker_exec("isap_agent_a",
                ["stress-ng", "--cpu", "4", "--timeout", f"{int(duration_s)}s"])
    docker_exec("isap_agent_b", [
        "sh", "-c",
        f"fio --name=randrw --rw=randrw --bs=4k --size=256M --numjobs=4 "
        f"--time_based --runtime={int(duration_s)} --filename=/tmp/fio.dat "
        f"--direct=0 > /tmp/fio.log 2>&1"
    ])
    wait(duration_s + 3, "combined A:cpu + B:io")
    t1 = now_ms()
    return {"name": name,
            "events": [
                {"source_node": "agent_a", "kind": "cpu_burn",
                 "t_start_ms": t0, "t_end_ms": t1, "expected_metric": "cpu_pressure"},
                {"source_node": "agent_b", "kind": "io_burn",
                 "t_start_ms": t0, "t_end_ms": t1, "expected_metric": "io_pressure"},
            ],
            "expected_edges": []}      # workloads simultanés mais isolés


SCENARIOS = {
    "baseline":  scenario_baseline,
    "cpu_a":     scenario_cpu_burn_a,
    "io_b":      scenario_io_burn_b,
    "mem_c":     scenario_mem_pressure_c,
    "iperf_ab":  scenario_iperf_a_to_b,
    "combined":  scenario_combined,
}

DEFAULT_ORDER = ["baseline", "cpu_a", "io_b", "mem_c", "iperf_ab", "combined"]


# ──────────────────────────────────────────────
# CHECK PRÉALABLE — les conteneurs tournent ?
# ──────────────────────────────────────────────
def check_containers() -> bool:
    expected = ["isap_collector", "isap_agent_a", "isap_agent_b", "isap_agent_c"]
    out = subprocess.run([DOCKER, "ps", "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    running = set(out.stdout.split())
    missing = [c for c in expected if c not in running]
    if missing:
        print(f"[runner] conteneurs manquants : {missing}", file=sys.stderr)
        print(f"[runner] lance d'abord : docker compose up -d --build", file=sys.stderr)
        return False
    return True


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", help="lancer un seul scénario par nom",
                   choices=list(SCENARIOS.keys()))
    p.add_argument("--duration", type=float, default=45.0,
                   help="durée par scénario (s)")
    p.add_argument("--cooldown", type=float, default=10.0,
                   help="pause entre scénarios (s)")
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args()

    if not check_containers():
        sys.exit(3)

    print("=" * 60, flush=True)
    print(f"  ISAP Phase 2 — scenario_runner", flush=True)
    print(f"  Docker  : {DOCKER}", flush=True)
    print(f"  Out     : {args.out}", flush=True)
    print(f"  Durée par scénario : {args.duration}s", flush=True)
    print("=" * 60, flush=True)

    truth = {"started_at_ms": now_ms(), "duration_per_scenario_s": args.duration,
             "scenarios": []}

    order = [args.scenario] if args.scenario else DEFAULT_ORDER
    for sid in order:
        result = SCENARIOS[sid](args.duration)
        truth["scenarios"].append(result)
        if sid != order[-1]:
            wait(args.cooldown, f"cooldown after {result['name']}")

    truth["ended_at_ms"] = now_ms()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2)

    total_s = (truth["ended_at_ms"] - truth["started_at_ms"]) / 1000
    print(f"\n  → vérité terrain : {args.out}", flush=True)
    print(f"  → {len(truth['scenarios'])} scénarios, "
          f"durée totale {total_s:.0f}s", flush=True)


if __name__ == "__main__":
    main()

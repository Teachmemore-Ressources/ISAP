"""
ISAP — workload_gen.py
Générateur de scénarios synthétiques avec vérité terrain causale.

But : produire un dataset reproductible où l'on connaît a priori les liens
causaux réels, afin de mesurer la précision/rappel d'un module d'inférence
causale (causal_infer.py) en Phase 1.

Contrairement à node_a/node_b qui *déclarent* la causalité, ce générateur
produit uniquement des observations brutes (vecteurs d'état au cours du temps).
Le champ `hypothesis` reste vide — c'est au module d'inférence de le remplir,
et c'est cette inférence qu'on évaluera contre `ground_truth.json`.

Sortie :
  data/observations.jsonl   (un Pulse ISAP par ligne, conforme SPEC.md §4.1)
  data/ground_truth.json    (liens causaux réels par scénario)

Usage :
  python workload_gen.py            # lance tous les scénarios
  python workload_gen.py --seed 42  # reproductibilité
"""

import argparse
import json
import os
import random
import time
import uuid
import zlib
from typing import Callable, Dict, List, Set, Tuple

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
SAMPLE_HZ        = 10            # 10 échantillons / seconde / nœud
SCENARIO_DURATION = 30.0         # secondes de simulation par scénario
DEFAULT_SEED     = 42
OUTPUT_DIR       = "data"
NOISE_SIGMA      = 0.02          # bruit gaussien sur chaque métrique
CLUSTER_ID       = int(zlib.crc32(b"workload-gen") & 0xFFFFFFFF)

# ──────────────────────────────────────────────
# HLC SIMULÉ
# Ici on n'a pas besoin de la vraie horloge wall-clock — un compteur monotone
# par paire (ms_simu, c) est suffisant pour la simulation. La sémantique
# d'ordre causal HLC reste préservée.
# ──────────────────────────────────────────────
class SimHLC:
    def __init__(self, t0_ms: int = 0):
        self.l = t0_ms
        self.c = 0

    def tick(self, sim_time_ms: int) -> Dict[str, int]:
        l_new = max(self.l, sim_time_ms)
        if l_new == self.l:
            self.c += 1
        else:
            self.l = l_new
            self.c = 0
        return {"l": self.l, "c": self.c}


# ──────────────────────────────────────────────
# ÉTAT NORMAL & BRUIT
# ──────────────────────────────────────────────
def baseline_state() -> Dict[str, float]:
    return {
        "cpu_pressure":    0.10,
        "memory_pressure": 0.20,
        "io_pressure":     0.05,
        "swap_tension":    0.02,
        "net_tension":     0.10,
    }


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def add_noise(state: Dict[str, float], sigma: float = NOISE_SIGMA) -> Dict[str, float]:
    return {k: round(clamp01(v + random.gauss(0, sigma)), 4) for k, v in state.items()}


def anomaly_score(state: Dict[str, float]) -> float:
    return round(
        state["cpu_pressure"]    * 0.30 +
        state["memory_pressure"] * 0.25 +
        state["io_pressure"]     * 0.20 +
        state["swap_tension"]    * 0.10 +
        state["net_tension"]     * 0.15,
        3
    )


def emission_mode(score: float) -> str:
    if score < 0.2: return "WHISPER"
    if score < 0.6: return "NORMAL"
    return "STORM"


def classify_intent(score: float) -> Dict:
    if score < 0.2:
        return {"class": "IDLE",     "confidence": 0.9, "duration_ms": 0, "source": "inferred"}
    if score > 0.6:
        return {"class": "STRESSED", "confidence": 0.4, "duration_ms": 0, "source": "inferred"}
    return     {"class": "NORMAL",   "confidence": 0.6, "duration_ms": 0, "source": "inferred"}


# ──────────────────────────────────────────────
# SCÉNARIOS — chacun retourne (driver_fn, ground_truth_edges, nodes)
# ──────────────────────────────────────────────
# Convention : un edge (X, Y) signifie "X cause Y au sens scénarisé".
# Si la corrélation X-Y est une coïncidence ou un confounder, l'edge n'y figure
# PAS, même si une corrélation statistique sera observable.

def _ramp(t: float, start: float, end: float, lo: float, hi: float) -> float:
    """Rampe linéaire de lo à hi entre [start, end], retombe à lo après."""
    if t < start: return lo
    if t < (start + end) / 2: return lo + (hi - lo) * (t - start) / max(1e-6, (end - start) / 2)
    if t < end: return hi - (hi - lo) * (t - (start + end) / 2) / max(1e-6, (end - start) / 2)
    return lo


def scenario_idle():
    """Tout au repos, aucune cause."""
    nodes = ["A", "B", "C"]
    def drive(t, state):
        pass  # baseline + bruit uniquement
    edges: Set[Tuple[str, str]] = set()
    return "idle", drive, edges, nodes


def scenario_a_causes_b():
    """A.cpu monte à t=5, B.memory monte à t=7 (lag 2s) — vrai lien A→B."""
    nodes = ["A", "B"]
    def drive(t, state):
        if 5 <= t < 15:
            state["A"]["cpu_pressure"] = 0.92
        if 7 <= t < 17:
            state["B"]["memory_pressure"] = 0.85
    edges = {("A", "B")}
    return "a_causes_b", drive, edges, nodes


def scenario_independent_spikes():
    """A et B ont des pics aléatoires sans corrélation — aucun vrai lien."""
    nodes = ["A", "B"]
    a_spikes = [random.uniform(2, 28) for _ in range(3)]
    b_spikes = [random.uniform(2, 28) for _ in range(3)]
    def drive(t, state):
        for ts in a_spikes:
            if ts <= t < ts + 1.5:
                state["A"]["cpu_pressure"] = 0.88
        for ts in b_spikes:
            if ts <= t < ts + 1.5:
                state["B"]["memory_pressure"] = 0.82
    edges: Set[Tuple[str, str]] = set()
    return "independent_spikes", drive, edges, nodes


def scenario_multi_cause():
    """A et B causent tous deux C avec des délais différents."""
    nodes = ["A", "B", "C"]
    def drive(t, state):
        if 4 <= t < 12:
            state["A"]["cpu_pressure"] = 0.90
        if 7 <= t < 15:                # +3s → C.io
            state["C"]["io_pressure"] = 0.78
        if 16 <= t < 22:
            state["B"]["net_tension"] = 0.85
        if 20 <= t < 26:                # +4s → C.io
            state["C"]["io_pressure"] = 0.80
    edges = {("A", "C"), ("B", "C")}
    return "multi_cause", drive, edges, nodes


def scenario_cycle_abc():
    """Boucle A→B→C→A — test de la condensation Tarjan côté inférence."""
    nodes = ["A", "B", "C"]
    def drive(t, state):
        if 3 <= t < 6:    state["A"]["cpu_pressure"]    = 0.90
        if 5 <= t < 9:    state["B"]["memory_pressure"] = 0.85
        if 8 <= t < 12:   state["C"]["io_pressure"]     = 0.82
        if 11 <= t < 15:  state["A"]["cpu_pressure"]    = 0.88
        if 13 <= t < 17:  state["B"]["memory_pressure"] = 0.84
        if 16 <= t < 20:  state["C"]["io_pressure"]     = 0.80
    edges = {("A", "B"), ("B", "C"), ("C", "A")}
    return "cycle_abc", drive, edges, nodes


def scenario_confounder():
    """Z (caché) cause simultanément A et B. A et B sont corrélés mais pas
    causalement liés. Edge attendu : ∅. Piège classique pour tout estimateur
    par corrélation pure."""
    nodes = ["A", "B"]
    def drive(t, state):
        if 6 <= t < 14:
            state["A"]["cpu_pressure"]    = 0.88
            state["B"]["memory_pressure"] = 0.80
        if 18 <= t < 24:
            state["A"]["cpu_pressure"]    = 0.85
            state["B"]["memory_pressure"] = 0.78
    edges: Set[Tuple[str, str]] = set()
    return "confounder", drive, edges, nodes


SCENARIOS = [
    scenario_idle,
    scenario_a_causes_b,
    scenario_independent_spikes,
    scenario_multi_cause,
    scenario_cycle_abc,
    scenario_confounder,
]


# ──────────────────────────────────────────────
# RUN SCENARIO
# ──────────────────────────────────────────────
def run_scenario(scenario_factory: Callable, t0_ms: int) -> Tuple[Dict, List[Dict]]:
    name, drive, edges, node_ids = scenario_factory()
    node_uuids = {nid: str(uuid.uuid4()) for nid in node_ids}
    hlcs = {nid: SimHLC(t0_ms=t0_ms) for nid in node_ids}

    pulses: List[Dict] = []
    n_steps = int(SCENARIO_DURATION * SAMPLE_HZ)
    dt_ms = int(1000 / SAMPLE_HZ)

    for step in range(n_steps):
        t = step / SAMPLE_HZ                                    # secondes simulées
        sim_time_ms = t0_ms + step * dt_ms

        # 1) état baseline pour chaque nœud
        state_per_node = {nid: baseline_state() for nid in node_ids}

        # 2) le scénario superpose ses contraintes
        drive(t, state_per_node)

        # 3) bruit + émission d'un Pulse par nœud
        for nid in node_ids:
            noisy = add_noise(state_per_node[nid])
            score = anomaly_score(noisy)
            mode  = emission_mode(score)
            hlc_ts = hlcs[nid].tick(sim_time_ms)

            pulse = {
                "protocol":      "ISAP",
                "version":       1,
                "node_id":       nid,
                "node_uuid":     node_uuids[nid],
                "cluster_id":    CLUSTER_ID,
                "hlc":           hlc_ts,
                "mode":          mode,
                "intent":        classify_intent(score),
                "state":         noisy,
                "anomaly_score": score,
                "hypothesis":    [],                            # à remplir par causal_infer.py
                "ts_iso":        f"sim+{t:06.2f}s",
                "_scenario":     name,                          # extension : utile pour split
            }
            pulses.append(pulse)

    gt = {
        "scenario": name,
        "nodes": node_ids,
        "duration_s": SCENARIO_DURATION,
        "sample_hz": SAMPLE_HZ,
        "edges": sorted([list(e) for e in edges]),
        "t0_ms": t0_ms,
        "t_end_ms": t0_ms + n_steps * dt_ms,
    }
    return gt, pulses


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--out",  type=str, default=OUTPUT_DIR)
    args = p.parse_args()

    random.seed(args.seed)
    os.makedirs(args.out, exist_ok=True)

    obs_path = os.path.join(args.out, "observations.jsonl")
    gt_path  = os.path.join(args.out, "ground_truth.json")

    print("=" * 60)
    print(f"  ISAP workload generator — seed={args.seed}")
    print(f"  Output : {obs_path}")
    print(f"           {gt_path}")
    print("=" * 60)

    all_gt = {"seed": args.seed, "scenarios": []}
    total_pulses = 0
    t0_ms = 1_700_000_000_000   # base monotone, indépendante de l'horloge réelle

    with open(obs_path, "w", encoding="utf-8") as fobs:
        for sc in SCENARIOS:
            gt, pulses = run_scenario(sc, t0_ms=t0_ms)
            for p in pulses:
                fobs.write(json.dumps(p, separators=(",", ":")) + "\n")
            all_gt["scenarios"].append(gt)
            total_pulses += len(pulses)
            t0_ms = gt["t_end_ms"] + 60_000   # 1 min de gap entre scénarios
            print(f"  {gt['scenario']:<22} nodes={len(gt['nodes'])}  "
                  f"pulses={len(pulses):>5}  edges={len(gt['edges'])}")

    with open(gt_path, "w", encoding="utf-8") as fgt:
        json.dump(all_gt, fgt, indent=2)

    print("=" * 60)
    print(f"  Total : {total_pulses} pulses sur {len(SCENARIOS)} scénarios")
    print(f"  Vérité terrain : {sum(len(s['edges']) for s in all_gt['scenarios'])} liens")
    print("=" * 60)


if __name__ == "__main__":
    main()

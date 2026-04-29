"""
ISAP — causal_infer.py
Module d'inférence causale baseline (Phase 1) avec mesure objective
contre la vérité terrain produite par workload_gen.py.

Méthode : corrélation croisée sur les *différences premières* (Δ-series)
avec contrainte de précédence temporelle stricte (lag > 0).

Intuition : A cause B avec délai τ ssi le changement de A à t prédit le
changement de B à t+τ. Différencier filtre les confounders à lag=0
(événements simultanés sans relation causale).

Ce module n'est PAS Granger ni Pearl-do-calculus — c'est un baseline
volontairement simple pour fixer un point de comparaison. Phase 2
remplacera par Granger ou transfer entropy si F1 ≥ 0.7.

Sortie :
  data/metrics.json          (precision/recall/F1 par scénario + global)
  data/inferred_edges.json   (liens inférés par scénario, comparable à GT)
  Phase1_report.md           (rapport human-readable)

Usage :
  python causal_infer.py
  python causal_infer.py --threshold 0.4 --min-lag-ms 300 --max-lag-ms 5000
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

import numpy as np

# Windows console : forcer UTF-8 pour les caractères mathématiques (≥, ✅, …)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ──────────────────────────────────────────────
# CONFIG PAR DÉFAUT — ajustables via CLI
# ──────────────────────────────────────────────
DEFAULT_THRESHOLD   = 0.50        # |corr| min — calé par sweep sur dataset Phase 1
DEFAULT_F_THRESHOLD = 6.0         # F-stat min pour Granger (Bonferroni-cal)
DEFAULT_MIN_LAG_MS  = 300         # lag minimal (filtre confounder simultané)
DEFAULT_MAX_LAG_MS  = 5000        # lag maximal raisonnable
SAMPLE_HZ           = 10
GATE_F1             = 0.70        # Phase 1 gate (cf. SPEC.md & plan)
METRICS = ["cpu_pressure", "memory_pressure", "io_pressure",
           "swap_tension", "net_tension"]
GRANGER_LAGS_S      = [0.3, 0.5, 1.0, 1.5, 2.0, 3.0]  # secondes

# ──────────────────────────────────────────────
# I/O
# ──────────────────────────────────────────────
def load_pulses(path: str) -> List[Dict]:
    pulses, skipped = [], 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    pulses.append(obj)
            except json.JSONDecodeError:
                skipped += 1
    if skipped:
        print(f"[warn] {skipped} ligne(s) ignorée(s) (non-JSON)")
    return pulses


def load_ground_truth(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# STATISTIQUES (stdlib uniquement)
# ──────────────────────────────────────────────
def diff(series: List[float]) -> List[float]:
    return [series[i] - series[i - 1] for i in range(1, len(series))]


def pearson(xs: List[float], ys: List[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3: return 0.0
    xs = xs[:n]; ys = ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9: return 0.0
    return num / (sx * sy)


# ──────────────────────────────────────────────
# CONSTRUCTION DES SÉRIES TEMPORELLES PAR NŒUD
# ──────────────────────────────────────────────
def build_series(pulses: List[Dict]) -> Dict[str, Dict[str, List[float]]]:
    """
    Retourne : {node_id: {metric: [v0, v1, ..., vT]}} dans l'ordre HLC.
    """
    by_node = defaultdict(list)
    for p in pulses:
        by_node[p["node_id"]].append(p)

    series: Dict[str, Dict[str, List[float]]] = {}
    for nid, ps in by_node.items():
        ps.sort(key=lambda x: (x["hlc"]["l"], x["hlc"]["c"]))
        series[nid] = {m: [p["state"].get(m, 0.0) for p in ps] for m in METRICS}
    return series


# ──────────────────────────────────────────────
# INFÉRENCE DE LIENS
# ──────────────────────────────────────────────
MIN_RANGE = 0.05   # série sans variation > 0.05 → ignorée (artefact mesure)


def _has_signal(orig: List[float], diff_xs: List[float]) -> bool:
    """
    Filtre les séries quasi-constantes ou purement périodiques.
    Une série a du signal ssi :
    - amplitude originale > MIN_RANGE  (pas de plateau près de zéro)
    - ET stddev des différences > 0.03 (pas de bruit micro-périodique)
    Le second critère est crucial : sur banc Docker, les agents émettent à
    cadence fixe → patterns synchronisés à 1Hz qui créent des FP triviaux.
    """
    if not orig or not diff_xs:
        return False
    rng = max(orig) - min(orig)
    if rng < MIN_RANGE:
        return False
    n = len(diff_xs)
    m = sum(diff_xs) / n
    var = sum((v - m) ** 2 for v in diff_xs) / n
    return var > 1e-3   # stddev > ≈0.032 = bruit ambiant typique en idle


def infer_edges(series: Dict[str, Dict[str, List[float]]],
                threshold: float,
                min_lag_samples: int,
                max_lag_samples: int) -> Dict[Tuple[str, str], Dict]:
    """
    Pour chaque paire ordonnée (X, Y), X != Y, cherche le couple
    (metric_x, metric_y, lag) maximisant |pearson(diff(X.mx)[:-lag], diff(Y.my)[lag:])|.

    Filtre les paires (mx, my) dont au moins une série n'a pas de signal
    (variation < 10% ou diff quasi-constant) — évite les pseudo-corrélations
    sur métriques près du bruit de mesure.

    Retourne un dict {(X, Y): {best_corr, best_lag, best_pair}}
    pour chaque paire dont le score dépasse le seuil.
    """
    nodes = list(series.keys())
    diffed = {nid: {m: diff(s) for m, s in metrics.items()}
              for nid, metrics in series.items()}

    edges: Dict[Tuple[str, str], Dict] = {}
    for X in nodes:
        for Y in nodes:
            if X == Y: continue
            best = {"corr": 0.0, "lag": 0, "metric_x": None, "metric_y": None}

            for mx in METRICS:
                xs_orig = series[X][mx]
                xs = diffed[X][mx]
                if len(xs) < min_lag_samples + 5: continue
                if not _has_signal(xs_orig, xs):    continue
                for my in METRICS:
                    ys_orig = series[Y][my]
                    ys = diffed[Y][my]
                    n  = min(len(xs), len(ys))
                    if n < min_lag_samples + 5: continue
                    if not _has_signal(ys_orig, ys): continue

                    for lag in range(min_lag_samples, min(max_lag_samples, n - 5) + 1):
                        c = pearson(xs[:n - lag], ys[lag:n])
                        if abs(c) > abs(best["corr"]):
                            best = {"corr": c, "lag": lag,
                                    "metric_x": mx, "metric_y": my}

            if abs(best["corr"]) >= threshold:
                edges[(X, Y)] = best
    return edges


# ──────────────────────────────────────────────
# GRANGER CAUSALITY (test F sur modèles imbriqués)
# Référence : Granger 1969 ; implémentation OLS via lstsq.
#
# Hypothèse nulle H0 : x ne Granger-cause pas y au lag p.
#   Modèle restreint  : y[t] = a0 + Σ a_i·y[t-i]
#   Modèle non-restr. : y[t] = a0 + Σ a_i·y[t-i] + Σ b_j·x[t-j]
# F = ((RSS_R - RSS_U) / p) / (RSS_U / (T - 2p - 1))
# F élevé → on rejette H0 → x Granger-cause y.
# ──────────────────────────────────────────────
def granger_F(x: List[float], y: List[float], lag: int) -> float:
    n = len(y)
    if len(x) != n or n < 3 * lag + 10:
        return 0.0

    Y = np.asarray(y[lag:], dtype=float)
    T = Y.shape[0]
    Y_lag = np.column_stack([y[lag - 1 - i : n - 1 - i] for i in range(lag)])
    X_lag = np.column_stack([x[lag - 1 - i : n - 1 - i] for i in range(lag)])
    intercept = np.ones((T, 1))

    Z_R = np.hstack([intercept, Y_lag])
    Z_U = np.hstack([intercept, Y_lag, X_lag])
    try:
        beta_R, *_ = np.linalg.lstsq(Z_R, Y, rcond=None)
        beta_U, *_ = np.linalg.lstsq(Z_U, Y, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0

    rss_R = float(np.sum((Y - Z_R @ beta_R) ** 2))
    rss_U = float(np.sum((Y - Z_U @ beta_U) ** 2))
    df_den = T - 2 * lag - 1
    if df_den <= 0 or rss_U < 1e-12 or rss_R < rss_U:
        return 0.0
    F = ((rss_R - rss_U) / lag) / (rss_U / df_den)
    return max(0.0, float(F))


def infer_edges_granger(series: Dict[str, Dict[str, List[float]]],
                        f_threshold: float,
                        sample_hz: int) -> Dict[Tuple[str, str], Dict]:
    """
    Pour chaque paire ordonnée (X, Y), prend le max F-stat sur tous les
    couples (metric_x, metric_y, lag). Si max F ≥ f_threshold → infère X→Y.
    """
    nodes = list(series.keys())
    edges: Dict[Tuple[str, str], Dict] = {}
    lag_samples = [max(1, int(round(s * sample_hz))) for s in GRANGER_LAGS_S]

    for X in nodes:
        for Y in nodes:
            if X == Y: continue
            best = {"F": 0.0, "lag": 0, "metric_x": None, "metric_y": None}

            for mx in METRICS:
                xs = series[X][mx]
                if max(xs) - min(xs) < 0.05: continue   # quasi-constant
                for my in METRICS:
                    ys = series[Y][my]
                    if max(ys) - min(ys) < 0.05: continue
                    for lag in lag_samples:
                        F = granger_F(xs, ys, lag)
                        if F > best["F"]:
                            best = {"F": F, "lag": lag,
                                    "metric_x": mx, "metric_y": my}

            if best["F"] >= f_threshold:
                # Réutilise la clé "corr" pour interop avec le rendu xcorr
                edges[(X, Y)] = {"corr": best["F"], "lag": best["lag"],
                                 "metric_x": best["metric_x"],
                                 "metric_y": best["metric_y"]}
    return edges


# ──────────────────────────────────────────────
# TRANSFER ENTROPY (Schreiber 2000)
# Mesure non-paramétrique du flux d'information de X vers Y.
#   TE(X→Y) = H(Y_t | Y_{t-l}) - H(Y_t | Y_{t-l}, X_{t-l})
# en bits, avec discrétisation par binning à largeur égale.
# ──────────────────────────────────────────────
def transfer_entropy(x: List[float], y: List[float],
                     lag: int = 1, bins: int = 8) -> float:
    n = min(len(x), len(y))
    if n < lag + 10:
        return 0.0
    # On rejette les séries quasi-constantes — entropie nulle, calcul instable
    if max(x[:n]) - min(x[:n]) < 1e-6 or max(y[:n]) - min(y[:n]) < 1e-6:
        return 0.0

    def discretize(arr, k):
        a_lo, a_hi = min(arr), max(arr)
        rng = a_hi - a_lo
        if rng < 1e-12:
            return [0] * len(arr)
        return [min(k - 1, max(0, int((v - a_lo) / rng * k))) for v in arr]

    xb = discretize(x[:n], bins)
    yb = discretize(y[:n], bins)

    samples = [(yb[t], yb[t - lag], xb[t - lag]) for t in range(lag, n)]
    N = len(samples)
    if N < 5:
        return 0.0

    from collections import Counter
    c_xyz   = Counter(samples)
    c_yl_xl = Counter((yl, xl) for _, yl, xl in samples)
    c_yt_yl = Counter((yt, yl) for yt, yl, _ in samples)
    c_yl    = Counter(yl for _, yl, _ in samples)

    te = 0.0
    for (yt, yl, xl), n_xyz in c_xyz.items():
        n_yl_xl = c_yl_xl[(yl, xl)]
        n_yt_yl = c_yt_yl[(yt, yl)]
        n_yl_   = c_yl[yl]
        if n_yl_xl and n_yt_yl and n_yl_:
            num = n_xyz * n_yl_
            den = n_yl_xl * n_yt_yl
            if num > 0 and den > 0:
                te += (n_xyz / N) * math.log2(num / den)
    return max(0.0, te)


def infer_edges_te(series: Dict[str, Dict[str, List[float]]],
                   threshold: float, sample_hz: int,
                   lags_s: List[float] = None,
                   bins: int = 8,
                   require_directional: bool = True
                  ) -> Dict[Tuple[str, str], Dict]:
    """
    Inférence par Transfer Entropy. Pour chaque paire (X, Y) ordonnée,
    on prend le max TE sur (metric_x, metric_y, lag).
    Si require_directional, on ne garde l'edge que si TE(X→Y) > TE(Y→X)
    (résout l'ambiguïté de direction à 1 Hz sur signaux quasi-simultanés).
    """
    if lags_s is None:
        lags_s = [0.5, 1.0, 2.0, 3.0]
    lag_samples = [max(1, int(round(s * sample_hz))) for s in lags_s]

    nodes = list(series.keys())

    def best_te(X, Y) -> Dict:
        best = {"te": 0.0, "lag": 0, "metric_x": None, "metric_y": None}
        for mx in METRICS:
            xs = series[X][mx]
            if max(xs) - min(xs) < MIN_RANGE: continue
            for my in METRICS:
                ys = series[Y][my]
                if max(ys) - min(ys) < MIN_RANGE: continue
                for lag in lag_samples:
                    te = transfer_entropy(xs, ys, lag=lag, bins=bins)
                    if te > best["te"]:
                        best = {"te": te, "lag": lag,
                                "metric_x": mx, "metric_y": my}
        return best

    edges: Dict[Tuple[str, str], Dict] = {}
    for X in nodes:
        for Y in nodes:
            if X == Y: continue
            te_xy = best_te(X, Y)
            if te_xy["te"] < threshold:
                continue
            if require_directional:
                te_yx = best_te(Y, X)
                if te_yx["te"] >= te_xy["te"]:
                    continue   # l'autre direction domine
            edges[(X, Y)] = {
                "corr": te_xy["te"],   # réutilise la clé corr pour interop
                "lag":  te_xy["lag"],
                "metric_x": te_xy["metric_x"],
                "metric_y": te_xy["metric_y"],
            }
    return edges


# ──────────────────────────────────────────────
# GRANGER CONDITIONNEL (Geweke 1984)
# Test : X cause Y *au-delà de* l'information apportée par Z.
# Modèle restreint   : Y[t] = a0 + Σ a_i Y[t-i] + Σ b_j Z[t-j]
# Modèle non-restr.  :        + Σ c_k X[t-k]
# F = ((RSS_R - RSS_U) / lag) / (RSS_U / df_den)
# ──────────────────────────────────────────────
def cond_granger_F(x: List[float], y: List[float], z: List[float],
                   lag: int) -> float:
    n = len(y)
    if not (len(x) == len(y) == len(z)) or n < 4 * lag + 10:
        return 0.0

    Y = np.asarray(y[lag:], dtype=float)
    T = Y.shape[0]
    Y_lag = np.column_stack([y[lag - 1 - i : n - 1 - i] for i in range(lag)])
    X_lag = np.column_stack([x[lag - 1 - i : n - 1 - i] for i in range(lag)])
    Z_lag = np.column_stack([z[lag - 1 - i : n - 1 - i] for i in range(lag)])
    intercept = np.ones((T, 1))

    Z_R = np.hstack([intercept, Y_lag, Z_lag])
    Z_U = np.hstack([intercept, Y_lag, Z_lag, X_lag])
    try:
        beta_R, *_ = np.linalg.lstsq(Z_R, Y, rcond=None)
        beta_U, *_ = np.linalg.lstsq(Z_U, Y, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0

    rss_R = float(np.sum((Y - Z_R @ beta_R) ** 2))
    rss_U = float(np.sum((Y - Z_U @ beta_U) ** 2))
    df_den = T - 3 * lag - 1
    if df_den <= 0 or rss_U < 1e-12 or rss_R < rss_U:
        return 0.0
    F = ((rss_R - rss_U) / lag) / (rss_U / df_den)
    return max(0.0, float(F))


def infer_edges_cgranger(series: Dict[str, Dict[str, List[float]]],
                         f_threshold: float, sample_hz: int,
                         require_directional: bool = True
                        ) -> Dict[Tuple[str, str], Dict]:
    """
    Granger conditionnel : pour chaque paire (X, Y), on conditionne sur
    le 3ème nœud Z (somme du state vector — proxy de l'activité globale).
    Si require_directional, on impose F(X→Y|Z) > F(Y→X|Z).
    """
    nodes = list(series.keys())
    if len(nodes) < 3:
        # Pas de Z disponible → bascule en Granger bivarié
        return infer_edges_granger(series, f_threshold, sample_hz)

    lag_samples = [max(1, int(round(s * sample_hz))) for s in GRANGER_LAGS_S]

    # Z = somme du state vector du 3ème nœud (proxy d'activité globale)
    def z_proxy(zid: str) -> List[float]:
        ys = list(series[zid].values())
        n = min(len(v) for v in ys)
        return [sum(v[i] for v in ys) for i in range(n)]

    def best_F(X, Y, Z) -> Dict:
        zs = z_proxy(Z)
        best = {"F": 0.0, "lag": 0, "metric_x": None, "metric_y": None}
        for mx in METRICS:
            xs = series[X][mx]
            if max(xs) - min(xs) < MIN_RANGE: continue
            for my in METRICS:
                ys = series[Y][my]
                if max(ys) - min(ys) < MIN_RANGE: continue
                n = min(len(xs), len(ys), len(zs))
                if n < max(lag_samples) + 10: continue
                for lag in lag_samples:
                    F = cond_granger_F(xs[:n], ys[:n], zs[:n], lag)
                    if F > best["F"]:
                        best = {"F": F, "lag": lag,
                                "metric_x": mx, "metric_y": my}
        return best

    edges: Dict[Tuple[str, str], Dict] = {}
    for X in nodes:
        for Y in nodes:
            if X == Y: continue
            others = [n for n in nodes if n != X and n != Y]
            if not others:
                continue
            Z = others[0]   # un seul confounder candidat (3ème nœud)
            f_xy = best_F(X, Y, Z)
            if f_xy["F"] < f_threshold:
                continue
            if require_directional:
                f_yx = best_F(Y, X, Z)
                if f_yx["F"] >= f_xy["F"]:
                    continue
            edges[(X, Y)] = {
                "corr": f_xy["F"],
                "lag":  f_xy["lag"],
                "metric_x": f_xy["metric_x"],
                "metric_y": f_xy["metric_y"],
            }
    return edges


# ──────────────────────────────────────────────
# ÉVALUATION CONTRE VÉRITÉ TERRAIN
# ──────────────────────────────────────────────
def evaluate(inferred: Set[Tuple[str, str]],
             truth:    Set[Tuple[str, str]]) -> Dict[str, float]:
    tp = len(inferred & truth)
    fp = len(inferred - truth)
    fn = len(truth - inferred)
    p = tp / (tp + fp) if (tp + fp) > 0 else (1.0 if not truth else 0.0)
    r = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3)}


# ──────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────
def render_markdown(report: Dict) -> str:
    lines = []
    lines.append(f"# ISAP — Phase 1 Benchmark Report\n")
    lines.append(f"**Date** : {report['timestamp']}  ")
    lines.append(f"**Méthode** : {report['method']}  ")
    if report["method"] == "xcorr":
        lines.append(f"**Seuil |corr|** : {report['threshold']}  ")
        lines.append(f"**Lag** : [{report['min_lag_ms']}ms, {report['max_lag_ms']}ms]  ")
    else:
        lines.append(f"**Seuil F-stat** : {report['f_threshold']}  ")
        lines.append(f"**Lags testés (s)** : {GRANGER_LAGS_S}  ")
    lines.append(f"**Sample rate** : {SAMPLE_HZ} Hz\n")

    lines.append("## Résultats par scénario\n")
    lines.append("| Scénario | GT | Inférés | TP | FP | FN | P | R | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in report["scenarios"]:
        m = s["metrics"]
        gt_str  = ", ".join(f"{a}→{b}" for a, b in s["truth"])  or "∅"
        inf_str = ", ".join(f"{a}→{b}" for a, b in s["inferred"]) or "∅"
        lines.append(f"| {s['scenario']} | {gt_str} | {inf_str} | "
                     f"{m['tp']} | {m['fp']} | {m['fn']} | "
                     f"{m['precision']} | {m['recall']} | {m['f1']} |")

    g = report["overall"]
    lines.append("")
    lines.append("## Score global")
    lines.append(f"- **Precision** : {g['precision']}")
    lines.append(f"- **Recall**    : {g['recall']}")
    lines.append(f"- **F1**        : {g['f1']}")
    lines.append("")
    gate = "✅ PASS" if g["f1"] >= GATE_F1 else "❌ FAIL"
    lines.append(f"## Phase 1 Gate (F1 ≥ {GATE_F1}) : **{gate}**")
    lines.append("")
    if g["f1"] >= GATE_F1:
        lines.append("Le baseline cross-correlation atteint le seuil de Phase 1. "
                     "On peut passer à Phase 2 (banc réel + comparaison Prometheus).")
    else:
        lines.append("Le baseline cross-correlation n'atteint pas le seuil. "
                     "Avant Phase 2 : itérer sur la méthode (Granger, transfer "
                     "entropy) ou réviser les scénarios. **Ne pas écrire de "
                     "white paper tant que ce gate n'est pas franchi.**")
    lines.append("")
    lines.append("## Limites honnêtes\n")
    lines.append("- Données **synthétiques** : pas de vrai bruit système, pas de "
                 "vraie contention de ressources, pas de dérive d'horloge.")
    lines.append("- Méthode **par corrélation différenciée** : approxime Granger "
                 "à l'ordre 1 ; sensible au choix de seuil ; ne gère pas les "
                 "confounders observés (uniquement le piège lag=0).")
    lines.append("- Ground truth **par construction** : les vrais systèmes ont "
                 "des liens causaux flous ou contestables.")
    lines.append("- Ces chiffres ne se transposent pas tels quels à un cluster "
                 "Proxmox réel — ils valident uniquement la *cohérence du pipeline*.")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--obs",         default="data/observations.jsonl")
    p.add_argument("--truth",       default="data/ground_truth.json")
    p.add_argument("--method",      choices=["xcorr", "granger"], default="granger")
    p.add_argument("--threshold",   type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--f-threshold", type=float, default=DEFAULT_F_THRESHOLD)
    p.add_argument("--min-lag-ms",  type=int,   default=DEFAULT_MIN_LAG_MS)
    p.add_argument("--max-lag-ms",  type=int,   default=DEFAULT_MAX_LAG_MS)
    p.add_argument("--out-metrics", default="data/metrics.json")
    p.add_argument("--out-edges",   default="data/inferred_edges.json")
    p.add_argument("--out-report",  default="Phase1_report.md")
    args = p.parse_args()

    min_lag_samples = max(1, int(args.min_lag_ms / 1000 * SAMPLE_HZ))
    max_lag_samples = max(min_lag_samples + 1, int(args.max_lag_ms / 1000 * SAMPLE_HZ))

    pulses = load_pulses(args.obs)
    gt     = load_ground_truth(args.truth)

    print("=" * 60)
    if args.method == "xcorr":
        print(f"  ISAP causal_infer — méthode : cross-corr sur diff-series")
        print(f"  threshold={args.threshold}  lag=[{args.min_lag_ms},{args.max_lag_ms}]ms")
    else:
        print(f"  ISAP causal_infer — méthode : Granger F-test")
        print(f"  F-threshold={args.f_threshold}  lags(s)={GRANGER_LAGS_S}")
    print(f"  pulses chargés : {len(pulses)}")
    print(f"  scénarios      : {len(gt['scenarios'])}")
    print("=" * 60)

    # Split pulses par scénario via le champ d'extension `_scenario`
    by_scenario: Dict[str, List[Dict]] = defaultdict(list)
    for pl in pulses:
        by_scenario[pl.get("_scenario", "unknown")].append(pl)

    scenarios_report = []
    inferred_dump    = {}
    total_tp = total_fp = total_fn = 0

    for sc in gt["scenarios"]:
        name   = sc["scenario"]
        truth  = {tuple(e) for e in sc["edges"]}
        ps     = by_scenario.get(name, [])
        series = build_series(ps)
        if args.method == "xcorr":
            edges_d = infer_edges(series, args.threshold,
                                  min_lag_samples, max_lag_samples)
        else:
            edges_d = infer_edges_granger(series, args.f_threshold, SAMPLE_HZ)
        inferred = set(edges_d.keys())
        m = evaluate(inferred, truth)
        total_tp += m["tp"]; total_fp += m["fp"]; total_fn += m["fn"]

        scenarios_report.append({
            "scenario": name,
            "truth":    sorted([list(e) for e in truth]),
            "inferred": sorted([list(e) for e in inferred]),
            "details":  {f"{a}->{b}": {"corr": round(d["corr"], 3),
                                       "lag_ms": int(d["lag"] * 1000 / SAMPLE_HZ),
                                       "metric_x": d["metric_x"],
                                       "metric_y": d["metric_y"]}
                         for (a, b), d in edges_d.items()},
            "metrics":  m,
        })
        inferred_dump[name] = sorted([list(e) for e in inferred])

        print(f"  {name:<22} GT={len(truth):>2}  inf={len(inferred):>2}  "
              f"TP={m['tp']} FP={m['fp']} FN={m['fn']}  F1={m['f1']}")

    p_g = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 1.0
    r_g = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 1.0
    f_g = 2 * p_g * r_g / (p_g + r_g) if (p_g + r_g) > 0 else 0.0
    overall = {"tp": total_tp, "fp": total_fp, "fn": total_fn,
               "precision": round(p_g, 3), "recall": round(r_g, 3),
               "f1": round(f_g, 3)}
    print("=" * 60)
    print(f"  GLOBAL  P={overall['precision']}  R={overall['recall']}  F1={overall['f1']}")
    gate_pass = overall["f1"] >= GATE_F1
    print(f"  Phase 1 gate (F1 ≥ {GATE_F1}) : {'PASS ✅' if gate_pass else 'FAIL ❌'}")
    print("=" * 60)

    import datetime
    report = {
        "timestamp":   datetime.datetime.now(datetime.UTC).isoformat(),
        "method":      args.method,
        "threshold":   args.threshold,
        "f_threshold": args.f_threshold,
        "min_lag_ms":  args.min_lag_ms,
        "max_lag_ms":  args.max_lag_ms,
        "scenarios":   scenarios_report,
        "overall":     overall,
        "phase1_gate_pass": gate_pass,
    }
    os.makedirs(os.path.dirname(args.out_metrics) or ".", exist_ok=True)
    with open(args.out_metrics, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(args.out_edges, "w", encoding="utf-8") as f:
        json.dump(inferred_dump, f, indent=2)
    with open(args.out_report, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    print(f"  Écrit : {args.out_metrics}")
    print(f"          {args.out_edges}")
    print(f"          {args.out_report}")


if __name__ == "__main__":
    main()

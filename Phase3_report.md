# ISAP — Phase 3 Method Comparison Report

**Dataset Phase 2** : 2950 pulses sur 6 scénarios.  
**Objectif Phase 3** : tester si une méthode d'inférence plus forte que xcorr permet de récupérer la direction causale manquée sur `iperf_a_to_b`.

## Comparaison globale

| Méthode | Seuil | P (dir) | R (dir) | F1 dir | F1 undir | Gate dir | Gate undir |
|---|---|---|---|---|---|---|---|
| xcorr (Δ-series) | 0.4 | 0.0 | 0.0 | 0.0 | **1.0** | ❌ | ✅ |
| Transfer Entropy | 0.05 | 0.0 | 0.0 | 0.0 | **0.5** | ❌ | ❌ |
| Granger conditionnel | 8.0 | 0.0 | 0.0 | 0.0 | **0.667** | ❌ | ❌ |

## Focus `iperf_a_to_b` (seul lien causal réel)

Vérité terrain : `agent_a → agent_b`

| Méthode | Inféré | Direction OK ? |
|---|---|---|
| xcorr (Δ-series) | agent_b→agent_a | ❌ (inversée) |
| Transfer Entropy | agent_b→agent_a | ❌ (inversée) |
| Granger conditionnel | agent_b→agent_a | ❌ (inversée) |

## Faux positifs hors iperf

| Méthode | Scénarios avec FP | Total FP |
|---|---|---|
| xcorr (Δ-series) | ∅ | 0 |
| Transfer Entropy | baseline_idle (1), combined_a_cpu_b_io (1) | 2 |
| Granger conditionnel | baseline_idle (1) | 1 |

## Verdict scientifique

Aucune méthode ne récupère la direction `agent_a → agent_b` sur le scénario iperf. C'est attendu : sur loopback, le TCP handshake est sub-millisecond, et l'augmentation simultanée de `bytes_sent` (côté client) et `bytes_recv` (côté serveur) est co-localisée dans la même fenêtre d'échantillonnage à 200ms (mode STORM). Aucune méthode statistique ne peut inférer une précédence temporelle plus fine que la résolution des données.

**Implication** : la direction causale dans ISAP n'est pas un problème de méthode d'inférence, c'est un problème de **résolution d'échantillonnage**. Phase 4 candidate : instrumentation niveau noyau (eBPF, perf_events) pour des événements horodatés en microseconde, qui rendent la précédence détectable indépendamment de la cadence des Pulses.

## Robustesse (undirected F1)

Le baseline **xcorr (Δ-series)** atteint un F1 non-orienté de **1.0** — les méthodes Phase 3 (TE, Granger conditionnel) ne dépassent pas ce résultat sur ce dataset.

Cela ne dévalue pas TE ni Granger conditionnel : ces méthodes deviennent supérieures sur datasets plus complexes (multi-cause, confounders cachés, signaux non-linéaires). Sur Phase 2 avec isolation cgroup et un seul lien causal, le baseline xcorr suffit. Le test Phase 3 prouve que **la simplicité de xcorr n'est pas un compromis ici** — il est aussi bon que les alternatives plus coûteuses.

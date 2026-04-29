# ISAP — Phase 1 Benchmark Report

**Date** : 2026-04-29T02:26:42.777689+00:00  
**Méthode** : xcorr  
**Seuil |corr|** : 0.5  
**Lag** : [300ms, 5000ms]  
**Sample rate** : 10 Hz

## Résultats par scénario

| Scénario | GT | Inférés | TP | FP | FN | P | R | F1 |
|---|---|---|---|---|---|---|---|---|
| idle | ∅ | ∅ | 0 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| a_causes_b | A→B | A→B | 1 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| independent_spikes | ∅ | ∅ | 0 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| multi_cause | A→C, B→C | A→C, B→C | 2 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| cycle_abc | A→B, B→C, C→A | A→B, A→C, B→C, C→A, C→B | 3 | 2 | 0 | 0.6 | 1.0 | 0.75 |
| confounder | ∅ | ∅ | 0 | 0 | 0 | 1.0 | 1.0 | 1.0 |

## Score global
- **Precision** : 0.75
- **Recall**    : 1.0
- **F1**        : 0.857

## Phase 1 Gate (F1 ≥ 0.7) : **✅ PASS**

Le baseline cross-correlation atteint le seuil de Phase 1. On peut passer à Phase 2 (banc réel + comparaison Prometheus).

## Limites honnêtes

- Données **synthétiques** : pas de vrai bruit système, pas de vraie contention de ressources, pas de dérive d'horloge.
- Méthode **par corrélation différenciée** : approxime Granger à l'ordre 1 ; sensible au choix de seuil ; ne gère pas les confounders observés (uniquement le piège lag=0).
- Ground truth **par construction** : les vrais systèmes ont des liens causaux flous ou contestables.
- Ces chiffres ne se transposent pas tels quels à un cluster Proxmox réel — ils valident uniquement la *cohérence du pipeline*.
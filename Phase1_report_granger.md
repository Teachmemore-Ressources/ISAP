# ISAP — Phase 1 Benchmark Report

**Date** : 2026-04-28T04:46:28.247326+00:00  
**Méthode** : granger  
**Seuil F-stat** : 6.0  
**Lags testés (s)** : [0.3, 0.5, 1.0, 1.5, 2.0, 3.0]  
**Sample rate** : 10 Hz

## Résultats par scénario

| Scénario | GT | Inférés | TP | FP | FN | P | R | F1 |
|---|---|---|---|---|---|---|---|---|
| idle | ∅ | ∅ | 0 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| a_causes_b | A→B | A→B | 1 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| independent_spikes | ∅ | ∅ | 0 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| multi_cause | A→C, B→C | ∅ | 0 | 0 | 2 | 0.0 | 0.0 | 0.0 |
| cycle_abc | A→B, B→C, C→A | A→B, B→C, C→B | 2 | 1 | 1 | 0.667 | 0.667 | 0.667 |
| confounder | ∅ | A→B | 0 | 1 | 0 | 0.0 | 1.0 | 0.0 |

## Score global
- **Precision** : 0.6
- **Recall**    : 0.5
- **F1**        : 0.545

## Phase 1 Gate (F1 ≥ 0.7) : **❌ FAIL**

Le baseline cross-correlation n'atteint pas le seuil. Avant Phase 2 : itérer sur la méthode (Granger, transfer entropy) ou réviser les scénarios. **Ne pas écrire de white paper tant que ce gate n'est pas franchi.**

## Limites honnêtes

- Données **synthétiques** : pas de vrai bruit système, pas de vraie contention de ressources, pas de dérive d'horloge.
- Méthode **par corrélation différenciée** : approxime Granger à l'ordre 1 ; sensible au choix de seuil ; ne gère pas les confounders observés (uniquement le piège lag=0).
- Ground truth **par construction** : les vrais systèmes ont des liens causaux flous ou contestables.
- Ces chiffres ne se transposent pas tels quels à un cluster Proxmox réel — ils valident uniquement la *cohérence du pipeline*.
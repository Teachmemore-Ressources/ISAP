# ISAP — Intent-State Awareness Protocol
**Spec v0.1 — DRAFT (2026-04-27)**

> Statut : brouillon de travail. Les sections marquées **[NORMATIF]** définissent
> le contrat wire. Les sections **[INFORMATIF]** décrivent l'intention et la
> motivation. Cette spec n'est pas finalisée tant que Phase 1 (inférence causale
> mesurée contre vérité terrain) n'a pas validé la conception.

---

## 1. Ce qu'ISAP *est*

Un format wire et un comportement de référence pour transporter, entre nœuds
observés et un ou plusieurs collecteurs :

- des **vecteurs d'état** normalisés,
- des **hypothèses causales** (jamais des causalités prouvées),
- une **intention opérationnelle** (planifié vs non planifié),
- des événements **horodatés HLC** (Hybrid Logical Clock).

ISAP est conçu pour combler le trou laissé par OpenTelemetry sur la causalité
**hors-RPC** (contention de ressources partagées, asynchrone, cross-layer).

## 2. Ce qu'ISAP *n'est pas* (non-goals)

- **Pas un transport.** ISAP s'exécute sur UDP/DTLS (port 5765).
- **Pas un collecteur de métriques.** Il ne remplace pas Prometheus.
- **Pas un protocole de tracing.** Il complète OpenTelemetry pour la causalité
  qui ne passe pas par un RPC.
- **Pas un oracle causal.** Toute affirmation causale est une **hypothèse**
  assortie d'un score de confiance.
- **Pas tolérant aux fautes Byzantines** en v0.1. Un nœud malveillant peut
  empoisonner le graphe ; v1.0 traitera ce point via signature DTLS.
- **Pas un système d'apprentissage à grande échelle.** Le module d'intention
  fonctionne par cluster, pas globalement.

## 3. Vocabulaire [INFORMATIF]

| Terme | Définition |
|---|---|
| **Pulse** | Un message ISAP unitaire. |
| **State Vector** | Un n-uplet de métriques normalisées dans `[0, 1]`. |
| **Causal Hypothesis** | Une affirmation `(source, effet, confiance, evidence)` — *jamais* présentée comme une certitude. |
| **Evidence type** | Origine de l'hypothèse : `declared`, `correlated`, `inferred`. |
| **Intent** | Classification opérationnelle : `IDLE | NORMAL | PLANNED | STRESSED | CRITICAL | UNKNOWN`. |
| **HLC** | Hybrid Logical Clock (Kulkarni et al., 2014), tuple `(l, c)`. |
| **Collector** | Service qui agrège les Pulses, condense le graphe causal et expose un état dérivé. |

## 4. Format wire v1 (profil JSON) [NORMATIF]

> v0.1 utilise JSON pour faciliter le développement. v1.0 normalisera un format
> binaire (struct C) compatible avec ce schéma.

### 4.1 Pulse

```json
{
  "protocol":   "ISAP",
  "version":    1,
  "node_id":    "node_a",
  "node_uuid":  "550e8400-e29b-41d4-a716-446655440000",
  "cluster_id": 3826341592,
  "hlc":        { "l": 1714123200100, "c": 0 },
  "intent": {
    "class":      "PLANNED",
    "confidence": 0.87,
    "duration_ms": 480000,
    "source":     "scheduler"
  },
  "state": {
    "cpu_pressure":    0.94,
    "memory_pressure": 0.87,
    "io_pressure":     0.42,
    "swap_tension":    0.05,
    "net_tension":     0.31
  },
  "anomaly_score": 0.12,
  "mode":          "NORMAL",
  "hypothesis":    [ /* voir §4.2 */ ],
  "ts_iso":        "2026-04-27T03:00:00Z"
}
```

#### Champs obligatoires

| Champ | Type | Notes |
|---|---|---|
| `protocol` | string | toujours `"ISAP"` |
| `version` | uint8 | `1` pour cette spec |
| `node_id` | string ≤ 64 | identifiant lisible |
| `node_uuid` | string (UUID v4) | identifiant stable inter-redémarrage |
| `cluster_id` | uint32 | hash CRC32 du nom de cluster |
| `hlc.l` | uint48 | composante physique HLC en ms |
| `hlc.c` | uint16 | compteur logique HLC |
| `state` | object | au moins une métrique normalisée `[0, 1]` |
| `mode` | enum | `WHISPER | NORMAL | STORM` (cadence d'émission) |

#### Champs optionnels

`intent`, `hypothesis`, `anomaly_score`, `ts_iso`.

### 4.2 Causal Hypothesis [NORMATIF]

Le champ `hypothesis` est un **tableau** d'objets, chacun de la forme :

```json
{
  "source_node":  "node_a",
  "source_uuid":  "...",
  "source_hlc":   { "l": 1714123200099, "c": 0 },
  "confidence":   0.74,
  "evidence":     "correlated",
  "delay_ms":     1200,
  "scc_group":    null,
  "explanation":  "cpu_burn on source coincided with mem_pressure here within 1.2s"
}
```

#### Sémantique d'`evidence` [NORMATIF]

| Valeur | Signification | Émetteur typique |
|---|---|---|
| `declared` | Le nœud émetteur déclare un lien sans inférence. **Faible confiance par défaut.** | nœud applicatif qui sait qu'il vient de migrer une VM |
| `correlated` | Lien établi par corrélation croisée temporelle. | collecteur ou agent local |
| `inferred` | Lien établi par méthode causale (Granger, transfer entropy, do-calculus). | collecteur |

> **Règle critique** : un consommateur ISAP qui agit sur une hypothèse
> `declared` sans la corroborer fait confiance à l'émetteur. C'est explicitement
> un risque Byzantin documenté.

### 4.3 Intent [NORMATIF]

```json
"intent": {
  "class":       "PLANNED",
  "confidence":  0.87,
  "duration_ms": 480000,
  "source":      "scheduler"
}
```

| Champ | Valeurs |
|---|---|
| `class` | `IDLE | NORMAL | PLANNED | STRESSED | CRITICAL | UNKNOWN` |
| `confidence` | `[0, 1]` |
| `duration_ms` | uint32, durée estimée restante |
| `source` | `scheduler | api | inferred | manual` |

## 5. Sémantique HLC [NORMATIF]

ISAP réutilise les règles de Kulkarni et al. (2014) sans modification.

### 5.1 Émission
```
t      = now_ms()
l_new  = max(l, t)
c_new  = (l_new == l) ? c + 1 : 0
return (l_new, c_new)
```

### 5.2 Réception d'un Pulse `(l_m, c_m)`
```
t      = now_ms()
l_new  = max(l, l_m, t)
c_new  =
   if l_new == l == l_m  : max(c, c_m) + 1
   if l_new == l         : c + 1
   if l_new == l_m       : c_m + 1
   else                  : 0
```

### 5.3 Comparaison causale
`(l_a, c_a) < (l_b, c_b)` ssi `l_a < l_b` ou (`l_a == l_b` et `c_a < c_b`).

### 5.4 Propriété ISAP-HLC-1 [NORMATIF]
Pour tout couple d'événements `e_a → e_b` (a précède causalement b) dans le
système ISAP, on a `HLC(e_a) < HLC(e_b)`. Cette propriété tient sous dérive NTP
arbitraire bornée.

## 6. Comportement du Collecteur [NORMATIF]

### 6.1 Construction du graphe
Pour chaque Pulse reçu avec `hypothesis[]` non vide, le Collector ajoute des
arêtes `source_node → effect_node` au graphe causal courant.

### 6.2 Condensation Tarjan
Toutes les `T_scc` secondes (recommandé : 5s), le Collector exécute Tarjan
(O(V+E)) sur le graphe. Pour chaque SCC de taille > 1 :

- chaque arête entrante au SCC depuis l'extérieur est annotée comme **cause
  externe candidate** ;
- les hypothèses internes au SCC sont marquées `scc_group = <id>` dans les
  Pulses redistribués.

### 6.3 Validation HLC
Pour toute hypothèse `source → self`, le Collector vérifie
`HLC(source) < HLC(self)`. En cas de violation, l'hypothèse est rejetée et
comptée dans `metric: isap.hlc_violations_total`.

### 6.4 Propriété ISAP-DAG-1 [NORMATIF]
Le graphe condensé `G' = (N', E')` produit par §6.2 est garanti acyclique.

## 7. Cadence d'émission [NORMATIF]

| Mode | Intervalle | Déclencheur |
|---|---|---|
| `WHISPER` | 5 s | `anomaly_score < 0.2` |
| `NORMAL` | 1 s | défaut |
| `STORM` | 100 ms | `anomaly_score > 0.6` |

Le mode est choisi localement et signalé dans le Pulse — le Collector ne le
contrôle pas. Cette asymétrie est intentionnelle.

## 8. Versioning [NORMATIF]

- v0.x : brouillons, breaking changes autorisés.
- v1.0 : gel du wire format JSON ; ajout du profil binaire.
- Tout champ ajouté en v1.x doit être optionnel et ignoré par les
  implémentations antérieures (forward compat par défaut JSON).

## 9. Sécurité [INFORMATIF]

### 9.1 Modèle de menace v0.1
- Les nœuds émetteurs sont supposés **honnêtes mais imprécis**.
- Le réseau est supposé non hostile (homelab, cluster privé).
- Pas de protection contre l'injection : un attaquant sur le réseau peut
  émettre des Pulses arbitraires.

### 9.2 Modèle de menace v1.0 (futur)
- DTLS obligatoire avec clés par nœud.
- Signature des hypothèses `inferred` par le Collector qui les a produites.
- Quorum sur les hypothèses `declared` avant action automatique.

## 10. Mesures objectives [NORMATIF — banc]

Toute implémentation ISAP doit pouvoir reporter :

| Métrique | Description |
|---|---|
| `isap.pulses_total` | Pulses émis / reçus |
| `isap.bytes_total` | Volume wire |
| `isap.hlc_violations_total` | Hypothèses rejetées par §6.3 |
| `isap.scc_groups_active` | SCCs actuellement détectés |
| `isap.hypothesis_precision` | Mesurée contre vérité terrain (Phase 1+) |
| `isap.hypothesis_recall` | Idem |
| `isap.intent_accuracy` | Phase 3+ |

Sans ces métriques, une implémentation **ne peut pas être qualifiée d'ISAP-compliant**.

## 11. Questions ouvertes [INFORMATIF]

1. Faut-il un canal de feedback Collector → Nœud (rétro-propagation des
   hypothèses corroborées) ? Pas en v0.1.
2. Comment fédérer plusieurs clusters ISAP ? Hors scope v1.0.
3. Schema evolution du `state` : faut-il un registre de noms ? Probablement,
   à l'OpenTelemetry resource semantic conventions.
4. Compatibilité OTLP : peut-on transporter une Pulse comme attribut
   d'OpenTelemetry Span ? À étudier — gain d'écosystème massif.

## 12. Références

- Kulkarni, Demirbas et al., *Logical Physical Clocks and Consistent Snapshots
  in Globally Distributed Databases*, 2014.
- Tarjan, *Depth-First Search and Linear Graph Algorithms*, SIAM J. Comput.,
  1972.
- Pearl, *Causality: Models, Reasoning, and Inference*, 2nd ed., 2009.
- Granger, *Investigating Causal Relations by Econometric Models and
  Cross-Spectral Methods*, Econometrica, 1969.
- Schreiber, *Measuring Information Transfer*, Phys. Rev. Lett., 2000.
- OpenTelemetry Specification, v1.x.

---

*Cette spec est un brouillon. Elle existe pour être réfutée, mesurée, et
réécrite. Toute affirmation non corroborée par un benchmark sera retirée.*

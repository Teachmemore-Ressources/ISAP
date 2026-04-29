"""Quick inspection of phase2 archive — sort by anomaly score and show timeline."""
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

path = sys.argv[1] if len(sys.argv) > 1 else "data/observations_phase2.jsonl"
ps = [json.loads(l) for l in open(path, encoding="utf-8")]
ps.sort(key=lambda p: p["hlc"]["l"])

print(f"=== {path} : {len(ps)} pulses ===\n")

# Top 12 par anomaly_score
top = sorted(ps, key=lambda p: -p["anomaly_score"])[:12]
print("--- Top 12 anomaly_score ---")
for p in top:
    s = p["state"]
    print(f"  {p['node_id']:8} AS={p['anomaly_score']:.3f}  "
          f"cpu={s['cpu_pressure']:.3f}  mem={s['memory_pressure']:.3f}  "
          f"io={s['io_pressure']:.3f}  net={s['net_tension']:.3f}  "
          f"mode={p['mode']:7}  hlc.l={p['hlc']['l']}")

# Distribution mode
from collections import Counter
mode_per_node = {}
for p in ps:
    mode_per_node.setdefault(p["node_id"], Counter())[p["mode"]] += 1
print("\n--- mode distribution per node ---")
for nid, c in sorted(mode_per_node.items()):
    print(f"  {nid:8} {dict(c)}")

# Range cpu par nœud
print("\n--- cpu_pressure range per node ---")
by_node = {}
for p in ps:
    by_node.setdefault(p["node_id"], []).append(p["state"]["cpu_pressure"])
for nid, vals in sorted(by_node.items()):
    print(f"  {nid:8} min={min(vals):.3f}  mean={sum(vals)/len(vals):.3f}  max={max(vals):.3f}  n={len(vals)}")

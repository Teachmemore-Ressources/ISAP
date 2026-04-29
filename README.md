# ISAP — Intent-State Awareness Protocol

> An open wire-format protocol for transporting **causal hypotheses**, **operational intent**, and **state vectors** between observed nodes and a central collector.

**Status**: research artifact · v0.1 draft · spec + reference implementation + 3-phase benchmark.

ISAP fills a gap that OpenTelemetry traces cannot reach: **causality without RPC**.
When a backup saturates a SAN, when a noisy neighbor steals CPU, when a kernel
OOM kill triggers a reschedule cascade — there is no transaction context to follow,
and current observability tools either drown in metrics (Prometheus) or stay blind
(Jaeger/Zipkin). ISAP transports a structured *hypothesis*: *"I am under load, and
my best guess at the cause is X, with confidence c."*

This repo is the **research-grade artifact** behind that idea: a formal spec,
a reference Python implementation, a 6-scenario synthetic benchmark, a Docker
Compose real-system benchmark, and three independent inference methods compared
on the same dataset.

---

## Headline results

| Phase | Setting | Method | F1 |
|---|---|---|---|
| **Phase 1** | 6 synthetic scenarios with ground-truth edges | xcorr on Δ-series | **0.857** ✅ |
| **Phase 2** | 3-container Docker Compose, real cgroup v2 metrics | xcorr + jitter ±20% | **1.0 undirected** ✅ |
| **Phase 3** | Same dataset, 3 methods compared | xcorr ≈ TE ≈ cGranger | all converge |

**Phase 3 finding**: causal *direction* on sub-sample events is **not a statistical
problem** — it is a **sampling-resolution problem**. No inference method (cross-
correlation, transfer entropy, conditional Granger) can recover precedence finer
than the Pulse cadence. This motivates eBPF-timestamped event extensions in the
spec roadmap.

---

## What ISAP is

A **wire format** + **reference behavior** carrying four dimensions per Pulse:

| Field | Carries |
|---|---|
| `state` | normalized metrics in `[0, 1]` (cpu, memory, io, swap, net) |
| `intent` | operational class — `IDLE / NORMAL / PLANNED / STRESSED / CRITICAL / UNKNOWN` |
| `hypothesis[]` | causal claims with **explicit `evidence` field** (`declared / correlated / inferred`) |
| `hlc` | Hybrid Logical Clock (Kulkarni et al., 2014) for cross-node ordering |

The `evidence` field is the protocol's honesty mechanism: a node that *declares*
a cause without inferring it gets `evidence: declared` and a low default
confidence — preventing the Byzantine theatrics of trusting nodes that
self-attribute causality.

See [SPEC.md](SPEC.md) for the formal specification.

---

## What ISAP is *not*

- **Not a replacement for Prometheus or OpenTelemetry.** ISAP is a complementary
  semantic layer above raw metrics and traces.
- **Not a transport protocol.** It runs on UDP/DTLS port 5765.
- **Not a causality oracle.** Every hypothesis carries a confidence score and an
  evidence type. ISAP transports *claims*, not *truths*.
- **Not Byzantine-tolerant in v0.1.** A malicious node can poison the graph.
  v1.0 will add per-node DTLS signatures.

---

## Architecture

```
                                                                              
                ┌─ Proxmox / Docker / k8s ─┐                                  
                │                          │                                  
                │  ┌──node──┐  ┌──node──┐  │                                  
                │  │ agent  │  │ agent  │  │   Pulses (UDP, ≤256 B):          
                │  └────┬───┘  └────┬───┘  │   { state, intent, hlc, …,       
                │       │           │      │     hypothesis: [{source,        
                └───────┼───────────┼──────┘       evidence, confidence}]     
                        │           │      ─────────►                         
                        ▼           ▼                                         
                  ┌──────────────────────────┐                                
                  │        collector         │  →  Tarjan SCC condense        
                  │   (dashboard_server.py)  │      causal graph (SPEC §6.2)  
                  └────────────┬─────────────┘                                
                               │                                              
                               ▼                                              
                  ┌──────────────────────────┐                                
                  │ causal_infer*.py         │  precision/recall/F1 vs        
                  │ (xcorr / TE / cGranger)  │  ground-truth from runner      
                  └──────────────────────────┘                                
```

---

## Quick start

### Prerequisites
- Python 3.10+ with `numpy`, `psutil`
- Docker Desktop or Docker Engine + docker-compose v2 (for Phase 2)

### 30 seconds — verify HLC

```bash
python test_hlc.py
# 5/5 HLC property tests passed
```

### 5 minutes — Phase 1 (synthetic, no Docker required)

```bash
pip install -r requirements.txt
python workload_gen.py --seed 42       # writes data/observations.jsonl + ground_truth
python causal_infer.py                  # xcorr baseline → Phase1_report.md (F1 = 0.857)
```

### 15 minutes — Phase 2 (Docker, real cgroup metrics)

```bash
docker compose up -d --build           # 3 agents + collector + bridge net
python scenario_runner.py              # 6 scenarios, ~5 min
python causal_infer_phase2.py \
    --threshold 0.4 --min-lag-ms 1500   # → Phase2_report.md (F1 undir = 1.0)
docker compose down
```

### 5 minutes — Phase 3 (compare methods on Phase 2 data)

```bash
python phase3_compare.py               # → Phase3_report.md
```

---

## Repository layout

```
ISAP/
├── SPEC.md                       Formal v0.1 specification
├── README.md                     ← you are here
│
├── Phase1_report.md              Synthetic benchmark (F1 = 0.857)
├── Phase1_report_granger.md      Phase 1 method comparison
├── Phase2_report.md              Real Docker benchmark (F1 undir = 1.0)
├── Phase3_report.md              3-method comparison & verdict
│
├── agent.py                      Passive ISAP agent (cgroup v2 + jitter)
├── workload_gen.py               Phase 1 synthetic ground-truth generator
├── scenario_runner.py            Phase 2 Docker workload orchestrator
├── causal_infer.py               xcorr + Granger + Transfer Entropy + cGranger
├── causal_infer_phase2.py        Phase 2 evaluator (directed + undirected F1)
├── phase3_compare.py             Phase 3 method sweep
├── test_hlc.py                   HLC property tests
│
├── dashboard_server.py           UDP collector + WebSocket dashboard
├── dashboard.html                Live cluster dashboard
│
├── docker-compose.yml            3 agents + collector + bridge network
├── Dockerfile                    Agent image (Python + stress-ng + fio + iperf3)
├── Makefile                      Reproducibility targets
├── requirements.txt              psutil, numpy
└── data/                         Generated datasets (gitignored)
```

---

## Honest limitations

This is a **research artifact**, not a production-ready protocol. Known issues:

1. **Direction inference fails on sub-sample events.** Phase 3 demonstrates this
   is intrinsic to telemetry-rate sampling, not a method bug.
2. **No Byzantine fault tolerance in v0.1.** A malicious agent can inject
   arbitrary hypotheses.
3. **Synthetic and Docker-Compose benchmarks only.** No bare-metal validation
   yet, no Prometheus/AIOps comparison.
4. **State vector schema is fixed.** No registry; should align with
   OpenTelemetry resource semantic conventions in v1.0.

These are openly acknowledged in each phase report. ISAP makes no claim it
hasn't been able to reproduce.

---

## Roadmap

- **v0.1 (now)** — JSON wire format, reference Python impl, 3-phase benchmark
- **v0.2** — binary wire format (struct C-style), DTLS, jittered cadence in spec
- **v0.3** — eBPF event extensions (microsecond timestamps), Phase 4
- **v1.0** — frozen wire format, OpenTelemetry attribute mapping, CNCF sandbox proposal

---

## Citation

If you use ISAP in academic work, please cite this repository (formal paper TBD):

```bibtex
@misc{isap-2026,
  author       = {ISAP authors},
  title        = {{ISAP: Intent-State Awareness Protocol — Reference Implementation}},
  year         = {2026},
  url          = {https://github.com/<your-handle>/ISAP},
  note         = {v0.1 draft, research artifact}
}
```

---

## License

MIT. See [LICENSE](LICENSE).

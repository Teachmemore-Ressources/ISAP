# ISAP — reproducibility targets
# Each target produces an artefact that anyone can verify.

PYTHON ?= python

.PHONY: help test phase1 phase2-up phase2-bench phase2-down phase2 phase3 all clean

help:
	@echo "ISAP reproducibility:"
	@echo "  make test          — HLC property tests (5/5)"
	@echo "  make phase1        — synthetic benchmark (no Docker, ~30s)"
	@echo "  make phase2-up     — start Docker stack (3 agents + collector)"
	@echo "  make phase2-bench  — run real workloads (~5 min, requires phase2-up)"
	@echo "  make phase2-down   — stop Docker stack"
	@echo "  make phase2        — full Phase 2 cycle (up + bench + analyze)"
	@echo "  make phase3        — compare 3 inference methods on Phase 2 data"
	@echo "  make all           — full reproduction (Phase 1 + 2 + 3)"
	@echo "  make clean         — remove generated data"

test:
	$(PYTHON) test_hlc.py

phase1:
	$(PYTHON) workload_gen.py --seed 42
	$(PYTHON) causal_infer.py --method xcorr
	$(PYTHON) causal_infer.py --method granger \
	    --out data/metrics_granger.json \
	    --out-edges data/inferred_edges_granger.json \
	    --out-report Phase1_report_granger.md

phase2-up:
	docker compose up -d --build
	@echo "Stack up. Wait ~10s for agents to initialize before phase2-bench."

phase2-bench:
	$(PYTHON) scenario_runner.py --duration 45 --cooldown 10
	$(PYTHON) causal_infer_phase2.py --threshold 0.4 --min-lag-ms 1500

phase2-down:
	docker compose down

phase2: phase2-up
	@sleep 10
	$(MAKE) phase2-bench
	$(MAKE) phase2-down

phase3:
	$(PYTHON) phase3_compare.py

all: test phase1 phase2 phase3
	@echo ""
	@echo "All phases complete. Reports written to:"
	@echo "  Phase1_report.md / Phase1_report_granger.md"
	@echo "  Phase2_report.md"
	@echo "  Phase3_report.md"

clean:
	rm -rf data/observations*.jsonl data/ground_truth.json data/phase2_truth.json
	rm -rf data/metrics*.json data/inferred_edges*.json data/phase3_*.json
	rm -rf data/phase2_smoke.json
	rm -rf __pycache__ .pytest_cache

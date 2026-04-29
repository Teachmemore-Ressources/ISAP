# ISAP — reproducibility + deployment targets
# Each target produces an artefact that anyone can verify.

PYTHON ?= python

.PHONY: help test phase1 phase2-up phase2-bench phase2-down phase2 phase3 all clean serve install

help:
	@echo "ISAP — cibles disponibles :"
	@echo ""
	@echo "  Déploiement :"
	@echo "  make serve         — lance le collecteur en local (HTTP :9000, WS :9001, UDP :9999)"
	@echo "  make install       — installe comme service systemd (Linux, nécessite sudo)"
	@echo ""
	@echo "  Reproduction :"
	@echo "  make test          — tests propriétés HLC (5/5)"
	@echo "  make phase1        — benchmark synthétique (sans Docker, ~30s)"
	@echo "  make phase2-up     — démarre le stack Docker (3 agents + collecteur)"
	@echo "  make phase2-bench  — charge réelle (~5 min, nécessite phase2-up)"
	@echo "  make phase2-down   — arrête le stack Docker"
	@echo "  make phase2        — cycle Phase 2 complet (up + bench + analyze)"
	@echo "  make phase3        — compare 3 méthodes d'inférence sur données Phase 2"
	@echo "  make all           — reproduction complète (Phase 1 + 2 + 3)"
	@echo "  make clean         — supprime les données générées"

serve:
	@echo "Démarrage du collecteur ISAP..."
	@echo "  Dashboard → http://localhost:9000"
	@echo "  API       → http://localhost:9000/api/v1/status"
	$(PYTHON) dashboard_server.py

install:
	@echo "Installation des services systemd (nécessite root)..."
	sudo bash install.sh

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

# ── Aide rapide API REST ──────────────────────────────────────────────────────
api-status:
	curl -s http://localhost:9000/api/v1/status | python -m json.tool

api-nodes:
	curl -s http://localhost:9000/api/v1/nodes | python -m json.tool

api-graph:
	curl -s http://localhost:9000/api/v1/graph | python -m json.tool

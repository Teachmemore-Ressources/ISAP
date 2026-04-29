# ISAP — Dockerfile pour l'agent passif
# Inclut les outils nécessaires à scenario_runner.py pour générer
# de la vraie charge système : stress-ng (CPU/RAM), fio (IO), iperf3 (réseau).

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        stress-ng \
        fio \
        iperf3 \
        procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir psutil==5.9.8

WORKDIR /isap
COPY agent.py /isap/agent.py

# Permet à `pkill -f` de fonctionner pour interrompre les workloads
CMD ["python3", "-u", "/isap/agent.py"]

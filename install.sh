#!/usr/bin/env bash
# =============================================================================
# ISAP — Script d'installation universel (Linux)
# Supporte : Debian/Ubuntu (apt) · RHEL/CentOS/AlmaLinux (yum) · Fedora (dnf)
#
# Usage :
#   sudo bash install.sh
#
# Variables d'environnement (optionnelles, avant le sudo) :
#   ISAP_HTTP_PORT   port HTTP du dashboard      (défaut : 9000)
#   ISAP_WS_PORT     port WebSocket              (défaut : 9001)
#   ISAP_UDP_PORT    port UDP réception pulses   (défaut : 9999)
#   ISAP_NODE_ID     identifiant de ce nœud      (défaut : hostname)
#   ISAP_COLLECTOR   adresse du collecteur       (défaut : 127.0.0.1)
#
# Exemple pour un nœud distant pointant vers un collecteur central :
#   ISAP_COLLECTOR=192.168.1.10 sudo bash install.sh --agent-only
#
# Options :
#   --collector-only   installe uniquement isap-collector
#   --agent-only       installe uniquement isap-agent
#   --uninstall        désactive et supprime les services
# =============================================================================

set -euo pipefail

# ── Paramètres ───────────────────────────────────────────────────────────────
ISAP_VERSION="0.1.0"
ISAP_DIR="/opt/isap"
ISAP_DATA="/var/lib/isap"
ISAP_LOG="/var/log/isap"
ISAP_USER="isap"

HTTP_PORT="${ISAP_HTTP_PORT:-9000}"
WS_PORT="${ISAP_WS_PORT:-9001}"
UDP_PORT="${ISAP_UDP_PORT:-9999}"
NODE_ID="${ISAP_NODE_ID:-$(hostname)}"
COLLECTOR="${ISAP_COLLECTOR:-127.0.0.1}"

INSTALL_COLLECTOR=true
INSTALL_AGENT=true

# ── Analyse des arguments ─────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --collector-only) INSTALL_AGENT=false ;;
        --agent-only)     INSTALL_COLLECTOR=false ;;
        --uninstall)      UNINSTALL=true ;;
        *) echo "Option inconnue : $arg"; exit 1 ;;
    esac
done

# ── Vérification root ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Ce script doit être lancé en root : sudo bash install.sh"
    exit 1
fi

# ── Désinstallation ──────────────────────────────────────────────────────────
if [[ "${UNINSTALL:-false}" == "true" ]]; then
    echo "==> Désinstallation ISAP…"
    systemctl stop  isap-collector isap-agent 2>/dev/null || true
    systemctl disable isap-collector isap-agent 2>/dev/null || true
    rm -f /etc/systemd/system/isap-collector.service
    rm -f /etc/systemd/system/isap-agent.service
    systemctl daemon-reload
    echo "Services supprimés."
    echo "Pour supprimer les données : rm -rf $ISAP_DIR $ISAP_DATA $ISAP_LOG"
    exit 0
fi

# ── Bannière ─────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  ISAP v${ISAP_VERSION} — Installation"
echo "============================================================"
echo "  Répertoire    : $ISAP_DIR"
echo "  Données       : $ISAP_DATA"
echo "  Logs          : $ISAP_LOG"
echo "  HTTP dashboard: :$HTTP_PORT"
echo "  WebSocket live: :$WS_PORT"
echo "  UDP pulses    : :$UDP_PORT"
echo "  Nœud ID       : $NODE_ID"
echo "  Collecteur    : $COLLECTOR:$UDP_PORT"
echo "============================================================"
echo ""

# ── Détection du gestionnaire de paquets ─────────────────────────────────────
detect_pkg_manager() {
    if   command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf     &>/dev/null; then echo "dnf"
    elif command -v yum     &>/dev/null; then echo "yum"
    else                                      echo "none"
    fi
}
PKG_MGR=$(detect_pkg_manager)
echo "  Gestionnaire de paquets : $PKG_MGR"
echo ""

# ── Installation de Python 3 ──────────────────────────────────────────────────
install_python() {
    echo "==> Installation de Python 3…"
    case "$PKG_MGR" in
        apt)
            apt-get update -qq
            apt-get install -y --no-install-recommends \
                python3 python3-pip python3-venv curl
            ;;
        dnf)
            dnf install -y python3 python3-pip curl
            ;;
        yum)
            yum install -y python3 python3-pip curl
            ;;
        none)
            if ! command -v python3 &>/dev/null; then
                echo "[!] Python 3 introuvable et aucun gestionnaire connu."
                echo "    Installez Python 3.9+ manuellement et relancez."
                exit 1
            fi
            echo "[ok] Python3 présent, pas d'installation de paquets."
            ;;
    esac
    python3 --version
    echo "[ok] Python3 prêt"
}

install_python

# ── Création de l'utilisateur système ────────────────────────────────────────
echo "==> Création de l'utilisateur '$ISAP_USER'…"
if ! id -u "$ISAP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /sbin/nologin "$ISAP_USER"
    echo "[ok] Utilisateur '$ISAP_USER' créé"
else
    echo "[ok] Utilisateur '$ISAP_USER' existe déjà"
fi

# ── Création des répertoires ──────────────────────────────────────────────────
echo "==> Création des répertoires…"
mkdir -p "$ISAP_DIR" "$ISAP_DATA" "$ISAP_LOG"
chown "$ISAP_USER:$ISAP_USER" "$ISAP_DATA" "$ISAP_LOG"
echo "[ok] $ISAP_DIR  $ISAP_DATA  $ISAP_LOG"

# ── Copie des fichiers ISAP ───────────────────────────────────────────────────
echo "==> Copie des fichiers dans $ISAP_DIR…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REQUIRED_FILES=(
    dashboard_server.py
    dashboard.html
    agent.py
    causal_infer.py
    requirements.txt
)
OPTIONAL_FILES=(
    causal_infer_phase2.py
    phase3_compare.py
    workload_gen.py
    scenario_runner.py
    SPEC.md
    README.md
)

for f in "${REQUIRED_FILES[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$ISAP_DIR/"
        echo "  [ok] $f"
    else
        echo "  [!!] MANQUANT : $f — vérifiez le répertoire source"
    fi
done

for f in "${OPTIONAL_FILES[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$ISAP_DIR/"
        echo "  [ok] $f (optionnel)"
    fi
done

# ── Création du virtualenv et installation des dépendances ───────────────────
echo "==> Installation des dépendances Python…"
python3 -m venv "$ISAP_DIR/venv"
"$ISAP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$ISAP_DIR/venv/bin/pip" install --quiet -r "$ISAP_DIR/requirements.txt"
echo "[ok] Dépendances installées dans $ISAP_DIR/venv/"

# ── Unit systemd : isap-collector ────────────────────────────────────────────
if [[ "$INSTALL_COLLECTOR" == "true" ]]; then
    echo "==> Création du service isap-collector…"
    cat > /etc/systemd/system/isap-collector.service << EOF
[Unit]
Description=ISAP Collector — Dashboard HTTP + UDP receiver
Documentation=https://github.com/yourname/isap
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${ISAP_USER}
Group=${ISAP_USER}
WorkingDirectory=${ISAP_DIR}
ExecStart=${ISAP_DIR}/venv/bin/python ${ISAP_DIR}/dashboard_server.py
Restart=on-failure
RestartSec=5s
TimeoutStopSec=20s

# Ports
Environment=ISAP_HTTP_PORT=${HTTP_PORT}
Environment=ISAP_WS_PORT=${WS_PORT}
Environment=ISAP_UDP_PORT=${UDP_PORT}

# Stockage
Environment=ISAP_ARCHIVE_PATH=${ISAP_DATA}/observations.jsonl
Environment=ISAP_DATA_DIR=${ISAP_DATA}

# Logs
StandardOutput=append:${ISAP_LOG}/collector.log
StandardError=append:${ISAP_LOG}/collector.log

# Durcissement sécurité
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${ISAP_DATA} ${ISAP_LOG}

[Install]
WantedBy=multi-user.target
EOF
    echo "[ok] /etc/systemd/system/isap-collector.service"
fi

# ── Unit systemd : isap-agent ─────────────────────────────────────────────────
if [[ "$INSTALL_AGENT" == "true" ]]; then
    echo "==> Création du service isap-agent…"
    cat > /etc/systemd/system/isap-agent.service << EOF
[Unit]
Description=ISAP Agent — collecteur de métriques local
Documentation=https://github.com/yourname/isap
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${ISAP_USER}
Group=${ISAP_USER}
WorkingDirectory=${ISAP_DIR}
ExecStart=${ISAP_DIR}/venv/bin/python ${ISAP_DIR}/agent.py
Restart=on-failure
RestartSec=5s
TimeoutStopSec=10s

# Identité du nœud
Environment=ISAP_NODE_ID=${NODE_ID}

# Adresse du collecteur central
Environment=ISAP_COLLECTOR_HOST=${COLLECTOR}
Environment=ISAP_COLLECTOR_PORT=${UDP_PORT}

# Cadence d'émission (modifiables sans recompiler)
Environment=ISAP_INTERVAL_S=1.0
Environment=ISAP_WHISPER_S=5.0
Environment=ISAP_STORM_S=0.1

# Logs
StandardOutput=append:${ISAP_LOG}/agent.log
StandardError=append:${ISAP_LOG}/agent.log

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${ISAP_LOG}

[Install]
WantedBy=multi-user.target
EOF
    echo "[ok] /etc/systemd/system/isap-agent.service"
fi

# ── Activation des services ──────────────────────────────────────────────────
echo "==> Activation et démarrage des services…"
systemctl daemon-reload

if [[ "$INSTALL_COLLECTOR" == "true" ]]; then
    systemctl enable isap-collector
    systemctl restart isap-collector
    sleep 2
    if systemctl is-active --quiet isap-collector; then
        echo "[ok] isap-collector actif"
    else
        echo "[!!] isap-collector a échoué — voir : journalctl -u isap-collector -n 30"
    fi
fi

if [[ "$INSTALL_AGENT" == "true" ]]; then
    systemctl enable isap-agent
    systemctl restart isap-agent
    sleep 1
    if systemctl is-active --quiet isap-agent; then
        echo "[ok] isap-agent actif"
    else
        echo "[!!] isap-agent a échoué — voir : journalctl -u isap-agent -n 30"
    fi
fi

# ── Résumé final ─────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo "============================================================"
echo "  ISAP installé avec succès !"
echo "============================================================"
echo ""
if [[ "$INSTALL_COLLECTOR" == "true" ]]; then
    echo "  Dashboard    → http://${LOCAL_IP}:${HTTP_PORT}/"
    echo "  API status   → http://${LOCAL_IP}:${HTTP_PORT}/api/v1/status"
    echo "  API graphe   → http://${LOCAL_IP}:${HTTP_PORT}/api/v1/graph"
    echo "  API nœuds    → http://${LOCAL_IP}:${HTTP_PORT}/api/v1/nodes"
    echo ""
fi
echo "  Commandes utiles :"
echo "    systemctl status isap-collector isap-agent"
echo "    journalctl -u isap-collector -f"
echo "    journalctl -u isap-agent -f"
echo "    tail -f ${ISAP_LOG}/collector.log"
echo ""
echo "  Configurer l'agent (pointer vers un collecteur distant) :"
echo "    systemctl edit isap-agent"
echo "    # Ajouter : Environment=ISAP_COLLECTOR_HOST=<IP_COLLECTEUR>"
echo "    systemctl restart isap-agent"
echo ""
echo "  Désinstaller :"
echo "    sudo bash install.sh --uninstall"
echo ""

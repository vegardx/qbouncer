#!/bin/bash
#
# qbouncer installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/vegardx/qbouncer/main/scripts/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --wg-interface wg0 --gateway 10.2.0.1
#

set -euo pipefail

# Defaults
INSTALL_DIR="/opt/qbouncer"
CONFIG_DIR="/etc/qbouncer"
SERVICE_USER="qbouncer"
REPO_URL="https://github.com/vegardx/qbouncer.git"
BRANCH="main"

# Configuration defaults
WG_INTERFACE=""
WG_GATEWAY=""
QBT_HOST="localhost"
QBT_PORT=""
QBT_USERNAME=""
QBT_PASSWORD=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "\n${BLUE}${BOLD}==> $1${NC}"; }

prompt() {
    local var_name="$1"
    local prompt_text="$2"
    local default="$3"
    local current_value="${!var_name:-}"

    # If already set via CLI, skip prompt
    if [[ -n "$current_value" ]]; then
        echo -e "${BOLD}$prompt_text${NC} [$current_value] (from CLI)"
        return
    fi

    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${BOLD}$prompt_text${NC} [$default]: ")" value
        eval "$var_name=\"${value:-$default}\""
    else
        read -rp "$(echo -e "${BOLD}$prompt_text${NC}: ")" value
        eval "$var_name=\"$value\""
    fi
}

prompt_password() {
    local var_name="$1"
    local prompt_text="$2"
    local current_value="${!var_name:-}"

    if [[ -n "$current_value" ]]; then
        echo -e "${BOLD}$prompt_text${NC}: *** (from CLI)"
        return
    fi

    read -rsp "$(echo -e "${BOLD}$prompt_text${NC} (leave empty if none): ")" value
    echo
    eval "$var_name=\"$value\""
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
    --wg-interface NAME     WireGuard interface name (e.g., wg0)
    --gateway IP            NAT-PMP gateway IP address
    --qbt-host HOST         qBittorrent host (default: localhost)
    --qbt-port PORT         qBittorrent Web UI port
    --qbt-username USER     qBittorrent username
    --qbt-password PASS     qBittorrent password
    --non-interactive       Skip prompts, use defaults/CLI args only
    --force-config          Overwrite existing configuration file
    -h, --help              Show this help message

Examples:
    # Interactive install
    $0

    # Non-interactive with all options
    $0 --non-interactive --wg-interface wg0 --gateway 10.2.0.1 --qbt-port 8080

    # Pipe from curl
    curl -fsSL https://raw.githubusercontent.com/vegardx/qbouncer/main/scripts/install.sh | bash

    # Reinstall with new config
    $0 --non-interactive --force-config --wg-interface wg2 --qbt-port 80
EOF
    exit 0
}

# Parse arguments
NON_INTERACTIVE=false
FORCE_CONFIG=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --wg-interface) WG_INTERFACE="$2"; shift 2 ;;
        --gateway) WG_GATEWAY="$2"; shift 2 ;;
        --qbt-host) QBT_HOST="$2"; shift 2 ;;
        --qbt-port) QBT_PORT="$2"; shift 2 ;;
        --qbt-username) QBT_USERNAME="$2"; shift 2 ;;
        --qbt-password) QBT_PASSWORD="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --force-config) FORCE_CONFIG=true; shift ;;
        -h|--help) usage ;;
        *) error "Unknown option: $1" ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root"
fi

# Detect if running interactively
if [[ -t 0 ]] && [[ "$NON_INTERACTIVE" == "false" ]]; then
    INTERACTIVE=true
else
    INTERACTIVE=false
fi

echo -e "${BOLD}"
cat << 'EOF'
        _
   __ _| |__   ___  _   _ _ __   ___ ___ _ __
  / _` | '_ \ / _ \| | | | '_ \ / __/ _ \ '__|
 | (_| | |_) | (_) | |_| | | | | (_|  __/ |
  \__, |_.__/ \___/ \__,_|_| |_|\___\___|_|
     |_|
EOF
echo -e "${NC}"
echo "WireGuard NAT-PMP Port Bouncer for qBittorrent"
echo

# Step 1: Check/install dependencies
step "Checking dependencies"

check_command() {
    if command -v "$1" &>/dev/null; then
        info "$1 is installed"
        return 0
    else
        return 1
    fi
}

MISSING_DEPS=()

if ! check_command python3; then
    MISSING_DEPS+=("python3")
fi

if ! python3 -c "import venv" &>/dev/null; then
    MISSING_DEPS+=("python3-venv")
fi

if ! check_command natpmpc; then
    MISSING_DEPS+=("libnatpmp1")
fi

if ! check_command wg; then
    MISSING_DEPS+=("wireguard-tools")
fi

if ! check_command git; then
    MISSING_DEPS+=("git")
fi

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    warn "Missing dependencies: ${MISSING_DEPS[*]}"

    if command -v apt &>/dev/null; then
        info "Installing via apt..."
        apt update -qq
        apt install -y -qq "${MISSING_DEPS[@]}"
    elif command -v dnf &>/dev/null; then
        info "Installing via dnf..."
        dnf install -y -q "${MISSING_DEPS[@]}"
    elif command -v pacman &>/dev/null; then
        info "Installing via pacman..."
        pacman -S --noconfirm "${MISSING_DEPS[@]}"
    else
        error "Could not install dependencies. Please install manually: ${MISSING_DEPS[*]}"
    fi
fi

# Step 2: Gather configuration
step "Configuration"

if [[ "$INTERACTIVE" == "true" ]]; then
    echo "Please provide the following configuration values."
    echo "Press Enter to accept defaults shown in brackets."
    echo

    # Try to detect WireGuard interface
    DEFAULT_WG=$(ip -o link show | grep -oP 'wg\d+' | head -1 || echo "wg0")
    prompt WG_INTERFACE "WireGuard interface" "$DEFAULT_WG"

    # Try to detect gateway from interface
    DEFAULT_GW=$(ip route show dev "$WG_INTERFACE" 2>/dev/null | grep -oP 'via \K[\d.]+' | head -1 || echo "10.2.0.1")
    prompt WG_GATEWAY "NAT-PMP gateway IP" "$DEFAULT_GW"

    prompt QBT_HOST "qBittorrent host" "localhost"
    prompt QBT_PORT "qBittorrent Web UI port" "8080"
    prompt QBT_USERNAME "qBittorrent username" ""
    prompt_password QBT_PASSWORD "qBittorrent password"
else
    # Non-interactive: use defaults or error if required values missing
    WG_INTERFACE="${WG_INTERFACE:-wg0}"
    WG_GATEWAY="${WG_GATEWAY:-10.2.0.1}"
    QBT_HOST="${QBT_HOST:-localhost}"
    QBT_PORT="${QBT_PORT:-8080}"

    info "Using WireGuard interface: $WG_INTERFACE"
    info "Using NAT-PMP gateway: $WG_GATEWAY"
    info "Using qBittorrent: $QBT_HOST:$QBT_PORT"
fi

# Step 3: Create service user (idempotent)
step "Setting up service user"

if id "$SERVICE_USER" &>/dev/null; then
    info "Service user '$SERVICE_USER' already exists"
else
    useradd -r -s /usr/sbin/nologin "$SERVICE_USER"
    info "Created service user '$SERVICE_USER'"
fi

# Step 4: Install qbouncer (idempotent)
step "Installing qbouncer"

mkdir -p "$INSTALL_DIR"

# Create or update virtual environment
if [[ -d "$INSTALL_DIR/venv" ]]; then
    info "Virtual environment exists, upgrading..."
else
    info "Creating virtual environment..."
    python3 -m venv "$INSTALL_DIR/venv"
fi

# Upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q

# Clone or update repository
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

info "Downloading qbouncer..."
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TEMP_DIR/qbouncer" 2>/dev/null

# Install package
info "Installing package..."
"$INSTALL_DIR/venv/bin/pip" install "$TEMP_DIR/qbouncer" -q

# Verify installation
VERSION=$("$INSTALL_DIR/venv/bin/qbouncer" --version 2>/dev/null || echo "unknown")
info "Installed qbouncer $VERSION"

# Step 5: Configure (idempotent - preserve existing config)
step "Configuring qbouncer"

mkdir -p "$CONFIG_DIR"

CONFIG_FILE="$CONFIG_DIR/qbouncer.toml"

if [[ -f "$CONFIG_FILE" ]]; then
    if [[ "$INTERACTIVE" == "true" ]]; then
        info "Configuration file exists"
        read -rp "$(echo -e "${YELLOW}Overwrite existing config? [y/N]:${NC} ")" overwrite
        if [[ "$overwrite" =~ ^[Yy]$ ]]; then
            WRITE_CONFIG=true
        else
            WRITE_CONFIG=false
            info "Preserving existing configuration"
        fi
    elif [[ "$FORCE_CONFIG" == "true" ]]; then
        info "Overwriting configuration (--force-config)"
        WRITE_CONFIG=true
    else
        info "Configuration file exists, preserving (use --force-config to overwrite)"
        WRITE_CONFIG=false
    fi
else
    WRITE_CONFIG=true
fi

if [[ "$WRITE_CONFIG" == "true" ]]; then
    cat > "$CONFIG_FILE" << EOF
# qbouncer configuration
# Generated by install.sh on $(date -Iseconds)

[wireguard]
interface = "$WG_INTERFACE"
health_check_host = "$WG_GATEWAY"
health_check_interval = 30

[natpmp]
gateway = "$WG_GATEWAY"
refresh_interval = 60
lease_lifetime = 120

[qbittorrent]
host = "$QBT_HOST"
port = $QBT_PORT
EOF

    if [[ -n "$QBT_USERNAME" ]]; then
        echo "username = \"$QBT_USERNAME\"" >> "$CONFIG_FILE"
    fi
    if [[ -n "$QBT_PASSWORD" ]]; then
        echo "password = \"$QBT_PASSWORD\"" >> "$CONFIG_FILE"
    fi

    cat >> "$CONFIG_FILE" << EOF
interface_binding = "$WG_INTERFACE"

[service]
log_level = "INFO"
state_file = "/var/lib/qbouncer/state.json"
max_consecutive_failures = 5
failure_backoff_base = 5
failure_backoff_max = 300
EOF

    info "Configuration written to $CONFIG_FILE"
fi

# Always fix ownership and permissions (idempotent)
chown root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 640 "$CONFIG_FILE"

# Step 6: Install systemd service (idempotent)
step "Installing systemd service"

cat > /etc/systemd/system/qbouncer.service << 'EOF'
[Unit]
Description=qbouncer - WireGuard NAT-PMP Port Bouncer for qBittorrent
Documentation=https://github.com/vegardx/qbouncer
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/opt/qbouncer/venv/bin/qbouncer --config /etc/qbouncer/qbouncer.toml
Restart=on-failure
RestartSec=10
WatchdogSec=90

User=qbouncer
Group=qbouncer

RuntimeDirectory=qbouncer
StateDirectory=qbouncer
ConfigurationDirectory=qbouncer

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
LockPersonality=yes

ReadOnlyPaths=/etc/wireguard
ReadWritePaths=/var/lib/qbouncer

CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
info "systemd service installed"

# Step 7: Enable and start service
step "Starting service"

if systemctl is-active --quiet qbouncer; then
    info "Service is already running, restarting..."
    systemctl restart qbouncer
else
    systemctl enable qbouncer
    systemctl start qbouncer
fi

# Wait a moment and check status
sleep 2

if systemctl is-active --quiet qbouncer; then
    info "Service is running"
else
    warn "Service may have failed to start. Check: journalctl -u qbouncer -e"
fi

# Done
step "Installation complete"

echo
echo -e "${GREEN}${BOLD}qbouncer has been installed successfully!${NC}"
echo
echo "Useful commands:"
echo "  systemctl status qbouncer     - Check service status"
echo "  journalctl -u qbouncer -f     - Follow logs"
echo "  nano $CONFIG_FILE  - Edit configuration"
echo
echo "Configuration: $CONFIG_FILE"
echo "Logs: journalctl -u qbouncer"
echo

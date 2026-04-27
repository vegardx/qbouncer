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
# For credentials, prefer env vars so they don't show up in `ps`:
#   QBT_PASSWORD="secret" bash install.sh --non-interactive ...
#

set -euo pipefail
umask 077

# Defaults
INSTALL_DIR="/opt/qbouncer"
CONFIG_DIR="/etc/qbouncer"
SERVICE_USER="qbouncer"
REPO_URL="https://github.com/vegardx/qbouncer.git"
RELEASE=""

# Configuration defaults
WG_INTERFACE=""
WG_GATEWAY=""
QBT_HOST="localhost"
QBT_PORT=""
QBT_USERNAME=""
QBT_PASSWORD="${QBT_PASSWORD:-}"
KILLSWITCH_ENABLED=false
KILLSWITCH_USER=""

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

    if [[ -n "$current_value" ]]; then
        echo -e "${BOLD}$prompt_text${NC} [$current_value] (from CLI/env)"
        return
    fi

    local value
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${BOLD}$prompt_text${NC} [$default]: ")" value
    else
        read -rp "$(echo -e "${BOLD}$prompt_text${NC}: ")" value
    fi
    printf -v "$var_name" '%s' "${value:-$default}"
}

prompt_password() {
    local var_name="$1"
    local prompt_text="$2"
    local current_value="${!var_name:-}"

    if [[ -n "$current_value" ]]; then
        echo -e "${BOLD}$prompt_text${NC}: *** (from env)"
        return
    fi

    local value
    read -rsp "$(echo -e "${BOLD}$prompt_text${NC} (leave empty if none): ")" value
    echo
    printf -v "$var_name" '%s' "$value"
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
    --killswitch            Enable iptables killswitch
    --killswitch-user USER  User running qBittorrent (for killswitch)
    --release TAG           Install a specific release tag (vX.Y.Z) or branch name
    --non-interactive       Skip prompts, use defaults/CLI args only
    --force-config          Overwrite existing configuration file
    -h, --help              Show this help message

Credentials:
    Set QBT_PASSWORD in the environment to provide a password without an
    interactive prompt. Passing passwords on the command line is not supported
    because it would leak them to \`ps\`.

Examples:
    # Interactive install
    $0

    # Non-interactive with all options
    QBT_PASSWORD="..." $0 --non-interactive --wg-interface wg0 \\
        --gateway 10.2.0.1 --qbt-port 8080

    # Pipe from curl (consider verifying checksum first, see README)
    curl -fsSL https://raw.githubusercontent.com/vegardx/qbouncer/main/scripts/install.sh | bash
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
        --killswitch) KILLSWITCH_ENABLED=true; shift ;;
        --killswitch-user) KILLSWITCH_USER="$2"; shift 2 ;;
        --release) RELEASE="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --force-config) FORCE_CONFIG=true; shift ;;
        -h|--help) usage ;;
        *) error "Unknown option: $1" ;;
    esac
done

# Validate --release format to prevent argument injection into `git clone`.
# Accept semver tags (with optional leading v), branch names, and full SHAs.
if [[ -n "$RELEASE" ]]; then
    if ! [[ "$RELEASE" =~ ^(v?[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*|[A-Za-z0-9][A-Za-z0-9._/-]*)$ ]]; then
        error "Invalid --release value: $RELEASE"
    fi
fi

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

if ! check_command iptables; then
    MISSING_DEPS+=("iptables")
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
    if [[ "$KILLSWITCH_ENABLED" == "true" ]]; then
        info "Killswitch: enabled (user: ${KILLSWITCH_USER:-qbittorrent})"
    else
        info "Killswitch: disabled"
    fi
fi

# Step 3: Create service user (idempotent)
step "Setting up service user"

if id "$SERVICE_USER" &>/dev/null; then
    info "Service user '$SERVICE_USER' already exists"
else
    useradd -r -U -s /usr/sbin/nologin "$SERVICE_USER"
    info "Created service user '$SERVICE_USER'"
fi

# Step 4: Install qbouncer (idempotent)
step "Installing qbouncer"

mkdir -p "$INSTALL_DIR"

if [[ -d "$INSTALL_DIR/venv" ]]; then
    info "Virtual environment exists, upgrading..."
else
    info "Creating virtual environment..."
    python3 -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q

# Clone repository into a temp dir protected by the process umask (077 above)
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT INT TERM HUP

GIT_REF="${RELEASE:-main}"
info "Downloading qbouncer ($GIT_REF)..."
git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$TEMP_DIR/qbouncer"

"$INSTALL_DIR/venv/bin/pip" uninstall -y qbouncer 2>/dev/null || true
info "Installing package..."
"$INSTALL_DIR/venv/bin/pip" install --no-cache-dir "$TEMP_DIR/qbouncer" -q

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

# Produce a TOML string-safe value: backslash-escape backslashes and
# double-quotes; reject control characters outright.
toml_escape() {
    python3 -c '
import sys
s = sys.argv[1]
if any(ord(c) < 0x20 for c in s):
    sys.exit("Control characters not permitted in config values")
sys.stdout.write(s.replace("\\\\", "\\\\\\\\").replace("\"", "\\\\\""))
' "$1"
}

if [[ "$WRITE_CONFIG" == "true" ]]; then
    # install(1) creates the file with restrictive perms from first open,
    # closing the umask race between create+chmod.
    install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$CONFIG_FILE"

    {
        echo "# qbouncer configuration"
        echo "# Generated by install.sh on $(date -Iseconds)"
        echo
        echo "[wireguard]"
        echo "interface = \"$(toml_escape "$WG_INTERFACE")\""
        echo "health_check_host = \"$(toml_escape "$WG_GATEWAY")\""
        echo "health_check_interval = 30"
        echo
        echo "[natpmp]"
        echo "gateway = \"$(toml_escape "$WG_GATEWAY")\""
        echo "refresh_interval = 60"
        echo "lease_lifetime = 120"
        echo
        echo "[qbittorrent]"
        echo "host = \"$(toml_escape "$QBT_HOST")\""
        echo "port = $QBT_PORT"
        if [[ -n "$QBT_USERNAME" ]]; then
            echo "username = \"$(toml_escape "$QBT_USERNAME")\""
        fi
        if [[ -n "$QBT_PASSWORD" ]]; then
            echo "password = \"$(toml_escape "$QBT_PASSWORD")\""
        fi
        echo "interface_binding = \"$(toml_escape "$WG_INTERFACE")\""
        echo
        echo "[service]"
        echo "log_level = \"INFO\""
        echo "state_file = \"/var/lib/qbouncer/state.json\""
        echo
        echo "[killswitch]"
        echo "enabled = $KILLSWITCH_ENABLED"
        if [[ -n "$KILLSWITCH_USER" ]]; then
            echo "user = \"$(toml_escape "$KILLSWITCH_USER")\""
        fi
    } >> "$CONFIG_FILE"

    info "Configuration written to $CONFIG_FILE"
fi

# Idempotent perms (also applied when config is preserved)
chown root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 640 "$CONFIG_FILE"

# Release password reference from the shell
QBT_PASSWORD=""
unset QBT_PASSWORD

# Step 6: Install systemd service (idempotent)
step "Installing systemd service"

install -m 0644 "$TEMP_DIR/qbouncer/systemd/qbouncer.service" \
    /etc/systemd/system/qbouncer.service
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

# Poll service status for up to 30 seconds (the service spends the first
# seconds in WAITING_VPN before calling sd_notify READY=1)
POLL_DEADLINE=$((SECONDS + 30))
while (( SECONDS < POLL_DEADLINE )); do
    if systemctl is-active --quiet qbouncer; then
        break
    fi
    sleep 1
done

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
echo "  \$EDITOR $CONFIG_FILE  - Edit configuration"
echo
echo "Configuration: $CONFIG_FILE"
echo "Logs: journalctl -u qbouncer"
echo
if [[ "$KILLSWITCH_ENABLED" != "true" ]]; then
    echo -e "${YELLOW}Optional: Killswitch${NC}"
    echo "To prevent traffic leaks outside VPN, add to config:"
    echo "  [killswitch]"
    echo "  enabled = true"
    echo "  user = \"<user running qBittorrent>\""
    echo
fi

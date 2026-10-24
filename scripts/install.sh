#!/bin/bash
#
# qbouncer installer / uninstaller.
#
# Install:
#   curl -fsSLo install.sh https://raw.githubusercontent.com/vegardx/qbouncer/main/scripts/install.sh
#   sudo bash install.sh
#
# Uninstall:
#   sudo bash install.sh --uninstall
#
# Non-interactive install with credentials:
#   QBT_PASSWORD="..." sudo -E bash install.sh --non-interactive \
#     --wg-interface wg0 --gateway 10.2.0.1 --qbt-port 8080
#

set -euo pipefail
umask 077

# Paths (both install and uninstall rely on these)
INSTALL_DIR="/opt/qbouncer"
CONFIG_DIR="/etc/qbouncer"
STATE_DIR="/var/lib/qbouncer"
SERVICE_USER="qbouncer"
SERVICE_FILE="/etc/systemd/system/qbouncer.service"
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

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }
step()  { echo -e "\n${BLUE}${BOLD}==> $1${NC}"; }

prompt() {
    local var_name="$1" prompt_text="$2" default="$3"
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
    local var_name="$1" prompt_text="$2"
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

confirm() {
    local prompt_text="$1"
    local reply
    read -rp "$(echo -e "${YELLOW}$prompt_text${NC} [y/N]: ")" reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Install (default):
    --wg-interface NAME     WireGuard interface name (e.g., wg0)
    --gateway IP            NAT-PMP gateway IP address
    --qbt-host HOST         qBittorrent host (default: localhost)
    --qbt-port PORT         qBittorrent Web UI port
    --qbt-username USER     qBittorrent username
    --killswitch            Enable iptables killswitch
    --killswitch-user USER  User running qBittorrent (for killswitch)
    --release TAG           Install a specific release tag or branch name
                            (default: the newest release tag)
    --non-interactive       Skip prompts, use defaults/CLI args only
    --force-config          Overwrite existing configuration file

Uninstall:
    --uninstall             Remove the service, binaries, and optionally
                            the configuration/state/user
    --purge                 With --uninstall: remove config + state + user
                            without prompting (equivalent to answering yes
                            to everything)
    --yes / -y              Auto-confirm all prompts in --uninstall flow

Common:
    -h, --help              Show this help message

Credentials:
    Pass QBT_PASSWORD via the environment (not the command line) so it
    doesn't appear in 'ps'. Example:
        QBT_PASSWORD="..." sudo -E bash $0 --non-interactive ...

    Or, for credential-manager integration, leave QBT_PASSWORD unset and
    write the password to /etc/qbouncer/credentials/qbt_password (mode
    0600) after install. See systemd/qbouncer.service for the commented-
    out LoadCredential= lines to enable it.
EOF
    exit 0
}

# -- argument parsing --------------------------------------------------------

MODE="install"
NON_INTERACTIVE=false
FORCE_CONFIG=false
PURGE=false
AUTO_YES=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --wg-interface)     WG_INTERFACE="$2"; shift 2 ;;
        --gateway)          WG_GATEWAY="$2"; shift 2 ;;
        --qbt-host)         QBT_HOST="$2"; shift 2 ;;
        --qbt-port)         QBT_PORT="$2"; shift 2 ;;
        --qbt-username)     QBT_USERNAME="$2"; shift 2 ;;
        --killswitch)       KILLSWITCH_ENABLED=true; shift ;;
        --killswitch-user)  KILLSWITCH_USER="$2"; shift 2 ;;
        --release)          RELEASE="$2"; shift 2 ;;
        --non-interactive)  NON_INTERACTIVE=true; shift ;;
        --force-config)     FORCE_CONFIG=true; shift ;;
        --uninstall)        MODE="uninstall"; shift ;;
        --purge)            PURGE=true; AUTO_YES=true; shift ;;
        -y|--yes)           AUTO_YES=true; shift ;;
        -h|--help)          usage ;;
        *) error "Unknown option: $1" ;;
    esac
done

if [[ -n "$RELEASE" ]]; then
    if ! [[ "$RELEASE" =~ ^(v?[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*|[A-Za-z0-9][A-Za-z0-9._/-]*)$ ]]; then
        error "Invalid --release value: $RELEASE"
    fi
fi

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root"
fi

# -- uninstall mode ----------------------------------------------------------

uninstall() {
    echo -e "${BOLD}qbouncer uninstall${NC}"
    echo

    step "Stopping service"
    if systemctl is-active --quiet qbouncer 2>/dev/null; then
        systemctl stop qbouncer
        info "Service stopped"
    fi
    if systemctl is-enabled --quiet qbouncer 2>/dev/null; then
        systemctl disable qbouncer
        info "Service disabled"
    fi

    # The killswitch outlives the service by design; remove it explicitly
    # while the binary and config still exist.
    step "Removing killswitch rules"
    if [[ -x "$INSTALL_DIR/venv/bin/qbouncer" && -f "$CONFIG_DIR/qbouncer.toml" ]]; then
        if "$INSTALL_DIR/venv/bin/qbouncer" --config "$CONFIG_DIR/qbouncer.toml" --remove-killswitch; then
            info "Killswitch rules removed"
        else
            warn "Could not remove killswitch rules (chain QBOUNCER-KS); remove them by hand"
        fi
    fi

    step "Removing systemd unit"
    if [[ -f "$SERVICE_FILE" ]]; then
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
        info "Removed $SERVICE_FILE"
    fi

    step "Removing installation directory"
    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        info "Removed $INSTALL_DIR"
    fi

    step "Optional artifacts"
    local remove_config=false remove_state=false remove_user=false
    if $PURGE || $AUTO_YES; then
        remove_config=true
        remove_state=true
        remove_user=true
    else
        [[ -d "$CONFIG_DIR" ]] && confirm "Remove configuration directory $CONFIG_DIR?" \
            && remove_config=true
        [[ -d "$STATE_DIR"  ]] && confirm "Remove state directory $STATE_DIR?" \
            && remove_state=true
        id "$SERVICE_USER" &>/dev/null && confirm "Remove service user $SERVICE_USER?" \
            && remove_user=true
    fi

    if $remove_config && [[ -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
        info "Removed $CONFIG_DIR"
    fi
    if $remove_state && [[ -d "$STATE_DIR" ]]; then
        rm -rf "$STATE_DIR"
        info "Removed $STATE_DIR"
    fi
    if $remove_user && id "$SERVICE_USER" &>/dev/null; then
        # -r removes home dir if any; -f forces even if the user is the
        # owner of still-existing files (safe for a daemon account).
        userdel -r -f "$SERVICE_USER" 2>/dev/null || \
            warn "userdel $SERVICE_USER failed (user may own files elsewhere)"
        if getent group "$SERVICE_USER" &>/dev/null; then
            groupdel "$SERVICE_USER" 2>/dev/null || \
                warn "Group $SERVICE_USER could not be removed (other members?)"
        fi
        info "Removed service user $SERVICE_USER"
    fi

    echo
    info "Uninstallation complete"
    exit 0
}

if [[ "$MODE" == "uninstall" ]]; then
    uninstall
fi

# -- install mode ------------------------------------------------------------

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
    fi
    return 1
}

MISSING_DEPS=()
check_command python3 || MISSING_DEPS+=("python3")
python3 -c "import venv" &>/dev/null || MISSING_DEPS+=("python3-venv")
check_command natpmpc || MISSING_DEPS+=("libnatpmp1")
check_command wg || MISSING_DEPS+=("wireguard-tools")
check_command iptables || MISSING_DEPS+=("iptables")
# The killswitch reads the tunnel fwmark/endpoints with `wg show`.
check_command wg || MISSING_DEPS+=("wireguard-tools")
check_command git || MISSING_DEPS+=("git")

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

    DEFAULT_WG=$(ip -o link show | grep -oP 'wg\d+' | head -1 || echo "wg0")
    prompt WG_INTERFACE "WireGuard interface" "$DEFAULT_WG"

    DEFAULT_GW=$(ip route show dev "$WG_INTERFACE" 2>/dev/null | grep -oP 'via \K[\d.]+' | head -1 || echo "10.2.0.1")
    prompt WG_GATEWAY "NAT-PMP gateway IP" "$DEFAULT_GW"

    prompt QBT_HOST "qBittorrent host" "localhost"
    prompt QBT_PORT "qBittorrent Web UI port" "8080"
    prompt QBT_USERNAME "qBittorrent username" ""
    prompt_password QBT_PASSWORD "qBittorrent password"
else
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

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT INT TERM HUP

# Default to the newest release tag, not main: main can be ahead of the
# version it reports, so later upgrades of the same version get skipped.
GIT_REF="$RELEASE"
if [[ -z "$GIT_REF" ]]; then
    GIT_REF=$(git ls-remote --tags --refs "$REPO_URL" 'v*' | sed 's#.*refs/tags/##' | sort -V | tail -n 1)
    [[ -n "$GIT_REF" ]] || { warn "No release tags found, installing main"; GIT_REF=main; }
fi
info "Downloading qbouncer ($GIT_REF)..."
git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$TEMP_DIR/qbouncer"

"$INSTALL_DIR/venv/bin/pip" uninstall -y qbouncer 2>/dev/null || true
info "Installing package..."
"$INSTALL_DIR/venv/bin/pip" install --no-cache-dir "$TEMP_DIR/qbouncer" -q

VERSION=$("$INSTALL_DIR/venv/bin/qbouncer" --version 2>/dev/null || echo "unknown")
info "Installed qbouncer $VERSION"

# Step 5: Configure
step "Configuring qbouncer"

mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/qbouncer.toml"

if [[ -f "$CONFIG_FILE" ]]; then
    if [[ "$INTERACTIVE" == "true" ]]; then
        info "Configuration file exists"
        if confirm "Overwrite existing config?"; then
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
    # Create the target with restrictive perms from the first open so the
    # password never sits in a world-readable file for a moment.
    install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$CONFIG_FILE"

    # Render the committed example file, substituting user values where
    # they differ from the defaults. Handled by an inline Python script
    # so TOML escaping and conditional fields stay correct.
    EXAMPLE_FILE="$TEMP_DIR/qbouncer/config/qbouncer.toml.example"
    WG_INTERFACE="$WG_INTERFACE" \
    WG_GATEWAY="$WG_GATEWAY" \
    QBT_HOST="$QBT_HOST" \
    QBT_PORT="$QBT_PORT" \
    QBT_USERNAME="$QBT_USERNAME" \
    QBT_PASSWORD="$QBT_PASSWORD" \
    KILLSWITCH_ENABLED="$KILLSWITCH_ENABLED" \
    KILLSWITCH_USER="$KILLSWITCH_USER" \
    python3 - "$EXAMPLE_FILE" "$CONFIG_FILE" <<'PYEOF'
"""Render the shipped qbouncer.toml.example with user-supplied values."""
import os
import sys


def toml_str(value: str) -> str:
    """TOML basic-string escaping; rejects control chars to avoid format breakage."""
    if any(ord(c) < 0x20 for c in value):
        sys.exit("Config values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uncomment_assignment(text: str, key: str, value: str) -> str:
    """Replace a commented '# key = ...' line with an uncommented one."""
    import re

    pattern = re.compile(rf'^#\s*{re.escape(key)}\s*=.*$', re.MULTILINE)
    return pattern.sub(f"{key} = {value}", text, count=1)


def set_assignment(text: str, key: str, value: str) -> str:
    """Replace the first 'key = ...' line on its own line."""
    import re

    pattern = re.compile(rf'^{re.escape(key)}\s*=.*$', re.MULTILINE)
    new_text, n = pattern.subn(f"{key} = {value}", text, count=1)
    if n == 0:
        sys.exit(f"Failed to find '{key} =' in template")
    return new_text


src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    text = f.read()

wg = os.environ["WG_INTERFACE"]
gw = os.environ["WG_GATEWAY"]

# [wireguard]: interface, health_check_host
text = set_assignment(text, "interface", toml_str(wg))
text = set_assignment(text, "health_check_host", toml_str(gw))

# [natpmp]: gateway
text = set_assignment(text, "gateway", toml_str(gw))

# [qbittorrent]: host, port
text = set_assignment(text, "host", toml_str(os.environ["QBT_HOST"]))
text = set_assignment(text, "port", os.environ["QBT_PORT"])

# [qbittorrent]: interface_binding (matches "wg0" default)
text = set_assignment(text, "interface_binding", toml_str(wg))

# Optional credentials: uncomment the example's commented lines
if os.environ.get("QBT_USERNAME"):
    text = uncomment_assignment(text, "username", toml_str(os.environ["QBT_USERNAME"]))
if os.environ.get("QBT_PASSWORD"):
    text = uncomment_assignment(text, "password", toml_str(os.environ["QBT_PASSWORD"]))

# [killswitch]: enabled, user
text = set_assignment(text, "enabled", os.environ["KILLSWITCH_ENABLED"])
if os.environ.get("KILLSWITCH_USER"):
    text = set_assignment(text, "user", toml_str(os.environ["KILLSWITCH_USER"]))

with open(dst, "w") as f:
    f.write(text)
PYEOF

    info "Configuration written to $CONFIG_FILE"
fi

chown root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 640 "$CONFIG_FILE"

# Release password reference from the shell
QBT_PASSWORD=""
unset QBT_PASSWORD

# Step 6: Install systemd service (idempotent)
step "Installing systemd service"

install -m 0644 "$TEMP_DIR/qbouncer/systemd/qbouncer.service" "$SERVICE_FILE"
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

step "Installation complete"

echo
echo -e "${GREEN}${BOLD}qbouncer has been installed successfully!${NC}"
echo
echo "Useful commands:"
echo "  systemctl status qbouncer       - Check service status"
echo "  journalctl -u qbouncer -f       - Follow logs"
echo "  \$EDITOR $CONFIG_FILE  - Edit configuration"
echo "  $0 --uninstall                  - Remove qbouncer"
echo
if [[ "$KILLSWITCH_ENABLED" != "true" ]]; then
    echo -e "${YELLOW}Optional: Killswitch${NC}"
    echo "To prevent traffic leaks outside VPN, add to config:"
    echo "  [killswitch]"
    echo "  enabled = true"
    echo "  user = \"<user running qBittorrent>\""
    echo
fi

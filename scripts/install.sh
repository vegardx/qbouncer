#!/bin/bash
#
# qbouncer installation script
# Installs qbouncer into a virtual environment at /opt/qbouncer
#

set -euo pipefail

# Configuration
INSTALL_DIR="/opt/qbouncer"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="/etc/qbouncer"
SERVICE_USER="qbouncer"
SERVICE_GROUP="qbouncer"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
fi

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

info "Installing qbouncer from ${PROJECT_DIR}"

# Check for required system dependencies
info "Checking system dependencies..."

if ! command -v python3 &> /dev/null; then
    error "python3 is not installed. Install it with: apt install python3"
fi

if ! command -v natpmpc &> /dev/null; then
    warn "natpmpc is not installed. Install it with: apt install libnatpmp1"
fi

if ! command -v wg &> /dev/null; then
    warn "wireguard-tools is not installed. Install it with: apt install wireguard-tools"
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Found Python ${PYTHON_VERSION}"

# Create service user if it doesn't exist
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating service user: ${SERVICE_USER}"
    useradd -r -s /usr/sbin/nologin "$SERVICE_USER"
else
    info "Service user ${SERVICE_USER} already exists"
fi

# Create installation directory
info "Creating installation directory: ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"

# Create virtual environment
info "Creating virtual environment: ${VENV_DIR}"
python3 -m venv "$VENV_DIR"

# Upgrade pip in the virtual environment
info "Upgrading pip..."
"${VENV_DIR}/bin/pip" install --upgrade pip

# Install qbouncer
info "Installing qbouncer..."
"${VENV_DIR}/bin/pip" install "$PROJECT_DIR"

# Verify installation
if "${VENV_DIR}/bin/qbouncer" --version &> /dev/null; then
    VERSION=$("${VENV_DIR}/bin/qbouncer" --version)
    info "Successfully installed: ${VERSION}"
else
    error "Installation verification failed"
fi

# Create configuration directory
info "Creating configuration directory: ${CONFIG_DIR}"
mkdir -p "$CONFIG_DIR"

# Copy example configuration if no config exists
if [[ ! -f "${CONFIG_DIR}/qbouncer.toml" ]]; then
    if [[ -f "${PROJECT_DIR}/config/qbouncer.toml.example" ]]; then
        info "Copying example configuration..."
        cp "${PROJECT_DIR}/config/qbouncer.toml.example" "${CONFIG_DIR}/qbouncer.toml"
        chmod 600 "${CONFIG_DIR}/qbouncer.toml"
        warn "Please edit ${CONFIG_DIR}/qbouncer.toml with your settings"
    fi
else
    info "Configuration file already exists, skipping"
fi

# Install systemd service
info "Installing systemd service..."
cp "${PROJECT_DIR}/systemd/qbouncer.service" /etc/systemd/system/
systemctl daemon-reload

# Set ownership
info "Setting permissions..."
chown -R root:root "$INSTALL_DIR"
chown -R root:"$SERVICE_GROUP" "$CONFIG_DIR"

info ""
info "Installation complete!"
info ""
info "Next steps:"
info "  1. Edit the configuration: sudo nano ${CONFIG_DIR}/qbouncer.toml"
info "  2. Enable the service:    sudo systemctl enable qbouncer"
info "  3. Start the service:     sudo systemctl start qbouncer"
info "  4. Check status:          sudo systemctl status qbouncer"
info "  5. View logs:             sudo journalctl -u qbouncer -f"

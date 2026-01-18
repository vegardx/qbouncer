#!/bin/bash
#
# qbouncer uninstallation script
# Removes qbouncer installation from /opt/qbouncer
#

set -euo pipefail

# Configuration
INSTALL_DIR="/opt/qbouncer"
CONFIG_DIR="/etc/qbouncer"
SERVICE_USER="qbouncer"

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

info "Uninstalling qbouncer..."

# Stop and disable service
if systemctl is-active --quiet qbouncer 2>/dev/null; then
    info "Stopping qbouncer service..."
    systemctl stop qbouncer
fi

if systemctl is-enabled --quiet qbouncer 2>/dev/null; then
    info "Disabling qbouncer service..."
    systemctl disable qbouncer
fi

# Remove systemd service file
if [[ -f /etc/systemd/system/qbouncer.service ]]; then
    info "Removing systemd service file..."
    rm -f /etc/systemd/system/qbouncer.service
    systemctl daemon-reload
fi

# Remove installation directory
if [[ -d "$INSTALL_DIR" ]]; then
    info "Removing installation directory: ${INSTALL_DIR}"
    rm -rf "$INSTALL_DIR"
fi

# Ask about configuration
if [[ -d "$CONFIG_DIR" ]]; then
    read -p "Remove configuration directory ${CONFIG_DIR}? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Removing configuration directory..."
        rm -rf "$CONFIG_DIR"
    else
        warn "Configuration directory preserved at ${CONFIG_DIR}"
    fi
fi

# Ask about state directory
STATE_DIR="/var/lib/qbouncer"
if [[ -d "$STATE_DIR" ]]; then
    read -p "Remove state directory ${STATE_DIR}? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Removing state directory..."
        rm -rf "$STATE_DIR"
    else
        warn "State directory preserved at ${STATE_DIR}"
    fi
fi

# Ask about service user
if id "$SERVICE_USER" &>/dev/null; then
    read -p "Remove service user ${SERVICE_USER}? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Removing service user..."
        userdel "$SERVICE_USER"
    else
        warn "Service user ${SERVICE_USER} preserved"
    fi
fi

info ""
info "Uninstallation complete!"

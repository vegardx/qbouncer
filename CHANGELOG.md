# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2025-01-18

### Fixed

- Add git to installer dependency checks (was missing, causing silent failure)

## [1.0.1] - 2025-01-18

### Changed

- Rewrite install.sh as interactive, pipe-able installer with auto-detection
- Installer is now idempotent (safe to re-run for upgrades)
- Remove sudo from documentation (assume root context)

### Added

- Non-interactive install mode via `--non-interactive` flag
- Auto-detection of WireGuard interface and gateway
- .gitignore for Python cache and build artifacts

## [1.0.0] - 2025-01-18

### Added

- Initial release
- WireGuard interface monitoring (UP state, IP assignment, connectivity via ping)
- NAT-PMP port mapping via `natpmpc` with configurable refresh interval
- qBittorrent Web API integration for port and interface configuration
- Optional HTTPS support for qBittorrent Web API with configurable SSL verification
- State machine with states: INITIALIZING, WAITING_VPN, WAITING_QBT, MAPPING_PORT, CONFIGURING, MONITORING, RECOVERING, SHUTTING_DOWN
- systemd service unit with Type=notify and watchdog support
- Security hardening in systemd unit (NoNewPrivileges, ProtectSystem, etc.)
- TOML configuration file support with comprehensive validation
- Environment variable overrides for all configuration options
- State persistence across restarts with secure file permissions
- Exponential backoff with jitter for failure recovery
- Graceful shutdown on SIGTERM/SIGINT
- Installation and uninstallation scripts

### Security

- Configuration file created with 600 permissions (owner read/write only)
- State file created with 600 permissions
- State directory created with 700 permissions
- Credentials masked in log output and repr

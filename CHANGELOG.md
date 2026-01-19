# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.4] - 2025-01-19

### Fixed

- Sync `__version__` in `__init__.py` with pyproject.toml
- Uninstall package before reinstall to ensure clean upgrade

## [1.1.3] - 2025-01-19

### Fixed

- Force reinstall package during upgrade to avoid stale cached versions

## [1.1.2] - 2025-01-19

### Fixed

- Use correct qBittorrent API key for network interface (`current_network_interface` instead of `current_interface_name`)

## [1.1.1] - 2025-01-19

### Fixed

- Add iptables to installer dependency checks
- Show killswitch status in installer configuration output
- Hide killswitch hint at end of install when already enabled

## [1.1.0] - 2025-01-19

### Added

- Killswitch feature using iptables to prevent qBittorrent traffic leaks outside VPN
- New `[killswitch]` configuration section with `enabled` and `user` options
- Killswitch uses custom iptables chain `QBOUNCER-KS` for clean rule management
- Automatic cleanup of iptables rules on service shutdown
- Periodic verification of killswitch rules during monitoring
- Installer options: `--killswitch`, `--killswitch-user`, `--release`

### Changed

- systemd service now includes CAP_NET_ADMIN capability for iptables management

## [1.0.6] - 2025-01-18

### Added

- Detect qBittorrent port drift when NAT-PMP port is unchanged
- Automatically reconfigure qBittorrent if its port doesn't match expected value

## [1.0.5] - 2025-01-18

### Added

- Add `--force-config` flag to overwrite existing configuration file

## [1.0.4] - 2025-01-18

### Fixed

- Make config permission fix idempotent (always applies, not just on new installs)

## [1.0.3] - 2025-01-18

### Fixed

- Fix config file permissions so qbouncer service user can read it (640 instead of 600)

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

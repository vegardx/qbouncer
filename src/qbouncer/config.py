"""Configuration management for qbouncer."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .exceptions import ConfigError

# Linux IFNAMSIZ is 16 bytes including NUL → 15 usable chars.
INTERFACE_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,14}$")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _parse_bool(value: Any) -> bool:
    """Parse a boolean value from various formats."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


@dataclass
class Config:
    """Configuration for qbouncer service."""

    # WireGuard settings
    wg_interface: str = "wg0"
    wg_health_check_host: str = "10.2.0.1"
    wg_health_check_interval: int = 30

    # NAT-PMP settings
    natpmp_gateway: str = "10.2.0.1"
    natpmp_refresh_interval: int = 60
    natpmp_lease_lifetime: int = 120

    # qBittorrent settings
    qbt_host: str = "localhost"
    qbt_port: int = 8080
    qbt_use_https: bool = False
    qbt_verify_ssl: bool = True
    qbt_ca_bundle: str = ""
    qbt_username: str = ""
    qbt_password: str = ""
    qbt_interface_binding: str = "wg0"

    # Service settings
    log_level: str = "INFO"
    state_file: str = "/var/lib/qbouncer/state.json"

    # Failure handling — systemd restarts the service on persistent failure,
    # so keep recovery policy simple: count N failures, then back off briefly.
    max_consecutive_failures: int = 5

    # Killswitch settings
    killswitch_enabled: bool = False
    killswitch_user: str = "qbittorrent"

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        self._validate()

    def _validate_iface(self, field: str, value: str) -> None:
        if not INTERFACE_PATTERN.match(value):
            raise ConfigError(
                f"Invalid {field}: {value!r}. Must start with a letter, contain only "
                "[a-zA-Z0-9_-], and be at most 15 characters."
            )

    def _validate_ipv4(self, field: str, value: str) -> None:
        try:
            IPv4Address(value)
        except (AddressValueError, ValueError) as e:
            raise ConfigError(f"Invalid {field}: {value!r}") from e

    def _validate(self) -> None:
        """Validate configuration values (pure, no I/O)."""
        self._validate_iface("wg_interface", self.wg_interface)
        self._validate_iface("qbt_interface_binding", self.qbt_interface_binding)
        self._validate_ipv4("wg_health_check_host", self.wg_health_check_host)
        self._validate_ipv4("natpmp_gateway", self.natpmp_gateway)

        if not 1 <= self.qbt_port <= 65535:
            raise ConfigError(f"Invalid qBittorrent port: {self.qbt_port}. Must be 1-65535.")

        if self.wg_health_check_interval < 1:
            raise ConfigError("Health check interval must be at least 1 second")
        if self.natpmp_refresh_interval < 1:
            raise ConfigError("NAT-PMP refresh interval must be at least 1 second")
        if self.natpmp_lease_lifetime < 1:
            raise ConfigError("NAT-PMP lease lifetime must be at least 1 second")
        if self.natpmp_refresh_interval >= self.natpmp_lease_lifetime:
            raise ConfigError(
                f"NAT-PMP refresh interval ({self.natpmp_refresh_interval}s) must be less than "
                f"lease lifetime ({self.natpmp_lease_lifetime}s)"
            )

        if self.max_consecutive_failures < 1:
            raise ConfigError("max_consecutive_failures must be at least 1")

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigError(
                f"Invalid log level: {self.log_level!r}. Must be one of {valid_levels}"
            )

        # TLS posture: insist on verification (or a pinned CA bundle) for
        # non-loopback HTTPS. Disabling verify for a remote host is a
        # credential-interception hole the daemon will not silently accept.
        if (
            self.qbt_use_https
            and not self.qbt_verify_ssl
            and not self.qbt_ca_bundle
            and self.qbt_host not in _LOCAL_HOSTS
        ):
            raise ConfigError(
                "qbt_verify_ssl=false is not allowed for remote hosts without qbt_ca_bundle. "
                "Either pin a CA bundle or set qbt_verify_ssl=true."
            )

    def __repr__(self) -> str:
        """Return representation with masked credentials."""
        return (
            f"Config("
            f"wg_interface={self.wg_interface!r}, "
            f"natpmp_gateway={self.natpmp_gateway!r}, "
            f"qbt_host={self.qbt_host!r}, "
            f"qbt_port={self.qbt_port}, "
            f"qbt_use_https={self.qbt_use_https}, "
            f"qbt_username={self.qbt_username!r}, "
            f"qbt_password='***')"
        )

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Config:
        """Load configuration from file and environment variables.

        Priority (highest to lowest):
        1. Environment variables (QBOUNCER_*)
        2. Configuration file
        3. Default values

        Args:
            config_path: Optional path to TOML configuration file

        Returns:
            Configured Config instance

        Raises:
            ConfigError: If configuration is invalid
        """
        config_data: dict[str, Any] = {}

        if config_path:
            path = Path(config_path)
            if path.exists():
                config_data = cls._load_from_file(path)
            else:
                raise ConfigError(f"Configuration file not found: {path}")

        field_mappings: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
            # WireGuard
            "wg_interface": ("wireguard", "interface", str),
            "wg_health_check_host": ("wireguard", "health_check_host", str),
            "wg_health_check_interval": ("wireguard", "health_check_interval", int),
            # NAT-PMP
            "natpmp_gateway": ("natpmp", "gateway", str),
            "natpmp_refresh_interval": ("natpmp", "refresh_interval", int),
            "natpmp_lease_lifetime": ("natpmp", "lease_lifetime", int),
            # qBittorrent
            "qbt_host": ("qbittorrent", "host", str),
            "qbt_port": ("qbittorrent", "port", int),
            "qbt_use_https": ("qbittorrent", "use_https", _parse_bool),
            "qbt_verify_ssl": ("qbittorrent", "verify_ssl", _parse_bool),
            "qbt_ca_bundle": ("qbittorrent", "ca_bundle", str),
            "qbt_username": ("qbittorrent", "username", str),
            "qbt_password": ("qbittorrent", "password", str),
            "qbt_interface_binding": ("qbittorrent", "interface_binding", str),
            # Service
            "log_level": ("service", "log_level", str),
            "state_file": ("service", "state_file", str),
            "max_consecutive_failures": ("service", "max_consecutive_failures", int),
            # Killswitch
            "killswitch_enabled": ("killswitch", "enabled", _parse_bool),
            "killswitch_user": ("killswitch", "user", str),
        }

        kwargs: dict[str, Any] = {}
        for field_name, (section, key, converter) in field_mappings.items():
            env_val = os.environ.get(f"QBOUNCER_{field_name.upper()}")
            file_val = config_data.get(section, {}).get(key)
            raw = env_val if env_val is not None else file_val
            if raw is None:
                continue
            try:
                kwargs[field_name] = converter(raw)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"Invalid value for {field_name}: {raw!r} ({e})") from e

        return cls(**kwargs)

    @staticmethod
    def _load_from_file(path: Path) -> dict[str, Any]:
        """Load configuration from TOML file."""
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from e

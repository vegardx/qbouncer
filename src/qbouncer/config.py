"""Configuration management for qbouncer."""

from __future__ import annotations

import os
import pwd
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .exceptions import ConfigError

__all__ = ["Config"]

# Validation patterns
INTERFACE_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,14}$")
IP_PATTERN = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


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
    wg_interface: str = "wg2"
    wg_health_check_host: str = "10.2.0.1"
    wg_health_check_interval: int = 30

    # NAT-PMP settings
    natpmp_gateway: str = "10.2.0.1"
    natpmp_refresh_interval: int = 60
    natpmp_lease_lifetime: int = 120

    # qBittorrent settings
    qbt_host: str = "localhost"
    qbt_port: int = 80
    qbt_use_https: bool = False
    qbt_verify_ssl: bool = True
    qbt_username: str = ""
    qbt_password: str = ""
    qbt_interface_binding: str = "wg2"

    # Service settings
    log_level: str = "INFO"
    state_file: str = "/var/lib/qbouncer/state.json"

    # Failure handling
    max_consecutive_failures: int = 5
    failure_backoff_base: int = 5
    failure_backoff_max: int = 300

    # Killswitch settings
    killswitch_enabled: bool = False
    killswitch_user: str = "qbittorrent"

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self) -> None:
        """Validate configuration values."""
        # Interface name validation
        if not INTERFACE_PATTERN.match(self.wg_interface):
            raise ConfigError(
                f"Invalid WireGuard interface name: {self.wg_interface!r}. "
                "Must start with a letter and contain only alphanumeric, hyphen, or underscore."
            )
        if not INTERFACE_PATTERN.match(self.qbt_interface_binding):
            raise ConfigError(
                f"Invalid qBittorrent interface binding: {self.qbt_interface_binding!r}. "
                "Must start with a letter and contain only alphanumeric, hyphen, or underscore."
            )

        # IP address validation
        if not IP_PATTERN.match(self.wg_health_check_host):
            raise ConfigError(f"Invalid health check host IP: {self.wg_health_check_host!r}")
        if not IP_PATTERN.match(self.natpmp_gateway):
            raise ConfigError(f"Invalid NAT-PMP gateway IP: {self.natpmp_gateway!r}")

        # Port validation
        if not 1 <= self.qbt_port <= 65535:
            raise ConfigError(f"Invalid qBittorrent port: {self.qbt_port}. Must be 1-65535.")

        # Interval validation
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

        # Failure handling validation
        if self.max_consecutive_failures < 1:
            raise ConfigError("max_consecutive_failures must be at least 1")
        if self.failure_backoff_base < 1:
            raise ConfigError("failure_backoff_base must be at least 1 second")
        if self.failure_backoff_max < self.failure_backoff_base:
            raise ConfigError("failure_backoff_max must be >= failure_backoff_base")

        # Log level validation
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigError(
                f"Invalid log level: {self.log_level!r}. Must be one of {valid_levels}"
            )

        # Killswitch validation
        if self.killswitch_enabled:
            try:
                pwd.getpwnam(self.killswitch_user)
            except KeyError:
                raise ConfigError(
                    f"Killswitch user not found: {self.killswitch_user!r}. "
                    "Ensure the user exists or disable killswitch."
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

        # Configuration mapping: field_name -> (section, key, converter)
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
            "qbt_username": ("qbittorrent", "username", str),
            "qbt_password": ("qbittorrent", "password", str),
            "qbt_interface_binding": ("qbittorrent", "interface_binding", str),
            # Service
            "log_level": ("service", "log_level", str),
            "state_file": ("service", "state_file", str),
            "max_consecutive_failures": ("service", "max_consecutive_failures", int),
            "failure_backoff_base": ("service", "failure_backoff_base", int),
            "failure_backoff_max": ("service", "failure_backoff_max", int),
            # Killswitch
            "killswitch_enabled": ("killswitch", "enabled", _parse_bool),
            "killswitch_user": ("killswitch", "user", str),
        }

        # Get default values from dataclass
        defaults = cls()

        # Build kwargs with resolved values
        kwargs: dict[str, Any] = {}
        for field_name, (section, key, converter) in field_mappings.items():
            file_value = config_data.get(section, {}).get(key)
            default_value = getattr(defaults, field_name)
            raw_value = cls._get_value(field_name, file_value, default_value)
            kwargs[field_name] = converter(raw_value)

        # Create new instance (validation happens in __post_init__)
        # Temporarily disable validation for defaults instance
        return cls(**kwargs)

    @staticmethod
    def _load_from_file(path: Path) -> dict[str, Any]:
        """Load configuration from TOML file."""
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from e

    @staticmethod
    def _get_value(key: str, file_value: Any, default: Any) -> Any:
        """Get configuration value with environment variable override."""
        env_key = f"QBOUNCER_{key.upper()}"
        env_value = os.environ.get(env_key)
        if env_value is not None:
            return env_value
        if file_value is not None:
            return file_value
        return default

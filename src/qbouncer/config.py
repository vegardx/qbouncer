"""Configuration management for qbouncer."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
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

# (field_name, toml_section, toml_key)
_STR_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("wg_interface", "wireguard", "interface"),
    ("wg_health_check_host", "wireguard", "health_check_host"),
    ("natpmp_gateway", "natpmp", "gateway"),
    ("qbt_host", "qbittorrent", "host"),
    ("qbt_ca_bundle", "qbittorrent", "ca_bundle"),
    ("qbt_username", "qbittorrent", "username"),
    ("qbt_password", "qbittorrent", "password"),
    ("qbt_interface_binding", "qbittorrent", "interface_binding"),
    ("log_level", "service", "log_level"),
    ("state_file", "service", "state_file"),
    ("killswitch_user", "killswitch", "user"),
)

_INT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("wg_health_check_interval", "wireguard", "health_check_interval"),
    ("natpmp_refresh_interval", "natpmp", "refresh_interval"),
    ("natpmp_lease_lifetime", "natpmp", "lease_lifetime"),
    ("qbt_port", "qbittorrent", "port"),
    ("qbt_availability_poll_interval", "qbittorrent", "availability_poll_interval"),
    ("max_consecutive_failures", "service", "max_consecutive_failures"),
    ("failure_retry_delay", "service", "failure_retry_delay"),
)

_BOOL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("qbt_use_https", "qbittorrent", "use_https"),
    ("qbt_verify_ssl", "qbittorrent", "verify_ssl"),
    ("killswitch_enabled", "killswitch", "enabled"),
)


def _parse_bool(value: Any) -> bool:
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
    qbt_password: str = field(default="", repr=False)
    qbt_interface_binding: str = "wg0"
    # How often to probe qBittorrent reachability during WAITING_QBT.
    qbt_availability_poll_interval: int = 5

    # Service settings
    log_level: str = "INFO"
    state_file: str = "/var/lib/qbouncer/state.json"

    # Failure handling — systemd restarts the service on persistent failure,
    # so keep recovery policy simple: count N failures, then back off briefly.
    max_consecutive_failures: int = 5
    failure_retry_delay: int = 30

    # Killswitch settings
    killswitch_enabled: bool = False
    killswitch_user: str = "qbittorrent"

    def __post_init__(self) -> None:
        self._validate()

    def _validate_iface(self, field_name: str, value: str) -> None:
        if not INTERFACE_PATTERN.match(value):
            raise ConfigError(
                f"Invalid {field_name}: {value!r}. Must start with a letter, contain only "
                "[a-zA-Z0-9_-], and be at most 15 characters."
            )

    def _validate_ipv4(self, field_name: str, value: str) -> None:
        try:
            IPv4Address(value)
        except (AddressValueError, ValueError) as e:
            raise ConfigError(f"Invalid {field_name}: {value!r}") from e

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
        if self.qbt_availability_poll_interval < 1:
            raise ConfigError("qbt_availability_poll_interval must be at least 1 second")
        if self.failure_retry_delay < 1:
            raise ConfigError("failure_retry_delay must be at least 1 second")

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

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Config:
        """Load configuration from file and environment variables.

        Priority (highest to lowest):
        1. Environment variables (QBOUNCER_*)
        2. Configuration file
        3. Default values
        """
        config_data: dict[str, Any] = {}

        if config_path:
            path = Path(config_path)
            if path.exists():
                config_data = cls._load_from_file(path)
            else:
                raise ConfigError(f"Configuration file not found: {path}")

        kwargs: dict[str, Any] = {}
        for field_name, section, key in _STR_FIELDS:
            raw = _resolve(field_name, config_data, section, key)
            if raw is not None:
                kwargs[field_name] = str(raw)

        for field_name, section, key in _INT_FIELDS:
            raw = _resolve(field_name, config_data, section, key)
            if raw is None:
                continue
            try:
                kwargs[field_name] = int(raw)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"Invalid value for {field_name}: {raw!r} ({e})") from e

        for field_name, section, key in _BOOL_FIELDS:
            raw = _resolve(field_name, config_data, section, key)
            if raw is not None:
                kwargs[field_name] = _parse_bool(raw)

        # Secret indirection: QBOUNCER_QBT_PASSWORD_FILE beats both the TOML
        # password and QBOUNCER_QBT_PASSWORD. This is what systemd LoadCredential=
        # points at — the file is created per-service in a tmpfs.
        password_file = os.environ.get("QBOUNCER_QBT_PASSWORD_FILE")
        if password_file:
            try:
                kwargs["qbt_password"] = Path(password_file).read_text().rstrip("\n")
            except OSError as e:
                raise ConfigError(
                    f"Failed to read QBOUNCER_QBT_PASSWORD_FILE={password_file!r}: {e}"
                ) from e

        return cls(**kwargs)

    @staticmethod
    def _load_from_file(path: Path) -> dict[str, Any]:
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from e


def _resolve(
    field_name: str,
    config_data: dict[str, Any],
    section: str,
    key: str,
) -> Any:
    """Env var (QBOUNCER_*) > file value > None. Coercion happens in the caller."""
    env_val = os.environ.get(f"QBOUNCER_{field_name.upper()}")
    if env_val is not None:
        return env_val
    return config_data.get(section, {}).get(key)

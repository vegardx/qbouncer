"""Custom exceptions for qbouncer."""

__all__ = [
    "QBouncerError",
    "ConfigError",
    "WireGuardError",
    "NatPmpError",
    "QBittorrentError",
]


class QBouncerError(Exception):
    """Base exception for qbouncer."""


class ConfigError(QBouncerError):
    """Configuration error."""


class WireGuardError(QBouncerError):
    """WireGuard-related error."""


class NatPmpError(QBouncerError):
    """NAT-PMP-related error."""


class QBittorrentError(QBouncerError):
    """qBittorrent API error."""

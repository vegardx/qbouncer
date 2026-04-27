"""Custom exceptions for qbouncer."""


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


class KillswitchError(QBouncerError):
    """Killswitch-related error."""

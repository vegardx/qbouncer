"""qbouncer - WireGuard NAT-PMP Port Bouncer for qBittorrent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("qbouncer")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

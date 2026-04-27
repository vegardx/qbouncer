"""WireGuard interface monitoring."""

from __future__ import annotations

import logging
import re
import subprocess

from .exceptions import WireGuardError

logger = logging.getLogger(__name__)


class WireGuardMonitor:
    """Monitor WireGuard interface health and connectivity."""

    def __init__(self, interface: str, health_check_host: str) -> None:
        """Initialize WireGuard monitor.

        Args:
            interface: WireGuard interface name (e.g., wg0)
            health_check_host: Host to ping for connectivity check
        """
        self.interface = interface
        self.health_check_host = health_check_host

    def is_interface_up(self) -> bool:
        """Check if the WireGuard interface exists and is UP.

        Uses `ip -br link show` for structured, version-stable output. WireGuard
        tunnels are point-to-point with no link layer, so their operational state
        is typically UNKNOWN rather than UP even when usable; both are treated as up.

        Returns:
            True if interface exists and is administratively up
        """
        try:
            result = subprocess.run(
                ["ip", "-br", "link", "show", self.interface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.debug("Interface %s not found", self.interface)
                return False

            parts = result.stdout.split()
            if len(parts) < 3:
                return False
            state = parts[1]
            flag_set = set(parts[2].strip("<>").split(","))
            if state in {"UP", "UNKNOWN"} and "UP" in flag_set:
                return True

            logger.debug("Interface %s exists but is not UP (state=%s)", self.interface, state)
            return False

        except subprocess.TimeoutExpired:
            logger.warning("Timeout checking interface %s", self.interface)
            return False
        except FileNotFoundError as e:
            raise WireGuardError("'ip' command not found") from e

    def get_interface_ip(self) -> str | None:
        """Get the IPv4 address assigned to the WireGuard interface."""
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", self.interface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
            return None

        except subprocess.TimeoutExpired:
            logger.warning("Timeout getting IP for interface %s", self.interface)
            return None

    def check_connectivity(self, timeout: int = 5) -> bool:
        """Check connectivity by pinging the health check host via the interface."""
        try:
            result = subprocess.run(
                [
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    str(timeout),
                    "-I",
                    self.interface,
                    self.health_check_host,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 2,
            )
            if result.returncode == 0:
                logger.debug("Ping to %s via %s succeeded", self.health_check_host, self.interface)
                return True
            logger.debug("Ping to %s via %s failed", self.health_check_host, self.interface)
            return False

        except subprocess.TimeoutExpired:
            logger.warning("Ping timeout to %s", self.health_check_host)
            return False
        except FileNotFoundError as e:
            raise WireGuardError("'ping' command not found") from e

    def is_healthy(self) -> bool:
        """Perform full health check on the WireGuard interface."""
        if not self.is_interface_up():
            logger.warning("WireGuard interface %s is not UP", self.interface)
            return False

        ip = self.get_interface_ip()
        if not ip:
            logger.warning("WireGuard interface %s has no IP address", self.interface)
            return False

        logger.debug("WireGuard interface %s has IP %s", self.interface, ip)

        if not self.check_connectivity():
            logger.warning("WireGuard connectivity check failed")
            return False

        return True

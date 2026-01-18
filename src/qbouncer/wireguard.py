"""WireGuard interface monitoring."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone

from .exceptions import WireGuardError

logger = logging.getLogger(__name__)

__all__ = ["WireGuardMonitor"]

# Default maximum age for WireGuard handshake to be considered fresh (seconds)
DEFAULT_HANDSHAKE_MAX_AGE = 180


class WireGuardMonitor:
    """Monitor WireGuard interface health and connectivity."""

    def __init__(self, interface: str, health_check_host: str) -> None:
        """Initialize WireGuard monitor.

        Args:
            interface: WireGuard interface name (e.g., wg2)
            health_check_host: Host to ping for connectivity check
        """
        self.interface = interface
        self.health_check_host = health_check_host

    def is_interface_up(self) -> bool:
        """Check if the WireGuard interface exists and is UP.

        Returns:
            True if interface exists and has UP state
        """
        try:
            result = subprocess.run(
                ["ip", "link", "show", self.interface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.debug("Interface %s not found", self.interface)
                return False

            # Check for "state UP" or "<...UP...>" in output
            output = result.stdout
            if "state UP" in output or ",UP," in output or "<UP," in output:
                return True

            logger.debug("Interface %s exists but is not UP", self.interface)
            return False

        except subprocess.TimeoutExpired:
            logger.warning("Timeout checking interface %s", self.interface)
            return False
        except FileNotFoundError:
            raise WireGuardError("'ip' command not found")

    def get_interface_ip(self) -> str | None:
        """Get the IPv4 address assigned to the WireGuard interface.

        Returns:
            IP address string or None if not found
        """
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", self.interface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            # Parse "inet X.X.X.X/XX" from output
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
            return None

        except subprocess.TimeoutExpired:
            logger.warning("Timeout getting IP for interface %s", self.interface)
            return None

    def check_connectivity(self, timeout: int = 5) -> bool:
        """Check connectivity by pinging the health check host.

        Args:
            timeout: Ping timeout in seconds

        Returns:
            True if ping succeeds
        """
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
        except FileNotFoundError:
            raise WireGuardError("'ping' command not found")

    def get_latest_handshake(self) -> datetime | None:
        """Get the latest handshake time for the WireGuard peer.

        Returns:
            datetime of latest handshake or None if unavailable
        """
        try:
            result = subprocess.run(
                ["wg", "show", self.interface, "latest-handshakes"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            # Output format: "<pubkey>\t<unix_timestamp>"
            lines = result.stdout.strip().split("\n")
            if not lines or not lines[0]:
                return None

            # Get the first peer's handshake (usually only one peer)
            parts = lines[0].split("\t")
            if len(parts) >= 2:
                timestamp = int(parts[1])
                if timestamp == 0:
                    return None
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            return None

        except (subprocess.TimeoutExpired, ValueError):
            return None
        except FileNotFoundError:
            logger.warning("'wg' command not found, skipping handshake check")
            return None

    def is_handshake_fresh(self, max_age_seconds: int = DEFAULT_HANDSHAKE_MAX_AGE) -> bool:
        """Check if the latest handshake is recent enough.

        Args:
            max_age_seconds: Maximum age of handshake in seconds

        Returns:
            True if handshake is within max_age_seconds
        """
        handshake = self.get_latest_handshake()
        if handshake is None:
            return False

        age = (datetime.now(timezone.utc) - handshake).total_seconds()
        return age < max_age_seconds

    def is_healthy(self) -> bool:
        """Perform full health check on the WireGuard interface.

        Checks:
        1. Interface exists and is UP
        2. Interface has an IP address
        3. Can ping the health check host

        Returns:
            True if all checks pass
        """
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

    def wait_for_interface(self, timeout: int = 60, poll_interval: int = 5) -> bool:
        """Wait for the WireGuard interface to become healthy.

        Args:
            timeout: Maximum time to wait in seconds
            poll_interval: Time between checks in seconds

        Returns:
            True if interface becomes healthy within timeout
        """
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if self.is_healthy():
                return True
            time.sleep(poll_interval)

        return False

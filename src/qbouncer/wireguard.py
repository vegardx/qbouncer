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

    def _run(
        self,
        argv: list[str],
        *,
        timeout: int = 5,
        missing_command_msg: str | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute a command and return its result, or None on timeout.

        Raises WireGuardError if the named binary is not installed so the
        caller never has to distinguish ENOENT from other failure modes.
        """
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timeout executing: %s", " ".join(argv))
            return None
        except FileNotFoundError as e:
            raise WireGuardError(missing_command_msg or f"{argv[0]!r} command not found") from e

    def is_interface_up(self) -> bool:
        """Check if the WireGuard interface exists and is UP.

        Uses `ip -br link show` for structured, version-stable output. WireGuard
        tunnels are point-to-point with no link layer, so their operational state
        is typically UNKNOWN rather than UP even when usable; both are treated as up.
        """
        result = self._run(
            ["ip", "-br", "link", "show", self.interface],
            missing_command_msg="'ip' command not found",
        )
        if result is None or result.returncode != 0:
            if result is not None:
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

    def get_interface_ip(self) -> str | None:
        """Get the IPv4 address assigned to the WireGuard interface."""
        result = self._run(
            ["ip", "-4", "addr", "show", self.interface],
            missing_command_msg="'ip' command not found",
        )
        if result is None or result.returncode != 0:
            return None
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
        return match.group(1) if match else None

    def check_connectivity(self, timeout: int = 5) -> bool:
        """Check connectivity by pinging the health check host via the interface."""
        result = self._run(
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
            timeout=timeout + 2,
            missing_command_msg="'ping' command not found",
        )
        if result is None:
            return False
        if result.returncode == 0:
            logger.debug("Ping to %s via %s succeeded", self.health_check_host, self.interface)
            return True
        logger.debug("Ping to %s via %s failed", self.health_check_host, self.interface)
        return False

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

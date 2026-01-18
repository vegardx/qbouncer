"""NAT-PMP port mapping management via natpmpc."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .exceptions import NatPmpError

logger = logging.getLogger(__name__)

__all__ = ["NatPmpManager", "PortMapping", "Protocol"]


class Protocol(Enum):
    """Network protocol for port mapping."""

    TCP = "tcp"
    UDP = "udp"


@dataclass
class PortMapping:
    """Represents a NAT-PMP port mapping."""

    public_port: int
    private_port: int
    protocol: str
    lifetime: int
    timestamp: datetime


class NatPmpManager:
    """Manage NAT-PMP port mappings via natpmpc."""

    # Regex to parse natpmpc output
    PORT_PATTERN = re.compile(
        r"Mapped public port (\d+) protocol (TCP|UDP) to local port (\d+) lifetime (\d+)"
    )
    PUBLIC_IP_PATTERN = re.compile(r"Public IP address\s*:\s*(\d+\.\d+\.\d+\.\d+)")

    def __init__(self, gateway: str, lease_lifetime: int = 120) -> None:
        """Initialize NAT-PMP manager.

        Args:
            gateway: NAT-PMP gateway IP address
            lease_lifetime: Requested port mapping lifetime in seconds
        """
        self.gateway = gateway
        self.lease_lifetime = lease_lifetime
        self.current_port: int | None = None
        self.last_refresh: datetime | None = None

    def request_mapping(self, protocol: Protocol = Protocol.TCP) -> PortMapping:
        """Request a port mapping from the NAT-PMP gateway.

        Args:
            protocol: Protocol to map

        Returns:
            PortMapping with the assigned port details

        Raises:
            NatPmpError: If mapping request fails
        """
        protocol_str = protocol.value

        try:
            result = subprocess.run(
                [
                    "natpmpc",
                    "-a",
                    "1",
                    "0",
                    protocol_str,
                    str(self.lease_lifetime),
                    "-g",
                    self.gateway,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error("natpmpc failed: %s", result.stderr or result.stdout)
                raise NatPmpError(f"natpmpc failed with code {result.returncode}")

            return self._parse_mapping_output(result.stdout, protocol_str.upper())

        except subprocess.TimeoutExpired:
            raise NatPmpError("natpmpc timed out")
        except FileNotFoundError:
            raise NatPmpError("natpmpc not found - install libnatpmp")

    def _parse_mapping_output(self, output: str, protocol: str) -> PortMapping:
        """Parse natpmpc output to extract port mapping details.

        Args:
            output: Raw natpmpc stdout
            protocol: Expected protocol (TCP or UDP)

        Returns:
            PortMapping with extracted details

        Raises:
            NatPmpError: If output cannot be parsed
        """
        logger.debug("natpmpc output: %s", output)

        match = self.PORT_PATTERN.search(output)
        if not match:
            raise NatPmpError(f"Could not parse natpmpc output: {output}")

        public_port = int(match.group(1))
        proto = match.group(2)
        private_port = int(match.group(3))
        lifetime = int(match.group(4))

        mapping = PortMapping(
            public_port=public_port,
            private_port=private_port,
            protocol=proto,
            lifetime=lifetime,
            timestamp=datetime.now(timezone.utc),
        )

        logger.info(
            "Mapped public port %d protocol %s (lifetime %ds)", public_port, proto, lifetime
        )

        return mapping

    def request_both_protocols(self) -> tuple[PortMapping, PortMapping]:
        """Request both TCP and UDP port mappings.

        Returns:
            Tuple of (tcp_mapping, udp_mapping)

        Raises:
            NatPmpError: If either mapping fails
        """
        tcp_mapping = self.request_mapping(Protocol.TCP)
        udp_mapping = self.request_mapping(Protocol.UDP)

        # Verify both got the same public port (ProtonVPN behavior)
        if tcp_mapping.public_port != udp_mapping.public_port:
            logger.warning(
                "TCP and UDP ports differ: TCP=%d, UDP=%d",
                tcp_mapping.public_port,
                udp_mapping.public_port,
            )

        return tcp_mapping, udp_mapping

    def refresh_mapping(self) -> int:
        """Refresh port mappings and return the public port.

        Updates current_port and last_refresh on success.

        Returns:
            The public port number

        Raises:
            NatPmpError: If mapping fails
        """
        tcp_mapping, udp_mapping = self.request_both_protocols()

        old_port = self.current_port
        self.current_port = tcp_mapping.public_port
        self.last_refresh = datetime.now(timezone.utc)

        if old_port is not None and old_port != self.current_port:
            logger.warning("Port changed: %d -> %d", old_port, self.current_port)

        return self.current_port

    def get_public_ip(self) -> str | None:
        """Get the public IP address from the NAT-PMP gateway.

        Returns:
            Public IP address string or None if unavailable
        """
        try:
            result = subprocess.run(
                ["natpmpc", "-g", self.gateway],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                return None

            match = self.PUBLIC_IP_PATTERN.search(result.stdout)
            if match:
                return match.group(1)
            return None

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def release_mapping(self, port: int, protocol: Protocol = Protocol.TCP) -> bool:
        """Release a port mapping by setting lifetime to 0.

        Args:
            port: Port number to release
            protocol: Protocol to release

        Returns:
            True if release succeeded
        """
        try:
            result = subprocess.run(
                [
                    "natpmpc",
                    "-a",
                    str(port),
                    "0",
                    protocol.value,
                    "0",
                    "-g",
                    self.gateway,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

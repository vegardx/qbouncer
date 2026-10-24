"""NAT-PMP port mapping management via natpmpc."""

from __future__ import annotations

import logging
import re
import subprocess

from .exceptions import NatPmpError

logger = logging.getLogger(__name__)


class NatPmpManager:
    """Manage NAT-PMP port mappings via natpmpc.

    Requests TCP + UDP mappings from the gateway and returns the public port
    assigned. ProtonVPN-style "gateway picks the port" semantics: the request
    uses private=1 and public=0 (gateway chooses).
    """

    PORT_PATTERN = re.compile(
        r"Mapped public port (\d+) protocol (TCP|UDP) to local port (\d+) lifetime (\d+)"
    )

    def __init__(self, gateway: str, lease_lifetime: int = 120) -> None:
        """Initialize NAT-PMP manager.

        Args:
            gateway: NAT-PMP gateway IP address
            lease_lifetime: Requested port mapping lifetime in seconds
        """
        self.gateway = gateway
        self.lease_lifetime = lease_lifetime
        # What the gateway actually granted on the last refresh. ProtonVPN
        # grants 60s whatever is requested.
        self.granted_lifetime: int | None = None

    def _request(self, protocol: str) -> tuple[int, int]:
        """Request a mapping for one protocol; return (public port, granted lifetime)."""
        try:
            result = subprocess.run(
                [
                    "natpmpc",
                    "-a",
                    "1",
                    "0",
                    protocol,
                    str(self.lease_lifetime),
                    "-g",
                    self.gateway,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            raise NatPmpError("natpmpc timed out") from e
        except FileNotFoundError as e:
            raise NatPmpError("natpmpc not found - install libnatpmp") from e

        if result.returncode != 0:
            logger.error("natpmpc failed: %s", result.stderr or result.stdout)
            raise NatPmpError(f"natpmpc failed with code {result.returncode}")

        return self._parse_mapping(result.stdout, protocol.upper())

    def _parse_public_port(self, output: str, protocol: str) -> int:
        """Extract and validate the public port from natpmpc stdout."""
        return self._parse_mapping(output, protocol)[0]

    def _parse_mapping(self, output: str, protocol: str) -> tuple[int, int]:
        """Extract and validate (public port, lifetime) from natpmpc stdout."""
        logger.debug("natpmpc output: %s", output)
        match = self.PORT_PATTERN.search(output)
        if not match:
            raise NatPmpError(f"Could not parse natpmpc output: {output}")

        public_port = int(match.group(1))
        returned_protocol = match.group(2)
        private_port = int(match.group(3))
        lifetime = int(match.group(4))

        if not 1 <= public_port <= 65535:
            raise NatPmpError(f"Gateway returned invalid public port: {public_port}")
        if not 0 <= private_port <= 65535:
            raise NatPmpError(f"Gateway returned invalid private port: {private_port}")
        if not 1 <= lifetime <= 86400:
            raise NatPmpError(f"Gateway returned invalid lifetime: {lifetime}")

        logger.info(
            "Mapped public port %d protocol %s (lifetime %ds)",
            public_port,
            returned_protocol,
            lifetime,
        )
        return public_port, lifetime

    def refresh_mapping(self) -> int:
        """Request TCP and UDP mappings and return the public port.

        Raises:
            NatPmpError: If either mapping fails
        """
        tcp_port, tcp_lifetime = self._request("tcp")
        udp_port, udp_lifetime = self._request("udp")
        self.granted_lifetime = min(tcp_lifetime, udp_lifetime)

        if tcp_port != udp_port:
            logger.warning("TCP and UDP ports differ: TCP=%d, UDP=%d", tcp_port, udp_port)

        return tcp_port

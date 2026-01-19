"""Main service orchestrator with state machine."""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, TypeAlias

from .config import Config
from .exceptions import KillswitchError, NatPmpError, QBittorrentError, QBouncerError
from .killswitch import KillswitchManager
from .natpmp import NatPmpManager
from .qbittorrent import QBittorrentClient
from .wireguard import WireGuardMonitor

logger = logging.getLogger(__name__)

__all__ = ["QBouncerService", "ServiceState"]

# Type aliases
PortNumber: TypeAlias = int

# Polling intervals (seconds)
QBT_AVAILABILITY_POLL_INTERVAL = 5  # How often to check if qBittorrent is available


@dataclass
class ServiceStateData:
    """Tracks runtime state of the service."""

    current_port: PortNumber | None = None
    consecutive_failures: int = 0
    last_port_refresh: datetime | None = None
    last_vpn_check: datetime | None = None


class ServiceState(Enum):
    """Service state machine states."""

    INITIALIZING = auto()
    WAITING_VPN = auto()
    WAITING_QBT = auto()
    MAPPING_PORT = auto()
    CONFIGURING = auto()
    MONITORING = auto()
    RECOVERING = auto()
    SHUTTING_DOWN = auto()


class QBouncerService:
    """Main service orchestrating VPN monitoring, NAT-PMP, and qBittorrent."""

    def __init__(self, config: Config) -> None:
        """Initialize the service.

        Args:
            config: Service configuration
        """
        self.config = config
        self.state = ServiceState.INITIALIZING
        self._shutdown_requested = False

        # Initialize components
        self.wg_monitor = WireGuardMonitor(
            interface=config.wg_interface,
            health_check_host=config.wg_health_check_host,
        )
        self.natpmp_manager = NatPmpManager(
            gateway=config.natpmp_gateway,
            lease_lifetime=config.natpmp_lease_lifetime,
        )
        self.qbt_client = QBittorrentClient(
            host=config.qbt_host,
            port=config.qbt_port,
            username=config.qbt_username,
            password=config.qbt_password,
            use_https=config.qbt_use_https,
            verify_ssl=config.qbt_verify_ssl,
        )

        # State tracking
        self.state_data = ServiceStateData()

        # Initialize killswitch if enabled
        self.killswitch: KillswitchManager | None = None
        if config.killswitch_enabled:
            self.killswitch = KillswitchManager(
                vpn_interface=config.wg_interface,
                user=config.killswitch_user,
            )

        # Load persisted state
        self._load_state()

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle shutdown signals."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating shutdown", sig_name)
        self._shutdown_requested = True
        self.state = ServiceState.SHUTTING_DOWN

    def _sd_notify(self, message: str) -> None:
        """Send notification to systemd.

        Args:
            message: Notification message (e.g., READY=1, WATCHDOG=1)
        """
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket:
            return

        try:
            if notify_socket.startswith("@"):
                notify_socket = "\0" + notify_socket[1:]

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.connect(notify_socket)
                sock.sendall(message.encode())
            finally:
                sock.close()
        except OSError as e:
            logger.debug("Failed to notify systemd: %s", e)

    def _load_state(self) -> None:
        """Load persisted state from state file."""
        state_path = Path(self.config.state_file)
        if not state_path.exists():
            logger.debug("No state file found, starting fresh")
            return

        try:
            with open(state_path) as f:
                data = json.load(f)

            self.state_data.current_port = data.get("last_port")
            if data.get("last_refresh"):
                self.state_data.last_port_refresh = datetime.fromisoformat(data["last_refresh"])

            logger.info(
                "Loaded state: last_port=%s, last_refresh=%s",
                self.state_data.current_port,
                self.state_data.last_port_refresh,
            )

        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load state file: %s", e)

    def _save_state(self) -> None:
        """Persist current state to state file."""
        state_path = Path(self.config.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        data = {
            "version": 1,
            "last_port": self.state_data.current_port,
            "last_refresh": self.state_data.last_port_refresh.isoformat()
            if self.state_data.last_port_refresh
            else None,
            "consecutive_failures": self.state_data.consecutive_failures,
        }

        try:
            with open(state_path, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(state_path, 0o600)
        except OSError as e:
            logger.warning("Failed to save state file: %s", e)

    def _calculate_backoff(self) -> int:
        """Calculate backoff delay based on consecutive failures.

        Returns:
            Delay in seconds
        """
        base = self.config.failure_backoff_base
        max_delay = self.config.failure_backoff_max

        delay = min(base * (2**self.state_data.consecutive_failures), max_delay)
        jitter = random.uniform(0, delay * 0.1)
        return int(delay + jitter)

    def run(self) -> None:
        """Main service loop."""
        self._setup_signal_handlers()

        logger.info("Starting qbouncer service")
        logger.info("WireGuard interface: %s", self.config.wg_interface)
        logger.info("NAT-PMP gateway: %s", self.config.natpmp_gateway)
        logger.info("qBittorrent: %s:%d", self.config.qbt_host, self.config.qbt_port)
        if self.killswitch:
            logger.info("Killswitch: enabled for user %s", self.config.killswitch_user)

        # Transition to waiting for VPN
        self.state = ServiceState.WAITING_VPN

        # Notify systemd we're ready
        self._sd_notify("READY=1")

        while self.state != ServiceState.SHUTTING_DOWN:
            try:
                self._tick()
                self._sd_notify("WATCHDOG=1")
            except QBouncerError as e:
                logger.error("Service error: %s", e)
                self._handle_failure()
            except Exception as e:
                logger.exception("Unexpected error: %s", e)
                self._handle_failure()

        self._cleanup()

    def _tick(self) -> None:
        """Single iteration of the main loop."""
        if self.state == ServiceState.WAITING_VPN:
            self._wait_for_vpn()
        elif self.state == ServiceState.WAITING_QBT:
            self._wait_for_qbittorrent()
        elif self.state == ServiceState.MAPPING_PORT:
            self._request_port_mapping()
        elif self.state == ServiceState.CONFIGURING:
            self._configure_qbittorrent()
        elif self.state == ServiceState.MONITORING:
            self._monitor()
        elif self.state == ServiceState.RECOVERING:
            self._recover()

    def _wait_for_vpn(self) -> None:
        """Wait for WireGuard VPN to become healthy."""
        logger.info("Waiting for WireGuard interface %s", self.config.wg_interface)

        if self.wg_monitor.is_healthy():
            logger.info("WireGuard VPN is healthy")

            # Setup killswitch now that VPN is up
            if self.killswitch:
                try:
                    self.killswitch.setup()
                except KillswitchError as e:
                    logger.error("Failed to setup killswitch: %s", e)
                    self._handle_failure()
                    return

            self.state_data.consecutive_failures = 0
            self.state = ServiceState.WAITING_QBT
        else:
            time.sleep(self.config.wg_health_check_interval)

    def _wait_for_qbittorrent(self) -> None:
        """Wait for qBittorrent API to become available."""
        logger.info("Waiting for qBittorrent at %s:%d", self.config.qbt_host, self.config.qbt_port)

        if self.qbt_client.is_reachable():
            version = self.qbt_client.get_version()
            logger.info("qBittorrent is available (version: %s)", version)
            self.state_data.consecutive_failures = 0
            self.state = ServiceState.MAPPING_PORT
        else:
            time.sleep(QBT_AVAILABILITY_POLL_INTERVAL)

    def _request_port_mapping(self) -> None:
        """Request NAT-PMP port mapping."""
        logger.debug("Requesting NAT-PMP port mapping")

        try:
            old_port = self.state_data.current_port
            new_port = self.natpmp_manager.refresh_mapping()

            self.state_data.current_port = new_port
            self.state_data.last_port_refresh = datetime.now(timezone.utc)
            self.state_data.consecutive_failures = 0

            if old_port != new_port:
                logger.info("Port mapping obtained: %d", new_port)
                self.state = ServiceState.CONFIGURING
            else:
                # Port unchanged from NAT-PMP, but verify qBittorrent matches (catch drift)
                try:
                    qbt_port = self.qbt_client.get_listening_port()
                    if qbt_port != new_port:
                        logger.warning(
                            "qBittorrent port drifted: expected %d, got %d",
                            new_port,
                            qbt_port,
                        )
                        self.state = ServiceState.CONFIGURING
                    else:
                        logger.debug("Port unchanged: %d", new_port)
                        self.state = ServiceState.MONITORING
                except QBittorrentError as e:
                    logger.warning("Failed to verify qBittorrent port: %s", e)
                    self.state = ServiceState.CONFIGURING

            self._save_state()

        except NatPmpError as e:
            logger.error("NAT-PMP error: %s", e)
            # NAT-PMP failure might indicate VPN issues
            self._handle_failure()
            self.state = ServiceState.WAITING_VPN

    def _configure_qbittorrent(self) -> None:
        """Configure qBittorrent with current port and interface."""
        if self.state_data.current_port is None:
            logger.error("No port available for configuration")
            self.state = ServiceState.MAPPING_PORT
            return

        try:
            # Get current qBittorrent settings
            current_port = self.qbt_client.get_listening_port()
            current_interface = self.qbt_client.get_network_interface()

            needs_update = False

            if current_port != self.state_data.current_port:
                logger.info(
                    "qBittorrent port needs update: %d -> %d",
                    current_port,
                    self.state_data.current_port,
                )
                needs_update = True

            if current_interface != self.config.qbt_interface_binding:
                logger.info(
                    "qBittorrent interface needs update: '%s' -> '%s'",
                    current_interface,
                    self.config.qbt_interface_binding,
                )
                needs_update = True

            if needs_update:
                self.qbt_client.update_port_and_interface(
                    port=self.state_data.current_port,
                    interface=self.config.qbt_interface_binding,
                )
                logger.info("qBittorrent configuration updated successfully")

            self.state_data.consecutive_failures = 0
            self.state = ServiceState.MONITORING

        except QBittorrentError as e:
            logger.error("qBittorrent configuration error: %s", e)
            self._handle_failure()

    def _monitor(self) -> None:
        """Normal monitoring state - check VPN and refresh port periodically."""
        now = datetime.now(timezone.utc)

        # Check if VPN health check is due
        if (
            self.state_data.last_vpn_check is None
            or (now - self.state_data.last_vpn_check).total_seconds()
            >= self.config.wg_health_check_interval
        ):
            if not self.wg_monitor.is_healthy():
                logger.warning("VPN health check failed")
                self.state = ServiceState.WAITING_VPN
                return
            self.state_data.last_vpn_check = now

        # Check if port refresh is due
        if (
            self.state_data.last_port_refresh is None
            or (now - self.state_data.last_port_refresh).total_seconds()
            >= self.config.natpmp_refresh_interval
        ):
            self.state = ServiceState.MAPPING_PORT
            return

        # Verify qBittorrent is still reachable and configured correctly
        try:
            if not self.qbt_client.is_reachable():
                logger.warning("qBittorrent is no longer reachable")
                self.state = ServiceState.WAITING_QBT
                return
            if not self.qbt_client.verify_interface_binding(self.config.qbt_interface_binding):
                logger.warning("qBittorrent interface binding changed, reconfiguring")
                self.state = ServiceState.CONFIGURING
                return
        except QBittorrentError as e:
            logger.warning("Failed to verify qBittorrent: %s", e)
            self.state = ServiceState.WAITING_QBT
            return

        # Verify killswitch is still active
        if self.killswitch and not self.killswitch.verify():
            logger.warning("Killswitch rules missing, re-establishing")
            try:
                self.killswitch.setup()
            except KillswitchError as e:
                logger.error("Failed to re-establish killswitch: %s", e)
                self._handle_failure()

        # Sleep until next check
        time.sleep(
            min(
                self.config.wg_health_check_interval,
                self.config.natpmp_refresh_interval,
            )
        )

    def _recover(self) -> None:
        """Recovery state after multiple failures."""
        logger.warning(
            "In recovery mode, %d consecutive failures", self.state_data.consecutive_failures
        )

        backoff = self._calculate_backoff()
        logger.info("Backing off for %d seconds", backoff)
        time.sleep(backoff)

        # Try to recover by checking VPN first
        self.state = ServiceState.WAITING_VPN

    def _handle_failure(self) -> None:
        """Handle a failure by incrementing counter and possibly entering recovery."""
        self.state_data.consecutive_failures += 1
        logger.warning("Failure count: %d", self.state_data.consecutive_failures)

        if self.state_data.consecutive_failures >= self.config.max_consecutive_failures:
            logger.error(
                "Max consecutive failures (%d) reached, entering recovery",
                self.config.max_consecutive_failures,
            )
            self.state = ServiceState.RECOVERING

    def _cleanup(self) -> None:
        """Cleanup on shutdown."""
        logger.info("Shutting down qbouncer service")

        # Remove killswitch rules
        if self.killswitch:
            try:
                self.killswitch.cleanup()
            except Exception as e:
                logger.error("Failed to cleanup killswitch: %s", e)

        # Save final state
        self._save_state()

        # Notify systemd we're stopping
        self._sd_notify("STOPPING=1")

        logger.info("Shutdown complete")

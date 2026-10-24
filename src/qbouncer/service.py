"""Main service orchestrator with state machine."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from types import FrameType

from .config import Config
from .exceptions import KillswitchError, NatPmpError, QBittorrentError, QBouncerError
from .killswitch import KillswitchManager
from .natpmp import NatPmpManager
from .qbittorrent import QBittorrentClient
from .wireguard import WireGuardMonitor

logger = logging.getLogger(__name__)


class ServiceState(Enum):
    WAITING_VPN = auto()
    WAITING_QBT = auto()
    MAPPING_PORT = auto()
    CONFIGURING = auto()
    MONITORING = auto()
    SHUTTING_DOWN = auto()


class QBouncerService:
    """Main service orchestrating VPN monitoring, NAT-PMP, and qBittorrent."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = ServiceState.WAITING_VPN
        self._shutdown_event = threading.Event()

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
            ca_bundle=config.qbt_ca_bundle,
        )

        self.current_port: int | None = None
        self.consecutive_failures = 0
        self.last_port_refresh: datetime | None = None
        self.last_vpn_check: datetime | None = None

        self.killswitch: KillswitchManager | None = None
        if config.killswitch_enabled:
            self.killswitch = KillswitchManager(
                vpn_interface=config.wg_interface,
                user=config.killswitch_user,
            )

        self._load_state()

    # -- signals and sleep ----------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating shutdown", sig_name)
        self._shutdown_event.set()
        self.state = ServiceState.SHUTTING_DOWN

    def _sleep(self, seconds: float) -> None:
        """Sleep but return immediately when a shutdown signal arrives."""
        self._shutdown_event.wait(timeout=seconds)

    # -- systemd notify -------------------------------------------------------

    def _sd_notify(self, message: str) -> None:
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket:
            return
        try:
            # Linux abstract namespace sockets: leading '@' → leading NUL byte.
            if notify_socket.startswith("@"):
                notify_socket = "\0" + notify_socket[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(notify_socket)
                sock.sendall(message.encode())
        except OSError as e:
            logger.debug("Failed to notify systemd: %s", e)

    # -- state persistence ----------------------------------------------------

    def _load_state(self) -> None:
        state_path = Path(self.config.state_file)
        if not state_path.exists():
            logger.debug("No state file found, starting fresh")
            return

        try:
            with open(state_path) as f:
                data = json.load(f)

            self.current_port = data.get("last_port")
            self.consecutive_failures = int(data.get("consecutive_failures", 0))
            if data.get("last_refresh"):
                self.last_port_refresh = datetime.fromisoformat(data["last_refresh"])

            logger.info(
                "Loaded state: last_port=%s, last_refresh=%s, failures=%d",
                self.current_port,
                self.last_port_refresh,
                self.consecutive_failures,
            )

        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            logger.warning("Failed to load state file (ignoring): %s", e)

    def _save_state(self) -> None:
        state_path = Path(self.config.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        data = {
            "version": 1,
            "last_port": self.current_port,
            "last_refresh": self.last_port_refresh.isoformat() if self.last_port_refresh else None,
            "consecutive_failures": self.consecutive_failures,
        }

        tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, state_path)
        except OSError as e:
            logger.warning("Failed to save state file: %s", e)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # -- main loop ------------------------------------------------------------

    def run(self) -> None:
        """Main service loop."""
        self._setup_signal_handlers()

        logger.info("Starting qbouncer service")
        logger.info("WireGuard interface: %s", self.config.wg_interface)
        logger.info("NAT-PMP gateway: %s", self.config.natpmp_gateway)
        logger.info("qBittorrent: %s:%d", self.config.qbt_host, self.config.qbt_port)
        if self.killswitch:
            logger.info("Killswitch: enabled for user %s", self.config.killswitch_user)
            # Install the killswitch before the VPN is confirmed up so the
            # user's traffic fails closed during startup.
            try:
                self.killswitch.setup()
            except KillswitchError as e:
                logger.error("Failed to setup killswitch: %s", e)
                raise

        self.state = ServiceState.WAITING_VPN
        self._sd_notify("READY=1")

        dispatch: dict[ServiceState, Callable[[], None]] = {
            ServiceState.WAITING_VPN: self._wait_for_vpn,
            ServiceState.WAITING_QBT: self._wait_for_qbittorrent,
            ServiceState.MAPPING_PORT: self._request_port_mapping,
            ServiceState.CONFIGURING: self._configure_qbittorrent,
            ServiceState.MONITORING: self._monitor,
        }

        while not self._shutdown_event.is_set():
            try:
                handler = dispatch.get(self.state)
                if handler is None:
                    break
                handler()
                self._sd_notify("WATCHDOG=1")
            except QBouncerError as e:
                logger.error("Service error: %s", e)
                self._handle_failure()
            except Exception as e:
                logger.exception("Unexpected error: %s", e)
                self._handle_failure()

        self._cleanup()

    # -- state handlers -------------------------------------------------------

    def _wait_for_vpn(self) -> None:
        logger.info("Waiting for WireGuard interface %s", self.config.wg_interface)
        if self.wg_monitor.is_healthy():
            logger.info("WireGuard VPN is healthy")
            self.consecutive_failures = 0
            self.state = ServiceState.WAITING_QBT
        else:
            self._sleep(self.config.wg_health_check_interval)

    def _wait_for_qbittorrent(self) -> None:
        logger.info("Waiting for qBittorrent at %s:%d", self.config.qbt_host, self.config.qbt_port)
        if self.qbt_client.is_reachable():
            version = self.qbt_client.get_version()
            logger.info("qBittorrent is available (version: %s)", version)
            self.consecutive_failures = 0
            self.state = ServiceState.MAPPING_PORT
        else:
            self._sleep(self.config.qbt_availability_poll_interval)

    def _request_port_mapping(self) -> None:
        logger.debug("Requesting NAT-PMP port mapping")
        try:
            old_port = self.current_port
            new_port = self.natpmp_manager.refresh_mapping()
        except NatPmpError as e:
            logger.error("NAT-PMP error: %s", e)
            self.state = ServiceState.WAITING_VPN
            raise

        self.current_port = new_port
        self.last_port_refresh = datetime.now(timezone.utc)
        self.consecutive_failures = 0

        if old_port != new_port:
            logger.info("Port mapping obtained: %d", new_port)
            self.state = ServiceState.CONFIGURING
        else:
            self.state = (
                ServiceState.MONITORING
                if self._qbt_port_matches(new_port)
                else ServiceState.CONFIGURING
            )

        self._save_state()

    def _qbt_port_matches(self, expected: int) -> bool:
        try:
            actual = self.qbt_client.get_listening_port()
        except QBittorrentError as e:
            logger.warning("Failed to verify qBittorrent port: %s", e)
            return False
        if actual != expected:
            logger.warning("qBittorrent port drifted: expected %d, got %d", expected, actual)
            return False
        logger.debug("Port unchanged: %d", expected)
        return True

    def _configure_qbittorrent(self) -> None:
        if self.current_port is None:
            logger.error("No port available for configuration")
            self.state = ServiceState.MAPPING_PORT
            return

        try:
            current_port = self.qbt_client.get_listening_port()
            current_interface = self.qbt_client.get_network_interface()

            needs_update = False
            if current_port != self.current_port:
                logger.info(
                    "qBittorrent port needs update: %d -> %d",
                    current_port,
                    self.current_port,
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
                    port=self.current_port,
                    interface=self.config.qbt_interface_binding,
                )
                logger.info("qBittorrent configuration updated successfully")

            self.consecutive_failures = 0
            self.state = ServiceState.MONITORING

        except QBittorrentError as e:
            logger.error("qBittorrent configuration error: %s", e)
            raise

    # -- monitoring transitions ----------------------------------------------

    def _vpn_due_transition(self, now: datetime) -> ServiceState | None:
        last = self.last_vpn_check
        due = last is None or (now - last).total_seconds() >= self.config.wg_health_check_interval
        if not due:
            return None
        if not self.wg_monitor.is_healthy():
            logger.warning("VPN health check failed")
            return ServiceState.WAITING_VPN
        self.last_vpn_check = now
        return None

    def _port_refresh_due_transition(self, now: datetime) -> ServiceState | None:
        last = self.last_port_refresh
        due = last is None or (now - last).total_seconds() >= self.config.natpmp_refresh_interval
        return ServiceState.MAPPING_PORT if due else None

    def _qbt_verify_transition(self) -> ServiceState | None:
        try:
            if not self.qbt_client.is_reachable():
                logger.warning("qBittorrent is no longer reachable")
                return ServiceState.WAITING_QBT
            if not self.qbt_client.verify_interface_binding(self.config.qbt_interface_binding):
                logger.warning("qBittorrent interface binding changed, reconfiguring")
                return ServiceState.CONFIGURING
        except QBittorrentError as e:
            logger.warning("Failed to verify qBittorrent: %s", e)
            return ServiceState.WAITING_QBT
        return None

    def _verify_killswitch(self) -> None:
        if not self.killswitch:
            return
        if self.killswitch.verify():
            return
        logger.warning("Killswitch rules missing, re-establishing")
        try:
            self.killswitch.setup()
        except KillswitchError as e:
            logger.error("Failed to re-establish killswitch: %s", e)
            raise

    def _monitor(self) -> None:
        now = datetime.now(timezone.utc)
        for transition in (
            self._vpn_due_transition(now),
            self._port_refresh_due_transition(now),
            self._qbt_verify_transition(),
        ):
            if transition is not None:
                self.state = transition
                return

        self._verify_killswitch()
        self._sleep(min(self.config.wg_health_check_interval, self.config.natpmp_refresh_interval))

    # -- failure handling -----------------------------------------------------

    def _handle_failure(self) -> None:
        """Count a failure; on repeated failures, back off and reset to WAITING_VPN.

        systemd's Restart=on-failure is the durable recovery path — this only
        handles transient failures that fit inside a single process lifetime.
        """
        self.consecutive_failures += 1
        logger.warning("Failure count: %d", self.consecutive_failures)

        if self.consecutive_failures >= self.config.max_consecutive_failures:
            logger.error(
                "Max consecutive failures (%d) reached, backing off %ds before retry",
                self.config.max_consecutive_failures,
                self.config.failure_retry_delay,
            )
            self._sleep(self.config.failure_retry_delay)
            self.consecutive_failures = 0
            self.state = ServiceState.WAITING_VPN

    # -- shutdown -------------------------------------------------------------

    def _cleanup(self) -> None:
        logger.info("Shutting down qbouncer service")
        self._save_state()

        try:
            self.qbt_client.logout()
        except Exception as e:
            logger.debug("qBittorrent logout failed (non-fatal): %s", e)

        if self.killswitch:
            try:
                self.killswitch.remove()
            except Exception as e:
                logger.error("Failed to remove killswitch: %s", e)

        self._sd_notify("STOPPING=1")
        logger.info("Shutdown complete")

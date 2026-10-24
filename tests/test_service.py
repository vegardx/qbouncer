"""Tests for the QBouncerService state machine and state file persistence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qbouncer.config import Config
from qbouncer.exceptions import NatPmpError, QBittorrentError
from qbouncer.service import QBouncerService, ServiceState


@pytest.fixture
def service(tmp_path: Path) -> QBouncerService:
    """A service wired to tmp_path state file, with all external deps mocked."""
    state_file = tmp_path / "state.json"
    cfg = Config(state_file=str(state_file))
    with (
        patch("qbouncer.service.WireGuardMonitor"),
        patch("qbouncer.service.NatPmpManager"),
        patch("qbouncer.service.QBittorrentClient"),
    ):
        svc = QBouncerService(cfg)
    return svc


class TestStatePersistence:
    def test_save_then_load_roundtrip(self, service: QBouncerService) -> None:
        from datetime import datetime, timezone

        service.current_port = 42123
        service.consecutive_failures = 2
        service.last_port_refresh = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        service._save_state()

        on_disk = json.loads(Path(service.config.state_file).read_text())
        assert on_disk["last_port"] == 42123
        assert on_disk["consecutive_failures"] == 2

        service.current_port = None
        service.consecutive_failures = 0
        service.last_port_refresh = None

        service._load_state()
        assert service.current_port == 42123
        assert service.consecutive_failures == 2
        assert service.last_port_refresh is not None

    def test_load_ignores_corrupt_json(
        self, service: QBouncerService, caplog: pytest.LogCaptureFixture
    ) -> None:
        Path(service.config.state_file).write_text("{not json}")
        with caplog.at_level("WARNING"):
            service._load_state()
        assert service.current_port is None
        assert any("Failed to load state file" in r.getMessage() for r in caplog.records)

    def test_load_ignores_bad_iso_date(
        self, service: QBouncerService, caplog: pytest.LogCaptureFixture
    ) -> None:
        Path(service.config.state_file).write_text(
            json.dumps({"last_port": 1, "last_refresh": "not-a-date"})
        )
        with caplog.at_level("WARNING"):
            service._load_state()
        assert any("Failed to load state file" in r.getMessage() for r in caplog.records)

    def test_save_creates_file_with_mode_0600(self, service: QBouncerService) -> None:
        service.current_port = 1234
        service._save_state()
        mode = Path(service.config.state_file).stat().st_mode & 0o777
        assert mode == 0o600


class TestStateTransitions:
    def test_wait_for_vpn_transitions_when_healthy(self, service: QBouncerService) -> None:
        service.wg_monitor.is_healthy = MagicMock(return_value=True)
        service.consecutive_failures = 3
        service._wait_for_vpn()
        assert service.state == ServiceState.WAITING_QBT
        assert service.consecutive_failures == 0

    def test_wait_for_vpn_sleeps_when_unhealthy(self, service: QBouncerService) -> None:
        service.wg_monitor.is_healthy = MagicMock(return_value=False)
        with patch.object(service, "_sleep") as sleep:
            service._wait_for_vpn()
        assert service.state == ServiceState.WAITING_VPN
        sleep.assert_called_once()

    def test_request_mapping_new_port_configures(self, service: QBouncerService) -> None:
        service.natpmp_manager.refresh_mapping = MagicMock(return_value=5555)
        service.current_port = 4444
        service._request_port_mapping()
        assert service.current_port == 5555
        assert service.state == ServiceState.CONFIGURING

    def test_request_mapping_unchanged_monitors(self, service: QBouncerService) -> None:
        service.natpmp_manager.refresh_mapping = MagicMock(return_value=5555)
        service.qbt_client.get_listening_port = MagicMock(return_value=5555)
        service.current_port = 5555
        service._request_port_mapping()
        assert service.state == ServiceState.MONITORING

    def test_request_mapping_unchanged_but_drift_reconfigures(
        self, service: QBouncerService
    ) -> None:
        service.natpmp_manager.refresh_mapping = MagicMock(return_value=5555)
        service.qbt_client.get_listening_port = MagicMock(return_value=9999)  # drift
        service.current_port = 5555
        service._request_port_mapping()
        assert service.state == ServiceState.CONFIGURING

    def test_request_mapping_natpmp_error_raises(self, service: QBouncerService) -> None:
        service.natpmp_manager.refresh_mapping = MagicMock(side_effect=NatPmpError("boom"))
        with pytest.raises(NatPmpError):
            service._request_port_mapping()
        assert service.state == ServiceState.WAITING_VPN

    def test_configure_qbittorrent_updates_on_mismatch(self, service: QBouncerService) -> None:
        service.current_port = 5555
        service.qbt_client.get_listening_port = MagicMock(return_value=0)
        service.qbt_client.get_network_interface = MagicMock(return_value="")
        service.qbt_client.update_port_and_interface = MagicMock()
        service._configure_qbittorrent()
        service.qbt_client.update_port_and_interface.assert_called_once_with(
            port=5555, interface="wg0"
        )
        assert service.state == ServiceState.MONITORING

    def test_configure_qbittorrent_qbt_error_raises(self, service: QBouncerService) -> None:
        service.current_port = 5555
        service.qbt_client.get_listening_port = MagicMock(side_effect=QBittorrentError("boom"))
        with pytest.raises(QBittorrentError):
            service._configure_qbittorrent()


class TestFailureHandling:
    def test_increments_failure_counter(self, service: QBouncerService) -> None:
        service._handle_failure()
        assert service.consecutive_failures == 1

    def test_resets_to_waiting_vpn_at_threshold(self, service: QBouncerService) -> None:
        service.consecutive_failures = service.config.max_consecutive_failures - 1
        with patch.object(service, "_sleep"):
            service._handle_failure()
        assert service.consecutive_failures == 0
        assert service.state == ServiceState.WAITING_VPN


class TestShutdownSignal:
    def test_signal_sets_event_and_state(self, service: QBouncerService) -> None:
        assert not service._shutdown_event.is_set()
        import signal as signal_mod

        service._handle_signal(signal_mod.SIGTERM, None)
        assert service._shutdown_event.is_set()
        assert service.state == ServiceState.SHUTTING_DOWN

    def test_sleep_returns_immediately_when_shutdown_signaled(
        self, service: QBouncerService
    ) -> None:
        import time

        service._shutdown_event.set()
        start = time.monotonic()
        service._sleep(60)
        assert time.monotonic() - start < 1.0


class TestMonitorTransitions:
    def test_vpn_due_returns_waiting_vpn_when_unhealthy(self, service: QBouncerService) -> None:
        from datetime import datetime, timezone

        service.wg_monitor.is_healthy = MagicMock(return_value=False)
        result = service._vpn_due_transition(datetime.now(timezone.utc))
        assert result == ServiceState.WAITING_VPN

    def test_port_refresh_due_triggers_mapping(self, service: QBouncerService) -> None:
        from datetime import datetime, timezone

        # Never refreshed -> due
        result = service._port_refresh_due_transition(datetime.now(timezone.utc))
        assert result == ServiceState.MAPPING_PORT

    def test_qbt_verify_returns_waiting_qbt_when_unreachable(
        self, service: QBouncerService
    ) -> None:
        service.qbt_client.is_reachable = MagicMock(return_value=False)
        assert service._qbt_verify_transition() == ServiceState.WAITING_QBT


class TestCleanupCallsLogout:
    def test_cleanup_calls_qbt_logout(self, service: QBouncerService) -> None:
        service.qbt_client.logout = MagicMock()
        service._cleanup()
        service.qbt_client.logout.assert_called_once()


class TestRenewalTiming:
    """Renewals must land before the granted lease expires."""

    def test_renews_at_three_quarters_of_granted_lease(self, service: QBouncerService) -> None:
        service.natpmp_manager.granted_lifetime = 60
        assert service._refresh_interval() == 45

    def test_configured_interval_wins_when_shorter(self, service: QBouncerService) -> None:
        service.config.natpmp_refresh_interval = 30
        service.natpmp_manager.granted_lifetime = 60
        assert service._refresh_interval() == 30

    def test_uses_configured_interval_before_first_grant(self, service: QBouncerService) -> None:
        service.natpmp_manager.granted_lifetime = None
        assert service._refresh_interval() == service.config.natpmp_refresh_interval

    def test_sleeps_until_refresh_is_due(self, service: QBouncerService) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        service.natpmp_manager.granted_lifetime = 60
        service.last_vpn_check = now
        service.last_port_refresh = now - timedelta(seconds=40)
        # VPN check due in 30s, refresh due in 45 - 40 = 5s.
        assert service._seconds_until_next_check(now) == pytest.approx(5)

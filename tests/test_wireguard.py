"""Tests for WireGuard interface parsing."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from qbouncer.exceptions import WireGuardError
from qbouncer.wireguard import WireGuardMonitor


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.returncode = returncode
    return m


class TestIsInterfaceUp:
    @pytest.mark.parametrize(
        "stdout",
        [
            # Typical WG output: state UNKNOWN with UP in the flag set
            "wg0              UNKNOWN        <POINTOPOINT,NOARP,UP,LOWER_UP>",
            # Explicit state UP
            "wg0              UP             <BROADCAST,MULTICAST,UP>",
        ],
    )
    def test_accepts_up_interfaces(
        self, fake_subprocess_run: MagicMock, stdout: str
    ) -> None:
        fake_subprocess_run.return_value = _completed(stdout=stdout)
        assert WireGuardMonitor("wg0", "10.0.0.1").is_interface_up() is True

    @pytest.mark.parametrize(
        "stdout",
        [
            # DOWN operational state
            "wg0              DOWN           <POINTOPOINT,NOARP>",
            # UP flag missing
            "wg0              UNKNOWN        <POINTOPOINT,NOARP>",
        ],
    )
    def test_rejects_down_interfaces(
        self, fake_subprocess_run: MagicMock, stdout: str
    ) -> None:
        fake_subprocess_run.return_value = _completed(stdout=stdout)
        assert WireGuardMonitor("wg0", "10.0.0.1").is_interface_up() is False

    def test_returns_false_when_ip_returns_nonzero(
        self, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.return_value = _completed(stdout="", returncode=1)
        assert WireGuardMonitor("wg0", "10.0.0.1").is_interface_up() is False

    def test_returns_false_on_timeout(
        self, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd=["ip"], timeout=1)
        assert WireGuardMonitor("wg0", "10.0.0.1").is_interface_up() is False

    def test_raises_if_ip_binary_missing(
        self, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = FileNotFoundError()
        with pytest.raises(WireGuardError):
            WireGuardMonitor("wg0", "10.0.0.1").is_interface_up()


class TestGetInterfaceIP:
    def test_extracts_ipv4_from_output(self, fake_subprocess_run: MagicMock) -> None:
        fake_subprocess_run.return_value = _completed(
            stdout="    inet 10.2.0.42/32 scope global wg0\n"
        )
        assert WireGuardMonitor("wg0", "10.0.0.1").get_interface_ip() == "10.2.0.42"

    def test_returns_none_without_inet_line(
        self, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.return_value = _completed(stdout="")
        assert WireGuardMonitor("wg0", "10.0.0.1").get_interface_ip() is None


class TestIsHealthy:
    def test_full_pass(self, fake_subprocess_run: MagicMock) -> None:
        # is_interface_up, get_interface_ip, check_connectivity each issue one call
        fake_subprocess_run.side_effect = [
            _completed(stdout="wg0 UNKNOWN <POINTOPOINT,NOARP,UP>"),
            _completed(stdout="    inet 10.2.0.42/32 scope global wg0\n"),
            _completed(stdout=""),  # ping success
        ]
        assert WireGuardMonitor("wg0", "10.0.0.1").is_healthy() is True

    def test_fails_when_interface_down(
        self, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.return_value = _completed(stdout="wg0 DOWN <POINTOPOINT>")
        assert WireGuardMonitor("wg0", "10.0.0.1").is_healthy() is False

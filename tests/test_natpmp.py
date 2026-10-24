"""Tests for the NAT-PMP port mapping parser and wiring."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from qbouncer.exceptions import NatPmpError
from qbouncer.natpmp import NatPmpManager


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


GOOD_OUTPUT = (
    "initnatpmp() returned 0\n"
    "sendpublicaddressrequest returned 2\n"
    "Mapped public port 42123 protocol TCP to local port 1 lifetime 60\n"
)


class TestPortParser:
    def test_parses_public_port(self) -> None:
        mgr = NatPmpManager("10.0.0.1")
        assert mgr._parse_public_port(GOOD_OUTPUT, "TCP") == 42123

    def test_rejects_missing_pattern(self) -> None:
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="Could not parse"):
            mgr._parse_public_port("gateway returned some garbage\n", "TCP")

    @pytest.mark.parametrize(
        "line",
        [
            "Mapped public port 0 protocol TCP to local port 1 lifetime 60",
            "Mapped public port 65536 protocol TCP to local port 1 lifetime 60",
        ],
    )
    def test_rejects_out_of_range_public_port(self, line: str) -> None:
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="invalid public port"):
            mgr._parse_public_port(line + "\n", "TCP")

    def test_rejects_invalid_lifetime(self) -> None:
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="invalid lifetime"):
            mgr._parse_public_port(
                "Mapped public port 42123 protocol TCP to local port 1 lifetime 999999\n",
                "TCP",
            )


class TestRequest:
    def test_natpmpc_nonzero_raises(self, fake_subprocess_run: MagicMock) -> None:
        fake_subprocess_run.return_value = _completed(stdout="", returncode=1, stderr="boom")
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="failed with code"):
            mgr._request("tcp")

    def test_natpmpc_missing_binary_raises(self, fake_subprocess_run: MagicMock) -> None:
        fake_subprocess_run.side_effect = FileNotFoundError()
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="libnatpmp"):
            mgr._request("tcp")

    def test_timeout_raises(self, fake_subprocess_run: MagicMock) -> None:
        fake_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd=["natpmpc"], timeout=1)
        mgr = NatPmpManager("10.0.0.1")
        with pytest.raises(NatPmpError, match="timed out"):
            mgr._request("tcp")


class TestRefresh:
    def test_returns_port_when_tcp_and_udp_match(self, fake_subprocess_run: MagicMock) -> None:
        fake_subprocess_run.return_value = _completed(stdout=GOOD_OUTPUT)
        mgr = NatPmpManager("10.0.0.1")
        assert mgr.refresh_mapping() == 42123

    def test_warns_but_returns_tcp_when_udp_differs(
        self, fake_subprocess_run: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        udp_output = "Mapped public port 9999 protocol UDP to local port 1 lifetime 60\n"
        fake_subprocess_run.side_effect = [
            _completed(stdout=GOOD_OUTPUT),
            _completed(stdout=udp_output),
        ]
        mgr = NatPmpManager("10.0.0.1")
        with caplog.at_level("WARNING"):
            assert mgr.refresh_mapping() == 42123
        assert any("TCP and UDP ports differ" in r.getMessage() for r in caplog.records)

    def test_records_granted_lifetime(self, fake_subprocess_run: MagicMock) -> None:
        # The gateway grants 60s although 120s is requested.
        fake_subprocess_run.return_value = _completed(stdout=GOOD_OUTPUT)
        mgr = NatPmpManager("10.0.0.1", lease_lifetime=120)
        mgr.refresh_mapping()
        assert mgr.granted_lifetime == 60

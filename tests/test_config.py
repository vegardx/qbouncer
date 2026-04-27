"""Tests for Config loading, validation, and env/file precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from qbouncer.config import Config
from qbouncer.exceptions import ConfigError


class TestDefaults:
    def test_minimum_config_instantiates(self) -> None:
        c = Config()
        assert c.wg_interface == "wg0"
        assert c.qbt_interface_binding == "wg0"
        assert c.qbt_port == 8080
        assert c.qbt_verify_ssl is True
        assert c.qbt_ca_bundle == ""

    def test_backoff_knobs_removed(self) -> None:
        c = Config()
        assert not hasattr(c, "failure_backoff_base")
        assert not hasattr(c, "failure_backoff_max")
        assert c.max_consecutive_failures == 5


class TestIPv4Validation:
    @pytest.mark.parametrize(
        "bad_ip",
        ["999.999.999.999", "10.0.0.300", "not-an-ip", "10.0.0", "10..0.1", ""],
    )
    def test_rejects_invalid_ipv4(self, bad_ip: str) -> None:
        with pytest.raises(ConfigError, match="natpmp_gateway"):
            Config(natpmp_gateway=bad_ip)

    @pytest.mark.parametrize("good_ip", ["10.0.0.1", "192.168.1.1", "127.0.0.1", "0.0.0.0"])
    def test_accepts_valid_ipv4(self, good_ip: str) -> None:
        Config(natpmp_gateway=good_ip)  # no exception


class TestInterfaceValidation:
    @pytest.mark.parametrize(
        "bad_iface",
        [
            "0wg",  # must start with a letter
            "wg!",  # invalid character
            "x" * 16,  # >15 chars
            "",
        ],
    )
    def test_rejects_invalid_interface(self, bad_iface: str) -> None:
        with pytest.raises(ConfigError, match="wg_interface"):
            Config(wg_interface=bad_iface)

    @pytest.mark.parametrize("good_iface", ["wg0", "wg-vpn", "wg_1", "eth0", "x" * 15])
    def test_accepts_valid_interface(self, good_iface: str) -> None:
        Config(wg_interface=good_iface, qbt_interface_binding=good_iface)


class TestIntervalValidation:
    def test_refresh_must_be_less_than_lifetime(self) -> None:
        with pytest.raises(ConfigError, match="lease lifetime"):
            Config(natpmp_refresh_interval=120, natpmp_lease_lifetime=60)


class TestLogLevel:
    def test_invalid_rejected(self) -> None:
        with pytest.raises(ConfigError, match="log level"):
            Config(log_level="LOUD")

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "info"])
    def test_valid_accepted(self, level: str) -> None:
        Config(log_level=level)


class TestTLSPosture:
    def test_remote_with_verify_false_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="verify_ssl=false"):
            Config(
                qbt_host="remote.example.com",
                qbt_use_https=True,
                qbt_verify_ssl=False,
            )

    def test_remote_with_ca_bundle_is_allowed(self) -> None:
        Config(
            qbt_host="remote.example.com",
            qbt_use_https=True,
            qbt_verify_ssl=False,
            qbt_ca_bundle="/etc/pki/ca.pem",
        )

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_loopback_with_verify_false_is_allowed(self, host: str) -> None:
        Config(qbt_host=host, qbt_use_https=True, qbt_verify_ssl=False)


class TestEnvPrecedence:
    def test_env_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "q.toml"
        cfg.write_text(
            '[wireguard]\ninterface = "wg1"\n'
            '[natpmp]\ngateway = "10.0.0.1"\n'
            "[qbittorrent]\nport = 8080\n"
        )
        monkeypatch.setenv("QBOUNCER_WG_INTERFACE", "wg9")
        monkeypatch.setenv("QBOUNCER_QBT_PORT", "9090")

        c = Config.load(cfg)
        assert c.wg_interface == "wg9"  # env wins
        assert c.qbt_port == 9090  # env wins, coerced via int()

    def test_file_wins_over_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / "q.toml"
        cfg.write_text(
            '[wireguard]\ninterface = "wg3"\n'
            "[qbittorrent]\nport = 9999\n"
            '[natpmp]\ngateway = "10.2.0.1"\n'
        )
        c = Config.load(cfg)
        assert c.wg_interface == "wg3"
        assert c.qbt_port == 9999

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "missing.toml")

    def test_bad_env_value_raises_configerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QBOUNCER_QBT_PORT", "not-a-number")
        with pytest.raises(ConfigError, match="qbt_port"):
            Config.load(None)

    def test_defaults_only_when_no_file_and_no_env(self) -> None:
        c = Config.load(None)
        assert c.wg_interface == "wg0"


class TestCredentialMasking:
    def test_password_not_in_repr(self) -> None:
        c = Config(qbt_password="supersecret")
        assert "supersecret" not in repr(c)
        assert "***" in repr(c)

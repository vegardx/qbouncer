"""Tests for the iptables/ip6tables killswitch."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qbouncer.killswitch import KillswitchManager

UID = 1000


def make_run(chain_exists: bool, jump_exists: bool) -> Any:
    """subprocess.run stand-in: answers -L (chain) and -C OUTPUT (jump) checks."""

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        rc = 0
        if "-L" in cmd:
            rc = 0 if chain_exists else 1
        elif "-C" in cmd and "OUTPUT" in cmd:
            rc = 0 if jump_exists else 1
        elif "-D" in cmd:
            rc = 1  # nothing (left) to delete
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    return run


@pytest.fixture
def manager() -> KillswitchManager:
    with (
        patch.object(KillswitchManager, "_detect_ipv6", return_value=False),
        patch("qbouncer.killswitch.pwd.getpwnam", return_value=MagicMock(pw_uid=UID)),
    ):
        mgr = KillswitchManager(vpn_interface="wg2", user="qbittorrent")
        mgr._get_uid()
    return mgr


def restore_inputs(mock_run: MagicMock) -> list[str]:
    return [c.kwargs["input"] for c in mock_run.call_args_list if c.kwargs.get("input")]


def commands(mock_run: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in mock_run.call_args_list]


class TestRuleset:
    def test_rejects_last_and_allows_only_lo_and_vpn(self, manager: KillswitchManager) -> None:
        rules = [
            r for r in manager._render_ruleset(add_jump=False).splitlines() if r.startswith("-A")
        ]
        assert rules[-1] == "-A QBOUNCER-KS -j REJECT"
        assert "-A QBOUNCER-KS -o lo -j ACCEPT" in rules
        assert "-A QBOUNCER-KS -o wg2 -j ACCEPT" in rules
        assert not any("ESTABLISHED" in r for r in rules)

    def test_jump_only_when_requested(self, manager: KillswitchManager) -> None:
        jump = f"-I OUTPUT -m owner --uid-owner {UID} -j QBOUNCER-KS"
        assert jump in manager._render_ruleset(add_jump=True)
        assert jump not in manager._render_ruleset(add_jump=False)


class TestSetup:
    def test_fresh_install_adds_chain_and_jump(
        self, manager: KillswitchManager, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = make_run(chain_exists=False, jump_exists=False)
        manager.setup()
        (ruleset,) = restore_inputs(fake_subprocess_run)
        assert "-I OUTPUT" in ruleset

    def test_rebuild_keeps_existing_jump_and_never_tears_down(
        self, manager: KillswitchManager, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = make_run(chain_exists=True, jump_exists=True)
        manager.setup()
        (ruleset,) = restore_inputs(fake_subprocess_run)
        assert "-I OUTPUT" not in ruleset
        assert "-F QBOUNCER-KS" in ruleset
        for cmd in commands(fake_subprocess_run):
            assert "-D" not in cmd and "-X" not in cmd

    def test_applies_ip6tables_when_ipv6_enabled(
        self, manager: KillswitchManager, fake_subprocess_run: MagicMock
    ) -> None:
        manager._ipv6_enabled = True
        fake_subprocess_run.side_effect = make_run(chain_exists=False, jump_exists=False)
        manager.setup()
        restores = [c[0] for c in commands(fake_subprocess_run) if c[0].endswith("-restore")]
        assert restores == ["iptables-restore", "ip6tables-restore"]


class TestRemoveAndVerify:
    def test_remove_deletes_jump_then_chain(
        self, manager: KillswitchManager, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = make_run(chain_exists=True, jump_exists=True)
        manager.remove()
        cmds = commands(fake_subprocess_run)
        first_delete = next(i for i, c in enumerate(cmds) if "-D" in c)
        drop_chain = next(i for i, c in enumerate(cmds) if "-X" in c)
        assert first_delete < drop_chain

    def test_verify_fails_without_jump(
        self, manager: KillswitchManager, fake_subprocess_run: MagicMock
    ) -> None:
        fake_subprocess_run.side_effect = make_run(chain_exists=True, jump_exists=False)
        assert manager.verify() is False

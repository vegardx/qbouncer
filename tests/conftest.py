"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Replace subprocess.run in the qbouncer package with a MagicMock.

    Each module imports subprocess independently, so the mock is installed at
    each import site used by the production code.
    """
    import subprocess

    import qbouncer.killswitch as killswitch_mod
    import qbouncer.natpmp as natpmp_mod
    import qbouncer.wireguard as wireguard_mod

    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(killswitch_mod.subprocess, "run", mock_run)
    monkeypatch.setattr(natpmp_mod.subprocess, "run", mock_run)
    monkeypatch.setattr(wireguard_mod.subprocess, "run", mock_run)
    yield mock_run

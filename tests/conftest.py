"""Test configuration: every test runs with real network access blocked."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*args: object, **kwargs: object) -> None:
        raise NetworkBlockedError("network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The default seeded synthetic dataset, written once per session to a temp directory."""
    from driftgate.generator.population import generate
    from driftgate.generator.writer import write_dataset

    out = tmp_path_factory.mktemp("dataset") / "data"
    write_dataset(generate(), out)
    return out

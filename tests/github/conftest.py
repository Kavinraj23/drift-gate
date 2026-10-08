"""Fixtures for the GitHub adapter tests (doubles live in gh_helpers.py)."""

from __future__ import annotations

import pytest
from gh_helpers import REPO, TOKEN, FakeClock, FakeSession

from driftgate.adapters.github_actions import GitHubActionsSource, GitHubActionsTarget
from driftgate.adapters.github_client import GitHubClient, GitHubConfig


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> GitHubConfig:
    return GitHubConfig(repo=REPO, token=TOKEN)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(config: GitHubConfig, session: FakeSession, clock: FakeClock) -> GitHubClient:
    return GitHubClient(config, session, clock=clock, sleep=clock.sleep)


@pytest.fixture
def source(client: GitHubClient) -> GitHubActionsSource:
    return GitHubActionsSource(client)


@pytest.fixture
def target(client: GitHubClient, clock: FakeClock) -> GitHubActionsTarget:
    return GitHubActionsTarget(client, clock=clock, sleep=clock.sleep, verify_timeout_s=120.0, poll_interval_s=10.0)

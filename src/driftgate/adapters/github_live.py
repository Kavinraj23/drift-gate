"""Composition for human-run live entry points: the playground's source and target from the environment.

Not used by tests or offline targets. It imports no HTTP library; the real session comes from `http_session`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from driftgate.adapters.github_actions import GitHubActionsSource, GitHubActionsTarget
from driftgate.adapters.github_client import GitHubClient, GitHubConfig, load_github_config
from driftgate.adapters.http_session import requests_session


@dataclass
class Playground:
    repo: str
    source: GitHubActionsSource
    target: GitHubActionsTarget
    _token: str

    def scrub(self, text: str) -> str:
        return text.replace(self._token, "[redacted]")


def connect_playground(
    loader: Callable[[], GitHubConfig] = load_github_config,
    session_factory: Callable[[], Any] = requests_session,
) -> Playground:
    """Raises ValueError when the repo or token is not configured (nothing is called)."""
    config = loader()
    client = GitHubClient(config, session_factory())
    return Playground(client.repo, GitHubActionsSource(client), GitHubActionsTarget(client), config.token)

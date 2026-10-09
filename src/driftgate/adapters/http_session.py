"""The one place a real HTTP library is imported. Everything else takes an injected session (see github_client)."""

from __future__ import annotations

from typing import Any


def requests_session() -> Any:
    import requests

    return requests.Session()

"""Thin wrapper over the GitHub REST API - the only module in drift_gate that knows
this is HTTP. GitHubActionsSource/Target talk to this, never to `requests` directly.

Auth: a fine-grained personal access token in GITHUB_TOKEN, scoped to Actions
(read+write) on the one target repo. Raises requests.HTTPError on any non-2xx so
callers don't have to check status codes themselves.
"""
from __future__ import annotations

import os

import requests

API_BASE = "https://api.github.com"


class GitHubClient:
    def __init__(self, owner: str, repo: str, token: str | None = None):
        self.owner = owner
        self.repo = repo
        token = token or os.environ["GITHUB_TOKEN"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _url(self, path: str) -> str:
        return f"{API_BASE}/repos/{self.owner}/{self.repo}{path}"

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(self._url(path), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_all_pages(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        """GETs every page of a paginated list endpoint and concatenates `key`."""
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        out: list[dict] = []
        while True:
            params["page"] = page
            data = self.get(path, params=params)
            items = data[key]
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    def get_text(self, path: str) -> str:
        """Follows GitHub's redirect to the signed log-download URL and returns the
        raw plain-text body."""
        r = self.session.get(self._url(path), timeout=30)
        r.raise_for_status()
        return r.text

    def post(self, path: str, json_body: dict | None = None) -> requests.Response:
        r = self.session.post(self._url(path), json=json_body, timeout=30)
        r.raise_for_status()
        return r

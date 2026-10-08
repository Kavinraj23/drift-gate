"""Signatures cover every error-catalog family and are keyed on catalog text only."""

from __future__ import annotations

import pytest

from driftgate.generator.catalog import CatalogEntry, load_catalog
from driftgate.generator.logs import placeholders
from driftgate.tools.signatures import BY_ID, GENERIC_EXIT_ID, SIGNATURES, UNKNOWN, match_all

CATALOG = load_catalog()
#: catalog entry -> signature id it must produce. Runner scaffolding has no signature of its own.
EXPECTED = {
    **{i: i for i in CATALOG.ids() if i in BY_ID},
    "gha_exit_code": GENERIC_EXIT_ID,
}
SCAFFOLDING = {"gha_group"}


def _render(entry: CatalogEntry) -> str:
    keys = set().union(*(placeholders(t) for t in entry.lines))
    return "\n".join(entry.render({k: "1" for k in keys}))


def test_every_catalog_family_has_a_signature() -> None:
    uncovered = [i for i in CATALOG.ids() if i not in EXPECTED and i not in SCAFFOLDING]
    assert uncovered == []


@pytest.mark.parametrize("entry_id", sorted(EXPECTED))
def test_catalog_entry_matches_its_signature(entry_id: str) -> None:
    text = _render(CATALOG[entry_id])
    ids = [s.id for s in match_all(text)]
    assert EXPECTED[entry_id] in ids
    # No other non-generic family fires on this text.
    assert [i for i in ids if i not in (EXPECTED[entry_id], GENERIC_EXIT_ID)] == []


def test_scaffolding_matches_nothing() -> None:
    assert match_all(_render(CATALOG["gha_group"])) == []


def test_signature_ids_are_catalog_ids_or_generic() -> None:
    assert {s.id for s in SIGNATURES} <= set(CATALOG.ids()) | {GENERIC_EXIT_ID}


def test_only_throttling_and_registry_rate_limit_have_tier0_rules() -> None:
    assert {s.id for s in SIGNATURES if s.tier0_rule} == {"aws_throttling", "docker_pull_rate_limit"}


def test_state_lock_outranks_throttling() -> None:
    text = _render(CATALOG["aws_throttling"]) + "\n" + _render(CATALOG["tf_state_lock"])
    assert match_all(text)[0].id == "tf_state_lock"


def test_unknown_matches_nothing() -> None:
    assert not UNKNOWN.matches("anything at all")
    assert match_all("") == []

"""The history index: BM25 ranking, query safety, redaction, and the no-leakage rule (invariant 16)."""

from __future__ import annotations

import pytest

from driftgate import retrieval
from driftgate.retrieval import FTS5Unavailable, HistoryIndex, IncidentRecord, match_query

T0 = 1_700_000_000.0  # the case under investigation starts here (fake clock: plain numbers, no sleeping)
CASE = "exec-case"


def rec(eid: str, finished_at: float, **kw: str) -> IncidentRecord:
    base = {
        "signature": "tf_state_lock",
        "fingerprint": "fp-" + eid,
        "excerpt": "Error acquiring the state lock ConditionalCheckFailedException",
        "hypothesis": "a stale lock from a dead run",
        "action": "none",
        "outcome": "escalated",
    }
    return IncidentRecord(eid, finished_at, **{**base, **kw})  # type: ignore[arg-type]


def ids(matches: list[retrieval.Match]) -> list[str]:
    return [m.execution_id for m in matches]


def test_returns_top_three_by_bm25_with_outcome() -> None:
    idx = HistoryIndex()
    idx.add(rec("a", T0 - 500, excerpt="state lock held by another run", outcome="verified", action="rerun"))
    idx.add(rec("b", T0 - 400, excerpt="state lock", signature="tf_state_lock"))
    idx.add(rec("c", T0 - 300, excerpt="npm ERR peer dependency conflict", signature="npm_peer_dep"))
    idx.add(rec("d", T0 - 200, excerpt="unrelated docker pull rate limit", signature="docker_rate_limit"))
    idx.add(rec("e", T0 - 100, excerpt="state lock timeout waiting", signature="tf_state_lock"))
    out = idx.query("tf_state_lock state lock", as_of=T0, case_id=CASE)
    assert len(out) == 3
    assert set(ids(out)) <= {"a", "b", "e"}
    assert all(m.score <= 0 for m in out)
    assert [m.score for m in out] == sorted(m.score for m in out)  # best (most negative) first
    best = next(m for m in out if m.execution_id == "a")
    assert best.outcome == "verified" and best.action == "rerun"


def test_no_match_and_empty_query_return_nothing() -> None:
    idx = HistoryIndex()
    idx.add(rec("a", T0 - 5))
    assert idx.query("zzzzqqqq", as_of=T0, case_id=CASE) == []
    assert idx.query("", as_of=T0, case_id=CASE) == []
    assert idx.query("!!! ?? -- ()", as_of=T0, case_id=CASE) == []


def _leak_index() -> HistoryIndex:
    idx = HistoryIndex()
    idx.add(rec("before", T0 - 1, excerpt="state lock before"))
    idx.add(rec("long-before", T0 - 10_000, excerpt="state lock long before"))
    idx.add(rec("equal", T0, excerpt="state lock equal to the case start"))
    idx.add(rec("after", T0 + 1, excerpt="state lock after"))
    idx.add(rec("far-after", T0 + 10_000, excerpt="state lock far after"))
    idx.add(rec(CASE, T0 - 50, excerpt="state lock the case itself, finished earlier than its own start?"))
    return idx


QUERIES = [
    "state lock",
    "state lock before equal after far-after exec-case",
    # adversarial: text that tries to be FTS5 syntax or to widen the result set
    'state OR "after" OR *',
    "NEAR(state lock, 5) OR after",
    "excerpt:after OR signature:tf_state_lock",
    '"state lock" NOT before',
    "state* lock*",
    '") OR (1=1) --',
    "state' OR 'a'='a",
    "AND OR NOT NEAR",
    "- state + lock ^ after",
    "{excerpt}: after",
    "state\x00lock after",
]


@pytest.mark.parametrize("query", QUERIES)
def test_only_strictly_earlier_non_self_incidents_can_be_returned(query: str) -> None:
    idx = _leak_index()
    out = idx.query(query, as_of=T0, case_id=CASE)
    assert set(ids(out)) <= {"before", "long-before"}


def test_incident_finishing_at_the_case_start_is_excluded_and_one_second_earlier_is_not() -> None:
    idx = HistoryIndex()
    idx.add(rec("equal", T0))
    idx.add(rec("just-before", T0 - 0.001))
    assert ids(idx.query("state lock", as_of=T0, case_id=CASE)) == ["just-before"]


def test_the_case_itself_is_excluded_even_when_it_finished_before_it_started() -> None:
    idx = HistoryIndex()
    idx.add(rec(CASE, T0 - 100))
    assert idx.query("state lock", as_of=T0, case_id=CASE) == []


def test_the_future_never_appears_whatever_the_limit() -> None:
    idx = _leak_index()
    out = idx.query("state lock", as_of=T0 - 5_000, case_id=CASE, limit=99)
    assert out == [] or set(ids(out)) <= {"long-before"}
    assert ids(idx.query("state lock", as_of=T0 - 20_000, case_id=CASE, limit=99)) == []


def test_index_rows_have_no_label_or_ground_truth_field() -> None:
    fields = set(IncidentRecord.__dataclass_fields__)
    assert fields == {
        "execution_id",
        "finished_at",
        "signature",
        "fingerprint",
        "excerpt",
        "hypothesis",
        "action",
        "outcome",
    }
    idx = HistoryIndex()
    cols = {r[1] for r in idx._con.execute("PRAGMA table_info(incident_text)")}
    cols |= {r[1] for r in idx._con.execute("PRAGMA table_info(incident_meta)")}
    assert not any(w in c for c in cols for w in ("label", "truth", "disposition", "correct", "holdout"))


@pytest.mark.parametrize("text", QUERIES)
def test_match_query_only_emits_quoted_alphanumeric_terms(text: str) -> None:
    expr = match_query(text)
    if not expr:
        return
    for term in expr.split(" OR "):
        assert term.startswith('"') and term.endswith('"')
        assert term[1:-1].isalnum()


def test_match_query_is_bounded_and_deduplicated() -> None:
    expr = match_query(" ".join(f"term{i}" for i in range(500)) + " state state state")
    assert len(expr.split(" OR ")) == retrieval.MAX_QUERY_TERMS
    assert match_query("lock lock LOCK").count("lock") == 1
    assert match_query("ab x") == ""  # too short to be useful


def test_text_is_redacted_and_clipped_on_the_way_in() -> None:
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    idx = HistoryIndex()
    idx.add(rec("a", T0 - 5, excerpt=f"state lock access key {key} " + "x" * 5000))
    (m,) = idx.query("state lock", as_of=T0, case_id=CASE)
    assert key not in m.excerpt
    assert len(m.excerpt) <= retrieval.MAX_FIELD_CHARS


def test_adding_the_same_execution_again_replaces_it() -> None:
    idx = HistoryIndex()
    idx.add(rec("a", T0 - 5, outcome="escalated"))
    idx.add(rec("a", T0 - 5, outcome="verified"))
    assert len(idx) == 1
    (m,) = idx.query("state lock", as_of=T0, case_id=CASE)
    assert m.outcome == "verified"


def test_index_persists_on_disk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "sub" / "history.sqlite"
    first = HistoryIndex(path)
    first.add(rec("a", T0 - 5))
    first.close()
    again = HistoryIndex(path)
    assert ids(again.query("state lock", as_of=T0, case_id=CASE)) == ["a"]


def test_missing_fts5_fails_with_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "fts5_available", lambda: False)
    with pytest.raises(FTS5Unavailable, match="FTS5"):
        HistoryIndex()


def test_fts5_is_available_in_this_build() -> None:
    assert retrieval.fts5_available()

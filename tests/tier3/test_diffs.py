"""Structured diffs: parsing, applying, and the deterministic pre-checks (diff validator)."""

from __future__ import annotations

from pathlib import Path

import pytest

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.diffs import DiffError, apply_diff, make_unified_diff, parse_diff
from driftgate.domain import FileContent, SourceError
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier3_scripts import BAD_KINDS, bad_diff
from driftgate.tier3 import Tier3Limits, check_diff, find_secret_literals, is_protected, path_allowed_for

GOOD = "alpha\nbeta\ngamma\n"


class MemSource:
    """Minimal ExecutionSource for unit cases: only read_file is used by the validator."""

    def __init__(self, files: dict[str, str], max_len: int | None = None) -> None:
        self.files, self.max_len = files, max_len

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
        if path not in self.files:
            raise SourceError(f"no such file: {path}")
        text = self.files[path]
        return FileContent(repo, path, ref, text[:max_bytes], len(text) > max_bytes)


def check(diff: str, files: dict[str, str], action: str = "fix_requirement_pin", declared=None, **kw):  # type: ignore[no-untyped-def]
    paths = declared if declared is not None else list(files)
    return check_diff(
        diff,
        action=action,
        declared_paths=paths,
        source=MemSource(files),
        repo="r",
        ref="main",
        **kw,  # type: ignore[arg-type]
    )


# --- parse / apply -------------------------------------------------------------------------------------------
def test_every_labelled_fix_diff_parses_and_applies(dataset_dir: Path, t3_labels: list[FailureLabel]) -> None:
    src = SyntheticSource(dataset_dir)
    for label in t3_labels:
        ex = src.get_execution(label.execution_id)
        diff = parse_diff(label.fix_diff or "")
        got = apply_diff(diff, lambda p, ex=ex: src.read_file(ex.pipeline, p, ex.refs["commit"], 10**6).content)
        assert tuple(got) == label.fix_paths and all(isinstance(v, str) for v in got.values())


def test_structured_diff_exposes_files_hunks_and_counts() -> None:
    d = parse_diff(make_unified_diff({"a.txt": GOOD}, {"a.txt": "alpha\nBETA\ngamma\ndelta\n"}))
    assert d.paths == ("a.txt",) and (d.added, d.removed) == (2, 1)
    f = d.files[0]
    assert f.hunks[0].old_start == 1 and not f.is_new and not f.is_deleted
    as_dict = d.to_dict()
    assert as_dict["files"][0]["path"] == "a.txt" and as_dict["files"][0]["hunks"][0]["lines"]


def test_new_file_and_delete_are_recognised() -> None:
    new = parse_diff(make_unified_diff({}, {"n.txt": "x\n"}))
    assert new.files[0].is_new and apply_diff(new, lambda p: None) == {"n.txt": "x\n"}
    gone = parse_diff(make_unified_diff({"d.txt": "x\ny\n"}, {}))
    assert gone.files[0].is_deleted and apply_diff(gone, lambda p: "x\ny\n") == {"d.txt": None}


def test_apply_tolerates_line_offset_but_not_a_mismatch() -> None:
    text = make_unified_diff({"a.txt": GOOD}, {"a.txt": "alpha\nBETA\ngamma\n"})
    shifted = "zero\none\n" + GOOD
    assert apply_diff(parse_diff(text), lambda p: shifted)["a.txt"] == "zero\none\nalpha\nBETA\ngamma\n"
    with pytest.raises(DiffError, match="does not match"):
        apply_diff(parse_diff(text), lambda p: "something\nelse\n")


def test_ambiguous_offset_is_refused() -> None:
    text = make_unified_diff({"a.txt": "x\n"}, {"a.txt": "y\n"})
    with pytest.raises(DiffError, match="several places"):
        apply_diff(parse_diff(text), lambda p: "q\nx\nq\nx\n")


def test_header_counts_are_recomputed_and_blank_lines_are_context() -> None:
    # the trailing blank line stands for a blank context line
    text = "--- a/a.txt\n+++ b/a.txt\n@@ -1,99 +1,99 @@\n alpha\n-beta\n+BETA\n\n"
    d = parse_diff(text)
    assert (d.files[0].hunks[0].old_count, d.files[0].hunks[0].new_count) == (3, 3)
    assert apply_diff(d, lambda p: "alpha\nbeta\n\n")["a.txt"] == "alpha\nBETA\n\n"


@pytest.mark.parametrize(
    "text",
    ["", "   \n", "not a diff at all", "--- a/x\n", "--- a/x\n+++ b/x\n", "--- a/x\n+++ b/x\n@@ nonsense @@\n"],
)
def test_garbage_is_a_diff_error(text: str) -> None:
    with pytest.raises(DiffError):
        parse_diff(text)


# --- validator: the labelled correct fixes and the seeded bad diffs -------------------------------------------
def _check_label(dataset_dir: Path, label: FailureLabel, text: str, paths):  # type: ignore[no-untyped-def]
    src = SyntheticSource(dataset_dir)
    ex = src.get_execution(label.execution_id)
    return check_diff(
        text,
        action=label.correct_action or "",
        declared_paths=paths,
        source=src,
        repo=ex.pipeline,
        ref=ex.refs["commit"],
    )


def test_correct_diffs_pass_every_check(dataset_dir: Path, t3_labels: list[FailureLabel]) -> None:
    for label in t3_labels:
        res = _check_label(dataset_dir, label, label.fix_diff or "", label.fix_paths)
        assert res.ok, (label.execution_id, res.reason)
        assert res.diff is not None and tuple(res.new_contents) == label.fix_paths


#: Kinds the deterministic checks must stop on their own, with the violation code they must name.
VALIDATOR_KINDS = {
    "delete_fix_file": "deletes_file",
    "secret_literal": "secret_literal",
    "huge": "too_large",
    "subtractive_first": "subtractive_before_additive",
}


@pytest.mark.parametrize("kind", sorted(VALIDATOR_KINDS))
def test_validator_catches_its_seeded_bad_diffs(dataset_dir: Path, t3_labels: list[FailureLabel], kind: str) -> None:
    src = SyntheticSource(dataset_dir)
    seen = 0
    for label in t3_labels:
        seeded = bad_diff(kind, label, src)
        if seeded is None:
            continue
        seen += 1
        res = _check_label(dataset_dir, label, seeded.text, seeded.paths)
        assert VALIDATOR_KINDS[kind] in res.codes, (label.fault_id, kind, res.reason)
    assert seen >= 3


def test_workflow_edit_is_unrelated_unless_the_action_changes_workflows(
    dataset_dir: Path, t3_labels: list[FailureLabel]
) -> None:
    src = SyntheticSource(dataset_dir)
    for label in t3_labels:
        seeded = bad_diff("unrelated_workflow", label, src)
        assert seeded is not None
        res = _check_label(dataset_dir, label, seeded.text, seeded.paths)
        if label.correct_action == "fix_undefined_variable":  # workflows are in scope there: only the reviewer can tell
            assert res.ok
        else:
            assert "unrelated_path" in res.codes


def test_semantic_bad_diffs_pass_the_validator_so_the_reviewer_is_the_control(
    dataset_dir: Path, t3_labels: list[FailureLabel]
) -> None:
    src = SyntheticSource(dataset_dir)
    for kind in ("wrong_file", "over_broad"):
        for label in t3_labels:
            seeded = bad_diff(kind, label, src)
            assert seeded is not None, (kind, label.fault_id)
            assert _check_label(dataset_dir, label, seeded.text, seeded.paths).ok, (kind, label.fault_id)


def test_every_bad_kind_applies_to_most_scenarios(dataset_dir: Path, t3_labels: list[FailureLabel]) -> None:
    src = SyntheticSource(dataset_dir)
    for kind in BAD_KINDS:
        assert sum(bad_diff(kind, lb, src) is not None for lb in t3_labels) >= 6, kind


# --- validator: unit cases -----------------------------------------------------------------------------------
def test_empty_and_unparseable_diffs() -> None:
    assert check("", {"requirements.txt": GOOD}).codes == ["no_diff"]
    assert check("garbage", {"requirements.txt": GOOD}).codes == ["parse_error"]


def test_undeclared_path_is_a_violation() -> None:
    text = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": "alpha\nBETA\ngamma\n"})
    assert check(text, {"requirements.txt": GOOD}, declared=["other.txt"]).codes[0] == "undeclared_path"
    assert check(text, {"requirements.txt": GOOD}).ok


@pytest.mark.parametrize(
    "path",
    [".env", "app/.env.production", "deploy/server.pem", "config/kill_switch.json", "credentials.json",
     "infra/secrets/prod.tfvars", "terraform.tfstate", ".git/config"],
)  # fmt: skip
def test_protected_paths_are_never_touchable(path: str) -> None:
    text = make_unified_diff({path: GOOD}, {path: "alpha\nBETA\ngamma\n"})
    res = check(text, {path: GOOD}, action="fix_undefined_variable")
    assert "protected_path" in res.codes and is_protected(path)
    assert res.new_contents == {}  # protected files are not even read


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "a/../../b.txt", "C:/x/y.txt"])
def test_unsafe_paths(path: str) -> None:
    text = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    assert "unsafe_path" in check(text, {}).codes


def test_rename_and_delete_are_violations() -> None:
    rename = "--- a/requirements.txt\n+++ b/requirements-old.txt\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    assert "rename" in check(rename, {"requirements.txt": "x\n"}).codes
    gone = make_unified_diff({"requirements.txt": GOOD}, {})
    assert "deletes_file" in check(gone, {"requirements.txt": GOOD}).codes


def test_caps_on_files_and_changed_lines() -> None:
    files = {f"requirements{i}.txt": GOOD for i in range(5)}
    text = make_unified_diff(files, {k: "alpha\nBETA\ngamma\n" for k in files})
    assert "too_many_files" in check(text, files).codes
    assert check(text, files, limits=Tier3Limits(max_files=5)).ok
    big = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": GOOD + "x\n" * 61})
    assert "too_large" in check(big, {"requirements.txt": GOOD}).codes
    assert check(big, {"requirements.txt": GOOD}, limits=Tier3Limits(max_changed_lines=61)).ok


def test_diff_that_does_not_apply_or_changes_nothing() -> None:
    text = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": "alpha\nBETA\ngamma\n"})
    assert check(text, {"requirements.txt": "different\ncontent\n"}).codes == ["does_not_apply"]
    assert check(text, {}, declared=["requirements.txt"]).codes == ["does_not_apply"]  # base file missing
    noop = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1,1 +1,1 @@\n-alpha\n+alpha\n"
    assert check(noop, {"requirements.txt": GOOD}).codes == ["no_op"]


def test_oversized_base_file_cannot_be_verified() -> None:
    from driftgate import tier3

    big = "x\n" * (tier3.MAX_BASE_BYTES // 2 + 10)
    text = make_unified_diff({"requirements.txt": big[:20]}, {"requirements.txt": big[:20] + "y\n"})
    res = check(text, {"requirements.txt": big})
    assert "does_not_apply" in res.codes and "too large" in res.reason


@pytest.mark.parametrize(
    "line",
    [
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----",
        'password = "hunter2hunter2hunter2"',
        "api_key: sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_secret_literals_are_detected_without_echoing_them(line: str) -> None:
    text = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": GOOD + line + "\n"})
    res = check(text, {"requirements.txt": GOOD})
    assert "secret_literal" in res.codes
    assert line not in res.reason and "AKIA" not in res.reason


def test_references_to_secrets_are_not_literals() -> None:
    assert find_secret_literals(["token: ${{ secrets.GITHUB_TOKEN }}", "password = var.db_password"]) == []


# Secret-shaped test values are assembled from parts so this file holds no literal that scanners would flag.
_RAND = "Zq8" + "Xv2Lm9" + "Rt4Pw7Kc1" + "Hn5Bd3Gs6Yj0"  # 30 random-looking characters
BROAD_SECRET_LINES = [
    "SLACK_BOT = " + "xox" + "b-1234567890-abcdefghijkl",
    "url: https://hooks." + "slack.com/services/T0000000/B0000000/" + "abcdefghijklmnopqrstuvwx",
    "stripe = " + "sk_" + "live_" + "abcdefghijklmnop1234",
    "restricted = " + "rk_" + "live_" + "abcdefghijklmnop1234",
    "maps = " + "AI" + "za" + "A" * 35,
    "token = " + "glp" + "at-" + "abcdefghijklmnopqrst12",
    "pat = " + "github_" + "pat_" + "abcdefghijklmnopqrst12",
    "jwt = " + "ey" + "J" + "abcdefghij.abcdefghij.abcdefghij",
    "DATABASE_URL=postgres://app:" + "s3cret" + "@db.internal:5432/app",
    'password = "p@ssw0rd!x"',
    "client_secret_blob: " + _RAND,
]


@pytest.mark.parametrize("line", BROAD_SECRET_LINES)
def test_broad_secret_shapes_are_flagged_without_echo(line: str) -> None:
    assert find_secret_literals([line])
    text = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": GOOD + line + "\n"})
    res = check(text, {"requirements.txt": GOOD})
    assert "secret_literal" in res.codes
    secret_part = line.split("= ", 1)[-1].split(": ", 1)[-1]
    assert secret_part not in res.reason and line not in res.reason


@pytest.mark.parametrize(
    "line",
    [
        "token: ${{ secrets.GITHUB_TOKEN }}",
        "      GH_TOKEN: ${{ secrets.GH_TOKEN }}",
        "password = var.db_password",
        "password = local.db_password",
        "password = data.aws_secretsmanager_secret_version.db.secret_string",
        "api_key: ${{ env.API_KEY }}",
        'password = "${var.db_password}"',
        "api_key=$API_KEY",
        "token: ${GITHUB_TOKEN}",
        "secret: secrets.MY_SECRET",
        "url: https://user:${{ secrets.PW }}@host/repo",
        "run: curl -H 'Authorization: Bearer ${{ secrets.TOKEN }}' https://example.com",
        "requests==2.31.0",
        'resource "aws_s3_bucket" "logs" {',
        "      - uses: actions/checkout@v4",
        'variable "db_password" {',
    ],
)
def test_benign_references_and_normal_code_are_not_flagged(line: str) -> None:
    assert find_secret_literals([line]) == []


def test_literal_secret_on_a_line_with_a_reference_is_still_flagged() -> None:
    lit = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    assert find_secret_literals([f"token: ${{{{ secrets.X }}}} # {lit}"])
    assert find_secret_literals(["password = var.x; backup_password = " + '"p@ssw0rd!x"'])


_GH = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
_AWS = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "line",
    [
        "token: ${TOKEN:-" + _GH + "}",
        "token: ${{ '" + _GH + "' }}",
        "run: $(echo " + _AWS + ")",
        "password = ${X}hunter2hunter2",
        "password: ${{ secrets.A }}hunter2hunter2",
        "url: https://u:${{ secrets.A }}literalpw@host",
        "url: https://u:${PW}literalpw@host",
        "token: secrets.A " + _GH,
    ],
)
def test_literals_hidden_in_or_next_to_references_are_flagged(line: str) -> None:
    assert find_secret_literals([line])


@pytest.mark.parametrize(
    "line",
    [
        "token: ${{ secrets.X }}",
        "token: ${{secrets.X}}",
        "password: '${{ secrets.DB_PASSWORD }}'",
        "password = var.db_password",
        "password = local.db.password",
        "password = data.a.b.c",
        "secret = secrets.MY_SECRET",
        "api_key: env.API_KEY",
        "api_key=$API_KEY",
        "token: ${GITHUB_TOKEN}",
        'password = "${var.db_password}"',
        'password = os.environ["DB_PASSWORD"]',
        "token = process.env.NPM_TOKEN",
        "url: https://user:${{ secrets.PW }}@host/repo",
    ],
)
def test_whole_value_references_are_not_flagged(line: str) -> None:
    assert find_secret_literals([line]) == []


def test_a_password_written_exactly_as_an_attribute_path_is_treated_as_a_reference() -> None:
    # Pinned residual risk: `data.hunter2hunter2` cannot be told apart from a Terraform data reference, so it is
    # allowed. Anything else in the value (a prefix, a suffix, quotes inside) is checked as written.
    assert find_secret_literals(["password = data.hunter2hunter2"]) == []
    assert find_secret_literals(["password = data.hunter2hunter2x!"])


def test_a_reference_containing_a_vendor_token_is_never_a_reference() -> None:
    assert find_secret_literals(["token: secrets." + _GH])
    assert find_secret_literals(["password = var." + _AWS])


@pytest.mark.parametrize(
    "line",
    [
        "password: [REDACTED]hunter2hunter2",
        "run: tool --token [REDACTED]abcdefghijklmnop",
        'password = "[REDACTED]p@ssw0rd!x"',
    ],
)
def test_placeholder_text_in_an_added_line_is_rejected(line: str) -> None:
    assert find_secret_literals([line])


def test_lines_without_the_placeholder_still_pass() -> None:
    assert find_secret_literals(["password: ${{ secrets.DB }}", "name: build"]) == []


def test_json_password_shape_is_flagged() -> None:
    assert find_secret_literals(['  "password": "x9Kd02mQ"'])
    assert find_secret_literals(['{"db": {"password": "hunter2hunter2hunter2"}}'])
    assert find_secret_literals(['  "password": "${{ secrets.DB }}"']) == []


def test_over_long_added_lines_are_rejected_fail_closed() -> None:
    from driftgate.tier3 import MAX_SCAN_LINE

    long_line = "x = " + "a" * MAX_SCAN_LINE
    assert find_secret_literals([long_line]) == ["line too long to scan"]
    assert find_secret_literals(["x = " + "a" * (MAX_SCAN_LINE - 10)]) == []
    text = make_unified_diff({"requirements.txt": GOOD}, {"requirements.txt": GOOD + long_line + "\n"})
    assert "secret_literal" in check(text, {"requirements.txt": GOOD}).codes


def test_undefined_variable_scenario_diffs_pass_the_secret_check(
    dataset_dir: Path, t3_labels: list[FailureLabel]
) -> None:
    labels = [lb for lb in t3_labels if lb.correct_action == "fix_undefined_variable"]
    assert labels
    for label in labels:
        res = _check_label(dataset_dir, label, label.fix_diff or "", label.fix_paths)
        assert "secret_literal" not in res.codes and res.ok, (label.execution_id, res.reason)


def test_secret_scope_fix_diff_passes_the_secret_check() -> None:
    # The synthetic dataset has no fix_secret_scope scenario, so this is the typical shape: pass the secret through
    # a scoped reference instead of a literal.
    path = ".github/workflows/ci.yml"
    old = "steps:\n  - run: ./deploy.sh\n"
    env = "    env:\n      DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n      password: ${{ secrets.DB_PASSWORD }}\n"
    res = check(make_unified_diff({path: old}, {path: old + env}), {path: old}, action="fix_secret_scope")
    assert res.ok, res.reason


def test_additive_before_subtractive_for_multi_file_changes() -> None:
    files = {"main.tf": "a\nb\nc\n", "variables.tf": "x\n"}
    shrink = ("main.tf", "a\nb\nc\n", "a\nc\n")
    grow = ("variables.tf", "x\n", "x\ny\n")

    def diff(*order: tuple[str, str, str]) -> str:
        return "".join(make_unified_diff({p: a}, {p: b}) for p, a, b in order)

    bad = check(diff(shrink, grow), files, action="fix_undefined_variable")
    assert bad.codes == ["subtractive_before_additive"]
    assert check(diff(grow, shrink), files, action="fix_undefined_variable").ok


def test_protected_and_pattern_helpers() -> None:
    assert path_allowed_for("regenerate_lockfile", "pkg/package-lock.json")
    assert not path_allowed_for("regenerate_lockfile", ".github/workflows/ci.yml")
    assert path_allowed_for("revert_template_bump", ".github/workflows/plan.yml")
    assert not path_allowed_for("not_an_action", "package-lock.json")

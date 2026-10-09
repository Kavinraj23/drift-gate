"""Credential redaction for everything that can reach a model, a pull request or the audit log.

Hand-written patterns, no dependencies. Three layers, applied in order:

1. specific token shapes (cloud/vendor prefixes, JWTs, PEM blocks, URL credentials, auth headers);
2. generic `key=value` / `key: value` / `"key": "value"` forms where the key NAME ends in a credential word;
3. a conservative entropy heuristic for long random-looking values next to a key name that merely CONTAINS a
   credential word (`TOKEN_VALUE=...`, `secret_blob: ...`).

AWS ARNs are shielded while the patterns run (`...:secret:my-db-AbCdEf` is a resource name, not a secret).
`redact` is idempotent, so it is safe to apply at several boundaries.
"""

from __future__ import annotations

import math
import re
from collections import Counter

REDACTED = "[REDACTED]"

# Key names whose value is a credential. The name must END with the keyword, so `secretsmanager:Get...` is safe.
KEYWORDS = (
    r"(?:password|passwd|passphrase|secret|token|credentials?|api[_-]?key|access[_-]?key|account[_-]?key|"
    r"(?:secret|private|signing|encryption|session)[_-]?key)"
)
_NOT_YET = r"(?!\[REDACTED\])"
_VALUE = r"[^\s\"',;}&]+"  # an unquoted value stops at whitespace, quotes, separators and `&` (URL query)
_SP = r"[ \t]*"
_PFX = r"(?<![A-Za-z0-9])"  # token prefixes must not be glued to a preceding word

ARN_RE = re.compile(r"\barn:aws[\w-]*:[\w-]+:[\w-]*:\d*:[^\s\"',;)\]}>]+")

REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM / OpenSSH / PGP private key blocks, whole; and an unterminated block (truncated log) to the end.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----.*?-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----", re.S),
        REDACTED,
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----(?:(?!-----END).)*\Z", re.S), REDACTED),
    # A bare 40-character AWS secret access key right after its access key id (before the id is redacted).
    (
        re.compile(r"(\b(?:AKIA|ASIA)[A-Z0-9]{16}\b[\s,:;'\"=]+)(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
        r"\1" + REDACTED,
    ),
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"), REDACTED),
    # Vendor token shapes.
    (re.compile(_PFX + r"gh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(_PFX + r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(_PFX + r"glpat-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(_PFX + r"sk-ant-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(_PFX + r"sk-(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"), REDACTED),
    (re.compile(_PFX + r"xox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(_PFX + r"xapp-\d-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"(hooks\.slack\.com/services/)[A-Za-z0-9/]{20,}"), r"\1" + REDACTED),
    (re.compile(_PFX + r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(_PFX + r"whsec_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(_PFX + r"AIza[0-9A-Za-z_-]{35}"), REDACTED),
    (re.compile(_PFX + r"ya29\.[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(_PFX + r"GOCSPX-[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(_PFX + r"npm_[A-Za-z0-9]{36}"), REDACTED),
    (re.compile(_PFX + r"pypi-[A-Za-z0-9_-]{50,}"), REDACTED),
    (re.compile(_PFX + r"dckr_pat_[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(_PFX + r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(_PFX + r"[A-Za-z0-9]{14}\.atlasv1\.[A-Za-z0-9_-]{40,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # Request signatures (AWS SigV4 query/header form, Azure SAS `sig=`).
    (re.compile(r"(?i)(\bSignature=)[0-9a-f]{32,}"), r"\1" + REDACTED),
    (re.compile(r"([?&;]sig=)[A-Za-z0-9%+/=_-]{16,}"), r"\1" + REDACTED),
    # Authorization headers and bare Bearer/Basic credentials.
    (
        re.compile(
            r"(?i)(\bAuthorization" + _SP + r"[:=]" + _SP + r"(?:token|bearer|basic)[ \t]+)" + _NOT_YET + r"[^\s\"',;]+"
        ),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(?i)\b(Bearer|Basic)[ \t]+" + _NOT_YET + r"[A-Za-z0-9._~+/=-]{12,}"), r"\1 " + REDACTED),
    # user:password@host in any URL (including an empty user: redis://:pw@host).
    (re.compile(r"(://[^/\s:@]*:)[^/\s@]+(@)"), r"\1" + REDACTED + r"\2"),
    # CLI credentials: curl -u user:pass, docker login -p pw, mysql -ppw.
    (re.compile(r"(?i)(\bcurl\b[^\n]*?\s(?:-u|--user)[ \t]+[^\s:]+:)" + _NOT_YET + r"[^\s\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\bdocker\s+login\b[^\n]*?\s-p[ \t]+)" + _NOT_YET + r"[^\s\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\bmysql(?:dump)?\b[^\n]*?\s-p)(?=\S)" + _NOT_YET + r"[^\s\"']+"), r"\1" + REDACTED),
    # CLI flag form: --password hunter2, --token x
    (re.compile(r"(?i)(\s--[\w-]*?" + KEYWORDS + r"[ \t]+)(?![-<])" + _NOT_YET + _VALUE), r"\1" + REDACTED),
    # npm / docker base64 auth: `_auth=...`, "auth": "..."
    (
        re.compile(
            r"(?i)((?:\b_auth|\"auth\"|'auth')[\"']?"
            + _SP
            + r"[:=]"
            + _SP
            + r"[\"']?)"
            + _NOT_YET
            + r"[A-Za-z0-9+/=]{8,}"
        ),
        r"\1" + REDACTED,
    ),
    # key: "quoted value" (may contain spaces), with any prefix on the key name.
    (
        re.compile(
            r"(?i)(\b[\w.-]*" + KEYWORDS + r"[\"']?" + _SP + r"[:=]" + _SP + r")([\"'])(?!\[REDACTED\]\2)(.+?)(?<!\\)\2"
        ),
        r"\1\2" + REDACTED + r"\2",
    ),
    # key=value, key: value (NPM_TOKEN, :_authToken, X-API-Key, ...).
    (
        re.compile(r"(?i)(\b[\w.-]*" + KEYWORDS + r"[\"']?" + _SP + r"[:=]" + _SP + r"[\"']?)" + _NOT_YET + _VALUE),
        r"\1" + REDACTED,
    ),
    # Connection-string `Pwd=` / `DB_PWD=`; never the shell's own `PWD=/some/dir`.
    (
        re.compile(r"(?i)(\b[\w.-]*pwd" + _SP + r"=" + _SP + r")(?![/~\\]|[A-Za-z]:[\\/])" + _NOT_YET + _VALUE),
        r"\1" + REDACTED,
    ),
)

# Key names that merely CONTAIN a credential word; their long random-looking values are redacted.
_LOOSE_KEY = re.compile(
    r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|credential|api[_-]?key|access[_-]?key|private[_-]?key|auth)"
    r"[\w.-]*[\"']?" + _SP + r"[:=]" + _SP + r"[\"']?)([A-Za-z0-9+/_=-]{24,})"
)
MIN_ENTROPY_BITS = 3.5


def shannon_entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def looks_random(s: str) -> bool:
    """Long, mixed letters and digits, and enough character diversity to be a key rather than a word or number."""
    return (
        len(s) >= 24
        and any(c.isdigit() for c in s)
        and any(c.isalpha() for c in s)
        and shannon_entropy(s) >= MIN_ENTROPY_BITS
    )


def _loose(m: re.Match[str]) -> str:
    return m.group(1) + REDACTED if looks_random(m.group(2)) else m.group(0)


def redact(text: str) -> str:
    shielded: list[str] = []

    def shield(m: re.Match[str]) -> str:
        shielded.append(m.group(0))
        return f"\x00{len(shielded) - 1}\x00"

    text = ARN_RE.sub(shield, text)
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    text = _LOOSE_KEY.sub(_loose, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: shielded[int(m.group(1))], text)


_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----")


def redact_lines(lines: list[str]) -> list[str]:
    """Line-wise redaction that also removes a private key block spread over several lines."""
    out: list[str] = []
    in_pem = False
    for line in lines:
        if in_pem:
            if _PEM_END.search(line):
                in_pem = False
            continue  # key material (and the END marker line) is dropped
        if _PEM_BEGIN.search(line) and not _PEM_END.search(line):
            in_pem = True
        out.append(redact(line))
    return out

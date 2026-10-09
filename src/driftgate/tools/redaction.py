"""Compatibility re-export: the redaction module lives at `driftgate.redaction` (neutral, importable by audit)."""

from driftgate.redaction import ARN_RE as ARN_RE
from driftgate.redaction import REDACTED as REDACTED
from driftgate.redaction import redact as redact
from driftgate.redaction import redact_lines as redact_lines
from driftgate.redaction import redact_value as redact_value

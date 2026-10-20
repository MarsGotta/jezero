"""Secret redaction, applied before anything is summarised, stored or indexed.

This is defence in depth, not a guarantee: it catches common credential shapes. Sources
known to carry secrets should also be filtered at the connector level.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_BUILTIN: tuple[tuple[str, str], ...] = (
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
    (
        "assignment",
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|auth)\b"
        r"(\s*[:=]\s*)(?:\"[^\"\n]{6,}\"|'[^'\n]{6,}'|[^\s\"',;]{6,})",
    ),
    ("url_credentials", r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
)


@dataclass(frozen=True, slots=True)
class Redaction:
    text: str
    count: int


class Redactor:
    def __init__(self, extra_patterns: Iterable[str] = (), *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._patterns = [(kind, re.compile(p)) for kind, p in _BUILTIN]
        self._patterns += [("custom", re.compile(p)) for p in extra_patterns]

    def redact(self, text: str) -> Redaction:
        if not self.enabled or not text:
            return Redaction(text, 0)
        total = 0
        for kind, pattern in self._patterns:
            if kind == "assignment":
                text, n = pattern.subn(_keep_key, text)
            else:
                text, n = pattern.subn(f"[REDACTED:{kind}]", text)
            total += n
        return Redaction(text, total)


def _keep_key(match: re.Match[str]) -> str:
    """For `key = value` keep the key and separator so the text stays readable."""
    whole = match.group(0)
    sep = match.group(1)
    key = whole[: whole.index(sep)]
    return f"{key}{sep}[REDACTED:assignment]"

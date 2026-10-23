"""Secret redaction, applied before anything is summarised, stored or indexed.

This is defence in depth, not a guarantee: it catches common credential shapes. Sources
known to carry secrets should also be filtered at the connector level.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Key names that hold a secret: `GITHUB_TOKEN`, `db_password`, `clientSecret`, `DB_PASS`.
# Bare `pass`, `auth`, `pwd` or `credentials` are left alone: in sessions they are test
# results ("3 tests PASS"), `OAuth`, the working directory and `persist-credentials`.
_SENSITIVE_NAME = (
    r"(?:(?:[\w-]*[_-]|access|refresh|bearer|client|session|private)?"
    r"(?:api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|secret|token|"
    r"password|passwd)"
    r"|[\w-]*[_-](?:pass|pwd|auth)"
    r"|(?-i:[A-Z][A-Z0-9]*(?:PASSWORD|PASSWD|SECRET|TOKEN)))"
)
# A value worth hiding: not a reference ($VAR, $(cmd), <placeholder>, os.environ...), not
# a path or a pattern, not a boolean.
_NOT_A_VALUE = (
    r"(?![$<{%/\\(\[.*^|?+~`]|\[REDACTED|os\.|process\.env|getenv"
    r"|(?:true|false|null|none|yes|no)\b"
    r"|[\d.*+/x-]+(?:[\s\"',;`)\]}]|$)"
    # code, not a value: a call, an attribute, a type name
    r"|[A-Za-z_][\w.]*\(|self\.|this\."
    r"|[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+(?![\w.\-/+=])"
    r"|(?:string|number|boolean|str|int|bool|bytes|undefined)\b)"
)
_SECRET_VALUE = (
    r"(?:\"" + _NOT_A_VALUE + r"(?!\s)[^\"\n]{6,}\"|'" + _NOT_A_VALUE + r"(?!\s)[^'\n]{6,}'"
    r"|" + _NOT_A_VALUE + r"[^\s\"',;`)\]}]{6,})"
)

# Passwords are often all digits (`DB_PASSWORD=93847561029384`) or dotted words
# (`password=correct.horse.battery`), shapes the general rule keeps because they look like
# numbers or attribute access. For password names only, those count as values; code that
# reads the password from somewhere (`password=config.db_password`, `self.x`) is kept.
_PW_NAME = (
    r"(?:(?:[\w-]*[_-])?(?:password|passwd)"
    r"|[\w-]*[_-](?:pass|pwd)"
    r"|(?-i:[A-Z][A-Z0-9]*(?:PASSWORD|PASSWD)))"
)
# Spanish names (`contraseña`, `db_clave`, `palabra-clave`) are also ordinary words: with
# a bare `:` and one plain word after it (`La contraseña: incorrecta`) they are prose.
_ES_PW_NAME = r"(?:(?:[\w-]*[_-])?contrase(?:ñ|n)a|[\w-]*[_-]clave)"
_CODE_OBJECTS = (
    r"(?:self|this|os|sys|config|cfg|conf|settings|options|opts|args|kwargs|params|env|"
    r"environ|request|req|ctx|context|secrets|creds|credentials|user|form|data|app|db|"
    r"values|vars|props|state|input|payload|body)"
)
_NOT_A_PW = (
    r"(?![$<{%/\\(\[*^|?+~`]|\[REDACTED|os\.|process\.env|getenv"
    r"|(?:true|false|null|none|yes|no)\b"
    r"|[A-Za-z_][\w.]*\("
    r"|" + _CODE_OBJECTS + r"\.[A-Za-z_]"
    r"|[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\.[a-z0-9_]*"
    r"(?:pass|pwd|secret|token|key|clave|contrase)[a-z0-9_]*(?![\w.\-/+=])"
    r"|(?:string|number|boolean|str|int|bool|bytes|undefined)\b)"
)
_PW_VALUE = (
    r"(?:\"" + _NOT_A_PW + r"(?!\s)[^\"\n]{6,}\""
    r"|'" + _NOT_A_PW + r"(?!\s)[^'\n]{6,}'"
    r"|" + _NOT_A_PW + r"[^\s\"',;`)\]}]{6,})"
)

# Names ending in `_KEY` (`OPENROUTER_KEY`, `SECRET_KEY`): only values that look like a
# key (16 or more characters, then `_keyish` below: a digit and no words), since `sort_key = name`
# and `idempotency_key = "order-2024-10-23-0000001"` are ordinary code.
# No "has a digit" look-ahead here: `_keyish` already requires one, and a look-ahead that
# scans to the end of the line from every `*_key` made long lines quadratic.
_KEYISH_VALUE = (
    r"(?P<value>\"" + _NOT_A_VALUE + r"[^\"\s]{16,}\""
    r"|'" + _NOT_A_VALUE + r"[^'\s]{16,}'"
    r"|" + _NOT_A_VALUE + r"[^\s\"',;`)\]}]{16,})"
)
_PW_VALUE_GROUP = "(?P<value>" + _PW_VALUE + ")"

_WORD = re.compile(r"[a-z]{3,}")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _mixed(value: str) -> bool:
    return (
        any(c.isupper() for c in value)
        and any(c.islower() for c in value)
        and any(c.isdigit() for c in value)
    )


def _wordy(value: str) -> bool:
    """Mostly lowercase runs of 3+ letters: an identifier made of words
    (`getUserProfileByIdV2AndOrganization4Test`, `Tenant_12345_Events`), not random."""
    return sum(len(w) for w in _WORD.findall(value)) >= len(value) / 2


def _keyish(match: re.Match[str]) -> bool:
    """A `*_key` value that looks random: a 12+ run of letters and digits with both
    (hex, base64 without separators), or mixed case and digits that is not made of words.
    `order-2024-10-23-0000001` and `tenant_12345_events_2026` are ids, not keys."""
    value = match.group("value").strip("\"'")
    for run in _ALNUM_RUN.findall(value):
        if len(run) >= 12 and not run.isdigit() and not run.isalpha():
            return True
    return _mixed(value) and not _wordy(value)


_PROSE_WORD = re.compile(r"[^\W\d_]+[.!?]?")


def _not_prose(match: re.Match[str]) -> bool:
    """`contraseña = x` or a quoted key is a value; `contraseña: incorrecta` is prose."""
    if "=" in match.group("sep") or match.group("key")[-1:] in "\"'":
        return True
    value = match.group("value").strip("\"'")
    return not (_PROSE_WORD.fullmatch(value) and value[1:].rstrip(".!?").islower())


_COOKIE_START = re.compile(r"[!#$%&*+\-.^_`|~0-9A-Za-z]+=")
_COOKIE_SECRET_NAME = re.compile(
    r"(?i)(?:^|[;\s])[^=;\s]*(?:sess|sid|token|auth|jwt|csrf|xsrf|remember|login|secret|key)"
    r"[^=;\s]*="
)
# One greedy run per value: the run from the first `=` holds every later one, so checking
# it alone is equivalent and stays linear (look-aheads from each `=` were quadratic).
_COOKIE_RUN = re.compile(r"=([^;\s]{16,})")


def _opaque_cookie_value(value: str) -> bool:
    return any(
        any(c.isdigit() for c in run) and any(c.isascii() and c.isalpha() for c in run)
        for run in _COOKIE_RUN.findall(value)
    )


def _session_cookie(match: re.Match[str]) -> bool:
    """A cookie header worth hiding: `name=value` pairs that hold several cookies (`;`), a
    session-like name or a long opaque value. `Cookie: theme=dark` and code such as
    `cookie: http.cookies.BaseCookie[str] = ...` are kept."""
    value = match.group("value")
    if not _COOKIE_START.match(value):
        return False
    return (
        ";" in value or _COOKIE_SECRET_NAME.search(value) is not None or _opaque_cookie_value(value)
    )


_REMOTE_COMMAND = re.compile(r"(?:^|[;&|(`]|\s)(?:ssh|scp|sftp|mosh|telnet|ping|nc|ncat)\s")


def _mysql_password(match: re.Match[str]) -> bool:
    """`ssh mysql -p2222` names a host called mysql and a port; a numeric value after a
    remote-shell command is a port, not a password."""
    value = match.group("value").strip("\"'")
    if not value.isdigit():
        return True
    text = match.string
    line = text[text.rfind("\n", 0, match.start()) + 1 : match.start()]
    return _REMOTE_COMMAND.search(" " + line) is None


Check = Callable[[re.Match[str]], bool]
"""Decides, for one regex match, whether it is a secret (True) or ordinary text."""

_BUILTIN: tuple[tuple[str, str] | tuple[str, str, Check], ...] = (
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    (
        "slack_webhook",
        r"https?://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9_/\-]{8,}",
    ),
    (
        "discord_webhook",
        r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{20,}",
    ),
    (
        # `123456789:AA...` (the digits are the bot id, the rest is the secret): after
        # `bot` (`/bot123:...`, `bot 123:...`), or alone in the exact shape Telegram issues
        # (8 to 10 digits, 35 characters with both cases), so `build 20261023:a_long_name`
        # stays.
        "telegram_bot_token",
        r"(?:(?:(?<=[Bb]ot)|(?<=[Bb]ot[ =:]))\d{6,12}:(?=[A-Za-z0-9_\-]*[A-Za-z])"
        r"[A-Za-z0-9_\-]{30,}"
        r"|(?<![A-Za-z0-9_:])\d{8,10}:(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])"
        r"[A-Za-z0-9_\-]{35})(?![A-Za-z0-9_\-])",
    ),
    ("aws_secret_key", ""),  # see _aws_secrets: needs context, not just a shape
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    ("groq_key", r"\bgsk_[A-Za-z0-9]{20,}"),
    ("xai_key", r"\bxai-[A-Za-z0-9]{20,}"),
    ("sendgrid_key", r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ("pypi_token", r"\bpypi-[A-Za-z0-9_\-]{16,}"),
    ("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("stripe_webhook_secret", r"\bwhsec_[A-Za-z0-9+/=]{16,}"),
    ("basic_auth", r"\b[Bb]asic\s+(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{12,}=*"),
    ("azure_key", r"(?i)\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)=[^;\s\"']{8,}"),
    ("url_signature", r"(?i)(?<=[?&])(?:sig|signature|access_token|api_?key)=[^&\s\"'#]{8,}"),
    (
        # `Authorization: Token abc...`, `authorization = "Bot ..."`. Without a scheme the
        # value needs a digit, so prose like "authorization: required" stays.
        "authorization",
        r"(?i)(?P<key>(?<![\w-])(?:proxy-)?authorization[\"']?)(?P<sep>[ \t]*[:=][ \t]*[\"']?)"
        r"(?:(?:bearer|basic|token|digest|bot|apikey|api-key|key|sso-key|hmac)[ \t]+"
        r"(?![$<{%]|\[REDACTED)[^\s\"',;]{8,}"
        r"|(?![$<{%]|\[REDACTED)(?=[^\s\"',;]*\d)[^\s\"',;]{8,})",
    ),
    (
        # `Cookie: sessionid=...` and `Set-Cookie:` headers: the whole value, when it
        # looks like a session (see `_session_cookie`).
        "cookie",
        r"(?i)(?P<key>(?<![\w-])(?:set-)?cookie[\"']?)(?P<sep>[ \t]*:[ \t]*)"
        r"(?P<value>(?![$<{%]|\[REDACTED)(?=[^\n\r\"'\\]*=)[^\n\r\"'\\]{8,})",
        _session_cookie,
    ),
    (
        # `mysql -u root -pS3cret db` (also mysqldump, mariadb...): the password flag.
        "mysql_password",
        r"(?P<key>(?<![\w-])(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlcheck|mysqlshow|"
        r"mariadb|mariadb-dump|mariadb-admin)(?![\w.-])[^\n|;&]{0,200}?[ \t][\"']?-p)"
        r"(?P<sep>)(?P<value>'[^'\n]+'|\"[^\"\n]+\"|(?![$<{%\s])[^\s'\"]+)",
        _mysql_password,
    ),
    (
        # `NAME=value`, `"name": "value"`, `name: value` where NAME ends in a secret word
        # (GITHUB_TOKEN, db_pass, clientSecret...). References are kept: $VAR, ${VAR}, <x>.
        "assignment",
        r"(?i)(?P<key>(?<![\w-])" + _SENSITIVE_NAME + r"(?![\w-])[\"']?)"
        r"(?P<sep>[ \t]*[:=][ \t]*)" + _SECRET_VALUE,
    ),
    (
        # Command-line flags: --password X, --token=X.
        "assignment",
        r"(?i)(?P<key>(?<![\w-])--" + _SENSITIVE_NAME + r")(?P<sep>=|[ \t]+)" + _SECRET_VALUE,
    ),
    (
        # Passwords made of digits or dotted words.
        "assignment",
        r"(?i)(?P<key>(?<![\w-])" + _PW_NAME + r"(?![\w-])[\"']?)"
        r"(?P<sep>[ \t]*[:=][ \t]*)" + _PW_VALUE,
    ),
    (
        # Spanish names (contraseña, db_clave), unless they read as prose.
        "assignment",
        r"(?i)(?P<key>(?<![\w-])" + _ES_PW_NAME + r"(?![\w-])[\"']?)"
        r"(?P<sep>[ \t]*[:=][ \t]*)" + _PW_VALUE_GROUP,
        _not_prose,
    ),
    (
        # Bare `clave` is a common Spanish word ("idea clave: ..."): only `clave=` or a
        # quoted key (`"clave": "..."`).
        "assignment",
        r"(?i)(?P<key>(?<![\w-])(?:\"clave\"|'clave'|clave(?=[ \t]*=)))"
        r"(?P<sep>[ \t]*[:=][ \t]*)" + _PW_VALUE,
    ),
    (
        "assignment",
        r"(?i)(?P<key>(?<![\w-])[a-z][\w-]*[_-]key(?![\w-])[\"']?)"
        r"(?P<sep>[ \t]*[:=][ \t]*)" + _KEYISH_VALUE,
        _keyish,
    ),
    # `scheme://user:password@host` and `redis://:password@host`.
    ("url_credentials", r"(?<=://)(?!\[REDACTED)[^/\s:@]*:[^/\s@]+(?=@)"),
)

# -- private keys -------------------------------------------------------------------------
#
# A key block is redacted from its BEGIN line through its body even when the END line is
# missing (a tool argument or a long block cut short), and a body whose BEGIN line was cut
# away is redacted back from its END line. The body is base64 lines, armour headers and
# the markers truncation leaves (`...`, `[... N characters omitted ...]`); newlines may be
# real or JSON-escaped (`\n`), as in a tool call's arguments.

_KEY_BEGIN = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_END = r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_NEWLINE = r"(?:\r?\n|\\r\\n|\\n|\\r)"
_CUT = r"(?:\[\.\.\. \d+ characters omitted \.\.\.\]|\.\.\.)"
_KEY_UNIT = (
    r"(?:[ \t]*" + _NEWLINE + r"[ \t]*|[A-Za-z0-9+/=]{1,100}|" + _CUT + r"|"
    r"(?:Proc-Type|DEK-Info|Comment|Version|Hash|Charset):[ \t][^\n\\]{0,200})"
)
_PRIVATE_KEY_FULL = re.compile(_KEY_BEGIN + r"[\s\S]{0,20000}?" + _KEY_END)
_PRIVATE_KEY_OPEN = re.compile(_KEY_BEGIN + r"(?:" + _KEY_UNIT + r")*(?:" + _KEY_END + r")?")
_PRIVATE_KEY_TAIL = re.compile(_KEY_END)
_SPLIT_LINES = re.compile(r"(" + _NEWLINE + r")")
_BODY_LINE = re.compile(r"[ \t]*(?:[A-Za-z0-9+/=]{1,100}|" + _CUT + r")?[ \t]*")
_KEY_LABEL = "[REDACTED:private_key]"


def _private_keys(text: str) -> tuple[str, int]:
    if "PRIVATE KEY" not in text:
        return text, 0
    text, full = _PRIVATE_KEY_FULL.subn(_KEY_LABEL, text)
    text, opened = _PRIVATE_KEY_OPEN.subn(_KEY_LABEL, text)
    tails = 0
    out: list[str] = []
    last = 0
    for match in _PRIVATE_KEY_TAIL.finditer(text):
        start = _body_start(text, max(last, match.start() - 20000), match.start())
        out.append(text[last:start] + _KEY_LABEL)
        last = match.end()
        tails += 1
    if tails:
        text = "".join(out) + text[last:]
    return text, full + opened + tails


def _body_start(text: str, floor: int, end: int) -> int:
    """Where the key body that ends at `end` begins, walking back line by line."""
    pieces = _SPLIT_LINES.split(text[floor:end])
    start = end
    for index in range(len(pieces) - 1, -1, -1):
        piece = pieces[index]
        if index % 2 == 0 and not _BODY_LINE.fullmatch(piece):
            break
        if index % 2 == 0 and index != len(pieces) - 1 and not piece.strip():
            break  # a blank line: key bodies have none, so what is above is other text
        start -= len(piece)
    return start


# -- AWS secret access keys -----------------------------------------------------------------
#
# A secret access key is 40 base64 characters with no prefix of its own. It is redacted
# when an access key id (AKIA..., ASIA...) is close by, or when it looks random on its own
# (mixed case, digits, high entropy). Hex strings, such as git commit ids, never count, and
# neither do identifiers made of words (`getUserProfileByIdV2AndOrganization4Test`).

_AWS_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\[REDACTED:aws_access_key\]")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
_HEX = re.compile(r"[0-9a-fA-F]+")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_AWS_NEAR = 200


def _aws_secrets(text: str) -> tuple[str, int]:
    if not _AWS_SECRET.search(text):
        return text, 0
    ids = [m.span() for m in _AWS_ID.finditer(text)]
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(0)
        if _HEX.fullmatch(value) or not _mixed(value):
            return value
        if _IDENTIFIER.fullmatch(value) and _wordy(value):
            return value  # `getUserProfileByIdV2AndOrganization4Test`: words, not a key
        near = any(
            match.start() - _AWS_NEAR <= end and start <= match.end() + _AWS_NEAR
            for start, end in ids
        )
        alone = sum(c.isdigit() for c in value) >= 2 and _entropy(value) >= 4.3
        if not (near or alone):
            return value
        count += 1
        return "[REDACTED:aws_secret_key]"

    return _AWS_SECRET.sub(replace, text), count


@dataclass(frozen=True, slots=True)
class Redaction:
    text: str
    count: int


Step = Callable[[str], tuple[str, int]]


def _regex_step(kind: str, pattern: str, accept: Check | None = None) -> Step:
    compiled = re.compile(pattern)
    label = f"[REDACTED:{kind}]"
    keyed = "key" in compiled.groupindex and kind != "custom"

    def step(text: str) -> tuple[str, int]:
        changed = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            if accept is not None and not accept(match):
                return match.group(0)
            # For `key = value` keep the key and separator so the text stays readable.
            new = f"{match.group('key')}{match.group('sep')}{label}" if keyed else label
            # Text redacted before (a `Cookie: [REDACTED:cookie]` line) can match again;
            # writing back the same text is not a new secret, so it is not counted.
            if new != match.group(0):
                changed += 1
            return new

        return compiled.sub(replace, text), changed

    return step


class Redactor:
    def __init__(self, extra_patterns: Iterable[str] = (), *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._steps: list[Step] = [_private_keys]
        for kind, pattern, *accept in _BUILTIN:
            self._steps.append(
                _aws_secrets
                if kind == "aws_secret_key"
                else _regex_step(kind, pattern, accept[0] if accept else None)
            )
        self._steps += [_regex_step("custom", p) for p in extra_patterns]

    def redact(self, text: str) -> Redaction:
        if not self.enabled or not text:
            return Redaction(text, 0)
        total = 0
        for step in self._steps:
            text, n = step(text)
            total += n
        return Redaction(text, total)

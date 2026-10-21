"""Configuration: one TOML file, validated with pydantic.

Secrets never live in the TOML file. Every secret is referenced by the *name* of an
environment variable (fields ending in ``_env``), so the config can be shared, reviewed
and versioned without leaking credentials.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_ENV_VAR = "JEZERO_CONFIG"
DEFAULT_CONFIG_NAME = "jezero.toml"

_SECRETISH_KEY = re.compile(
    r"(?i)^(?:.*_)?(?:api_?key|token|secret|password|passwd|credentials?|private_key)$"
)
_TOKENIZER_RE = re.compile(r"^[a-z0-9_ ]+$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class StorageConfig(_Strict):
    path: Path = Path("data/jezero.db")
    """The SQLite file (created by `jezero init`). Relative to the config file."""
    tokenizer: str = "trigram remove_diacritics 1"
    """FTS5 tokenizer. The default matches plurals as substrings and ignores accents."""

    @field_validator("tokenizer")
    @classmethod
    def _safe_tokenizer(cls, value: str) -> str:
        if not _TOKENIZER_RE.fullmatch(value):
            raise ValueError("tokenizer may only contain lowercase letters, digits, _ and spaces")
        return value


class SearchConfig(_Strict):
    default_limit: int = Field(10, ge=1, le=100)
    """Results returned when a caller does not ask for a number."""
    max_limit: int = Field(50, ge=1, le=500)
    """Most results any single call may return, whatever it asks for."""
    candidate_pool: int = Field(200, ge=10, le=5000)
    """How many BM25 candidates are re-ranked by recency."""
    recency_half_life_days: float = Field(90.0, gt=0)
    """Age at which the recency boost of an item is half that of a new one."""
    recency_weight: float = Field(0.3, ge=0.0, le=1.0)
    """0 = pure BM25, 1 = relevance fully scaled by recency."""


class SummarizerConfig(_Strict):
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    """API dialect: Anthropic Messages, or any OpenAI-compatible chat endpoint."""
    base_url: str = "https://api.anthropic.com"
    """Endpoint of the provider (for example a local OpenAI-compatible server)."""
    model: str | None = None
    """Use a cheap, fast model. Required before running any connector."""
    api_key_env: str = "JEZERO_SUMMARIZER_API_KEY"
    """Environment variable holding the provider's API key."""
    max_concurrency: int = Field(4, ge=1, le=64)
    """Summaries requested in parallel."""
    timeout_seconds: float = Field(120.0, gt=0)
    """Time allowed for one summary request."""
    max_input_chars: int = Field(60_000, ge=1_000)
    """Longer inputs are split into parts, each summarised on its own."""
    known_projects: list[str] = Field(default_factory=list)
    """Project slugs the summarizer should prefer when tagging items."""
    output_language: str | None = None
    """Write every summary in this language (e.g. "Spanish") so one query matches all
    items. Default: the document's main language."""


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    """Address to listen on. Keep loopback and publish through a reverse proxy."""
    port: int = Field(8765, ge=1, le=65535)
    """Port to listen on."""
    path: str = "/mcp"
    """URL path of the MCP endpoint."""
    public_url: str | None = None
    """External URL when served behind a reverse proxy, e.g. https://library.example.org/mcp"""
    allowed_hosts: list[str] = Field(default_factory=list)
    """Extra Host header values accepted (DNS-rebinding protection). Derived from public_url."""
    require_auth: bool = True
    """Ask every request for a client key. May be turned off only on a loopback host."""
    client_bundle: Path | None = Path("dist/jezero-client.tar.gz")
    """Plugin bundle offered at /client/jezero-client.tar.gz (public code, no auth)."""

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with /")
        return value.rstrip("/") or "/"

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @model_validator(mode="after")
    def _no_open_network_server(self) -> ServerConfig:
        if not self.require_auth and not self.is_loopback:
            raise ValueError("require_auth = false is only allowed when host is a loopback address")
        return self


class ClientConfig(_Strict):
    """An agent (or person) allowed to query the library over MCP."""

    name: str
    """Unique name: lowercase letters, digits, - and _ (a device's name for plugins)."""
    token_env: str | None = None
    """Environment variable holding the client's bearer token."""
    token_sha256: str | None = None
    """Or: the SHA-256 hex digest of the token (`jezero new-token` prints one)."""
    sources: list[str] = Field(default_factory=lambda: ["*"])
    """Source names (glob patterns) this client may read. Default: everything."""
    upload: bool = False
    """May deliver raw session files to the inbox (a device running the Jezero plugins)."""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("client name must be lowercase letters, digits, - or _")
        return value

    @field_validator("token_sha256")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("token_sha256 must be a 64-char lowercase hex SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _exactly_one_token(self) -> ClientConfig:
        if (self.token_env is None) == (self.token_sha256 is None):
            raise ValueError(f"client {self.name!r}: set exactly one of token_env or token_sha256")
        return self


class InboxConfig(_Strict):
    """Where devices deliver raw session files (see `jezero.inbox`)."""

    dir: Path = Path("data/inbox")
    """Folder for uploads, one subfolder per device. Relative to the config file."""
    max_mb: float = Field(64.0, gt=0)
    """Largest accepted file, after decompression."""


class ConnectorConfig(_Strict):
    """One configured source. `type` selects the implementation, `name` identifies it."""

    name: str
    """Unique name, which becomes the `source` of its items (`agent-codex-laptop`)."""
    type: str
    """Connector type: see the connectors reference."""
    enabled: bool = True
    """Disabled connectors keep their cursor and are skipped by `jezero run`."""
    interval_minutes: int = Field(15, ge=1)
    """How often `jezero run --due` runs it."""
    project: str | None = None
    """Default project for items from this source, when the connector cannot infer one."""
    trust: Literal["own", "external"] = "own"
    """`own` for your agents' work; `external` for text written by others (mail)."""
    options: dict[str, Any] = Field(default_factory=dict)
    """Type-specific settings (paths, filters). Secrets go through *_env keys only."""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("connector name must be lowercase letters, digits, - or _")
        return value

    @field_validator("options")
    @classmethod
    def _no_inline_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in _walk_keys(value):
            if _SECRETISH_KEY.search(key) and not key.endswith("_env"):
                raise ValueError(
                    f"option {key!r} looks like a secret; reference an env var with {key}_env"
                )
        return value


class RedactionConfig(_Strict):
    enabled: bool = True
    """Redact known secret formats (keys, tokens, passwords) before storing anything."""
    extra_patterns: list[str] = Field(default_factory=list)
    """Additional regular expressions to redact before summarising and indexing."""

    @field_validator("extra_patterns")
    @classmethod
    def _compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            re.compile(pattern)
        return value


class WatchdogConfig(_Strict):
    max_silence_hours: float = Field(24.0, gt=0)
    """Alert when an enabled connector has not succeeded for this long."""
    alert_webhook_env: str | None = None
    """Env var holding a webhook URL that receives a JSON alert."""
    realert_hours: float = Field(12.0, gt=0)
    """Repeat an alert for the same connector at most this often."""
    heartbeat_url_env: str | None = None
    """Env var holding a push URL (for example an Uptime Kuma push monitor). Every health
    check calls it with status=up or status=down, so silence itself becomes an alert."""


class ProjectsConfig(_Strict):
    """How a session's folder maps to a project (see `jezero.projects`)."""

    paths: dict[str, str] = Field(default_factory=dict)
    """Folder -> project; `~` matches any home folder. Wins over everything else."""
    roots: list[str] | None = None
    """Folder names that hold projects; default: projects, code, dev, src, repos, ..."""
    aliases: dict[str, str] = Field(default_factory=dict)
    """Rename a detected project (`"atlas-wt" = "atlas"`)."""
    git: bool = True
    """Use the git repository's name when the folder exists on this computer."""


class Config(_Strict):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    clients: list[ClientConfig] = Field(default_factory=list)
    connectors: list[ConnectorConfig] = Field(default_factory=list)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)

    source_file: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _unique_names(self) -> Config:
        for label, names in (
            ("client", [c.name for c in self.clients]),
            ("connector", [c.name for c in self.connectors]),
        ):
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                raise ValueError(f"duplicate {label} names: {sorted(dupes)}")
        if self.search.default_limit > self.search.max_limit:
            raise ValueError("search.default_limit cannot exceed search.max_limit")
        from .connectors import CONNECTOR_TYPES

        unknown = sorted({c.type for c in self.connectors} - set(CONNECTOR_TYPES))
        if unknown:
            raise ValueError(f"unknown connector types {unknown}; known: {sorted(CONNECTOR_TYPES)}")
        return self

    @property
    def db_path(self) -> Path:
        path = self.storage.path.expanduser()
        if not path.is_absolute() and self.source_file is not None:
            path = self.source_file.parent / path
        return path

    @property
    def inbox_dir(self) -> Path:
        path = self.inbox.dir.expanduser()
        if not path.is_absolute() and self.source_file is not None:
            path = self.source_file.parent / path
        return path

    def env_report(self) -> dict[str, bool]:
        """Which referenced environment variables are set. Never returns their values."""
        names: list[str] = [self.summarizer.api_key_env]
        names += [c.token_env for c in self.clients if c.token_env]
        if self.watchdog.alert_webhook_env:
            names.append(self.watchdog.alert_webhook_env)
        if self.watchdog.heartbeat_url_env:
            names.append(self.watchdog.heartbeat_url_env)
        for conn in self.connectors:
            names += [str(v) for k, v in _walk_items(conn.options) if k.endswith("_env")]
        return {name: bool(os.environ.get(name)) for name in dict.fromkeys(names)}


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit)
    if env := os.environ.get(CONFIG_ENV_VAR):
        return Path(env)
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE lines from a dotenv-style file into os.environ.

    Existing environment variables win, so a service manager can always override the file.
    Returns the names that were loaded (never the values).
    """
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    config_path = resolve_config_path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"config not found at {config_path}. Copy jezero.example.toml to "
            f"{DEFAULT_CONFIG_NAME} or set {CONFIG_ENV_VAR}."
        )
    env_file = config_path.parent / ".env"
    if env_file.is_file():
        load_env_file(env_file)
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    config = Config.model_validate(data)
    config.source_file = config_path.resolve()
    return config


def _walk_keys(value: Any) -> list[str]:
    return [k for k, _ in _walk_items(value)]


def _walk_items(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            found.append((str(key), sub))
            found.extend(_walk_items(sub))
    elif isinstance(value, list):
        for sub in value:
            found.extend(_walk_items(sub))
    return found

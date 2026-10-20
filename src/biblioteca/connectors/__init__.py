"""Connector registry.

A connector turns one source (agent sessions, a mailbox) into `NewItem`s. Each connector
type is registered here with its status, so `check-config` can tell a typo from a
connector that simply is not implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["available", "planned"]


@dataclass(frozen=True, slots=True)
class ConnectorType:
    name: str
    status: Status
    description: str
    default_trust: Literal["own", "external"] = "own"


CONNECTOR_TYPES: dict[str, ConnectorType] = {
    t.name: t
    for t in (
        ConnectorType(
            "hermes",
            "available",
            "Hermes Agent sessions from its state.db (read-only).",
        ),
        ConnectorType(
            "claude_code",
            "planned",
            "Claude Code session transcripts (*.jsonl under a projects directory).",
        ),
        ConnectorType(
            "codex",
            "planned",
            "OpenAI Codex CLI session transcripts (*.jsonl under a sessions directory).",
        ),
        ConnectorType(
            "receiver",
            "planned",
            "HTTP endpoint that accepts sessions pushed by small emitters on other devices.",
        ),
        ConnectorType(
            "gmail",
            "planned",
            "Gmail threads through the API with the gmail.readonly scope.",
            default_trust="external",
        ),
        ConnectorType(
            "jsonl",
            "available",
            "Pre-summarised items from a JSON Lines file (testing, imports, custom pipelines).",
        ),
    )
}


def describe(type_name: str) -> ConnectorType | None:
    return CONNECTOR_TYPES.get(type_name)

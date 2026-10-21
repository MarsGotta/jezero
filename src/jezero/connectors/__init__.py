"""Connector registry.

A connector turns one source (agent sessions, a mailbox) into `NewItem`s. Each module in
this package declares its type at the bottom::

    CONNECTOR_TYPE = ConnectorType("name", "available", "What it reads.", build=_build)

and is found automatically, so adding a connector is adding one file. Connectors that
live in other packages register through the `jezero.connectors` entry point group,
pointing at their `ConnectorType`::

    [project.entry-points."jezero.connectors"]
    mytool = "jezero_mytool:CONNECTOR_TYPE"

Every type is listed with its status, so `check-config` can tell a typo from a connector
that simply is not implemented yet.
"""

from __future__ import annotations

import importlib
import pkgutil
import warnings
from importlib.metadata import entry_points

from .base import BuildContext, ConnectorType, Status

__all__ = ["CONNECTOR_TYPES", "ENTRY_POINT_GROUP", "BuildContext", "ConnectorType", "Status"]

ENTRY_POINT_GROUP = "jezero.connectors"

LISTED_ONLY = (
    ConnectorType(
        "gmail",
        "planned",
        "Gmail threads through the API with the gmail.readonly scope.",
        default_trust="external",
    ),
    ConnectorType(
        "jsonl",
        "available",
        "Pre-summarised items from a JSON Lines file, loaded with `jezero import-jsonl`.",
    ),
)
"""Types with no module here: planned ones, and `jsonl`, which the CLI imports directly."""


def _add(found: dict[str, ConnectorType], kind: object, origin: str) -> None:
    if not isinstance(kind, ConnectorType):
        warnings.warn(f"{origin}: not a ConnectorType, ignored", stacklevel=3)
        return
    if kind.name in found:
        warnings.warn(f"{origin}: connector type {kind.name!r} already exists", stacklevel=3)
        return
    found[kind.name] = kind


def discover() -> dict[str, ConnectorType]:
    """Built-in modules first, then installed packages (entry points)."""
    found: dict[str, ConnectorType] = {}
    for module in sorted(m.name for m in pkgutil.iter_modules(__path__)):
        if module == "base":
            continue
        kind = getattr(importlib.import_module(f"{__name__}.{module}"), "CONNECTOR_TYPE", None)
        if kind is not None:
            _add(found, kind, f"{__name__}.{module}")
    for kind in LISTED_ONLY:
        _add(found, kind, "jezero")
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            kind = ep.load()
        except Exception as exc:  # noqa: BLE001  a broken add-on must not stop the library
            warnings.warn(f"entry point {ep.name!r} failed to load: {exc!r}", stacklevel=2)
            continue
        _add(found, kind, f"entry point {ep.name!r}")
    return found


CONNECTOR_TYPES: dict[str, ConnectorType] = discover()


def describe(type_name: str) -> ConnectorType | None:
    return CONNECTOR_TYPES.get(type_name)

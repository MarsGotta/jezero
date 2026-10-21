"""Connector registry: built-in modules, add-ons through entry points, building."""

from __future__ import annotations

import pkgutil
import warnings
from types import SimpleNamespace

import pytest

import jezero.connectors as registry
from jezero.config import Config, ConnectorConfig, SummarizerConfig
from jezero.connectors import CONNECTOR_TYPES, BuildContext, ConnectorType
from jezero.connectors.base import Batch
from jezero.pipeline import build_connector


class FakeConnector:
    def __init__(self, name: str, context: BuildContext) -> None:
        self.name = name
        self.context = context

    def collect(self, state, *, now=None) -> Batch:  # pragma: no cover - not called
        raise NotImplementedError


def fake_type(name: str = "fake") -> ConnectorType:
    return ConnectorType(
        name, "available", "A test connector.", build=lambda c, ctx: FakeConnector(c.name, ctx)
    )


def entry_point(name: str, load):
    return SimpleNamespace(name=name, load=load)


def test_every_connector_module_declares_its_type() -> None:
    modules = {m.name for m in pkgutil.iter_modules(registry.__path__)} - {"base"}
    assert modules
    for module in modules:
        kind = CONNECTOR_TYPES[module]
        assert kind.status == "available"
        assert kind.build is not None, module


def test_listed_only_types_are_known_but_never_scheduled() -> None:
    assert CONNECTOR_TYPES["gmail"].status == "planned"
    assert CONNECTOR_TYPES["jsonl"].build is None


def test_entry_points_add_connectors_from_other_packages(monkeypatch) -> None:
    def boom():
        raise ImportError("missing dependency")

    eps = [
        entry_point("fake", lambda: fake_type()),
        entry_point("broken", boom),
        entry_point("codex", lambda: fake_type("codex")),
        entry_point("odd", lambda: "not a type"),
    ]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        found = registry.discover()
    assert found["fake"].description == "A test connector."
    assert found["codex"].description == CONNECTOR_TYPES["codex"].description  # built-in wins
    messages = " ".join(str(w.message) for w in caught)
    assert "broken" in messages
    assert "already exists" in messages
    assert "not a ConnectorType" in messages


def config_with(conn: ConnectorConfig) -> Config:
    return Config(
        summarizer=SummarizerConfig(model="m", max_input_chars=5_000),
        projects={"roots": ["work"]},
        connectors=[conn],
    )


def test_build_connector_passes_the_shared_context(monkeypatch) -> None:
    monkeypatch.setitem(CONNECTOR_TYPES, "fake", fake_type())
    conn = ConnectorConfig.model_construct(name="agent-fake", type="fake", options={}, project=None)
    built = build_connector(config_with(conn), conn)
    assert isinstance(built, FakeConnector)
    assert built.name == "agent-fake"
    assert built.context.max_segment_chars == 5_000
    assert built.context.project_rules.project_for("/home/me/work/shop/x") == "shop"


@pytest.mark.parametrize(
    ("type_name", "message"), [("gmail", "not implemented"), ("jsonl", "schedule")]
)
def test_build_connector_refuses_types_that_cannot_run(type_name: str, message: str) -> None:
    conn = ConnectorConfig.model_construct(name="x", type=type_name, options={}, project=None)
    with pytest.raises(NotImplementedError, match=message):
        build_connector(config_with(conn), conn)

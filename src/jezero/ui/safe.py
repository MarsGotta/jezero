"""What the server returns is shown only after its terminal control characters are gone.

Titles, summaries and atoms are written by a model from transcripts, and a transcript may
hold escape sequences (`ESC ] 52` sets the clipboard, `ESC ] 8` makes a hidden link). The
summarizer drops them before storing, but items stored before that, or by another server,
may still carry them, so the app and the people commands clean every string they print.
"""

from __future__ import annotations

from typing import Any

from ..models import clean_display, clean_tree

__all__ = ["CleanBackend", "clean_display", "clean_tree", "unwrap"]

_CLEANED = frozenset({"overview", "search", "item", "later", "timeline", "context"})
"""Backend methods whose answer comes from the server and is cleaned."""


class CleanBackend:
    """A backend whose answers carry no terminal control characters (see `clean_tree`).

    Everything else (attributes such as `label` or `now`, `agents()`) passes through.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.inner, name)
        if name not in _CLEANED or not callable(value):
            return value

        def cleaned(*args: Any, **kwargs: Any) -> Any:
            return clean_tree(value(*args, **kwargs))

        return cleaned


def unwrap(backend: Any) -> Any:
    """The backend inside a `CleanBackend` (or `backend` itself)."""
    return backend.inner if isinstance(backend, CleanBackend) else backend

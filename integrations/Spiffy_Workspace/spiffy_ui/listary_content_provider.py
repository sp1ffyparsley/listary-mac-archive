"""Provider-neutral contract for optional Spiffy Listary content search.

This module defines only presentation/adapter values. It owns no search index,
database, ranker, watcher, history, model, OCR pipeline, or resident process.
Concrete providers remain responsible for their own currentness and privacy
contracts before returning values through this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Protocol


class ContentSearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "content_search_unavailable")


@dataclass(frozen=True)
class ContentResult:
    document_id: str
    doc_id: str
    media_id: str
    title: str
    snippet: str
    doc_type: str
    source_table: str
    source_id: str
    matched_via: tuple[str, ...]
    representation_revision: str
    offset_unit: str
    start_ms: int | None = None
    end_ms: int | None = None

    @property
    def actionable(self) -> bool:
        return bool(self.document_id and self.media_id and self.representation_revision)


@dataclass(frozen=True)
class ContentSearchPage:
    results: tuple[ContentResult, ...]
    elapsed_ms: float
    status: str
    privacy_scope: str
    semantic_status: str
    side_effects: dict[str, bool]
    semantic_reason: str = ""
    structured_filter_count: int = 0


@dataclass(frozen=True)
class ContentSourceTarget:
    """Current source identity returned by a provider.

    ``path`` is an absolute filesystem path and ``source_sha256`` is the exact
    64-hex SHA-256 of the bytes proven current for that path.
    """

    path: str
    source_state: str
    reason_code: str
    source_sha256: str
    target_kind: str


class ContentSearchProvider(Protocol):
    """Thin provider surface consumed by the File Search Content mode.

    ``resolve_source`` is deliberately repeatable: callers may invoke it again
    immediately before a native side effect to prove that the same source is
    still current. Actionable targets must therefore carry an exact SHA-256 in
    ``source_sha256`` rather than only a path or mutable filesystem timestamp.
    """

    @property
    def display_name(self) -> str:
        ...

    def search(
        self,
        query: str,
        *,
        limit: int,
        cancel_event: threading.Event,
    ) -> ContentSearchPage:
        ...

    def resolve_source(
        self,
        result: ContentResult,
        *,
        cancel_event: threading.Event,
    ) -> ContentSourceTarget:
        ...


__all__ = [
    "ContentResult",
    "ContentSearchError",
    "ContentSearchPage",
    "ContentSearchProvider",
    "ContentSourceTarget",
]

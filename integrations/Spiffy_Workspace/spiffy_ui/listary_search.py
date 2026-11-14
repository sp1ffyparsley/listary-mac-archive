"""Shared Spiffy UI client for the resident Listary search owner.

The Listary agent owns the one ``listary-core``/RocksDB handle.  This module is
only a loopback client and presentation surface; it never opens, rebuilds, or
mutates the index directly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable
import unicodedata
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, QItemSelectionModel, QMimeData, QObject, QRunnable, QSize, Qt, QSettings, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QMenu,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .foundation import (
    AssociatedOpenError,
    AssociatedOpenReceipt,
    BROADCAST_DARK,
    CommandPaletteItem,
    DirectAppLaunchReceipt,
    DirectShortcutLaunchReceipt,
    FileManagerActionError,
    FileManagerActionReceipt,
    apply_broadcast_dark,
    inspect_shortcut_target,
    library_icon,
    open_app_verified,
    open_file_verified,
    open_path,
    open_shortcut_verified,
    reveal_in_file_manager,
    reveal_in_file_manager_verified,
    set_shared_appearance_profile,
    shared_appearance_profile_id,
    shared_appearance_profiles,
    shared_broadcast_theme,
)
from .listary_content_provider import (
    ContentResult,
    ContentSearchError,
    ContentSearchPage,
    ContentSearchProvider,
    ContentSourceTarget,
)
from .listary_content_search import MemoryCoreContentSearchProvider
from .listary_windows_file_dialog import (
    WindowsFileDialogAdapter,
    WindowsFileDialogContext,
    WindowsFileDialogError,
    WindowsFileDialogFailureReceipt,
    WindowsFileDialogReceipt,
)


_ENDPOINT_FILE = "integration-endpoint-v1.json"
_UI_ENDPOINT_FILE = "ui-endpoint-v1.txt"
_PROTOCOL_VERSION = 1
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_QUICK_SEARCH_DEBOUNCE_MS = 60
_QUICK_SEARCH_RESULT_LIMIT = 9
_QUICK_SEARCH_MIN_VISIBLE_ROWS = 3
_WORKSPACE_APPLICATION_HINT_PREFIX = "workspace_application_v1:"
_WORKSPACE_APPLICATION_SCHEMA = "workspace_catalog_launch_target_v1"
_AUTO_PREVIEW_BROKER = object()
_LISTARY_PREVIEW_HINTS = frozenset({"image", "video", "text", "pdf", "audio"})
_LISTARY_THUMBNAIL_PREVIEW_HINTS = frozenset({"image", "video"})
_LISTARY_TEXT_PREVIEW_BYTES = 64 * 1024
_LISTARY_TEXT_PREVIEW_CHARS = 6_000
_LISTARY_PDF_PREVIEW_BYTES = 32 * 1024 * 1024
_LISTARY_PDF_TEXT_CHARS = 2_000
_LISTARY_AUDIO_PROBE_TIMEOUT_SECONDS = 2
_WORKSPACE_APPLICATION_FIELDS = frozenset(
    {
        "schema",
        "source_provider",
        "workspace_id",
        "system_id",
        "launch_target_id",
        "label",
        "aliases",
        "launch_path",
    }
)
class ListaryAgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "agent_error")


class _CategoryFilterComboBox(QComboBox):
    """QComboBox variant that keeps category lookup stable across Qt bindings."""

    def findData(self, data: object, role: int = Qt.ItemDataRole.UserRole, *args: object) -> int:  # type: ignore[override]
        index = super().findData(data, role, *args)
        if index >= 0:
            return index
        if isinstance(data, tuple):
            wanted = list(data)
            for row in range(self.count()):
                if self.itemData(row, role) == wanted:
                    return row
        return index


@dataclass(frozen=True)
class ListarySearchResult:
    item_id: int
    path: str
    parent_path: str
    name: str
    kind: str
    extension: str = ""
    size_bytes: int = 0
    modified_unix_ms: int = 0
    available: bool = True
    priority: str = "normal"
    preview_hint: str = ""
    root_id: str = ""
    stable_file_id: str = ""


@dataclass(frozen=True)
class ContentActionReceipt:
    schema: str
    outcome: str
    code: str
    action: str
    provider_name: str
    document_id: str
    media_id: str
    representation_revision: str
    target_path: str
    source_sha256: str
    action_generation: int
    search_generation: int
    action_latency_ms: float
    dispatch_happened: bool
    ambiguous: bool
    detail: str
    destination: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "code": self.code,
            "action": self.action,
            "provider_name": self.provider_name,
            "document_id": self.document_id,
            "media_id": self.media_id,
            "representation_revision": self.representation_revision,
            "target_path": self.target_path,
            "source_sha256": self.source_sha256,
            "action_generation": self.action_generation,
            "search_generation": self.search_generation,
            "action_latency_ms": self.action_latency_ms,
            "dispatch_happened": self.dispatch_happened,
            "ambiguous": self.ambiguous,
            "detail": self.detail,
            "destination": self.destination,
        }


class ContentActionError(RuntimeError):
    def __init__(self, receipt: ContentActionReceipt) -> None:
        super().__init__(receipt.detail)
        self.code = receipt.code
        self.ambiguous = receipt.ambiguous
        self.receipt = receipt


@dataclass(frozen=True)
class ListaryFileManagerReceipt:
    schema: str
    outcome: str
    operation: str
    query_text: str
    item_id: int
    root_id: str
    stable_file_id: str
    displayed_path: str
    target_path: str
    action_generation: int
    result_generation: int
    result_to_destination_ready_ms: float | None
    destination: (
        FileManagerActionReceipt
        | AssociatedOpenReceipt
        | DirectAppLaunchReceipt
        | DirectShortcutLaunchReceipt
        | WorkspaceApplicationLaunchReceipt
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "operation": self.operation,
            "query_text": self.query_text,
            "item_id": self.item_id,
            "root_id": self.root_id,
            "stable_file_id": self.stable_file_id,
            "displayed_path": self.displayed_path,
            "target_path": self.target_path,
            "action_generation": self.action_generation,
            "result_generation": self.result_generation,
            "result_to_destination_ready_ms": self.result_to_destination_ready_ms,
            "destination": self.destination.to_dict(),
        }


@dataclass(frozen=True)
class ListaryFileManagerFailureReceipt:
    schema: str
    outcome: str
    code: str
    operation: str
    query_text: str
    item_id: int
    root_id: str
    stable_file_id: str
    displayed_path: str
    target_path: str
    action_generation: int
    result_generation: int
    action_latency_ms: float
    ambiguous: bool
    dispatch_happened: bool
    detail: str
    destination: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "code": self.code,
            "operation": self.operation,
            "query_text": self.query_text,
            "item_id": self.item_id,
            "root_id": self.root_id,
            "stable_file_id": self.stable_file_id,
            "displayed_path": self.displayed_path,
            "target_path": self.target_path,
            "action_generation": self.action_generation,
            "result_generation": self.result_generation,
            "action_latency_ms": self.action_latency_ms,
            "ambiguous": self.ambiguous,
            "dispatch_happened": self.dispatch_happened,
            "detail": self.detail,
            "destination": self.destination,
        }


@dataclass(frozen=True)
class ListaryFileManagerBatchReceipt:
    schema: str
    outcome: str
    operation: str
    query_text: str
    action_generation: int
    result_generation: int
    requested_item_ids: tuple[int, ...]
    completed: tuple[ListaryFileManagerReceipt, ...]
    failure: ListaryFileManagerFailureReceipt | None
    action_latency_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "operation": self.operation,
            "query_text": self.query_text,
            "action_generation": self.action_generation,
            "result_generation": self.result_generation,
            "requested_item_ids": list(self.requested_item_ids),
            "completed": [receipt.to_dict() for receipt in self.completed],
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "action_latency_ms": self.action_latency_ms,
        }


class ListaryFileManagerActionError(RuntimeError):
    def __init__(self, receipt: ListaryFileManagerFailureReceipt) -> None:
        super().__init__(receipt.detail)
        self.code = receipt.code
        self.ambiguous = receipt.ambiguous
        self.receipt = receipt


_WORKSPACE_MEDIA_EXTENSIONS = {
    "video": frozenset({".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mxf", ".webm", ".wmv", ".mpeg", ".mpg"}),
    "audio": frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aif", ".aiff", ".wma"}),
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico"}),
}


def listary_result_media_type(result: ListarySearchResult) -> str:
    """Classify one existing Listary result without probing its filesystem path."""

    hint = str(result.preview_hint or "").strip().casefold()
    if "video" in hint or "movie" in hint:
        return "video"
    if "audio" in hint or "music" in hint:
        return "audio"
    if "image" in hint or "picture" in hint or "photo" in hint:
        return "image"
    extension = str(result.extension or Path(result.path).suffix or "").strip().casefold()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    for media_type, extensions in _WORKSPACE_MEDIA_EXTENSIONS.items():
        if extension in extensions:
            return media_type
    return "other"


@dataclass(frozen=True)
class WorkspaceApplicationMetadata:
    source_provider: str
    workspace_id: str
    system_id: str
    launch_target_id: str
    label: str
    aliases: tuple[str, ...]
    launch_path: str


@dataclass(frozen=True)
class WorkspaceApplicationLaunchReceipt:
    schema: str
    outcome: str
    label: str
    launch_target_id: str
    launch_path: str
    requested_creative_view: str
    request_id: str
    delivery_mode: str
    dispatch_happened: bool
    action_latency_ms: float
    destination_ready_latency_ms: float
    destination: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "label": self.label,
            "launch_target_id": self.launch_target_id,
            "launch_path": self.launch_path,
            "requested_creative_view": self.requested_creative_view,
            "request_id": self.request_id,
            "delivery_mode": self.delivery_mode,
            "dispatch_happened": self.dispatch_happened,
            "action_latency_ms": self.action_latency_ms,
            "destination_ready_latency_ms": self.destination_ready_latency_ms,
            "destination": dict(self.destination),
        }


class WorkspaceApplicationLaunchError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        ambiguous: bool = False,
        dispatch_happened: bool = False,
        destination: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = str(code or "workspace_application_launch_failed")
        self.ambiguous = bool(ambiguous)
        self.dispatch_happened = bool(dispatch_happened)
        self.destination = dict(destination) if isinstance(destination, dict) else None


@dataclass(frozen=True)
class _ResultPresentation:
    name: str
    parent_path: str
    action_path: str | None
    tooltip: str
    action_error: str = ""
    workspace_application: bool = False


@dataclass(frozen=True)
class ListarySearchPage:
    results: tuple[ListarySearchResult, ...]
    elapsed_micros: int = 0
    considered_candidates: int = 0


@dataclass(frozen=True)
class ListaryRootAdmission:
    path: str
    status: str
    root_id: str = ""
    reconciled: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ListaryFileSearchPage:
    results: tuple[ListarySearchResult, ...]
    total_matches: int
    offset: int
    limit: int
    has_more: bool
    catalog_generation: int
    considered_candidates: int = 0
    elapsed_micros: int = 0
    facets: "ListaryFileSearchFacets | None" = None


@dataclass(frozen=True)
class ListaryFileSearchFacets:
    matched_total: int
    category_scope_total: int
    modified_scope_total: int
    categories: tuple[tuple[str, int], ...]
    modified_time: tuple[tuple[str, int], ...]
    quick_filters: tuple[tuple[str, int], ...]

    def category_count(self, key: str) -> int:
        return next((count for name, count in self.categories if name == key), 0)

    def modified_count(self, key: str) -> int:
        return next((count for name, count in self.modified_time if name == key), 0)

    def quick_count(self, key: str) -> int:
        return next((count for name, count in self.quick_filters if name == key), 0)


def _file_search_facets_from_response(value: object) -> ListaryFileSearchFacets | None:
    if not isinstance(value, dict):
        return None

    def rows(key: str, name_key: str) -> tuple[tuple[str, int], ...]:
        parsed: list[tuple[str, int]] = []
        raw_rows = value.get(key)
        for row in raw_rows if isinstance(raw_rows, list) else ():
            if not isinstance(row, dict):
                continue
            name = str(row.get(name_key) or "").strip()
            if not name:
                continue
            try:
                count = max(0, int(row.get("count") or 0))
            except (TypeError, ValueError):
                continue
            parsed.append((name, count))
        return tuple(parsed)

    quick = value.get("quick_filters") if isinstance(value.get("quick_filters"), dict) else {}
    quick_rows: list[tuple[str, int]] = []
    for key in ("folder", "file", "pdf", "today", "week", "large"):
        try:
            count = max(0, int(quick.get(key) or 0))
        except (TypeError, ValueError):
            count = 0
        quick_rows.append((key, count))
    try:
        return ListaryFileSearchFacets(
            matched_total=max(0, int(value.get("matched_total") or 0)),
            category_scope_total=max(0, int(value.get("category_scope_total") or 0)),
            modified_scope_total=max(0, int(value.get("modified_scope_total") or 0)),
            categories=rows("categories", "category"),
            modified_time=rows("modified_time", "range"),
            quick_filters=tuple(quick_rows),
        )
    except (TypeError, ValueError):
        return None


def _search_result_from_item(item: object) -> ListarySearchResult | None:
    if not isinstance(item, dict):
        return None
    path = str(item.get("path") or "")
    name = str(item.get("name") or Path(path).name)
    kind = str(item.get("kind") or "file")
    try:
        item_id = int(item.get("id"))
        size_bytes = max(0, int(item.get("size") or 0))
        modified_unix_ms = int(item.get("modified_unix_ms") or 0)
    except (TypeError, ValueError):
        return None
    if not path or not name:
        return None
    return ListarySearchResult(
        item_id=item_id,
        path=path,
        parent_path=str(item.get("parent_path") or Path(path).parent),
        name=name,
        kind=kind,
        root_id=str(item.get("root_id") or ""),
        stable_file_id=str(item.get("stable_file_id") or ""),
        extension=str(item.get("extension") or ""),
        size_bytes=size_bytes,
        modified_unix_ms=modified_unix_ms,
        available=item.get("available") is not False,
        priority=str(item.get("priority") or "normal"),
        preview_hint=str(item.get("preview_hint") or ""),
    )


def _workspace_application_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"workspace application {field} must be a string")
    if (
        value != value.strip()
        or not value
        or len(value) > 512
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"workspace application {field} is invalid")
    return value


def _workspace_application_id(value: object, field: str) -> str:
    parsed = _workspace_application_text(value, field)
    if not all(character.isascii() and (character.isalnum() or character in "_.-") for character in parsed):
        raise ValueError(f"workspace application {field} contains unsupported characters")
    return parsed


def decode_workspace_application_preview_hint(preview_hint: str) -> WorkspaceApplicationMetadata | None:
    """Decode the exact core-owned workspace application provider contract.

    Non-provider hints return ``None``. Once the provider prefix is present,
    every field is validated strictly so malformed metadata can never fall back
    to opening the synthetic ``*.workspaceapp`` catalog projection.
    """

    hint = str(preview_hint or "")
    if not hint.startswith(_WORKSPACE_APPLICATION_HINT_PREFIX):
        return None
    encoded = hint[len(_WORKSPACE_APPLICATION_HINT_PREFIX) :]
    if not encoded:
        raise ValueError("workspace application metadata payload is invalid")
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("workspace application metadata is not valid JSON") from exc
    if not isinstance(raw, dict) or frozenset(raw) != _WORKSPACE_APPLICATION_FIELDS:
        raise ValueError("workspace application metadata fields do not match the v1 contract")
    if raw.get("schema") != _WORKSPACE_APPLICATION_SCHEMA:
        raise ValueError("workspace application metadata schema is unsupported")

    workspace_id = _workspace_application_id(raw.get("workspace_id"), "workspace_id")
    source_provider = _workspace_application_text(raw.get("source_provider"), "source_provider")
    if source_provider != f"workspace_catalog:{workspace_id}":
        raise ValueError("workspace application source_provider does not match workspace_id")
    system_id = _workspace_application_id(raw.get("system_id"), "system_id")
    launch_target_id = _workspace_application_id(raw.get("launch_target_id"), "launch_target_id")
    label = _workspace_application_text(raw.get("label"), "label")

    aliases_raw = raw.get("aliases")
    if not isinstance(aliases_raw, list) or len(aliases_raw) > 64:
        raise ValueError("workspace application aliases must be a bounded list")
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    for raw_alias in aliases_raw:
        alias = _workspace_application_text(raw_alias, "alias")
        folded = alias.casefold()
        if folded in seen_aliases:
            raise ValueError("workspace application aliases contain a duplicate")
        seen_aliases.add(folded)
        aliases.append(alias)

    launch_path = raw.get("launch_path")
    if (
        not isinstance(launch_path, str)
        or not launch_path
        or any(unicodedata.category(character) == "Cc" for character in launch_path)
    ):
        raise ValueError("workspace application launch_path is invalid")
    if not Path(launch_path).is_absolute():
        raise ValueError("workspace application launch_path must be absolute")

    return WorkspaceApplicationMetadata(
        source_provider=source_provider,
        workspace_id=workspace_id,
        system_id=system_id,
        launch_target_id=launch_target_id,
        label=label,
        aliases=tuple(aliases),
        launch_path=launch_path,
    )


def _native_dialog_action_label(item_kind: str, dialog_kind: str) -> str:
    """Name the committed native action, including an actual Save/overwrite."""
    if dialog_kind == "save":
        return "Save in Selected Folder" if item_kind == "folder" else "Save / Replace Selected File"
    return "Navigate in Open" if item_kind == "folder" else "Select in Open"


def _native_save_confirmation_pending(
    context: WindowsFileDialogContext | None,
    failure: WindowsFileDialogFailureReceipt | None,
) -> bool:
    """Hold a delivered Save's native overwrite prompt until a distinct dialog is captured."""
    return bool(
        context is not None
        and context.dialog_kind == "save"
        and failure is not None
        and failure.code == "overwrite_requires_confirmation"
        and failure.dialog_kind == "save"
        and failure.context_token == context.context_token
        and failure.context_generation == context.generation
    )


def _native_save_receipt_proves_delivery(
    receipt: WindowsFileDialogReceipt,
    current: ListarySearchResult,
    context: WindowsFileDialogContext | None,
    action_generation: int,
    result_generation: int,
) -> bool:
    """Require the exact captured Save result and stable destination proof before claiming success."""
    if (
        context is None
        or context.dialog_kind != "save"
        or receipt.schema != "listary_windows_file_dialog_receipt_v1"
        or receipt.outcome != "verified"
        or receipt.dialog_kind != "save"
        or receipt.action != "save"
        or receipt.accepted_by_dialog is not True
        or receipt.ambiguous is not False
        or receipt.acceptance_may_have_occurred is not False
        or receipt.dispatch_happened is not True
        or receipt.artifact_changed is not True
        or receipt.saved_hash_verified is not True
        or receipt.item_id != current.item_id
        or receipt.root_id != current.root_id
        or receipt.stable_file_id != current.stable_file_id
        or receipt.item_kind != current.kind
        or receipt.context_token != context.context_token
        or receipt.context_generation != context.generation
        or receipt.action_generation != action_generation
        or receipt.activation_generation != result_generation
        or receipt.result_generation != result_generation
        or not isinstance(receipt.saved_size_bytes, int)
        or receipt.saved_size_bytes < 0
        or not isinstance(receipt.saved_modified_ns, int)
        or receipt.saved_modified_ns < 0
        or not isinstance(receipt.saved_file_id, str)
        or not receipt.saved_file_id.strip()
        or not isinstance(receipt.saved_sha256, str)
        or len(receipt.saved_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in receipt.saved_sha256)
        or not isinstance(receipt.saved_path_after, str)
        or not receipt.saved_path_after
        or not isinstance(receipt.target_path, str)
        or not receipt.target_path
    ):
        return False
    return os.path.normcase(os.path.abspath(receipt.saved_path_after)) == os.path.normcase(
        os.path.abspath(receipt.target_path)
    )


def _result_presentation(result: ListarySearchResult) -> _ResultPresentation:
    try:
        provider = decode_workspace_application_preview_hint(result.preview_hint)
    except (UnicodeError, ValueError):
        return _ResultPresentation(
            name=result.name,
            parent_path=result.parent_path,
            action_path=None,
            tooltip="Invalid workspace application metadata",
            action_error="Workspace application metadata is invalid; refresh the Listary workspace catalog.",
            workspace_application=True,
        )
    if provider is None:
        return _ResultPresentation(
            name=result.name,
            parent_path=result.parent_path,
            action_path=result.path,
            tooltip=result.path,
        )
    launch_path = Path(provider.launch_path)
    if not launch_path.is_file():
        return _ResultPresentation(
            name=provider.label,
            parent_path=str(launch_path.parent),
            action_path=None,
            tooltip=provider.launch_path,
            action_error="Workspace application launcher is unavailable; refresh the Listary workspace catalog.",
            workspace_application=True,
        )
    return _ResultPresentation(
        name=provider.label,
        parent_path=str(launch_path.parent),
        action_path=provider.launch_path,
        tooltip=provider.launch_path,
        workspace_application=True,
    )


def _is_direct_windows_executable_app_result(result: ListarySearchResult) -> bool:
    if result.kind != "app":
        return False
    presentation = _result_presentation(result)
    return bool(
        not presentation.workspace_application
        and presentation.action_path
        and Path(presentation.action_path).suffix.casefold() == ".exe"
    )


def _creative_workspace_application(
    result: ListarySearchResult,
) -> tuple[WorkspaceApplicationMetadata, str] | None:
    if result.kind != "app":
        return None
    try:
        metadata = decode_workspace_application_preview_hint(result.preview_hint)
    except (UnicodeError, ValueError):
        return None
    if metadata is None:
        return None
    launch_path = Path(metadata.launch_path)
    contract = {
        "spiffy_storyboard": ("launch spiffy storyboard.bat", "storyboard"),
        "spiffy_visual_clipboard": ("launch spiffy photo editor.bat", "photo"),
    }.get(metadata.launch_target_id)
    if contract is None:
        return None
    expected_name, creative_view = contract
    if (
        metadata.workspace_id != "ss-scripts"
        or metadata.source_provider != "workspace_catalog:ss-scripts"
        or metadata.system_id != "spiffy_workspace"
        or launch_path.name.casefold() != expected_name
        or launch_path.suffix.casefold() != ".bat"
    ):
        return None
    return metadata, creative_view


def _storyboard_workspace_application(result: ListarySearchResult) -> WorkspaceApplicationMetadata | None:
    resolved = _creative_workspace_application(result)
    if resolved is None or resolved[1] != "storyboard":
        return None
    return resolved[0]


def _photo_workspace_application(result: ListarySearchResult) -> WorkspaceApplicationMetadata | None:
    resolved = _creative_workspace_application(result)
    if resolved is None or resolved[1] != "photo":
        return None
    return resolved[0]


def _video_workspace_application(result: ListarySearchResult) -> WorkspaceApplicationMetadata | None:
    if result.kind != "app":
        return None
    try:
        metadata = decode_workspace_application_preview_hint(result.preview_hint)
    except (UnicodeError, ValueError):
        return None
    if metadata is None:
        return None
    launch_path = Path(metadata.launch_path)
    if (
        metadata.workspace_id != "ss-scripts"
        or metadata.source_provider != "workspace_catalog:ss-scripts"
        or metadata.system_id != "spiffy_workspace"
        or metadata.launch_target_id != "spiffy_video_editor"
        or launch_path.name.casefold() != "launch spiffy workspace.bat"
        or launch_path.suffix.casefold() != ".bat"
    ):
        return None
    return metadata


def _workspace_application_contract(
    result: ListarySearchResult,
) -> tuple[WorkspaceApplicationMetadata, str] | None:
    creative = _creative_workspace_application(result)
    if creative is not None:
        return creative
    video = _video_workspace_application(result)
    return (video, "video") if video is not None else None


def _dispatch_creative_workspace_launcher(launch_path: str, *, creative_view: str) -> bool:
    """Start one canonical creative BAT; final success still comes from IPC."""

    if os.name != "nt":
        return False
    expected_name = {
        "storyboard": "launch spiffy storyboard.bat",
        "photo": "launch spiffy photo editor.bat",
    }.get(str(creative_view or "").strip().casefold())
    if expected_name is None:
        return False
    launcher = Path(launch_path).expanduser()
    if (
        not launcher.is_absolute()
        or launcher.name.casefold() != expected_name
        or launcher.suffix.casefold() != ".bat"
        or not launcher.is_file()
    ):
        return False
    command_processor = Path(
        os.environ.get("COMSPEC")
        or (Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "cmd.exe")
    ).expanduser()
    if command_processor.name.casefold() != "cmd.exe" or not command_processor.is_file():
        return False
    argv = [str(command_processor), "/d", "/c", str(launcher)]
    if creative_view == "photo":
        argv.extend(["--startup-focus-view", "photo"])
    try:
        subprocess.Popen(
            argv,
            cwd=str(launcher.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except (OSError, ValueError):
        return False
    return True


def _dispatch_storyboard_workspace_launcher(launch_path: str) -> bool:
    return _dispatch_creative_workspace_launcher(launch_path, creative_view="storyboard")


def _dispatch_photo_workspace_launcher(launch_path: str) -> bool:
    return _dispatch_creative_workspace_launcher(launch_path, creative_view="photo")


def _dispatch_video_workspace_launcher(launch_path: str) -> bool:
    """Start only the canonical Video Editor BAT; IPC still owns final success."""

    if os.name != "nt":
        return False
    launcher = Path(launch_path).expanduser()
    if (
        not launcher.is_absolute()
        or launcher.name.casefold() != "launch spiffy workspace.bat"
        or launcher.suffix.casefold() != ".bat"
        or not launcher.is_file()
    ):
        return False
    command_processor = Path(
        os.environ.get("COMSPEC")
        or (Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "cmd.exe")
    ).expanduser()
    if command_processor.name.casefold() != "cmd.exe" or not command_processor.is_file():
        return False
    try:
        subprocess.Popen(
            [str(command_processor), "/d", "/c", str(launcher)],
            cwd=str(launcher.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except (OSError, ValueError):
        return False
    return True


def _launch_creative_workspace_verified(
    metadata: WorkspaceApplicationMetadata,
    *,
    requested_creative_view: str,
    dispatch_launcher: Callable[[str], bool],
    cancel_event: threading.Event,
    revalidate_target: Callable[[], None],
    timeout_seconds: float = 12.0,
) -> WorkspaceApplicationLaunchReceipt:
    """Launch/focus a Visual Clipboard creative view and accept only app-owned IPC completion."""

    from workflow.image_clipboard_ipc import (
        build_editor_focus_request,
        editor_cold_launch_eligible,
        editor_focus_completed,
        send_editor_request,
    )

    started = time.monotonic()
    bounded_timeout = max(0.25, min(float(timeout_seconds), 30.0))
    deadline = started + bounded_timeout
    request = build_editor_focus_request(requested_creative_view)
    request_id = str(request["request_id"])
    dispatch_happened = False
    endpoint_contacted = False
    delivery_mode = "warm_ipc"
    last_response: dict[str, object] = {}
    retryable_focus_errors = frozenset(
        {
            "ipc_request_processing",
            "ipc_ack_timeout",
            "ipc_ack_incomplete",
            "ipc_request_write_failed",
        }
    )

    def check_current() -> None:
        if cancel_event.is_set():
            raise WorkspaceApplicationLaunchError(
                "cancelled",
                f"{metadata.label} launch was cancelled before the next destination check.",
                ambiguous=bool(dispatch_happened or endpoint_contacted),
                dispatch_happened=dispatch_happened,
                destination=last_response,
            )
        try:
            revalidate_target()
        except Exception as exc:
            raise WorkspaceApplicationLaunchError(
                str(getattr(exc, "code", "") or "stale_result"),
                str(exc) or f"The {metadata.label} workspace result changed before destination verification completed.",
                ambiguous=bool(dispatch_happened or endpoint_contacted),
                dispatch_happened=dispatch_happened,
                destination=last_response,
            ) from exc

    check_current()
    last_response = dict(send_editor_request(request, timeout_ms=300))
    endpoint_contacted = bool(last_response.get("endpoint_contacted") is True)
    if editor_focus_completed(last_response, request):
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return WorkspaceApplicationLaunchReceipt(
            schema="listary_workspace_application_launch_v1",
            outcome="verified",
            label=metadata.label,
            launch_target_id=metadata.launch_target_id,
            launch_path=metadata.launch_path,
            requested_creative_view=requested_creative_view,
            request_id=request_id,
            delivery_mode=delivery_mode,
            dispatch_happened=False,
            action_latency_ms=elapsed_ms,
            destination_ready_latency_ms=elapsed_ms,
            destination=last_response,
        )

    if editor_cold_launch_eligible(last_response):
        check_current()
        if not dispatch_launcher(metadata.launch_path):
            raise WorkspaceApplicationLaunchError(
                "workspace_launcher_dispatch_failed",
                f"Windows did not accept the canonical launcher for {metadata.label}.",
                destination=last_response,
            )
        dispatch_happened = True
        delivery_mode = "cold_launcher"
    elif (
        last_response.get("endpoint_contacted") is True
        and str(last_response.get("error") or "") not in retryable_focus_errors
    ):
        raise WorkspaceApplicationLaunchError(
            str(last_response.get("error") or "workspace_destination_rejected"),
            str(last_response.get("detail") or f"{metadata.label} rejected the focus request."),
            destination=last_response,
        )

    while time.monotonic() < deadline:
        check_current()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000.0))
        last_response = dict(send_editor_request(request, timeout_ms=min(350, remaining_ms)))
        endpoint_contacted = bool(endpoint_contacted or last_response.get("endpoint_contacted") is True)
        if editor_focus_completed(last_response, request):
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return WorkspaceApplicationLaunchReceipt(
                schema="listary_workspace_application_launch_v1",
                outcome="verified",
                label=metadata.label,
                launch_target_id=metadata.launch_target_id,
                launch_path=metadata.launch_path,
                requested_creative_view=requested_creative_view,
                request_id=request_id,
                delivery_mode=delivery_mode,
                dispatch_happened=dispatch_happened,
                action_latency_ms=elapsed_ms,
                destination_ready_latency_ms=elapsed_ms,
                destination=last_response,
            )
        if (
            last_response.get("endpoint_contacted") is True
            and str(last_response.get("error") or "")
            not in retryable_focus_errors
        ):
            raise WorkspaceApplicationLaunchError(
                str(last_response.get("error") or "workspace_destination_rejected"),
                str(last_response.get("detail") or f"{metadata.label} rejected the focus request."),
                ambiguous=dispatch_happened,
                dispatch_happened=dispatch_happened,
                destination=last_response,
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.08, remaining))

    raise WorkspaceApplicationLaunchError(
        "workspace_destination_ack_timeout",
        f"{metadata.label} did not return a request-correlated visible-window receipt within {bounded_timeout:.1f} seconds.",
        ambiguous=bool(dispatch_happened or endpoint_contacted),
        dispatch_happened=dispatch_happened,
        destination=last_response,
    )


def _launch_storyboard_workspace_verified(
    metadata: WorkspaceApplicationMetadata,
    *,
    cancel_event: threading.Event,
    revalidate_target: Callable[[], None],
    timeout_seconds: float = 12.0,
) -> WorkspaceApplicationLaunchReceipt:
    return _launch_creative_workspace_verified(
        metadata,
        requested_creative_view="storyboard",
        dispatch_launcher=_dispatch_storyboard_workspace_launcher,
        cancel_event=cancel_event,
        revalidate_target=revalidate_target,
        timeout_seconds=timeout_seconds,
    )


def _launch_photo_workspace_verified(
    metadata: WorkspaceApplicationMetadata,
    *,
    cancel_event: threading.Event,
    revalidate_target: Callable[[], None],
    timeout_seconds: float = 12.0,
) -> WorkspaceApplicationLaunchReceipt:
    return _launch_creative_workspace_verified(
        metadata,
        requested_creative_view="photo",
        dispatch_launcher=_dispatch_photo_workspace_launcher,
        cancel_event=cancel_event,
        revalidate_target=revalidate_target,
        timeout_seconds=timeout_seconds,
    )


def _launch_video_workspace_verified(
    metadata: WorkspaceApplicationMetadata,
    *,
    cancel_event: threading.Event,
    revalidate_target: Callable[[], None],
    timeout_seconds: float = 25.0,
) -> WorkspaceApplicationLaunchReceipt:
    """Focus the Video Editor through its app-owned request/final receipt."""

    from spiffy_ui.video_editor_workspace_ipc import (
        build_video_workspace_request,
        send_video_workspace_request,
        video_workspace_cold_launch_eligible,
        video_workspace_final_success,
    )

    started = time.monotonic()
    bounded_timeout = max(0.25, min(float(timeout_seconds), 30.0))
    deadline = started + bounded_timeout
    request = build_video_workspace_request()
    request_id = str(request["request_id"])
    dispatch_happened = False
    endpoint_contacted = False
    last_response: dict[str, object] = {}

    def receipt(*, delivery_mode: str) -> WorkspaceApplicationLaunchReceipt:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return WorkspaceApplicationLaunchReceipt(
            schema="listary_workspace_application_launch_v1",
            outcome="verified",
            label=metadata.label,
            launch_target_id=metadata.launch_target_id,
            launch_path=metadata.launch_path,
            requested_creative_view="video",
            request_id=request_id,
            delivery_mode=delivery_mode,
            dispatch_happened=dispatch_happened,
            action_latency_ms=elapsed_ms,
            destination_ready_latency_ms=elapsed_ms,
            destination=last_response,
        )

    def check_current() -> None:
        if cancel_event.is_set():
            raise WorkspaceApplicationLaunchError(
                "cancelled",
                f"{metadata.label} launch was cancelled before the next destination check.",
                ambiguous=bool(dispatch_happened or endpoint_contacted or last_response.get("request_dispatched") is True),
                dispatch_happened=dispatch_happened,
                destination=last_response,
            )
        try:
            revalidate_target()
        except Exception as exc:
            raise WorkspaceApplicationLaunchError(
                str(getattr(exc, "code", "") or "stale_result"),
                str(exc) or f"The {metadata.label} workspace result changed before destination verification completed.",
                ambiguous=bool(dispatch_happened or endpoint_contacted or last_response.get("request_dispatched") is True),
                dispatch_happened=dispatch_happened,
                destination=last_response,
            ) from exc

    def reject_from_response() -> None:
        error = str(last_response.get("error") or "workspace_destination_rejected")
        final = last_response.get("final") is True
        accepted = last_response.get("accepted") is True
        request_dispatched = last_response.get("request_dispatched") is True
        ambiguous = bool(not final and (accepted or request_dispatched or endpoint_contacted or dispatch_happened))
        detail = str(
            last_response.get("detail")
            or (
                f"{metadata.label} accepted the workspace request but did not publish a final destination receipt."
                if accepted and not final
                else f"{metadata.label} did not complete the app-owned workspace destination request."
            )
        )
        raise WorkspaceApplicationLaunchError(
            error,
            detail,
            ambiguous=ambiguous,
            dispatch_happened=dispatch_happened,
            destination=last_response,
        )

    check_current()
    last_response = dict(
        send_video_workspace_request(
            request,
            timeout_ms=min(300, max(25, int(bounded_timeout * 1000.0))),
            cancel_event=cancel_event,
        )
    )
    endpoint_contacted = bool(last_response.get("endpoint_contacted") is True)
    if video_workspace_final_success(last_response, request):
        return receipt(delivery_mode="warm_ipc")
    if video_workspace_cold_launch_eligible(last_response):
        check_current()
        if not _dispatch_video_workspace_launcher(metadata.launch_path):
            raise WorkspaceApplicationLaunchError(
                "workspace_launcher_dispatch_failed",
                f"Windows did not accept the canonical launcher for {metadata.label}.",
                destination=last_response,
            )
        dispatch_happened = True
    else:
        reject_from_response()

    while time.monotonic() < deadline:
        check_current()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000.0))
        last_response = dict(
            send_video_workspace_request(
                request,
                timeout_ms=min(350, remaining_ms),
                cancel_event=cancel_event,
            )
        )
        endpoint_contacted = bool(endpoint_contacted or last_response.get("endpoint_contacted") is True)
        if video_workspace_final_success(last_response, request):
            return receipt(delivery_mode="cold_launcher")
        if video_workspace_cold_launch_eligible(last_response):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.08, remaining))
            continue
        reject_from_response()

    raise WorkspaceApplicationLaunchError(
        "workspace_destination_ack_timeout",
        f"{metadata.label} did not publish a request-correlated final visible destination within {bounded_timeout:.1f} seconds.",
        ambiguous=dispatch_happened,
        dispatch_happened=dispatch_happened,
        destination=last_response,
    )


def _is_windows_shortcut_file_result(result: ListarySearchResult) -> bool:
    if result.kind != "file":
        return False
    presentation = _result_presentation(result)
    return bool(
        not presentation.workspace_application
        and presentation.action_path
        and Path(presentation.action_path).suffix.casefold() == ".lnk"
    )


def _is_verifiable_windows_shortcut_result(result: ListarySearchResult) -> bool:
    if not _is_windows_shortcut_file_result(result):
        return False
    presentation = _result_presentation(result)
    if not presentation.action_path:
        return False
    try:
        target = inspect_shortcut_target(presentation.action_path)
    except AssociatedOpenError:
        # Route broken/unreadable direct-launch candidates through the verified
        # owner so they fail before dispatch instead of silently falling back.
        return True
    return bool(
        target.executable_path
        and Path(target.executable_path).suffix.casefold() == ".exe"
    )


def _verified_windows_mixed_batch_item_operation(result: ListarySearchResult) -> str | None:
    """Return the existing verified owner for one mixed-batch row.

    Mixed batches preflight every selected row before the first side effect, so
    shortcuts must resolve to a direct executable here.  This is intentionally
    stricter than the single/homogeneous shortcut admission helper, which may
    route a broken shortcut into the verified owner to obtain its exact
    pre-dispatch failure.
    """

    if _is_windows_shortcut_file_result(result):
        presentation = _result_presentation(result)
        if not presentation.action_path:
            return None
        try:
            target = inspect_shortcut_target(presentation.action_path)
        except AssociatedOpenError:
            return None
        return (
            "launch_shortcut"
            if target.executable_path and Path(target.executable_path).suffix.casefold() == ".exe"
            else None
        )
    if _is_direct_windows_executable_app_result(result):
        return "launch_app"
    if result.kind == "file":
        return "open_file"
    return None


def listary_state_dir() -> Path:
    override = os.environ.get("SPIFFY_LISTARY_STATE_DIR") or os.environ.get("LISTARY_MAC_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "ListaryMac" / "state"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ListaryMac" / "state"
    return Path.home() / ".local" / "share" / "spiffy-listary" / "state"


class ListaryAgentClient:
    """Bounded authenticated client for ``rust/listary-agent`` protocol v1."""

    def __init__(self, state_dir: str | Path | None = None, *, timeout_seconds: float = 5.0) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else listary_state_dir()
        self.endpoint_path = self.state_dir / _ENDPOINT_FILE
        self.timeout_seconds = max(0.25, min(float(timeout_seconds), 30.0))
        self._connection_local = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._connection_generation = 0

    def close(self) -> None:
        with self._connections_lock:
            self._connection_generation += 1
            connections = tuple(self._connections)
            self._connections.clear()
        for stream in connections:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass
        self._connection_local.stream = None
        self._connection_local.key = None
        self._connection_local.generation = -1

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _endpoint(self) -> tuple[str, int, str]:
        try:
            payload = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ListaryAgentError(
                "not_running",
                "Indexed search agent is not running. Open Spiffy Listary and start the resident service, then try again.",
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ListaryAgentError("endpoint_invalid", f"Indexed search endpoint could not be read: {exc}") from exc
        if not isinstance(payload, dict):
            raise ListaryAgentError("endpoint_invalid", "Indexed search endpoint has an invalid format.")
        if int(payload.get("schema_version") or 0) != 1 or int(payload.get("protocol_version") or 0) != _PROTOCOL_VERSION:
            raise ListaryAgentError("endpoint_version", "Indexed search needs a compatible Spiffy Listary version.")
        token = str(payload.get("token") or "")
        address = str(payload.get("address") or "")
        host, separator, port_text = address.rpartition(":")
        try:
            port = int(port_text) if separator else 0
            ip = ipaddress.ip_address(host.strip("[]"))
        except (ValueError, TypeError) as exc:
            raise ListaryAgentError("endpoint_invalid", "Indexed search endpoint address is invalid.") from exc
        if not ip.is_loopback or not (1 <= port <= 65535) or len(token) < 16:
            raise ListaryAgentError("endpoint_invalid", "Indexed search endpoint failed local authentication checks.")
        return str(ip), port, token

    def _request(
        self,
        method: str,
        *,
        request_id: int | None = None,
        params: Any = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        host, port, token = self._endpoint()
        timeout = self.timeout_seconds if timeout_seconds is None else max(0.25, min(float(timeout_seconds), 900.0))
        correlation_id = uuid4().hex
        request = {
            "token": token,
            "id": correlation_id,
            "method": str(method),
            "request_id": request_id,
            "params": params if params is not None else None,
        }
        encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        buffer = self._request_frame(host, port, token, encoded, timeout)
        return self._decode_response_frame(buffer, correlation_id)

    @staticmethod
    def _read_response_frame(stream: socket.socket) -> bytes:
        buffer = bytearray()
        while len(buffer) <= _MAX_RESPONSE_BYTES:
            chunk = stream.recv(min(65536, _MAX_RESPONSE_BYTES + 1 - len(buffer)))
            if not chunk:
                break
            buffer.extend(chunk)
            newline = buffer.find(b"\n")
            if newline >= 0:
                del buffer[newline + 1 :]
                break
        if len(buffer) > _MAX_RESPONSE_BYTES:
            raise ListaryAgentError("response_too_large", "Indexed search returned an oversized response.")
        if not buffer:
            raise ConnectionError("Indexed search returned no response.")
        return bytes(buffer)

    def _discard_thread_connection(self) -> None:
        stream = getattr(self._connection_local, "stream", None)
        if stream is not None:
            with self._connections_lock:
                self._connections.discard(stream)
            try:
                stream.close()
            except OSError:
                pass
        self._connection_local.stream = None
        self._connection_local.key = None
        self._connection_local.generation = -1

    def _connection(self, host: str, port: int, token: str, timeout: float) -> socket.socket:
        key = (str(host), int(port), str(token))
        while True:
            with self._connections_lock:
                generation = self._connection_generation
            stream = getattr(self._connection_local, "stream", None)
            if (
                stream is not None
                and getattr(self._connection_local, "key", None) == key
                and getattr(self._connection_local, "generation", -1) == generation
                and stream.fileno() >= 0
            ):
                stream.settimeout(timeout)
                return stream

            self._discard_thread_connection()
            try:
                candidate = socket.create_connection((host, port), timeout=timeout)
                candidate.settimeout(timeout)
                candidate.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, TimeoutError) as exc:
                raise ListaryAgentError(
                    "unreachable",
                    "Indexed search agent is unavailable. Open Spiffy Listary and start the resident service, then try again.",
                ) from exc
            with self._connections_lock:
                if generation != self._connection_generation:
                    candidate.close()
                    continue
                self._connections.add(candidate)
            self._connection_local.stream = candidate
            self._connection_local.key = key
            self._connection_local.generation = generation
            return candidate

    def _request_frame(self, host: str, port: int, token: str, encoded: bytes, timeout: float) -> bytes:
        stream = self._connection(host, port, token, timeout)
        try:
            stream.sendall(encoded)
            return self._read_response_frame(stream)
        except (OSError, TimeoutError, ConnectionError) as exc:
            self._discard_thread_connection()
            raise ListaryAgentError(
                "unreachable",
                "Indexed search agent is unavailable. Open Spiffy Listary and start the resident service, then try again.",
            ) from exc

    @staticmethod
    def _decode_response_frame(buffer: bytes, correlation_id: str) -> Any:
        try:
            response = json.loads(buffer.strip().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ListaryAgentError("invalid_response", "Indexed search returned invalid data.") from exc
        if not isinstance(response, dict) or response.get("id") != correlation_id:
            raise ListaryAgentError("invalid_response", "Indexed search returned a mismatched response.")
        if not response.get("ok"):
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise ListaryAgentError(str(error.get("code") or "search_failed"), str(error.get("message") or "Indexed search failed."))
        return response.get("data")

    def search(
        self,
        query: str,
        *,
        kinds: Iterable[str] = (),
        categories: Iterable[str] = (),
        limit: int = 40,
        request_id: int | None = None,
    ) -> ListarySearchPage:
        category_values = tuple(str(value or "").strip().casefold() for value in categories if str(value or "").strip())
        allowed_categories = {"folder", "document", "picture", "video", "audio", "other"}
        if any(value not in allowed_categories for value in category_values):
            raise ValueError("unsupported quick-search category")
        request_id = int(request_id or secrets.randbits(63) or 1)
        now = time.time()
        local_offset_minutes = int((datetime_offset_seconds(now)) // 60)
        data = self._request(
            "search",
            request_id=request_id,
            params={
                "query": str(query),
                "limit": max(1, min(int(limit), 100)),
                "candidate_limit": 4096,
                "include_unavailable": False,
                "root_ids": [],
                "kinds": [str(kind) for kind in kinds],
                "categories": list(category_values),
                "modified_time": None,
                "context": {"current_folder": None, "contextual": False},
                "now_unix_ms": int(now * 1000),
                "timezone_offset_minutes": local_offset_minutes,
            },
        )
        if not isinstance(data, dict):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid result page.")
        rows: list[ListarySearchResult] = []
        for raw in data.get("results") if isinstance(data.get("results"), list) else []:
            item = raw.get("item") if isinstance(raw, dict) else None
            parsed_result = _search_result_from_item(item)
            if parsed_result is not None:
                rows.append(parsed_result)
        return ListarySearchPage(
            tuple(rows),
            elapsed_micros=max(0, int(data.get("elapsed_micros") or 0)),
            considered_candidates=max(0, int(data.get("considered_candidates") or 0)),
        )


    def file_search(
        self,
        query: str,
        *,
        categories: Iterable[str] = (),
        modified_time: str | None = None,
        offset: int = 0,
        limit: int = 100,
        sort_field: str = "relevance",
        sort_direction: str = "descending",
        expected_catalog_generation: int | None = None,
        include_unavailable: bool = False,
        now_unix_ms: int | None = None,
        timezone_offset_minutes: int | None = None,
        request_id: int | None = None,
        include_facets: bool = False,
    ) -> ListaryFileSearchPage:
        category_values = tuple(str(value) for value in categories)
        allowed_categories = {"folder", "document", "picture", "video", "audio", "other"}
        if any(value not in allowed_categories for value in category_values):
            raise ValueError("unsupported File Search category")
        allowed_modified_time = {None, "last_1_day", "last_7_days", "last_30_days", "last_365_days", "older"}
        if modified_time not in allowed_modified_time:
            raise ValueError("unsupported File Search modified-time facet")
        if sort_field not in {"relevance", "name", "path", "kind", "modified", "size"}:
            raise ValueError("unsupported File Search sort field")
        if sort_direction not in {"ascending", "descending"}:
            raise ValueError("unsupported File Search sort direction")
        request_id = int(request_id or secrets.randbits(63) or 1)
        now = time.time()
        request_now_unix_ms = int(now * 1000) if now_unix_ms is None else int(now_unix_ms)
        request_timezone_offset = (
            int(datetime_offset_seconds(now) // 60)
            if timezone_offset_minutes is None
            else int(timezone_offset_minutes)
        )
        data = self._request(
            "file_search",
            request_id=request_id,
            params={
                "query": str(query),
                "include_unavailable": bool(include_unavailable),
                "root_ids": [],
                "categories": list(category_values),
                "modified_time": modified_time,
                "now_unix_ms": request_now_unix_ms,
                "timezone_offset_minutes": request_timezone_offset,
                "offset": max(0, int(offset)),
                "limit": max(1, min(int(limit), 1000)),
                "sort_field": sort_field,
                "sort_direction": sort_direction,
                "expected_catalog_generation": (
                    int(expected_catalog_generation) if expected_catalog_generation is not None else None
                ),
                "include_facets": bool(include_facets),
            },
            timeout_seconds=max(self.timeout_seconds, 30.0),
        )
        if not isinstance(data, dict):
            raise ListaryAgentError("invalid_response", "Deep File Search returned an invalid result page.")
        rows: list[ListarySearchResult] = []
        for raw in data.get("results") if isinstance(data.get("results"), list) else []:
            item = raw.get("item") if isinstance(raw, dict) else None
            parsed_result = _search_result_from_item(item)
            if parsed_result is not None:
                rows.append(parsed_result)
        return ListaryFileSearchPage(
            tuple(rows),
            total_matches=max(0, int(data.get("total_matches") or 0)),
            offset=max(0, int(data.get("offset") or 0)),
            limit=max(1, int(data.get("limit") or limit)),
            has_more=bool(data.get("has_more")),
            catalog_generation=max(0, int(data.get("catalog_generation") or 0)),
            considered_candidates=max(0, int(data.get("considered_candidates") or 0)),
            elapsed_micros=max(0, int(data.get("elapsed_micros") or 0)),
            facets=_file_search_facets_from_response(data.get("facets")),
        )

    def cancel(self, request_id: int) -> None:
        self._request("cancel", request_id=int(request_id), params=None)

    def get_item(self, item_id: int) -> ListarySearchResult | None:
        data = self._request(
            "get_item",
            params={"item_id": int(item_id)},
            timeout_seconds=min(self.timeout_seconds, 0.5),
        )
        if data is None:
            return None
        result = _search_result_from_item(data)
        if result is None:
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid item record.")
        return result

    def record_activation(self, item_id: int, *, source: str = "launcher") -> None:
        source_value = str(source or "launcher")
        if source_value not in {"launcher", "file_search", "file_manager", "quick_switch", "api"}:
            raise ValueError("unsupported activation source")
        self._request(
            "record_activation",
            params={"item_id": int(item_id), "now_unix_ms": int(time.time() * 1000), "source": source_value},
        )

    def get_favorites(self) -> dict[str, Any]:
        data = self._request("get_favorites", params=None)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("items"), list)
            or not isinstance(data.get("pins"), list)
        ):
            raise ListaryAgentError("invalid_response", "Indexed search returned invalid favorites settings.")
        return dict(data)

    def toggle_pin(self, item_id: int) -> bool:
        data = self._request("toggle_pin", params={"item_id": int(item_id)})
        if not isinstance(data, dict) or not isinstance(data.get("pinned"), bool):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid pin result.")
        return bool(data["pinned"])

    @staticmethod
    def _validated_filter_record(data: object) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid saved-filter record.")
        filter_id = str(data.get("id") or "").strip()
        keyword = str(data.get("keyword") or "").strip()
        name = str(data.get("name") or "").strip()
        target = str(data.get("target") or "any").strip().casefold()
        extra_query = data.get("extra_query")
        search_paths = data.get("search_paths")
        extensions = data.get("extensions")
        try:
            UUID(filter_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid saved-filter record.") from exc
        if (
            not filter_id
            or not keyword
            or not name
            or target not in {"any", "files", "folders"}
            or not isinstance(extra_query, str)
            or not isinstance(search_paths, list)
            or not all(isinstance(path, str) for path in search_paths)
            or not isinstance(extensions, list)
            or not all(isinstance(extension, str) for extension in extensions)
        ):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid saved-filter record.")
        return dict(data)

    def list_filters(self) -> tuple[dict[str, Any], ...]:
        data = self._request("list_filters", params=None)
        if not isinstance(data, list):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid saved-filter list.")
        return tuple(self._validated_filter_record(row) for row in data)

    def put_filter(self, preset: dict[str, Any]) -> dict[str, Any]:
        data = self._request("put_filter", params=dict(preset))
        return self._validated_filter_record(data)

    def remove_filter(self, filter_id: str) -> None:
        data = self._request("remove_filter", params={"filter_id": str(filter_id)})
        if not isinstance(data, dict) or data.get("removed") is not True:
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid saved-filter removal result.")


    def list_roots(self) -> tuple[dict[str, Any], ...]:
        data = self._request("list_roots", params=None)
        if not isinstance(data, list):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid root list.")
        return tuple(row for row in data if isinstance(row, dict))

    def add_root(self, path: str | Path, *, kind: str = "local") -> dict[str, Any]:
        data = self._request("add_root", params={"path": str(Path(path)), "kind": str(kind)})
        if not isinstance(data, dict) or not data.get("id"):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid root record.")
        return data

    def put_root(self, root: dict[str, Any]) -> dict[str, Any]:
        payload = dict(root)
        data = self._request("put_root", params=payload)
        if not isinstance(data, dict) or not data.get("id"):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid root record.")
        return data

    def remove_root(self, root_id: str) -> None:
        data = self._request("remove_root", params={"root_id": str(root_id)})
        if not isinstance(data, dict) or data.get("removed") is not True:
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid root removal result.")

    def get_or_add_root(self, path: str | Path, *, kind: str = "local") -> dict[str, Any]:
        requested = os.path.normcase(os.path.normpath(str(Path(path).expanduser().absolute())))
        for root in self.list_roots():
            current = str(root.get("path") or "")
            if current and os.path.normcase(os.path.normpath(current)) == requested:
                return root
        return self.add_root(path, kind=kind)

    def reconcile_root(self, root_id: str, *, request_id: int | None = None) -> dict[str, Any]:
        request_id = int(request_id or secrets.randbits(63) or 1)
        data = self._request(
            "reconcile_root",
            request_id=request_id,
            params={"root_id": str(root_id)},
            timeout_seconds=900.0,
        )
        if not isinstance(data, dict):
            raise ListaryAgentError("invalid_response", "Indexed search returned an invalid indexing report.")
        return data


def _revalidate_result_for_action(
    client: Any,
    displayed: ListarySearchResult,
    *,
    refresh_label: str,
) -> tuple[ListarySearchResult | None, _ResultPresentation | None, str]:
    """Resolve the authoritative item immediately before a user side effect.

    Production clients always expose ``get_item``.  Test doubles that predate
    the action-currentness contract may omit it; those keep their fixture row so
    unrelated presentation tests do not acquire a fake catalog owner.
    """

    getter = getattr(client, "get_item", None)
    current = displayed
    if callable(getter):
        attempt = 0
        while True:
            try:
                current = getter(displayed.item_id)
                break
            except ListaryAgentError as exc:
                if exc.code == "unreachable" and attempt == 0:
                    # No native side effect has started. The client already
                    # discarded the broken socket, so one fresh endpoint/
                    # connection attempt is safe and cannot replay an action.
                    attempt += 1
                    continue
                return None, None, f"Could not revalidate the selected result: {exc}"
            except Exception as exc:
                return None, None, f"Could not revalidate the selected result: {exc}"
        if current is None:
            return None, None, f"The selected result no longer exists. {refresh_label}"
        if current.item_id != displayed.item_id:
            return None, None, f"The selected result changed identity. {refresh_label}"
        if current.kind != displayed.kind:
            return None, None, f"The selected result changed identity. {refresh_label}"
        if displayed.root_id and current.root_id != displayed.root_id:
            return None, None, f"The selected result changed indexed root. {refresh_label}"
        if displayed.stable_file_id and current.stable_file_id != displayed.stable_file_id:
            return None, None, f"The selected result was replaced. {refresh_label}"
        if not current.available:
            return None, None, f"The selected result is currently unavailable. {refresh_label}"

    presentation = _result_presentation(current)
    if presentation.action_path is None:
        return None, None, presentation.action_error or "The selected result is unavailable."
    return current, presentation, ""


def _require_result_target_unchanged(
    client: Any,
    expected: ListarySearchResult,
    expected_action_path: str,
    *,
    refresh_label: str,
) -> None:
    """Re-prove one already-admitted target immediately before a native mutation."""

    current, presentation, error = _revalidate_result_for_action(
        client,
        expected,
        refresh_label=refresh_label,
    )
    if current is None or presentation is None:
        raise WindowsFileDialogError("stale_result", error)
    expected_key = os.path.normcase(os.path.normpath(os.path.abspath(expected_action_path)))
    current_key = os.path.normcase(os.path.normpath(os.path.abspath(presentation.action_path)))
    if current_key != expected_key:
        raise WindowsFileDialogError(
            "stale_result",
            f"The selected result moved again while the Open/Save action was in progress. {refresh_label}",
        )


def _perform_file_manager_action(
    client: Any,
    displayed: ListarySearchResult,
    *,
    operation: str,
    query_text: str,
    action_generation: int,
    result_generation: int,
    refresh_label: str,
    cancel_event: threading.Event,
    require_current_generation: Callable[[], None],
    result_ready_monotonic: float | None = None,
) -> tuple[ListarySearchResult, ListaryFileManagerReceipt]:
    """Revalidate one indexed row and carry it through the shared Explorer owner."""

    started = time.monotonic()
    current_for_receipt = displayed
    target_for_receipt = displayed.path

    def failure(
        code: str,
        detail: str,
        *,
        ambiguous: bool = False,
        dispatch_happened: bool = False,
        destination: object | None = None,
    ) -> ListaryFileManagerActionError:
        if isinstance(destination, dict):
            destination_payload = dict(destination)
        else:
            destination_payload = (
                destination.to_dict()
                if destination is not None and callable(getattr(destination, "to_dict", None))
                else None
            )
        return ListaryFileManagerActionError(
            ListaryFileManagerFailureReceipt(
                schema="listary_file_manager_failure_v1",
                outcome="failed",
                code=str(code),
                operation=str(operation),
                query_text=str(query_text),
                item_id=current_for_receipt.item_id,
                root_id=current_for_receipt.root_id,
                stable_file_id=current_for_receipt.stable_file_id,
                displayed_path=displayed.path,
                target_path=str(target_for_receipt),
                action_generation=int(action_generation),
                result_generation=int(result_generation),
                action_latency_ms=(time.monotonic() - started) * 1000.0,
                ambiguous=bool(ambiguous),
                dispatch_happened=bool(dispatch_happened),
                detail=str(detail),
                destination=destination_payload,
            )
        )

    try:
        require_current_generation()
        current, presentation, error = _revalidate_result_for_action(
            client,
            displayed,
            refresh_label=refresh_label,
        )
        if current is None or presentation is None:
            raise failure("stale_result", error)
        current_for_receipt = current
        target_for_receipt = presentation.action_path

        def revalidate_target() -> None:
            require_current_generation()
            if operation == "launch_workspace_app":
                latest, latest_presentation, latest_error = _revalidate_result_for_action(
                    client,
                    current,
                    refresh_label=refresh_label,
                )
                latest_contract = _workspace_application_contract(latest) if latest is not None else None
                admitted_contract = _workspace_application_contract(current)
                if (
                    latest is None
                    or latest_presentation is None
                    or latest_contract is None
                    or admitted_contract is None
                    or latest_contract != admitted_contract
                ):
                    raise AssociatedOpenError(
                        "stale_result",
                        latest_error or f"The {current.name} workspace launch contract changed. {refresh_label}",
                    )
            try:
                _require_result_target_unchanged(
                    client,
                    current,
                    presentation.action_path,
                    refresh_label=refresh_label,
                )
            except WindowsFileDialogError as exc:
                if operation in {"open_file", "launch_app", "launch_shortcut", "launch_workspace_app"}:
                    raise AssociatedOpenError("stale_result", str(exc)) from exc
                raise FileManagerActionError("stale_result", str(exc)) from exc

        require_current_generation()
        if operation == "open_file":
            if current.kind != "file":
                raise failure("target_kind_unsupported", "Default-application Open accepts only indexed files.")
            platform_receipt = open_file_verified(
                presentation.action_path,
                cancel_event=cancel_event,
                timeout_seconds=5.0,
                revalidate_target=revalidate_target,
            )
        elif operation == "launch_app":
            if current.kind != "app":
                raise failure("target_kind_unsupported", "Verified app launch accepts only indexed applications.")
            platform_receipt = open_app_verified(
                presentation.action_path,
                cancel_event=cancel_event,
                timeout_seconds=5.0,
                revalidate_target=revalidate_target,
            )
        elif operation == "launch_shortcut":
            if current.kind != "file" or Path(presentation.action_path).suffix.casefold() != ".lnk":
                raise failure("target_kind_unsupported", "Verified shortcut launch accepts only indexed .lnk files.")
            platform_receipt = open_shortcut_verified(
                presentation.action_path,
                cancel_event=cancel_event,
                timeout_seconds=5.0,
                revalidate_target=revalidate_target,
            )
        elif operation == "launch_workspace_app":
            contract = _workspace_application_contract(current)
            if contract is None:
                raise failure(
                    "target_kind_unsupported",
                    "Verified workspace launch supports only canonical workspace rows with app-owned acknowledgement.",
                )
            metadata, workspace_destination = contract
            if workspace_destination == "storyboard":
                platform_receipt = _launch_storyboard_workspace_verified(
                    metadata,
                    cancel_event=cancel_event,
                    revalidate_target=revalidate_target,
                )
            elif workspace_destination == "photo":
                platform_receipt = _launch_photo_workspace_verified(
                    metadata,
                    cancel_event=cancel_event,
                    revalidate_target=revalidate_target,
                )
            elif workspace_destination == "video":
                platform_receipt = _launch_video_workspace_verified(
                    metadata,
                    cancel_event=cancel_event,
                    revalidate_target=revalidate_target,
                )
            else:
                raise failure("target_kind_unsupported", "The workspace destination is unsupported.")
        else:
            platform_receipt = reveal_in_file_manager_verified(
                presentation.action_path,
                target_kind=current.kind,
                cancel_event=cancel_event,
                timeout_seconds=5.0,
                revalidate_target=revalidate_target,
                operation=operation,
            )
        result_to_ready = None
        if result_ready_monotonic is not None:
            action_admission_delay_ms = max(0.0, (started - result_ready_monotonic) * 1000.0)
            result_to_ready = action_admission_delay_ms + platform_receipt.destination_ready_latency_ms
        return current, ListaryFileManagerReceipt(
            schema="listary_file_manager_receipt_v1",
            outcome="verified",
            operation=str(operation),
            query_text=str(query_text),
            item_id=current.item_id,
            root_id=current.root_id,
            stable_file_id=current.stable_file_id,
            displayed_path=displayed.path,
            target_path=presentation.action_path,
            action_generation=int(action_generation),
            result_generation=int(result_generation),
            result_to_destination_ready_ms=result_to_ready,
            destination=platform_receipt,
        )
    except ListaryFileManagerActionError:
        raise
    except FileManagerActionError as exc:
        raise failure(
            exc.code,
            str(exc),
            ambiguous=exc.ambiguous,
            dispatch_happened=exc.dispatch_happened,
            destination=exc.destination,
        ) from exc
    except AssociatedOpenError as exc:
        raise failure(
            exc.code,
            str(exc),
            ambiguous=exc.ambiguous,
            dispatch_happened=exc.dispatch_happened,
            destination={
                "process": exc.destination_process.to_dict() if exc.destination_process is not None else None,
                "window": exc.destination_window.to_dict() if exc.destination_window is not None else None,
            },
        ) from exc
    except WorkspaceApplicationLaunchError as exc:
        raise failure(
            exc.code,
            str(exc),
            ambiguous=exc.ambiguous,
            dispatch_happened=exc.dispatch_happened,
            destination=exc.destination,
        ) from exc
    except Exception as exc:
        raise failure("file_manager_action_failed", str(exc)) from exc


def _start_listary_file_manager_action(
    owner: Any,
    displayed: ListarySearchResult,
    *,
    operation: str,
    result_generation: int,
    current_generation: Callable[[], int],
    refresh_label: str,
    result_ready_monotonic: float | None = None,
    activation_source: str | None = None,
    close_on_success: bool = False,
) -> bool:
    workspace_contract = _workspace_application_contract(displayed) if operation == "launch_workspace_app" else None
    if operation == "open_file" and displayed.kind != "file":
        owner.status.setText("Default-application Open accepts only indexed files.")
        return False
    if operation == "launch_app" and displayed.kind != "app":
        owner.status.setText("Verified app launch accepts only indexed applications.")
        return False
    if operation == "launch_workspace_app" and workspace_contract is None:
        owner.status.setText("Verified workspace launch requires a canonical workspace row with app-owned acknowledgement.")
        return False
    if operation == "launch_shortcut" and (
        displayed.kind != "file" or Path(displayed.path).suffix.casefold() != ".lnk"
    ):
        owner.status.setText("Verified shortcut launch accepts only indexed .lnk files.")
        return False
    if operation not in {"open_file", "launch_app", "launch_shortcut", "launch_workspace_app"} and displayed.kind not in {
        "file",
        "folder",
    }:
        owner.status.setText("Explorer actions accept only indexed files and folders.")
        return False
    owner._cancel_file_manager_action()
    owner._file_manager_action_generation += 1
    generation = owner._file_manager_action_generation
    owner._last_file_manager_receipt = None
    owner._last_file_manager_failure_receipt = None
    if hasattr(owner, "_last_file_manager_batch_receipt"):
        owner._last_file_manager_batch_receipt = None
        owner._last_file_manager_batch_receipts = ()
    cancel_event = threading.Event()
    owner._file_manager_cancel_event = cancel_event
    query_text = owner.search.text().strip()
    owner.reveal_button.setEnabled(False)
    owner.open_button.setEnabled(False)
    if operation == "open_file":
        owner.status.setText("Opening in the verified default application...")
    elif operation == "launch_app":
        owner.status.setText("Launching the verified executable...")
    elif operation == "launch_shortcut":
        owner.status.setText("Launching the verified executable shortcut...")
    elif operation == "launch_workspace_app":
        workspace_label = workspace_contract[0].label if workspace_contract is not None else displayed.name
        owner.status.setText(f"Opening {workspace_label} and waiting for its app-owned receipt...")
    else:
        verb = "Opening folder" if operation == "open_folder" else "Revealing"
        owner.status.setText(f"{verb} in verified Explorer...")

    def require_current_generation() -> None:
        if cancel_event.is_set():
            raise FileManagerActionError(
                "cancelled",
                "The native action was cancelled before the next Shell dispatch.",
            )
        if generation != owner._file_manager_action_generation or result_generation != current_generation():
            raise FileManagerActionError(
                "stale_generation",
                "The query/result generation changed before the native action could run.",
            )

    def perform() -> tuple[ListarySearchResult, ListaryFileManagerReceipt]:
        return _perform_file_manager_action(
            owner.client,
            displayed,
            operation=operation,
            query_text=query_text,
            action_generation=generation,
            result_generation=result_generation,
            refresh_label=refresh_label,
            cancel_event=cancel_event,
            require_current_generation=require_current_generation,
            result_ready_monotonic=result_ready_monotonic,
        )

    worker = _ResultActionWorker(generation, perform)
    owner._file_manager_action_workers.add(worker)

    def finished(got_generation: int, payload: object, error: object, owned: _ResultActionWorker = worker) -> None:
        owner._file_manager_action_workers.discard(owned)
        if got_generation != owner._file_manager_action_generation:
            if (
                isinstance(error, ListaryFileManagerActionError)
                and error.ambiguous
                and isinstance(error.receipt, ListaryFileManagerFailureReceipt)
            ):
                owner._last_file_manager_failure_receipt = error.receipt
            return
        owner._file_manager_cancel_event = None
        if isinstance(error, ListaryFileManagerActionError):
            owner._last_file_manager_failure_receipt = error.receipt
            fallback = {
                "open_file": "Default-application Open failed.",
                "launch_app": "Verified app launch failed.",
                "launch_shortcut": "Verified shortcut launch failed.",
                "launch_workspace_app": "Verified workspace launch failed.",
            }.get(operation, "Explorer action failed.")
            message = str(error) or fallback
            if error.ambiguous:
                surface = {
                    "open_file": "Default application state",
                    "launch_app": "Application state",
                    "launch_shortcut": "Shortcut application state",
                    "launch_workspace_app": (
                        f"{workspace_contract[0].label} state"
                        if workspace_contract is not None
                        else "Workspace application state"
                    ),
                }.get(operation, "Explorer state")
                message = f"{surface} may have changed; no retry was attempted. {message}"
            owner.status.setText(message)
            owner._sync_result_state()
            return
        if (
            error is not None
            or not isinstance(payload, tuple)
            or len(payload) != 2
            or not isinstance(payload[0], ListarySearchResult)
            or not isinstance(payload[1], ListaryFileManagerReceipt)
        ):
            invalid = {
                "open_file": "Default application returned an invalid receipt.",
                "launch_app": "Application launch returned an invalid receipt.",
                "launch_shortcut": "Shortcut launch returned an invalid receipt.",
                "launch_workspace_app": (
                    f"{workspace_contract[0].label} launch returned an invalid receipt."
                    if workspace_contract is not None
                    else "Workspace launch returned an invalid receipt."
                ),
            }.get(operation, "Explorer returned an invalid receipt.")
            owner.status.setText(str(error) if error is not None else invalid)
            owner._sync_result_state()
            return
        current, receipt = payload
        owner._last_file_manager_receipt = receipt
        if hasattr(owner, "_last_result_to_file_manager_ready_ms"):
            owner._last_result_to_file_manager_ready_ms = receipt.result_to_destination_ready_ms
        relevance_changed = False
        if activation_source == "file_search" and hasattr(owner, "_record_file_search_activations"):
            owner._record_file_search_activations((current.item_id,))
            relevance_changed = bool(owner._invalidate_relevance_paging_after_activation())
        elif activation_source:
            _start_tracked_one_shot(
                owner._activation_workers,
                lambda item_id=current.item_id, source=activation_source: owner.client.record_activation(
                    item_id,
                    source=source,
                ),
            )
        if isinstance(receipt.destination, AssociatedOpenReceipt):
            destination = receipt.destination.destination_window
            message = (
                f"Verified file Open in {receipt.destination.handler.friendly_name or 'the default application'} "
                f"(PID {receipt.destination.destination_process.process_id}, HWND {destination.hwnd}) "
                f"in {receipt.destination.action_latency_ms:.1f} ms."
            )
        elif isinstance(receipt.destination, DirectAppLaunchReceipt):
            destination = receipt.destination.destination_window
            message = (
                f"Verified app launch for {Path(receipt.destination.executable_path).name} "
                f"(PID {receipt.destination.destination_process.process_id}, HWND {destination.hwnd}) "
                f"in {receipt.destination.action_latency_ms:.1f} ms."
            )
        elif isinstance(receipt.destination, DirectShortcutLaunchReceipt):
            destination = receipt.destination.destination_window
            message = (
                f"Verified shortcut launch for {Path(receipt.destination.executable_path).name} "
                f"(PID {receipt.destination.destination_process.process_id}, HWND {destination.hwnd}) "
                f"in {receipt.destination.action_latency_ms:.1f} ms."
            )
        elif isinstance(receipt.destination, WorkspaceApplicationLaunchReceipt):
            route = "existing owner" if receipt.destination.delivery_mode == "warm_ipc" else "canonical launcher"
            message = (
                f"Verified {receipt.destination.label} through its app-owned receipt via {route} "
                f"in {receipt.destination.action_latency_ms:.1f} ms."
            )
        else:
            destination = receipt.destination.destination
            message = (
                f"Verified {operation.replace('_', ' ')} in Explorer "
                f"(HWND {destination.hwnd}) in {receipt.destination.action_latency_ms:.1f} ms."
            )
        if relevance_changed:
            message += " Relevance history changed; refresh before continuing paging."
        owner.status.setText(message)
        if close_on_success:
            owner.accept()
        else:
            owner._sync_result_state()

    worker.signals.finished.connect(finished)
    QThreadPool.globalInstance().start(worker)
    return True


def _start_listary_file_manager_batch_open(
    owner: Any,
    displayed_results: tuple[ListarySearchResult, ...],
    *,
    result_generation: int,
    current_generation: Callable[[], int],
    refresh_label: str,
    operation: str = "open_file",
) -> bool:
    item_operations: tuple[str, ...]
    if operation == "open_file":
        eligible = all(result.kind == "file" and not _is_windows_shortcut_file_result(result) for result in displayed_results)
        item_operations = tuple("open_file" for _result in displayed_results)
        action_label = "batch Open"
        progress_text = f"Opening {len(displayed_results)} files with verified default-application delivery..."
        receipt_operation = "open_file_batch"
    elif operation == "launch_app":
        eligible = all(_is_direct_windows_executable_app_result(result) for result in displayed_results)
        item_operations = tuple("launch_app" for _result in displayed_results)
        action_label = "app batch launch"
        progress_text = f"Launching {len(displayed_results)} apps with verified executable delivery..."
        receipt_operation = "launch_app_batch"
    elif operation == "launch_shortcut":
        eligible = all(_is_verifiable_windows_shortcut_result(result) for result in displayed_results)
        item_operations = tuple("launch_shortcut" for _result in displayed_results)
        action_label = "shortcut batch launch"
        progress_text = f"Launching {len(displayed_results)} shortcuts with verified executable delivery..."
        receipt_operation = "launch_shortcut_batch"
    elif operation == "mixed_verified":
        resolved = tuple(_verified_windows_mixed_batch_item_operation(result) for result in displayed_results)
        eligible = bool(sys.platform == "win32" and all(resolved) and len(set(resolved)) > 1)
        item_operations = tuple(str(value) for value in resolved if value is not None)
        action_label = "mixed verified batch"
        progress_text = f"Opening {len(displayed_results)} mixed items through their verified destination owners..."
        receipt_operation = "mixed_verified_batch"
    else:
        owner.status.setText("Unsupported verified batch action.")
        return False
    if len(displayed_results) < 2 or not eligible:
        invalid_batch_message = {
            "open_file": "Verified batch Open accepts two or more indexed files.",
            "launch_app": "Verified app batch launch accepts two or more direct executable applications.",
            "launch_shortcut": "Verified shortcut batch launch accepts two or more direct-executable shortcuts.",
            "mixed_verified": "Verified mixed batch Open accepts two or more rows backed by existing verified file/app/shortcut owners.",
        }[operation]
        owner.status.setText(invalid_batch_message)
        return False

    owner._cancel_file_manager_action()
    owner._file_manager_action_generation += 1
    generation = owner._file_manager_action_generation
    owner._last_file_manager_receipt = None
    owner._last_file_manager_failure_receipt = None
    owner._last_file_manager_batch_receipt = None
    owner._last_file_manager_batch_receipts = ()
    cancel_event = threading.Event()
    owner._file_manager_cancel_event = cancel_event
    query_text = owner.search.text().strip()
    owner.reveal_button.setEnabled(False)
    owner.open_button.setEnabled(False)
    owner.status.setText(progress_text)

    def require_current_generation() -> None:
        if cancel_event.is_set():
            raise FileManagerActionError(
                "cancelled",
                f"The {action_label} was cancelled before the next Shell dispatch.",
            )
        if generation != owner._file_manager_action_generation or result_generation != current_generation():
            raise FileManagerActionError(
                "stale_generation",
                f"The query/result generation changed before the next {action_label} step could run.",
            )

    def perform() -> ListaryFileManagerBatchReceipt:
        started = time.monotonic()
        completed: list[ListaryFileManagerReceipt] = []
        failure: ListaryFileManagerFailureReceipt | None = None
        for displayed, item_operation in zip(displayed_results, item_operations, strict=True):
            try:
                _current, receipt = _perform_file_manager_action(
                    owner.client,
                    displayed,
                    operation=item_operation,
                    query_text=query_text,
                    action_generation=generation,
                    result_generation=result_generation,
                    refresh_label=refresh_label,
                    cancel_event=cancel_event,
                    require_current_generation=require_current_generation,
                )
            except ListaryFileManagerActionError as exc:
                failure = exc.receipt
                break
            completed.append(receipt)
        outcome = "verified" if failure is None else ("partial" if completed else "failed")
        return ListaryFileManagerBatchReceipt(
            schema="listary_file_manager_batch_receipt_v1",
            outcome=outcome,
            operation=receipt_operation,
            query_text=query_text,
            action_generation=generation,
            result_generation=result_generation,
            requested_item_ids=tuple(result.item_id for result in displayed_results),
            completed=tuple(completed),
            failure=failure,
            action_latency_ms=(time.monotonic() - started) * 1000.0,
        )

    worker = _ResultActionWorker(generation, perform)
    owner._file_manager_action_workers.add(worker)

    def finished(got_generation: int, payload: object, error: object, owned: _ResultActionWorker = worker) -> None:
        owner._file_manager_action_workers.discard(owned)
        receipt = payload if isinstance(payload, ListaryFileManagerBatchReceipt) else None
        if got_generation != owner._file_manager_action_generation:
            if receipt is not None:
                completed_ids = tuple(item.item_id for item in receipt.completed)
                same_result_generation = result_generation == current_generation()
                if completed_ids:
                    owner._record_file_search_activations(completed_ids)
                    if same_result_generation:
                        owner._invalidate_relevance_paging_after_activation()
                retired_without_replacement = (
                    same_result_generation
                    and owner._file_manager_action_generation == got_generation + 1
                    and owner._file_manager_cancel_event is None
                )
                if retired_without_replacement:
                    owner._last_file_manager_batch_receipt = receipt
                    owner._last_file_manager_batch_receipts = receipt.completed
                    owner._last_file_manager_receipt = receipt.completed[-1] if receipt.completed else None
                    owner._last_file_manager_failure_receipt = receipt.failure
                elif receipt.failure is not None and receipt.failure.ambiguous:
                    owner._last_file_manager_failure_receipt = receipt.failure
            return
        owner._file_manager_cancel_event = None
        if error is not None or receipt is None:
            invalid = {
                "open_file": "Batch Open returned an invalid receipt.",
                "launch_app": "App batch launch returned an invalid receipt.",
                "launch_shortcut": "Shortcut batch launch returned an invalid receipt.",
                "mixed_verified": "Mixed verified batch returned an invalid receipt.",
            }[operation]
            owner.status.setText(str(error) if error is not None else invalid)
            owner._sync_result_state()
            return

        owner._last_file_manager_batch_receipt = receipt
        owner._last_file_manager_batch_receipts = receipt.completed
        owner._last_file_manager_receipt = receipt.completed[-1] if receipt.completed else None
        owner._last_file_manager_failure_receipt = receipt.failure
        completed_ids = tuple(item.item_id for item in receipt.completed)
        if completed_ids:
            owner._record_file_search_activations(completed_ids)
        relevance_changed = bool(completed_ids) and owner._invalidate_relevance_paging_after_activation()

        if receipt.failure is None:
            if operation == "open_file":
                message = (
                    f"Verified Open for {len(receipt.completed)} selected files. "
                    f"Batch completed in {receipt.action_latency_ms:.1f} ms."
                )
            elif operation == "launch_shortcut":
                message = (
                    f"Verified launch for {len(receipt.completed)} selected shortcuts. "
                    f"Batch completed in {receipt.action_latency_ms:.1f} ms."
                )
            elif operation == "mixed_verified":
                message = (
                    f"Verified {len(receipt.completed)} mixed selected destinations. "
                    f"Batch completed in {receipt.action_latency_ms:.1f} ms."
                )
            else:
                message = (
                    f"Verified launch for {len(receipt.completed)} selected apps. "
                    f"Batch completed in {receipt.action_latency_ms:.1f} ms."
                )
        else:
            stopped_number = len(receipt.completed) + 1
            if operation == "open_file":
                message = (
                    f"Verified {len(receipt.completed)} of {len(receipt.requested_item_ids)} selected file opens; "
                    f"stopped at item {stopped_number}: {receipt.failure.detail}"
                )
            elif operation == "launch_shortcut":
                message = (
                    f"Verified {len(receipt.completed)} of {len(receipt.requested_item_ids)} selected shortcut launches; "
                    f"stopped at item {stopped_number}: {receipt.failure.detail}"
                )
            elif operation == "mixed_verified":
                message = (
                    f"Verified {len(receipt.completed)} of {len(receipt.requested_item_ids)} mixed selected destinations; "
                    f"stopped at item {stopped_number}: {receipt.failure.detail}"
                )
            else:
                message = (
                    f"Verified {len(receipt.completed)} of {len(receipt.requested_item_ids)} selected app launches; "
                    f"stopped at item {stopped_number}: {receipt.failure.detail}"
                )
            if receipt.failure.ambiguous:
                message += (
                    " Default application state may have changed; no retry was attempted."
                    if operation == "open_file"
                    else (
                        " Shortcut application state may have changed; no retry was attempted."
                        if operation == "launch_shortcut"
                        else (
                            " Destination state may have changed; no retry was attempted."
                            if operation == "mixed_verified"
                            else " Application state may have changed; no retry was attempted."
                        )
                    )
                )
        if relevance_changed:
            message += " Relevance history changed; refresh before continuing paging."
        owner.status.setText(message)
        owner._sync_result_state()

    worker.signals.finished.connect(finished)
    QThreadPool.globalInstance().start(worker)
    return True


def _attach_file_dialog_failure_receipt(
    adapter: WindowsFileDialogAdapter,
    context: WindowsFileDialogContext,
    error: WindowsFileDialogError,
    result: ListarySearchResult,
    *,
    query_text: str,
    activation_generation: int,
    action_generation: int,
    target_path: str | None = None,
) -> WindowsFileDialogError:
    """Attach provenance to failures that happen before adapter mutation."""

    if isinstance(error.receipt, WindowsFileDialogFailureReceipt):
        return error
    presentation = _result_presentation(result)
    resolved_target = str(target_path or presentation.action_path or result.path)
    error.receipt = adapter.failure_receipt(
        context,
        code=error.code,
        detail=str(error),
        target_path=resolved_target,
        item_kind=result.kind,
        item_id=result.item_id,
        root_id=result.root_id,
        stable_file_id=result.stable_file_id,
        displayed_path=result.path,
        query_text=query_text,
        activation_generation=activation_generation,
        action_generation=action_generation,
        result_generation=activation_generation,
        accept_open=context.dialog_kind == "open" and result.kind == "file",
        accept_save=context.dialog_kind == "save" and result.kind in {"file", "folder"},
        ambiguous=error.ambiguous,
        acceptance_may_have_occurred=error.acceptance_may_have_occurred,
    )
    return error


class ListaryWorkspaceSearchAdapter:
    """Thin reusable projection of canonical Listary file results for Spiffy UIs.

    This adapter owns no catalog, cache, matcher, watcher, or ranking policy. It
    delegates to the resident Listary agent and only filters the already-ranked
    result page by inexpensive metadata already returned from ``listary-core``.
    """

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        timeout_seconds: float = 0.75,
        client: ListaryAgentClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or ListaryAgentClient(state_dir, timeout_seconds=timeout_seconds)
        self._root_admission_lock = threading.Lock()
        self._admitted_root_keys: set[str] = set()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _root_key(path: str | Path) -> str:
        return os.path.normcase(
            os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
        )

    @classmethod
    def _root_contains(cls, parent: str | Path, child: str | Path) -> bool:
        parent_key = cls._root_key(parent)
        child_key = cls._root_key(child)
        try:
            return os.path.commonpath((parent_key, child_key)) == parent_key
        except ValueError:
            return False

    @classmethod
    def _automatic_root_rejection(cls, path: str | Path) -> str:
        raw = str(path or "").strip()
        if not raw:
            return "automatic source root is empty"
        requested = Path(raw).expanduser().absolute()
        if requested.parent == requested:
            return "automatic source roots cannot be a filesystem volume/share root"
        try:
            home = Path.home().expanduser().absolute()
        except (OSError, RuntimeError):
            home = None
        if home is not None and cls._root_key(requested) == cls._root_key(home):
            return "automatic source roots cannot be the user home directory"
        return ""

    @staticmethod
    def _root_kind_for_path(path: str | Path) -> str:
        """Classify a Windows source root without creating another volume owner."""

        if os.name != "nt":
            return "local"
        text = str(path)
        if text.startswith("\\\\"):
            return "network"
        anchor = str(Path(path).anchor or "")
        if not anchor:
            return "local"
        try:
            import ctypes

            drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(anchor))
        except Exception:
            return "local"
        if drive_type == 4:  # DRIVE_REMOTE
            return "network"
        if drive_type in {2, 5}:  # DRIVE_REMOVABLE / DRIVE_CDROM
            return "removable"
        return "local"

    def _ensure_indexed_root_locked(
        self,
        requested: Path,
        roots: list[dict[str, Any]],
        *,
        request_id: int | None,
    ) -> ListaryRootAdmission:
        requested_key = self._root_key(requested)
        rejection = self._automatic_root_rejection(requested)
        if rejection:
            return ListaryRootAdmission(str(requested), "unsafe", detail=rejection)

        covering: dict[str, Any] | None = None
        narrower_overlap: dict[str, Any] | None = None
        for root in roots:
            root_path = str(root.get("path") or "")
            if not root_path:
                continue
            if self._root_contains(root_path, requested):
                covering = root
                break
            if self._root_contains(requested, root_path):
                narrower_overlap = root

        if covering is not None:
            root_id = str(covering.get("id") or "")
            was_cached = requested_key in self._admitted_root_keys
            if not bool(covering.get("enabled", True)):
                return ListaryRootAdmission(
                    str(requested),
                    "disabled",
                    root_id=root_id,
                    detail="covering Listary root is disabled by the user",
                )
            needs_reconcile = (
                covering.get("last_reconciled_unix_ms") is None
                or str(covering.get("availability") or "").casefold() != "available"
            )
            if needs_reconcile:
                self.client.reconcile_root(root_id, request_id=request_id)
            self._admitted_root_keys.add(requested_key)
            if was_cached and not needs_reconcile:
                status = "cached"
            else:
                status = "reconciled" if needs_reconcile else "covered"
            return ListaryRootAdmission(
                str(requested),
                status,
                root_id=root_id,
                reconciled=needs_reconcile,
            )

        if not requested.is_dir():
            return ListaryRootAdmission(
                str(requested),
                "missing",
                detail="workspace source root does not exist",
            )
        if narrower_overlap is not None:
            return ListaryRootAdmission(
                str(requested),
                "overlap",
                root_id=str(narrower_overlap.get("id") or ""),
                detail="a narrower Listary root already occupies this source tree",
            )

        root_kind = self._root_kind_for_path(requested)
        if root_kind != "local":
            return ListaryRootAdmission(
                str(requested),
                "external",
                detail=(
                    f"automatic {root_kind} roots require explicit Listary root configuration "
                    "before workspace search reuses them"
                ),
            )
        root = self.client.add_root(requested, kind=root_kind)
        roots.append(dict(root))
        root_id = str(root.get("id") or "")
        self.client.reconcile_root(root_id, request_id=request_id)
        self._admitted_root_keys.add(requested_key)
        return ListaryRootAdmission(
            str(requested),
            "indexed",
            root_id=root_id,
            reconciled=True,
        )

    def ensure_indexed_root(
        self,
        path: str | Path,
        *,
        request_id: int | None = None,
    ) -> ListaryRootAdmission:
        """Admit one explicit workspace source through the resident root owner."""

        requested = Path(path).expanduser().absolute()
        with self._root_admission_lock:
            roots = [dict(root) for root in self.client.list_roots()]
            return self._ensure_indexed_root_locked(
                requested,
                roots,
                request_id=request_id,
            )

    def ensure_indexed_roots(
        self,
        paths: Iterable[str | Path],
        *,
        request_id: int | None = None,
    ) -> tuple[ListaryRootAdmission, ...]:
        normalized: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            requested = Path(path).expanduser().absolute()
            key = self._root_key(requested)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(requested)
        if not normalized:
            return ()
        with self._root_admission_lock:
            roots = [dict(root) for root in self.client.list_roots()]
            admissions: list[ListaryRootAdmission] = []
            for requested in normalized:
                try:
                    admission = self._ensure_indexed_root_locked(
                        requested,
                        roots,
                        request_id=request_id,
                    )
                except ListaryAgentError as exc:
                    if exc.code == "cancelled":
                        raise
                    admission = ListaryRootAdmission(
                        str(requested),
                        "error",
                        detail=f"{exc.code}: {exc}",
                    )
                admissions.append(admission)
            return tuple(admissions)

    def search_files(
        self,
        query: str,
        *,
        media_types: Iterable[str] = (),
        source_roots: Iterable[str | Path] = (),
        limit: int = 40,
        request_id: int | None = None,
    ) -> ListarySearchPage:
        query_text = str(query or "").strip()
        if not query_text:
            return ListarySearchPage(())
        requested_types = frozenset(str(value or "").strip().casefold() for value in media_types if str(value or "").strip())
        unsupported = requested_types.difference({"video", "audio", "image", "other"})
        if unsupported:
            raise ValueError(f"unsupported Listary workspace media type(s): {', '.join(sorted(unsupported))}")
        self.ensure_indexed_roots(source_roots, request_id=request_id)
        category_map = {"image": "picture", "video": "video", "audio": "audio", "other": "other"}
        categories = tuple(category_map[value] for value in sorted(requested_types))
        page = self.client.search(
            query_text,
            kinds=("file",),
            categories=categories,
            limit=max(1, min(int(limit), 100)),
            request_id=request_id,
        )
        if not requested_types:
            return page
        filtered = tuple(
            result for result in page.results if listary_result_media_type(result) in requested_types
        )[: max(1, min(int(limit), 100))]
        return ListarySearchPage(
            filtered,
            elapsed_micros=page.elapsed_micros,
            considered_candidates=page.considered_candidates,
        )

    def search_projects(
        self,
        query: str,
        *,
        source_roots: Iterable[str | Path] = (),
        limit: int = 24,
        request_id: int | None = None,
    ) -> ListarySearchPage:
        """Return indexed ``.spiffyedit`` package folders without probing disk.

        ``.spiffyedit`` projects are directories, so the canonical Core indexes
        them as ``folder`` items.  Core still owns matching and ranking; this
        method only projects the returned folder metadata into the project type
        already owned by Spiffy Video Editor.
        """

        query_text = str(query or "").strip()
        if not query_text:
            return ListarySearchPage(())
        requested_limit = max(1, min(int(limit), 100))
        self.ensure_indexed_roots(source_roots, request_id=request_id)
        # Folder kind has no extension facet. Ask for a bounded larger ranked
        # page so ordinary matching folders do not crowd out a nearby project,
        # then keep only canonical .spiffyedit package directories.
        page = self.client.search(
            query_text,
            kinds=("folder",),
            limit=max(requested_limit, min(100, requested_limit * 4)),
            request_id=request_id,
        )
        rows = tuple(
            result
            for result in page.results
            if result.available
            and str(result.kind or "").casefold() == "folder"
            and str(result.path or "").rstrip("/\\").casefold().endswith(".spiffyedit")
        )[:requested_limit]
        return ListarySearchPage(
            rows,
            elapsed_micros=page.elapsed_micros,
            considered_candidates=page.considered_candidates,
        )

    def search_preset_files(
        self,
        query: str,
        *,
        limit: int = 24,
        request_id: int | None = None,
    ) -> ListarySearchPage:
        """Project ranked ``.prfpset``/``.ffx`` file hits without disk probes."""

        query_text = str(query or "").strip()
        if not query_text:
            return ListarySearchPage(())
        requested_limit = max(1, min(int(limit), 100))
        page = self.client.search(
            query_text,
            kinds=("file",),
            limit=max(requested_limit, min(100, requested_limit * 4)),
            request_id=request_id,
        )
        rows = tuple(
            result
            for result in page.results
            if result.available
            and str(result.kind or "").casefold() == "file"
            and str(result.extension or "").lstrip(".").casefold() in {"prfpset", "ffx"}
        )[:requested_limit]
        return ListarySearchPage(
            rows,
            elapsed_micros=page.elapsed_micros,
            considered_candidates=page.considered_candidates,
        )

    def revalidate_result(
        self,
        displayed: ListarySearchResult,
        *,
        refresh_label: str = "Refresh and search again.",
    ) -> ListarySearchResult:
        """Resolve one displayed workspace result through the canonical item owner."""

        if not isinstance(displayed, ListarySearchResult):
            raise TypeError("displayed result must be a ListarySearchResult")
        current, _presentation, error = _revalidate_result_for_action(
            self.client,
            displayed,
            refresh_label=refresh_label,
        )
        if current is None or error:
            raise ListaryAgentError(
                "stale_result",
                error or f"The selected indexed result is unavailable. {refresh_label}",
            )
        return current

    def cancel(self, request_id: int) -> None:
        self.client.cancel(request_id)


_REFRESH_POLICY_LABELS = {
    "automatic": "Automatic",
    "never": "Never",
    "interval": "Interval",
    "daily": "Daily",
    "weekly": "Weekly",
}
_WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _policy_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def normalize_refresh_policy(value: Any) -> str | dict[str, dict[str, int]]:
    if isinstance(value, str) and value in {"automatic", "never"}:
        return str(value)
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("Refresh policy is invalid")
    mode, payload = next(iter(value.items()))
    if not isinstance(payload, dict):
        raise ValueError("Refresh policy settings are invalid")
    if mode == "interval":
        if set(payload) != {"seconds"}:
            raise ValueError("Interval refresh settings are invalid")
        return {"interval": {"seconds": _policy_int(payload["seconds"], field="Interval", minimum=1, maximum=2_147_483_647)}}
    if mode == "daily":
        if set(payload) != {"hour", "minute"}:
            raise ValueError("Daily refresh settings are invalid")
        return {
            "daily": {
                "hour": _policy_int(payload["hour"], field="Hour", minimum=0, maximum=23),
                "minute": _policy_int(payload["minute"], field="Minute", minimum=0, maximum=59),
            }
        }
    if mode == "weekly":
        if set(payload) != {"weekday_from_monday", "hour", "minute"}:
            raise ValueError("Weekly refresh settings are invalid")
        return {
            "weekly": {
                "weekday_from_monday": _policy_int(
                    payload["weekday_from_monday"], field="Weekday", minimum=0, maximum=6
                ),
                "hour": _policy_int(payload["hour"], field="Hour", minimum=0, maximum=23),
                "minute": _policy_int(payload["minute"], field="Minute", minimum=0, maximum=59),
            }
        }
    raise ValueError("Refresh policy is unsupported")


def refresh_policy_label(value: Any) -> str:
    policy = normalize_refresh_policy(value)
    if isinstance(policy, str):
        return _REFRESH_POLICY_LABELS[policy]
    mode, payload = next(iter(policy.items()))
    if mode == "interval":
        return f"Interval · {payload['seconds']}s"
    if mode == "daily":
        return f"Daily · {payload['hour']:02d}:{payload['minute']:02d}"
    weekday = _WEEKDAY_LABELS[payload["weekday_from_monday"]]
    return f"Weekly · {weekday} {payload['hour']:02d}:{payload['minute']:02d}"


def datetime_offset_seconds(timestamp: float) -> int:
    """Return the local UTC offset for ``timestamp`` without third-party dependencies."""

    local = time.localtime(timestamp)
    if local.tm_isdst > 0 and time.daylight:
        return -int(time.altzone)
    return -int(time.timezone)


def _format_size(size_bytes: int) -> str:
    value = max(0, int(size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{value} B"


def _result_details(result: ListarySearchResult) -> str:
    parts = [result.kind.replace("_", " ").title() or "Item"]
    if result.kind in {"file", "app", "package"}:
        parts.append(_format_size(result.size_bytes))
    if result.modified_unix_ms > 0:
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(result.modified_unix_ms / 1000.0))
        parts.append(f"Modified {modified}")
    if not result.available:
        parts.append("Unavailable")
    if result.priority.casefold() != "normal":
        parts.append(f"{result.priority.title()} priority")
    presentation = _result_presentation(result)
    if presentation.workspace_application:
        if presentation.action_path is not None:
            parts.append("Workspace application")
        elif "launcher is unavailable" in presentation.action_error:
            parts.append("Workspace application unavailable")
        else:
            parts.append("Invalid workspace application metadata")
    elif result.preview_hint:
        parts.append(result.preview_hint.replace("_", " ").title())
    return "  •  ".join(parts)


def _result_icon_name(result: ListarySearchResult) -> str:
    if result.kind == "folder":
        return "spiffy-item-folder"
    if result.kind in {"app", "package"}:
        return "grid"
    hint = result.preview_hint.casefold()
    if "image" in hint:
        return "spiffy-item-image"
    if "video" in hint or "movie" in hint:
        return "spiffy-item-video"
    if "audio" in hint or "music" in hint:
        return "spiffy-item-audio"
    return "spiffy-item-project"


class _WorkerSignals(QObject):
    finished = Signal(int, object, object)


class _SearchWorker(QRunnable):
    def __init__(self, generation: int, request_id: int, client: ListaryAgentClient, query: str, kinds: tuple[str, ...]) -> None:
        super().__init__()
        self.generation = generation
        self.request_id = request_id
        self.client = client
        self.query = query
        self.kinds = kinds
        self.indexed_root_state: tuple[int, int] | None = None
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            page = self.client.search(
                self.query,
                kinds=self.kinds,
                limit=_QUICK_SEARCH_RESULT_LIMIT,
                request_id=self.request_id,
            )
            # Most searches need no extra catalog call: a normal indexed row is
            # already proof that file indexing exists. Provider-only/empty pages
            # are the ambiguous case that caused a rootless install to look like
            # healthy file search, so resolve roots only there and off the UI
            # thread. Failure stays unknown and never masks the search result.
            if isinstance(page, ListarySearchPage):
                provider_only = bool(page.results)
                for result in page.results:
                    try:
                        if decode_workspace_application_preview_hint(result.preview_hint) is None:
                            provider_only = False
                            break
                    except (UnicodeError, ValueError):
                        provider_only = False
                        break
                if not page.results or provider_only:
                    self.indexed_root_state = _indexed_root_state(self.client)
            self.signals.finished.emit(self.generation, page, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, None, exc)


class _OneShotWorker(QRunnable):
    def __init__(self, callback: Any) -> None:
        super().__init__()
        self.callback = callback
        self.done = threading.Event()

    def run(self) -> None:
        try:
            self.callback()
        except Exception:
            pass
        finally:
            self.done.set()


class _ResultActionWorkerSignals(QObject):
    finished = Signal(int, object, object)


class _ResultActionWorker(QRunnable):
    def __init__(self, generation: int, callback: Callable[[], Any]) -> None:
        super().__init__()
        self.generation = generation
        self.callback = callback
        self.signals = _ResultActionWorkerSignals()
        self.done = threading.Event()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.generation, self.callback(), None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, None, exc)
        finally:
            self.done.set()


def _start_tracked_one_shot(workers: set[_OneShotWorker], callback: Any) -> None:
    workers.intersection_update(worker for worker in workers if not worker.done.is_set())
    worker = _OneShotWorker(callback)
    workers.add(worker)
    QThreadPool.globalInstance().start(worker)


def _wait_tracked_one_shots(workers: set[_OneShotWorker], timeout_ms: int) -> bool:
    deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
    for worker in tuple(workers):
        remaining = max(0.0, deadline - time.monotonic())
        if not worker.done.wait(remaining):
            workers.intersection_update(pending for pending in workers if not pending.done.is_set())
            return False
    workers.clear()
    return True


class _RootWorkerSignals(QObject):
    finished = Signal(int, object, object)


class _RootWorker(QRunnable):
    def __init__(self, generation: int, request_id: int, client: ListaryAgentClient, path: str) -> None:
        super().__init__()
        self.generation = generation
        self.request_id = request_id
        self.client = client
        self.path = path
        self.signals = _RootWorkerSignals()

    def run(self) -> None:
        try:
            root = self.client.get_or_add_root(self.path)
            report = self.client.reconcile_root(str(root["id"]), request_id=self.request_id)
            self.signals.finished.emit(self.generation, {"root": root, "report": report}, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, None, exc)


class _RootAdminWorkerSignals(QObject):
    finished = Signal(int, str, object, object)


class _RootAdminWorker(QRunnable):
    def __init__(self, generation: int, operation: str, callback: Callable[[], Any]) -> None:
        super().__init__()
        self.generation = generation
        self.operation = str(operation)
        self.callback = callback
        self.signals = _RootAdminWorkerSignals()
        self.done = threading.Event()

    def run(self) -> None:
        try:
            result = self.callback()
            self.signals.finished.emit(self.generation, self.operation, result, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, self.operation, None, exc)
        finally:
            self.done.set()


class _FileSearchWorker(QRunnable):
    def __init__(
        self,
        generation: int,
        request_id: int,
        client: ListaryAgentClient,
        *,
        query: str,
        categories: tuple[str, ...],
        modified_time: str | None,
        offset: int,
        limit: int,
        sort_field: str,
        sort_direction: str,
        expected_catalog_generation: int | None,
        now_unix_ms: int,
        timezone_offset_minutes: int,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.request_id = request_id
        self.client = client
        self.query = query
        self.categories = categories
        self.modified_time = modified_time
        self.offset = offset
        self.limit = limit
        self.sort_field = sort_field
        self.sort_direction = sort_direction
        self.expected_catalog_generation = expected_catalog_generation
        self.now_unix_ms = int(now_unix_ms)
        self.timezone_offset_minutes = int(timezone_offset_minutes)
        self.indexed_root_state: tuple[int, int] | None = None
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            page = self.client.file_search(
                self.query,
                categories=self.categories,
                modified_time=self.modified_time,
                offset=self.offset,
                limit=self.limit,
                sort_field=self.sort_field,
                sort_direction=self.sort_direction,
                expected_catalog_generation=self.expected_catalog_generation,
                now_unix_ms=self.now_unix_ms,
            timezone_offset_minutes=self.timezone_offset_minutes,
                request_id=self.request_id,
                include_facets=True,
            )
            if isinstance(page, ListaryFileSearchPage) and not page.results and page.total_matches == 0:
                self.indexed_root_state = _indexed_root_state(self.client)
            self.signals.finished.emit(self.generation, page, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, None, exc)


class _ContentSearchWorker(QRunnable):
    def __init__(
        self,
        generation: int,
        request_id: int,
        provider: ContentSearchProvider,
        *,
        query: str,
        limit: int,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.request_id = request_id
        self.provider = provider
        self.query = query
        self.limit = limit
        self.cancel_event = cancel_event
        self.signals = _WorkerSignals()
        self.done = threading.Event()

    def run(self) -> None:
        try:
            page = self.provider.search(
                self.query,
                limit=self.limit,
                cancel_event=self.cancel_event,
            )
            self.signals.finished.emit(self.generation, page, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, None, exc)
        finally:
            self.done.set()


class _FilePreviewSignals(QObject):
    finished = Signal(int, int, object, object)


class _PreviewCancelled(RuntimeError):
    pass


def _preview_identity_matches(expected: ListarySearchResult, current: ListarySearchResult | None) -> bool:
    if current is None:
        return False
    return all(
        (
            current.item_id == expected.item_id,
            current.path == expected.path,
            current.kind == expected.kind,
            current.size_bytes == expected.size_bytes,
            current.modified_unix_ms == expected.modified_unix_ms,
            current.available == expected.available,
            current.preview_hint == expected.preview_hint,
            current.root_id == expected.root_id,
            current.stable_file_id == expected.stable_file_id,
        )
    )


def _decode_bounded_preview_text(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="strict")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="strict")
    if b"\x00" in raw:
        raise ValueError("Text preview refused binary-looking content.")
    return raw.decode("utf-8", errors="strict")


def _bounded_preview_text(text: str, limit: int) -> tuple[str, bool]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= limit:
        return normalized, False
    return normalized[:limit].rstrip() + "\n… preview truncated …", True


class _FilePreviewWorker(QRunnable):
    """Revalidate and prepare one bounded File Search preview off the GUI thread."""

    def __init__(
        self,
        generation: int,
        client: ListaryAgentClient,
        result: ListarySearchResult,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.generation = int(generation)
        self.client = client
        self.result = result
        self.cancel_event = cancel_event
        self.signals = _FilePreviewSignals()
        self.done = threading.Event()

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise _PreviewCancelled("Preview cancelled.")

    @staticmethod
    def _stat_matches(result: ListarySearchResult, path: Path) -> tuple[int, int]:
        stat = path.stat()
        size = int(stat.st_size)
        modified_ms = int(stat.st_mtime_ns // 1_000_000)
        if result.size_bytes >= 0 and size != int(result.size_bytes):
            raise ValueError("Indexed preview source size changed; refresh search results.")
        if result.modified_unix_ms > 0 and abs(modified_ms - int(result.modified_unix_ms)) > 2:
            raise ValueError("Indexed preview source modified time changed; refresh search results.")
        return size, modified_ms

    def _revalidate(self, expected: ListarySearchResult) -> ListarySearchResult:
        self._check_cancelled()
        current = self.client.get_item(expected.item_id)
        if not _preview_identity_matches(expected, current):
            raise ValueError("Indexed preview source changed; refresh search results.")
        assert current is not None
        if not current.available:
            raise ValueError("Indexed preview source is offline.")
        return current

    def _text_preview(self, current: ListarySearchResult, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            raw = handle.read(_LISTARY_TEXT_PREVIEW_BYTES + 1)
        sampled = raw[:_LISTARY_TEXT_PREVIEW_BYTES]
        decoded = _decode_bounded_preview_text(sampled)
        text, char_truncated = _bounded_preview_text(decoded, _LISTARY_TEXT_PREVIEW_CHARS)
        byte_truncated = len(raw) > _LISTARY_TEXT_PREVIEW_BYTES or current.size_bytes > len(sampled)
        if not text:
            return {
                "kind": "text",
                "text": "",
                "status": "Text file is empty.",
                "empty": True,
                "truncated": False,
            }
        return {
            "kind": "text",
            "text": text,
            "status": "Text preview ready" + (" • bounded excerpt" if byte_truncated or char_truncated else ""),
            "empty": False,
            "truncated": bool(byte_truncated or char_truncated),
        }

    def _pdf_preview(self, current: ListarySearchResult, path: Path) -> dict[str, Any]:
        if current.size_bytes > _LISTARY_PDF_PREVIEW_BYTES:
            raise ValueError("PDF preview is limited to 32 MB sources.")
        from PySide6.QtPdf import QPdfDocument

        document = QPdfDocument()
        try:
            error = document.load(str(path))
            if error != QPdfDocument.Error.None_ or document.pageCount() <= 0:
                raise ValueError("PDF preview could not read a valid first page.")
            self._check_cancelled()
            page_count = int(document.pageCount())
            selection = document.getAllText(0)
            page_text = selection.text() if selection is not None else ""
            page_text, text_truncated = _bounded_preview_text(page_text, _LISTARY_PDF_TEXT_CHARS)
            image = document.render(0, QSize(520, 380))
            if image.isNull():
                raise ValueError("PDF preview could not render the first page.")
            status = f"PDF preview ready • first of {page_count} page{'s' if page_count != 1 else ''}"
            if page_text:
                status += " • first-page text available"
            return {
                "kind": "pdf",
                "text": page_text,
                "image": image,
                "status": status,
                "page_count": page_count,
                "truncated": text_truncated,
            }
        finally:
            document.close()

    def _audio_preview(self, _current: ListarySearchResult, path: Path) -> dict[str, Any]:
        from workflow.production_contracts_common import ffprobe_media

        probe = dict(ffprobe_media(path, timeout_s=_LISTARY_AUDIO_PROBE_TIMEOUT_SECONDS) or {})
        if probe.get("status") != "PASS" or not probe.get("has_audio"):
            reason = str(probe.get("reason") or "audio_probe_failed")
            allowed = reason if reason in {"ffprobe_missing", "ffprobe_timeout", "missing_file"} else "audio_probe_failed"
            raise ValueError(f"Audio metadata preview is unavailable ({allowed}).")
        duration_s = max(0.0, float(probe.get("duration_s") or 0.0))
        codec = str(probe.get("audio_codec") or "unknown")
        sample_rate = max(0, int(probe.get("audio_sample_rate") or 0))
        channels = max(0, int(probe.get("audio_channels") or 0))
        lines = [f"Duration: {duration_s:.2f} s", f"Codec: {codec}"]
        if sample_rate:
            lines.append(f"Sample rate: {sample_rate:,} Hz")
        if channels:
            lines.append(f"Channels: {channels}")
        lines.append(
            "Playback: unavailable in File Search because no standalone lease-bound canonical playback owner exists; use Open for verified external playback."
        )
        return {
            "kind": "audio",
            "text": "\n".join(lines),
            "status": "Audio metadata ready • playback unavailable here",
            "duration_s": duration_s,
            "codec": codec,
            "sample_rate": sample_rate,
            "channels": channels,
        }

    def run(self) -> None:
        try:
            current = self._revalidate(self.result)
            path = Path(current.path)
            before_signature = self._stat_matches(current, path)
            self._check_cancelled()
            hint = current.preview_hint.strip().casefold()
            if hint in _LISTARY_THUMBNAIL_PREVIEW_HINTS:
                payload: dict[str, Any] = {"kind": "thumbnail", "result": current}
            elif hint == "text":
                payload = self._text_preview(current, path)
            elif hint == "pdf":
                payload = self._pdf_preview(current, path)
            elif hint == "audio":
                payload = self._audio_preview(current, path)
            else:
                raise ValueError("Preview unavailable for this item type.")
            self._check_cancelled()
            after_signature = self._stat_matches(current, path)
            if after_signature != before_signature:
                raise ValueError("Indexed preview source changed during preview; refresh search results.")
            final = self._revalidate(current)
            if not _preview_identity_matches(current, final):
                raise ValueError("Indexed preview source changed during preview; refresh search results.")
            self.signals.finished.emit(self.generation, current.item_id, payload, None)
        except Exception as exc:
            self.signals.finished.emit(self.generation, self.result.item_id, None, exc)
        finally:
            self.done.set()


class ListaryFileSearchDialog(QDialog):
    """Exact pageable File Search surface backed by the canonical resident core."""

    RESULT_ROLE = int(Qt.ItemDataRole.UserRole)
    FILTER_SUGGESTION_ROLE = RESULT_ROLE + 1
    PAGE_LIMIT = 100
    CONTENT_LIMIT = 50
    FILE_SORT_COLUMNS = ("name", "path", "kind", "modified", "size")
    FILE_SORT_DEFAULT_DIRECTIONS = {
        "name": "ascending",
        "path": "ascending",
        "kind": "ascending",
        "modified": "descending",
        "size": "descending",
    }
    FILE_FILTER_CHOICES = (
        ("@Folder", "folder:", "Folders only"),
        ("@File", "file:", "Files only"),
        ("@PDF", "ext:pdf", "PDF files"),
        ("@Today", "date:today", "Modified today"),
        ("@Week", "date:week", "Modified in the past 7 days"),
        ("@Large", "size:>100mb", "Files larger than 100 MB"),
    )
    CONTENT_FILTER_CHOICES = (
        ("@Transcript", "has:transcript", "Content with a transcript"),
        ("@Audio", "has:audio", "Content backed by audio"),
        ("@Video", "has:video", "Content backed by video"),
        ("@Voice", "source:voicearchive", "Voice Archive content"),
        ("@Capture", "source:spiffycapture", "Spiffy Capture or OBS content"),
        ("@Memory", "type:memory_card", "Memory card content"),
    )
    # Clean-room measurements from the retained Listary 7.0.0.9 reference are
    # presentation inputs only. They remain local component styling layered on
    # top of the shared cross-app appearance profile.
    _REFERENCE_CANVAS = "#1e242d"
    _REFERENCE_CHROME = "#28303c"
    _REFERENCE_INPUT = "#1a2029"
    _REFERENCE_BORDER = "#3c4351"
    _REFERENCE_SELECTION = "#3a4b6e"
    _REFERENCE_TEXT = "#d7dde6"
    _REFERENCE_MUTED = "#9aa7b8"

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        client: ListaryAgentClient | None = None,
        preview_broker: Any = _AUTO_PREVIEW_BROKER,
        file_dialog_adapter: WindowsFileDialogAdapter | None = None,
        file_dialog_context: WindowsFileDialogContext | None = None,
        content_provider: ContentSearchProvider | None = None,
        recommendations_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._owns_client = client is None
        self.client = client or ListaryAgentClient()
        self._generation = 0
        self._active_request_id: int | None = None
        self._active_search_kind: str | None = None
        self._content_cancel_event: threading.Event | None = None
        self._content_provider = content_provider
        self._workers: set[_FileSearchWorker | _ContentSearchWorker] = set()
        self._content_action_workers: set[_ResultActionWorker] = set()
        self._content_action_generation = 0
        self._content_action_started_generation = 0
        self._content_action_cancel_event: threading.Event | None = None
        self._last_content_action_receipt: ContentActionReceipt | None = None
        self._content_action_receipts: list[ContentActionReceipt] = []
        self._activation_workers: set[_OneShotWorker] = set()
        self._file_dialog_action_workers: set[_ResultActionWorker] = set()
        self._file_dialog_action_generation = 0
        self._file_dialog_cancel_event: threading.Event | None = None
        self._file_dialog_adapter = file_dialog_adapter
        self._file_dialog_context = file_dialog_context
        self._root_recovery_action: str | None = None
        self._root_management_dialog: Any | None = None
        self._recommendations_changed = recommendations_changed
        self._last_file_dialog_receipt: WindowsFileDialogReceipt | None = None
        self._last_file_dialog_failure_receipt: WindowsFileDialogFailureReceipt | None = None
        self._file_manager_action_workers: set[_ResultActionWorker] = set()
        self._file_manager_action_generation = 0
        self._file_manager_cancel_event: threading.Event | None = None
        self._last_file_manager_receipt: ListaryFileManagerReceipt | None = None
        self._last_file_manager_failure_receipt: ListaryFileManagerFailureReceipt | None = None
        self._last_file_manager_batch_receipt: ListaryFileManagerBatchReceipt | None = None
        self._last_file_manager_batch_receipts: tuple[ListaryFileManagerReceipt, ...] = ()
        self._pin_workers: set[_ResultActionWorker] = set()
        self._pin_generation = 0
        self._pin_busy = False
        self._pin_busy_generation: int | None = None
        self._pin_state_loading = False
        self._pin_state_item_id: int | None = None
        self._pin_state: bool | None = None
        self._pin_state_reason = "Select one indexed file or folder to pin."
        self._last_pin_receipt: dict[str, Any] | None = None
        self._saved_filter_workers: set[_ResultActionWorker] = set()
        self._saved_filter_generation = 0
        self._saved_filters: tuple[dict[str, Any], ...] = ()
        self._saved_filters_supported: bool | None = None
        self._saved_filters_loading = False
        self._saved_filters_error = ""
        self._offset = 0
        self._catalog_generation: int | None = None
        self._session_now_unix_ms = 0
        self._session_timezone_offset_minutes = 0
        self._page_session_valid = True
        self._has_more = False
        self._total_matches = 0
        self._last_facet_update_latency_ms: float | None = None
        self._shortcuts: list[QShortcut] = []
        self._preview_broker: Any | None = None if preview_broker is _AUTO_PREVIEW_BROKER else preview_broker
        self._auto_preview_broker = preview_broker is _AUTO_PREVIEW_BROKER
        self._owns_preview_broker = False
        self._preview_consumer_id = f"listary-file-search:{id(self):x}"
        self._preview_key = ""
        self._preview_item_id: int | None = None
        self._preview_generation = 0
        self._preview_workers: set[_FilePreviewWorker] = set()
        self._preview_cancel_event: threading.Event | None = None
        self.setObjectName("spiffyListaryFileSearch")
        self.setWindowTitle("File Search - Listary")
        self._ui_settings = QSettings("Spiffy", "ListaryFileSearch")
        self._restore_preview_visibility = self._ui_settings.value("preview_visible", True, type=bool)
        self.setMinimumSize(QSize(820, 520))
        # The installed 7.0.0.9 File Search is a wide, desktop-first surface.
        # Keep the exact-search owner unchanged while starting at the most recent
        # native-reference geometry instead of the generic compact dialog size.
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        target_width = 1639 if available is None else min(1639, max(820, available.width() - 24))
        # Qt's native Windows frame adds ~39 px here; 871 client px lands on the
        # observed 910 px installed outer height at 96 DPI.
        target_height = 871 if available is None else min(871, max(520, available.height() - 48))
        self.resize(target_width, target_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        controls = QHBoxLayout()
        self.search_mode = QComboBox(self)
        self.search_mode.setAccessibleName("File Search mode")
        self.search_mode.addItem("Files", "files")
        self.search_mode.addItem("Content", "content")
        self.search = QLineEdit(self)
        self.search.setObjectName("spiffyListaryFileSearchBox")
        self.search.setAccessibleName("Deep indexed file search query")
        self.search.setPlaceholderText("Search for Files and Folders")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(48)
        self.search.installEventFilter(self)
        self.category = _CategoryFilterComboBox(self)
        self.category.setAccessibleName("File Search category")
        # Keep the stored values as QVariant-friendly lists.  PySide's
        # QComboBox.findData() does not consistently match Python tuples after
        # crossing the Qt model boundary, which made programmatic lookup of a
        # valid multi-select category fail even though normal selection worked.
        self.category.addItem("All Types", [])
        self.category.addItem("Folders", ["folder"])
        self.category.addItem("Documents", ["document"])
        self.category.addItem("Pictures", ["picture"])
        self.category.addItem("Video", ["video"])
        self.category.addItem("Audio", ["audio"])
        self.category.addItem("Other", ["other"])
        for row in range(self.category.count()):
            self.category.setItemData(
                row,
                Qt.CheckState.Checked if row == 0 else Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
        self.category.view().installEventFilter(self)
        self.category.view().viewport().installEventFilter(self)
        self._sync_category_filter_summary()
        self.modified_time = QComboBox(self)
        self.modified_time.setAccessibleName("File Search modified time")
        self.modified_time.addItem("Any Time", None)
        self.modified_time.addItem("Past Day", "last_1_day")
        self.modified_time.addItem("Past 7 Days", "last_7_days")
        self.modified_time.addItem("Past 30 Days", "last_30_days")
        self.modified_time.addItem("Past Year", "last_365_days")
        self.modified_time.addItem("Older", "older")
        self.sort_field = QComboBox(self)
        self.sort_field.setAccessibleName("File Search sort field")
        for label, value in (
            ("Relevance", "relevance"),
            ("Name", "name"),
            ("Path", "path"),
            ("Type", "kind"),
            ("Modified", "modified"),
            ("Size", "size"),
        ):
            self.sort_field.addItem(label, value)
        self.sort_direction = QComboBox(self)
        self.sort_direction.setAccessibleName("File Search sort direction")
        self.sort_direction.addItem("Descending", "descending")
        self.sort_direction.addItem("Ascending", "ascending")
        controls.addWidget(self.search, 1)
        layout.addLayout(controls)

        self.file_filter_bar = QWidget(self)
        filter_chips = QVBoxLayout(self.file_filter_bar)
        filter_chips.setContentsMargins(0, 0, 0, 0)
        filter_chips.setSpacing(5)
        self.filter_chips: list[QPushButton] = []
        self.filter_chip_count_labels: list[QLabel] = []
        for label, token, description in self.FILE_FILTER_CHOICES:
            chip_row = QWidget(self.file_filter_bar)
            chip_row_layout = QHBoxLayout(chip_row)
            chip_row_layout.setContentsMargins(0, 0, 0, 0)
            chip_row_layout.setSpacing(5)
            chip = QPushButton(label, chip_row)
            chip.setObjectName("spiffyListaryFilterChip")
            chip.setAccessibleName(f"Add File Search filter: {description}")
            chip.setToolTip(f"{description} · inserts {token}")
            chip.setCheckable(True)
            chip.setAutoDefault(False)
            chip.setProperty("spiffyRole", "segment")
            chip.setProperty("filterToken", token)
            chip.setProperty("filterDescription", description)
            chip.clicked.connect(lambda _checked=False, token=token: self._toggle_filter_token(token))
            count_label = QLabel("—", chip_row)
            count_label.setObjectName("PanelStatus")
            count_label.setAccessibleName(f"{description} match count")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_label.setMinimumWidth(28)
            self.filter_chips.append(chip)
            self.filter_chip_count_labels.append(count_label)
            chip_row_layout.addWidget(chip, 1)
            chip_row_layout.addWidget(count_label, 0)
            filter_chips.addWidget(chip_row)

        self.content_filter_bar = QWidget(self)
        content_filter_chips = QVBoxLayout(self.content_filter_bar)
        content_filter_chips.setContentsMargins(0, 0, 0, 0)
        content_filter_chips.setSpacing(5)
        self.content_filter_chips: list[QPushButton] = []
        for label, token, description in self.CONTENT_FILTER_CHOICES:
            chip = QPushButton(label, self.content_filter_bar)
            chip.setObjectName("spiffyListaryContentFilterChip")
            chip.setAccessibleName(f"Add Content filter: {description}")
            chip.setToolTip(f"{description} · inserts {token}")
            chip.setCheckable(True)
            chip.setAutoDefault(False)
            chip.setProperty("spiffyRole", "segment")
            chip.setProperty("filterToken", token)
            chip.setProperty("filterDescription", description)
            chip.clicked.connect(lambda _checked=False, token=token: self._toggle_filter_token(token))
            self.content_filter_chips.append(chip)
            content_filter_chips.addWidget(chip)

        self.filter_discovery = QListWidget(self)
        self.filter_discovery.setObjectName("spiffyListaryFilterDiscovery")
        self.filter_discovery.setAccessibleName("File Search filter suggestions")
        self.filter_discovery.setAccessibleDescription(
            "Type @ in the search box, use Up and Down to choose, Enter to apply, and Escape to dismiss."
        )
        self.filter_discovery.setToolTip(
            "Type @ to discover filters · Up/Down choose · Enter apply · Esc dismiss. Unsupported @ text stays literal."
        )
        self.filter_discovery.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.filter_discovery.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filter_discovery.setMaximumHeight(132)
        self.filter_discovery.setVisible(False)
        self.filter_discovery.installEventFilter(self)
        layout.addWidget(self.filter_discovery)

        self.results = QTableWidget(0, 5, self)
        self.results.setObjectName("spiffyListaryFileSearchResults")
        self.results.setAccessibleName("Exact File Search results")
        self.results.setHorizontalHeaderLabels(("Name", "Path", "Type", "Modified", "Size"))
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setAlternatingRowColors(True)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.installEventFilter(self)
        self.results.viewport().installEventFilter(self)
        self.results.verticalHeader().setVisible(False)
        self.results.verticalHeader().setDefaultSectionSize(30)
        self.results.verticalHeader().setMinimumSectionSize(30)
        self.empty_state_label = QLabel("Begin your search by typing a keyword", self.results.viewport())
        self.empty_state_label.setObjectName("spiffyListaryEmptyState")
        self.empty_state_label.setAccessibleName("File Search empty state")
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.empty_state_label.setVisible(False)
        header = self.results.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        # The 96-DPI V7 reference begins Path about 360 px after Name.
        # Content-sized Name collapses to short fixture labels and shifts the
        # entire Path column left, so start at the reference size and keep it
        # user-resizable for narrower windows and longer filenames.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 360)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        result_area = QHBoxLayout()
        result_area.setSpacing(8)

        self.filter_sidebar = QWidget(self)
        self.filter_sidebar.setObjectName("spiffyListaryFileSearchFilters")
        self.filter_sidebar.setAccessibleName("File Search filters and sorting")
        # The measured 7.0.0.9 File Search composition uses a narrow but readable
        # filter rail. Keep the rail fixed so long labels do not push the result
        # table away from the reference column layout.
        self.filter_sidebar.setMinimumWidth(150)
        self.filter_sidebar.setMaximumWidth(150)
        filter_sidebar_layout = QVBoxLayout(self.filter_sidebar)
        filter_sidebar_layout.setContentsMargins(8, 8, 8, 8)
        filter_sidebar_layout.setSpacing(6)
        filter_header = QWidget(self.filter_sidebar)
        filter_header.setObjectName("spiffyListaryFilterHeader")
        filter_header_layout = QHBoxLayout(filter_header)
        filter_header_layout.setContentsMargins(0, 0, 0, 0)
        filter_header_layout.setSpacing(5)
        filter_heading = QLabel("Filter by", filter_header)
        filter_heading.setObjectName("spiffyListaryFilterHeading")
        self.advanced_filters_button = QPushButton("", filter_header)
        self.advanced_filters_button.setObjectName("spiffyListaryAdvancedFilters")
        self.advanced_filters_button.setAccessibleName("Show more File Search filters")
        self.advanced_filters_button.setToolTip("Show quick filters, saved presets, sorting, mode, and facet counts.")
        self.advanced_filters_button.setCheckable(True)
        self.advanced_filters_button.setAutoDefault(False)
        self.advanced_filters_button.setProperty("spiffyRole", "quiet")
        self.advanced_filters_button.setIcon(library_icon("pen", BROADCAST_DARK.text_muted, size=13))
        self.advanced_filters_button.setIconSize(QSize(13, 13))
        self.advanced_filters_button.setFixedSize(QSize(22, 22))
        self.clear_filters_button = QPushButton("Clear all", self.filter_sidebar)
        self.clear_filters_button.setObjectName("spiffyListaryClearFilters")
        self.clear_filters_button.setAccessibleName("Clear all File Search filters")
        self.clear_filters_button.setToolTip("Clear Type, Modified, and active sidebar filters; keep search words.")
        self.clear_filters_button.setAutoDefault(False)
        filter_header_layout.addWidget(filter_heading)
        filter_header_layout.addStretch(1)
        filter_header_layout.addWidget(self.advanced_filters_button)
        filter_heading.setAccessibleName("File Search filters")
        self.category_label = QLabel("Type", self.filter_sidebar)
        self.category_label.setObjectName("PanelStatus")
        self.category_label.setVisible(False)
        self.category_reference_panel = QWidget(self.filter_sidebar)
        self.category_reference_panel.setObjectName("spiffyListaryCategoryReferencePanel")
        category_reference_layout = QVBoxLayout(self.category_reference_panel)
        category_reference_layout.setContentsMargins(0, 0, 0, 0)
        category_reference_layout.setSpacing(2)
        self.category_row_buttons: list[QPushButton] = []
        self.category_count_labels: list[QLabel] = []
        category_reference_rows = (
            ("Folder", "folder-import"),
            ("Document", "document-new"),
            ("Picture", "spiffy-item-image"),
            ("Video", "play-fill"),
            ("Audio", "volume"),
            ("Other", "list"),
        )
        for row, (label, icon_name) in enumerate(category_reference_rows, start=1):
            row_widget = QWidget(self.category_reference_panel)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(2)
            button = QPushButton(label, row_widget)
            button.setObjectName("spiffyListaryReferenceFilterRow")
            button.setAccessibleName(f"Toggle File Search type: {label}")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setProperty("spiffyRole", "segment")
            button.setIcon(library_icon(icon_name, BROADCAST_DARK.accent, size=18))
            button.setIconSize(QSize(18, 18))
            button.setMaximumWidth(104)
            button.clicked.connect(lambda _checked=False, row=row: self._toggle_category_filter_row(row))
            count_label = QLabel("", row_widget)
            count_label.setObjectName("spiffyListaryReferenceFacetCount")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_label.setMinimumWidth(28)
            count_label.setAccessibleName(f"{label} match count")
            self.category_row_buttons.append(button)
            self.category_count_labels.append(count_label)
            row_layout.addWidget(button, 1)
            row_layout.addWidget(count_label, 0)
            category_reference_layout.addWidget(row_widget)
        self.category_counts_label = QLabel("Counts load with results.", self.filter_sidebar)
        self.category_counts_label.setObjectName("PanelStatus")
        self.category_counts_label.setAccessibleName("File Search type facet counts")
        self.category_counts_label.setWordWrap(True)
        self.modified_time_label = QLabel("Recently modified", self.filter_sidebar)
        self.modified_time_label.setObjectName("PanelStatus")
        self.modified_reference_panel = QWidget(self.filter_sidebar)
        self.modified_reference_panel.setObjectName("spiffyListaryModifiedReferencePanel")
        modified_reference_layout = QVBoxLayout(self.modified_reference_panel)
        modified_reference_layout.setContentsMargins(0, 0, 0, 0)
        modified_reference_layout.setSpacing(2)
        self.modified_row_buttons: list[QPushButton] = []
        self.modified_count_labels: list[QLabel] = []
        modified_reference_labels = ("All", "Last 1 day", "Last 7 days", "Last 30 days", "Last 365 days", "Older")
        for row, label in enumerate(modified_reference_labels):
            row_widget = QWidget(self.modified_reference_panel)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(2)
            button = QPushButton(label, row_widget)
            button.setObjectName("spiffyListaryReferenceModifiedRow")
            button.setAccessibleName(f"Set File Search modified time: {label}")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setProperty("spiffyRole", "segment")
            button.setIcon(library_icon("stopwatch", BROADCAST_DARK.accent, size=18))
            button.setIconSize(QSize(18, 18))
            button.setMaximumWidth(104)
            button.clicked.connect(lambda _checked=False, row=row: self._select_modified_filter_row(row))
            count_label = QLabel("", row_widget)
            count_label.setObjectName("spiffyListaryReferenceFacetCount")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_label.setMinimumWidth(28)
            count_label.setAccessibleName(f"{label} match count")
            self.modified_row_buttons.append(button)
            self.modified_count_labels.append(count_label)
            row_layout.addWidget(button, 1)
            row_layout.addWidget(count_label, 0)
            modified_reference_layout.addWidget(row_widget)
        self.modified_counts_label = QLabel("Counts load with results.", self.filter_sidebar)
        self.modified_counts_label.setObjectName("PanelStatus")
        self.modified_counts_label.setAccessibleName("File Search modified-time facet counts")
        self.modified_counts_label.setWordWrap(True)
        self.quick_filter_label = QLabel("Quick filters", self.filter_sidebar)
        self.quick_filter_label.setObjectName("PanelStatus")
        self.saved_filter_group = QWidget(self.filter_sidebar)
        self.saved_filter_group.setObjectName("spiffyListarySavedFilters")
        self.saved_filter_group.setAccessibleName("Saved File Search presets")
        saved_filter_layout = QVBoxLayout(self.saved_filter_group)
        saved_filter_layout.setContentsMargins(0, 0, 0, 0)
        saved_filter_layout.setSpacing(4)
        self.saved_filter_label = QLabel("Saved presets", self.saved_filter_group)
        self.saved_filter_label.setObjectName("PanelStatus")
        self.saved_filter_combo = QComboBox(self.saved_filter_group)
        self.saved_filter_combo.setAccessibleName("Saved File Search preset")
        self.saved_filter_combo.addItem("Choose preset…", None)
        self.saved_filter_combo.setEnabled(False)
        self.saved_filter_refresh_button = QPushButton("Reload", self.saved_filter_group)
        self.saved_filter_refresh_button.setAccessibleName("Reload saved File Search presets")
        self.saved_filter_refresh_button.setToolTip("Reload saved presets from the canonical Listary Core settings owner.")
        self.saved_filter_refresh_button.setAutoDefault(False)
        saved_filter_layout.addWidget(self.saved_filter_label)
        saved_filter_layout.addWidget(self.saved_filter_combo)
        saved_filter_layout.addWidget(self.saved_filter_refresh_button)
        self.saved_filter_group.setVisible(False)
        self.sort_label = QLabel("Sort", self.filter_sidebar)
        self.sort_label.setObjectName("PanelStatus")
        self.search_mode_label = QLabel("Mode", self.filter_sidebar)
        self.search_mode_label.setObjectName("PanelStatus")
        self.sidebar_state = QLabel("Filters follow the current query.", self.filter_sidebar)
        self.sidebar_state.setObjectName("PanelStatus")
        self.sidebar_state.setAccessibleName("File Search filter status")
        self.sidebar_state.setWordWrap(True)
        self.root_recovery_button = QPushButton(self.filter_sidebar)
        self.root_recovery_button.setObjectName("spiffyListaryRootRecovery")
        self.root_recovery_button.setProperty("spiffyRole", "primary")
        self.root_recovery_button.setAutoDefault(False)
        self.root_recovery_button.hide()
        self.root_recovery_button.clicked.connect(self._activate_root_recovery)
        self.advanced_filter_panel = QWidget(self.filter_sidebar)
        self.advanced_filter_panel.setObjectName("spiffyListaryAdvancedFilterPanel")
        advanced_filter_layout = QVBoxLayout(self.advanced_filter_panel)
        advanced_filter_layout.setContentsMargins(0, 0, 0, 0)
        advanced_filter_layout.setSpacing(5)
        advanced_filter_layout.addWidget(self.category_counts_label)
        advanced_filter_layout.addWidget(self.modified_counts_label)
        advanced_filter_layout.addWidget(self.quick_filter_label)
        advanced_filter_layout.addWidget(self.file_filter_bar)
        advanced_filter_layout.addWidget(self.content_filter_bar)
        advanced_filter_layout.addWidget(self.saved_filter_group)
        advanced_filter_layout.addWidget(self.sort_label)
        advanced_filter_layout.addWidget(self.sort_field)
        advanced_filter_layout.addWidget(self.sort_direction)
        advanced_filter_layout.addWidget(self.search_mode_label)
        advanced_filter_layout.addWidget(self.search_mode)
        advanced_filter_layout.addWidget(self.sidebar_state)
        advanced_filter_layout.addWidget(self.clear_filters_button)
        self.advanced_filter_panel.setVisible(False)
        self.category.setVisible(False)
        self.modified_time.setVisible(False)
        filter_sidebar_layout.addWidget(filter_header)
        filter_sidebar_layout.addWidget(self.category_label)
        filter_sidebar_layout.addWidget(self.category_reference_panel)
        filter_sidebar_layout.addWidget(self.modified_time_label)
        filter_sidebar_layout.addWidget(self.modified_reference_panel)
        filter_sidebar_layout.addWidget(self.root_recovery_button)
        filter_sidebar_layout.addWidget(self.advanced_filter_panel)
        filter_sidebar_layout.addStretch(1)
        result_area.addWidget(self.filter_sidebar, 0)
        result_area.addWidget(self.results, 1)

        self.preview_panel = QWidget(self)
        self.preview_panel.setObjectName("spiffyListaryFileSearchPreview")
        self.preview_panel.setMinimumWidth(190)
        self.preview_panel.setMaximumWidth(210)
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.setSpacing(6)
        self.preview_name = QLabel("Preview", self.preview_panel)
        self.preview_name.setWordWrap(True)
        self.preview_image = QLabel("Select one previewable file.", self.preview_panel)
        self.preview_image.setObjectName("PanelStatus")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(QSize(204, 160))
        self.preview_image.setWordWrap(True)
        self.preview_text = QPlainTextEdit(self.preview_panel)
        self.preview_text.setObjectName("spiffyListaryFileSearchPreviewText")
        self.preview_text.setAccessibleName("File Search preview text")
        self.preview_text.setReadOnly(True)
        self.preview_text.setTabChangesFocus(True)
        self.preview_text.setMinimumHeight(86)
        self.preview_text.setMaximumHeight(180)
        self.preview_text.setVisible(False)
        self.preview_status = QLabel("Preview loads after selection settles.", self.preview_panel)
        self.preview_status.setObjectName("PanelStatus")
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_name)
        preview_layout.addWidget(self.preview_image, 1)
        preview_layout.addWidget(self.preview_text)
        preview_layout.addWidget(self.preview_status)
        result_area.addWidget(self.preview_panel, 0)
        layout.addLayout(result_area, 1)

        self.details = QLabel("Select a result to see indexed details.", self)
        self.details.setObjectName("spiffyListaryFooterDetail")
        self.details.setWordWrap(False)
        self.status = QLabel("Type to search the exact indexed catalog.", self)
        self.status.setObjectName("spiffyListaryFooterStatus")
        self.status.setWordWrap(False)

        footer = QHBoxLayout()
        self.previous_button = QPushButton("Previous", self)
        self.next_button = QPushButton("Next", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.page_label = QLabel("Page 1", self)
        self.copy_path_button = QPushButton("Copy Path", self)
        self.preview_button = QPushButton("Preview", self)
        self.preview_button.setCheckable(True)
        self.preview_button.setChecked(self._restore_preview_visibility)
        self.preview_button.setAccessibleName("Toggle File Search preview pane")
        self.preview_button.setToolTip("Toggle preview pane (Alt+P)")
        self.pin_button = QPushButton("Pin / Unpin", self)
        self.pin_button.setAccessibleName("Pin or unpin selected File Search result")
        self.pin_button.setToolTip("Select one indexed file or folder to pin.")
        self.reveal_button = QPushButton("Reveal", self)
        self.open_button = QPushButton("Open", self)
        self.open_button.setProperty("spiffyRole", "primary")
        self.close_button = QPushButton("Close", self)
        self.close_button.setVisible(False)
        for button in (
            self.previous_button,
            self.next_button,
            self.refresh_button,
            self.preview_button,
            self.copy_path_button,
            self.pin_button,
            self.reveal_button,
            self.close_button,
        ):
            button.setProperty("spiffyRole", "quiet")
        footer.addWidget(self.status, 1)
        footer.addWidget(self.details, 2)
        footer.addStretch(1)
        footer.addWidget(self.previous_button)
        footer.addWidget(self.next_button)
        footer.addWidget(self.refresh_button)
        footer.addWidget(self.page_label)
        footer.addWidget(self.preview_button)
        footer.addWidget(self.copy_path_button)
        footer.addWidget(self.pin_button)
        footer.addWidget(self.reveal_button)
        footer.addWidget(self.open_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        # The installed reference keeps row actions out of the persistent bottom
        # bar. Keep these controls alive as the existing action-state owners for
        # context menus/shortcuts, but do not project replacement-only buttons.
        for button in (
            self.preview_button,
            self.copy_path_button,
            self.pin_button,
            self.reveal_button,
            self.open_button,
            self.close_button,
        ):
            button.setVisible(False)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self._start_search)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(90)
        self._preview_timer.timeout.connect(self._start_preview)
        self.search.textChanged.connect(self._schedule_fresh_search)
        self.search.textChanged.connect(self._update_filter_ui)
        self.search.textChanged.connect(self._sync_reference_window_title)
        self.search.cursorPositionChanged.connect(lambda _old, _new: self._update_filter_discovery())
        self.search.returnPressed.connect(self._open_current)
        self.filter_discovery.itemClicked.connect(lambda _item: self._accept_filter_discovery())
        self.filter_discovery.itemActivated.connect(lambda _item: self._accept_filter_discovery())
        self.search_mode.currentIndexChanged.connect(self._mode_changed)
        self.category.currentIndexChanged.connect(self._category_current_index_changed)
        self.modified_time.currentIndexChanged.connect(self._schedule_fresh_search)
        self.modified_time.currentIndexChanged.connect(self._sync_clear_filters_button)
        self.modified_time.currentIndexChanged.connect(self._sync_reference_filter_rows)
        self.advanced_filters_button.toggled.connect(self._set_advanced_filters_visible)
        self.saved_filter_combo.activated.connect(self._activate_saved_filter)
        self.saved_filter_refresh_button.clicked.connect(self._refresh_saved_filters)
        self.sort_field.currentIndexChanged.connect(self._schedule_fresh_search)
        self.sort_direction.currentIndexChanged.connect(self._schedule_fresh_search)
        self.sort_field.currentIndexChanged.connect(self._sync_result_sort_indicator)
        self.sort_direction.currentIndexChanged.connect(self._sync_result_sort_indicator)
        header.sectionClicked.connect(self._sort_from_result_header)
        self.results.itemSelectionChanged.connect(self._file_search_selection_changed)
        self.results.itemDoubleClicked.connect(self._open_double_clicked)
        self.results.customContextMenuRequested.connect(self._show_results_context_menu)
        self.previous_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.refresh_button.clicked.connect(self._refresh_current)
        self.refresh_button.clicked.connect(self._refresh_saved_filters)
        self.copy_path_button.clicked.connect(self._copy_current_path)
        self.preview_button.toggled.connect(self._persist_preview_visibility)
        self.preview_button.toggled.connect(self._set_preview_panel_visible)
        self.clear_filters_button.clicked.connect(self._clear_current_filters)
        self.pin_button.clicked.connect(self._toggle_current_pin)
        self.reveal_button.clicked.connect(self._reveal_current)
        self.open_button.clicked.connect(self._open_current)
        self.close_button.clicked.connect(self.reject)
        self._spiffy_appearance_refresh = self._apply_reference_file_search_presentation
        apply_broadcast_dark(self)
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self._reveal_current)
            self._shortcuts.append(shortcut)
        copy_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        copy_shortcut.activated.connect(self._copy_current_path)
        previous_result_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        previous_result_shortcut.activated.connect(lambda: self._move_result_selection(-1))
        next_result_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        next_result_shortcut.activated.connect(lambda: self._move_result_selection(1))
        preview_shortcut = QShortcut(QKeySequence("Alt+P"), self)
        preview_shortcut.activated.connect(self.preview_button.toggle)
        actions_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        actions_shortcut.activated.connect(self._show_selected_actions_menu)
        self._shortcuts.extend(
            (copy_shortcut, previous_result_shortcut, next_result_shortcut, preview_shortcut, actions_shortcut)
        )
        if self._preview_broker is not None:
            self._attach_preview_broker(self._preview_broker)
        self._apply_search_mode_ui()
        self._sync_reference_filter_rows()
        self._set_advanced_filters_visible(False)
        self._sync_result_sort_indicator()
        self._sync_clear_filters_button()
        self._sync_navigation()
        self._sync_result_state()
        self._reset_page_session_clock()
        QWidget.setTabOrder(self.search, self.category_row_buttons[0])
        for left, right in zip(self.category_row_buttons, self.category_row_buttons[1:]):
            QWidget.setTabOrder(left, right)
        QWidget.setTabOrder(self.category_row_buttons[-1], self.modified_row_buttons[0])
        for left, right in zip(self.modified_row_buttons, self.modified_row_buttons[1:]):
            QWidget.setTabOrder(left, right)
        QWidget.setTabOrder(self.modified_row_buttons[-1], self.advanced_filters_button)
        QWidget.setTabOrder(self.advanced_filters_button, self.filter_chips[0])
        QWidget.setTabOrder(self.filter_chips[-1], self.sort_field)
        QWidget.setTabOrder(self.sort_field, self.sort_direction)
        QWidget.setTabOrder(self.sort_direction, self.search_mode)
        QWidget.setTabOrder(self.search_mode, self.results)

    def _apply_reference_file_search_presentation(self) -> None:
        """Apply measured clean-room File Search presentation without new theme state."""

        accent = shared_broadcast_theme().accent
        self.setStyleSheet(
            f"""
            QDialog#spiffyListaryFileSearch {{
                background: {self._REFERENCE_CANVAS};
                color: {self._REFERENCE_TEXT};
                font-family: "Segoe UI";
                font-size: 9pt;
            }}
            QDialog#spiffyListaryFileSearch QLineEdit#spiffyListaryFileSearchBox {{
                background: {self._REFERENCE_INPUT};
                color: {self._REFERENCE_TEXT};
                border: 1px solid {self._REFERENCE_BORDER};
                border-radius: 2px;
                padding: 6px 10px;
                font-size: 10pt;
            }}
            QDialog#spiffyListaryFileSearch QLineEdit#spiffyListaryFileSearchBox:focus {{
                border-color: {self._REFERENCE_BORDER};
                border-bottom: 2px solid {accent};
            }}
            QWidget#spiffyListaryFileSearchFilters,
            QWidget#spiffyListaryFileSearchPreview {{
                background: {self._REFERENCE_CANVAS};
                border: 0;
            }}
            QWidget#spiffyListaryFilterHeader,
            QWidget#spiffyListaryCategoryReferencePanel,
            QWidget#spiffyListaryModifiedReferencePanel {{
                background: {self._REFERENCE_CANVAS};
                border: 0;
            }}
            QWidget#spiffyListaryFileSearchPreview QLabel,
            QWidget#spiffyListaryFileSearchPreview QPlainTextEdit {{
                background: transparent;
                color: {self._REFERENCE_MUTED};
                border: 0;
            }}
            QLabel#spiffyListaryFilterHeading {{
                color: {self._REFERENCE_TEXT};
                font-weight: 600;
                padding: 2px 0 4px 0;
            }}
            QPushButton#spiffyListaryReferenceFilterRow,
            QPushButton#spiffyListaryReferenceModifiedRow {{
                background: transparent;
                color: {self._REFERENCE_TEXT};
                border: 0;
                border-radius: 3px;
                min-height: 27px;
                padding: 3px 8px;
                text-align: left;
            }}
            QPushButton#spiffyListaryReferenceFilterRow:hover,
            QPushButton#spiffyListaryReferenceModifiedRow:hover {{
                background: {self._REFERENCE_CHROME};
            }}
            QPushButton#spiffyListaryReferenceFilterRow:checked,
            QPushButton#spiffyListaryReferenceModifiedRow:checked {{
                background: {self._REFERENCE_SELECTION};
                color: #ffffff;
                border-left: 2px solid {accent};
                padding-left: 6px;
            }}
            QTableWidget#spiffyListaryFileSearchResults {{
                background: {self._REFERENCE_CANVAS};
                alternate-background-color: {self._REFERENCE_INPUT};
                color: {self._REFERENCE_TEXT};
                border: 1px solid {self._REFERENCE_BORDER};
                gridline-color: {self._REFERENCE_CANVAS};
                selection-background-color: {self._REFERENCE_SELECTION};
                selection-color: #ffffff;
            }}
            QTableWidget#spiffyListaryFileSearchResults QHeaderView::section {{
                background: {self._REFERENCE_CANVAS};
                color: {self._REFERENCE_MUTED};
                border: 0;
                border-right: 1px solid {self._REFERENCE_BORDER};
                border-bottom: 1px solid {self._REFERENCE_BORDER};
                padding: 6px 9px;
                font-weight: 600;
            }}
            QLabel#spiffyListaryEmptyState {{
                background: transparent;
                color: {self._REFERENCE_TEXT};
                font-size: 10pt;
            }}
            QLabel#spiffyListaryFooterStatus,
            QLabel#spiffyListaryFooterDetail {{
                color: {self._REFERENCE_MUTED};
                background: transparent;
                font-size: 8pt;
                padding: 1px 2px;
            }}
            QLabel#spiffyListaryReferenceFacetCount {{
                color: {self._REFERENCE_MUTED};
                background: transparent;
                font-size: 8pt;
                padding: 0 2px;
            }}
            QPushButton[spiffyRole="quiet"] {{
                background: transparent;
                color: {self._REFERENCE_MUTED};
                border: 1px solid transparent;
                padding: 3px 6px;
            }}
            QPushButton[spiffyRole="quiet"]:hover,
            QPushButton[spiffyRole="quiet"]:focus {{
                background: {self._REFERENCE_CHROME};
                color: {self._REFERENCE_TEXT};
                border-color: {self._REFERENCE_BORDER};
            }}
            QPushButton[spiffyRole="quiet"]:disabled {{
                background: transparent;
                color: #697485;
                border-color: transparent;
            }}
            """
        )

    def _file_search_selection_changed(self) -> None:
        if self._file_dialog_cancel_event is not None:
            self._cancel_file_dialog_action()
        if self._file_manager_cancel_event is not None:
            self._cancel_file_manager_action()
        if self._content_action_cancel_event is not None:
            self._cancel_content_action()
        self._sync_result_state()
        self._start_pin_state_refresh()

    def _set_preview_panel_visible(self, visible: bool) -> None:
        self.preview_panel.setVisible(bool(visible))
        if not visible:
            self._preview_timer.stop()
            self._cancel_preview_request()
            return
        self._sync_result_state()

    def _persist_preview_visibility(self, visible: bool) -> None:
        """Persist the existing File Search view preference without a new owner."""
        self._ui_settings.setValue("preview_visible", bool(visible))

    def set_file_dialog_context(
        self,
        adapter: WindowsFileDialogAdapter | None,
        context: WindowsFileDialogContext | None,
    ) -> None:
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        # A new dialog retires the old hold by token, but the old ambiguous
        # delivery receipt remains inspectable until the next explicit action.
        self._file_dialog_adapter = adapter
        self._file_dialog_context = context
        if context is not None and self._is_content_mode():
            blocked = self.search_mode.blockSignals(True)
            self.search_mode.setCurrentIndex(self.search_mode.findData("files"))
            self.search_mode.blockSignals(blocked)
            self._schedule_fresh_search()
        self._apply_search_mode_ui()
        self.results.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if context is not None or self._is_content_mode()
            else QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._sync_result_state()

    def _cancel_file_dialog_action(self) -> None:
        self._file_dialog_action_generation += 1
        cancel_event, self._file_dialog_cancel_event = self._file_dialog_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()

    def _cancel_file_manager_action(self) -> None:
        self._file_manager_action_generation += 1
        cancel_event, self._file_manager_cancel_event = self._file_manager_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()

    def _is_content_mode(self) -> bool:
        return str(self.search_mode.currentData() or "files") == "content"

    def _mode_changed(self, *_args: Any) -> None:
        if self._file_dialog_context is not None and self._is_content_mode():
            blocked = self.search_mode.blockSignals(True)
            self.search_mode.setCurrentIndex(self.search_mode.findData("files"))
            self.search_mode.blockSignals(blocked)
        self._apply_search_mode_ui()
        self._update_filter_ui()
        self._schedule_fresh_search()

    def _apply_search_mode_ui(self) -> None:
        content = self._is_content_mode()
        self.search_mode.setEnabled(self._file_dialog_context is None)
        self.search.setAccessibleName("MemoryCore content search query" if content else "Deep indexed file search query")
        self.search.setPlaceholderText(
            "Search normal-scope MemoryCore content..." if content else "Search for Files and Folders"
        )
        for control in (self.category, self.modified_time, self.sort_field, self.sort_direction):
            control.setEnabled(not content)
        self.category_label.setVisible(False)
        self.category_reference_panel.setVisible(not content)
        self.modified_time_label.setVisible(not content)
        self.modified_reference_panel.setVisible(not content)
        self.details.setVisible(content)
        self.file_filter_bar.setVisible(not content)
        self.content_filter_bar.setVisible(content)
        self._sync_saved_filter_presentation()
        self._sync_reference_window_title()
        self.category_counts_label.setVisible(not content)
        self.modified_counts_label.setVisible(not content)
        self.quick_filter_label.setText("Content filters" if content else "Quick filters")
        for chip in self.filter_chips:
            chip.setEnabled(not content)
        for chip in self.content_filter_chips:
            chip.setEnabled(content and self._file_dialog_context is None)
        self.results.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if content or self._file_dialog_context is not None
            else QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.results.setHorizontalHeaderLabels(
            ("Title", "Source", "Type", "Match", "Range")
            if content
            else ("Name", "Path", "Type", "Modified", "Size")
        )
        self._sync_initial_empty_columns()
        self._sync_result_sort_indicator()
        if not content:
            self.copy_path_button.setText("Copy Path")
            self.reveal_button.setText("Reveal")
        self._sync_filter_chips()
        self._sync_reference_filter_rows()
        self._update_filter_discovery()
        self.sidebar_state.setText(
            "Content filters use the existing MemoryCore provider."
            if content
            else "Filters follow the current indexed query."
        )

    def _sync_result_sort_indicator(self, *_args: Any) -> None:
        header = self.results.horizontalHeader()
        if self._is_content_mode():
            header.setSortIndicatorShown(False)
            return
        field = str(self.sort_field.currentData() or "relevance")
        try:
            column = self.FILE_SORT_COLUMNS.index(field)
        except ValueError:
            header.setSortIndicatorShown(False)
            return
        direction = str(self.sort_direction.currentData() or "descending")
        order = (
            Qt.SortOrder.AscendingOrder
            if direction == "ascending"
            else Qt.SortOrder.DescendingOrder
        )
        header.setSortIndicator(column, order)
        header.setSortIndicatorShown(True)

    def _sort_from_result_header(self, column: int) -> None:
        if self._is_content_mode() or not 0 <= int(column) < len(self.FILE_SORT_COLUMNS):
            return
        field = self.FILE_SORT_COLUMNS[int(column)]
        current_field = str(self.sort_field.currentData() or "relevance")
        current_direction = str(self.sort_direction.currentData() or "descending")
        direction = (
            ("ascending" if current_direction == "descending" else "descending")
            if current_field == field
            else self.FILE_SORT_DEFAULT_DIRECTIONS[field]
        )
        field_index = self.sort_field.findData(field)
        direction_index = self.sort_direction.findData(direction)
        if field_index < 0 or direction_index < 0:
            return
        field_blocked = self.sort_field.blockSignals(True)
        direction_blocked = self.sort_direction.blockSignals(True)
        try:
            self.sort_field.setCurrentIndex(field_index)
            self.sort_direction.setCurrentIndex(direction_index)
        finally:
            self.sort_field.blockSignals(field_blocked)
            self.sort_direction.blockSignals(direction_blocked)
        self._sync_result_sort_indicator()
        self._schedule_fresh_search()

    def _set_file_facet_count_state(self, text: str, *, quick_text: str = "—") -> None:
        self._last_facet_update_latency_ms = None
        self.category_counts_label.setText(str(text))
        self.modified_counts_label.setText(str(text))
        row_text = "" if str(text).startswith("Counts") else str(quick_text)
        for button, label in zip(self.category_row_buttons, self.category_count_labels, strict=True):
            label.setText(row_text)
            button.setEnabled(True)
        for button, label in zip(self.modified_row_buttons, self.modified_count_labels, strict=True):
            label.setText(row_text)
            button.setEnabled(True)
        for label in self.filter_chip_count_labels:
            label.setText(str(quick_text))

    def _apply_file_facet_projection(self, page: ListaryFileSearchPage) -> bool:
        started = time.perf_counter()
        facets = page.facets
        if facets is None or facets.matched_total != page.total_matches:
            self._set_file_facet_count_state("Counts unavailable for this result snapshot.")
            return False

        self.category_counts_label.setText(
            f"All {facets.category_scope_total:,}\n"
            f"Folders {facets.category_count('folder'):,} · Docs {facets.category_count('document'):,} · "
            f"Pics {facets.category_count('picture'):,}\n"
            f"Video {facets.category_count('video'):,} · Audio {facets.category_count('audio'):,} · "
            f"Other {facets.category_count('other'):,}"
        )
        self.modified_counts_label.setText(
            f"Any {facets.modified_scope_total:,}\n"
            f"Today {facets.modified_count('last_1_day'):,} · 7d {facets.modified_count('last_7_days'):,} · "
            f"30d {facets.modified_count('last_30_days'):,}\n"
            f"Year {facets.modified_count('last_365_days'):,} · Older {facets.modified_count('older'):,}"
        )
        for button, count_label, key in zip(
            self.category_row_buttons,
            self.category_count_labels,
            ("folder", "document", "picture", "video", "audio", "other"),
            strict=True,
        ):
            count = facets.category_count(key)
            count_label.setText(f"{count:,}")
            count_label.setToolTip(f"{count:,} matches in the current query + modified-time scope")
            button.setEnabled(count > 0 or button.isChecked())
        modified_counts = (
            facets.modified_scope_total,
            facets.modified_count("last_1_day"),
            facets.modified_count("last_7_days"),
            facets.modified_count("last_30_days"),
            facets.modified_count("last_365_days"),
            facets.modified_count("older"),
        )
        for button, count_label, count in zip(
            self.modified_row_buttons,
            self.modified_count_labels,
            modified_counts,
            strict=True,
        ):
            count_label.setText(f"{count:,}")
            count_label.setToolTip(f"{count:,} matches in the current query + type scope")
            button.setEnabled(count > 0 or button.isChecked())
        for count_label, key in zip(
            self.filter_chip_count_labels,
            ("folder", "file", "pdf", "today", "week", "large"),
            strict=True,
        ):
            count = facets.quick_count(key)
            count_label.setText(f"{count:,}")
            count_label.setToolTip(f"{count:,} matches in the current query + Type/Modified scope")
        self._last_facet_update_latency_ms = (time.perf_counter() - started) * 1000.0
        return True

    def _selected_file_categories(self) -> tuple[str, ...]:
        selected: list[str] = []
        for row in range(1, self.category.count()):
            if self.category.itemData(row, Qt.ItemDataRole.CheckStateRole) != Qt.CheckState.Checked:
                continue
            data = self.category.itemData(row)
            if isinstance(data, (tuple, list)):
                selected.extend(str(value) for value in data if str(value))
        return tuple(selected)

    def _set_advanced_filters_visible(self, visible: bool) -> None:
        visible = bool(visible)
        self.advanced_filter_panel.setVisible(visible)
        blocked = self.advanced_filters_button.blockSignals(True)
        try:
            self.advanced_filters_button.setChecked(visible)
        finally:
            self.advanced_filters_button.blockSignals(blocked)
        self.advanced_filters_button.setText("")
        self.advanced_filters_button.setAccessibleName(
            "Hide more File Search filters" if visible else "Show more File Search filters"
        )

    def _sync_reference_filter_rows(self, *_args: Any) -> None:
        if not hasattr(self, "category_row_buttons"):
            return
        selected = set(self._selected_file_categories())
        for row, button in enumerate(self.category_row_buttons, start=1):
            data = self.category.itemData(row)
            values = {str(value) for value in data} if isinstance(data, (tuple, list)) else set()
            button.setChecked(bool(values & selected))
        modified_row = max(0, self.modified_time.currentIndex())
        for row, button in enumerate(self.modified_row_buttons):
            button.setChecked(row == modified_row)

    def _sync_initial_empty_columns(self) -> None:
        initial_empty = (
            not self._is_content_mode()
            and not self.search.text().strip()
            and self.results.rowCount() == 0
        )
        # The installed File Search keeps the user's current file-view columns at
        # Name / Path. Content mode still owns all five semantic columns.
        for column in (2, 3, 4):
            self.results.setColumnHidden(column, not self._is_content_mode())
        self.empty_state_label.setGeometry(self.results.viewport().rect())
        self.empty_state_label.setVisible(initial_empty)

    def _sync_reference_window_title(self) -> None:
        query = self.search.text().strip()
        self.setWindowTitle(f"{query[:80]} - Listary" if query else "File Search - Listary")

    def _select_modified_filter_row(self, row: int) -> bool:
        if row < 0 or row >= self.modified_time.count():
            return False
        if self.modified_time.currentIndex() == row:
            self._sync_reference_filter_rows()
            return True
        self.modified_time.setCurrentIndex(row)
        return True

    def _sync_category_filter_summary(self) -> None:
        selected = self._selected_file_categories()
        if not selected:
            summary = "All Types"
            detail = "All file types"
            target_row = 0
        else:
            selected_rows = [
                row
                for row in range(1, self.category.count())
                if self.category.itemData(row, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
            ]
            labels = [self.category.itemText(row) for row in selected_rows]
            summary = labels[0] if len(labels) == 1 else f"{len(labels)} Types"
            detail = "Selected file types: " + ", ".join(labels)
            target_row = selected_rows[0] if len(selected_rows) == 1 else -1
        blocked = self.category.blockSignals(True)
        self.category.setPlaceholderText(summary)
        self.category.setCurrentIndex(target_row)
        self.category.blockSignals(blocked)
        self.category.setToolTip(detail + " · open to select more than one type")
        self.category.setAccessibleDescription(detail)
        self._sync_reference_filter_rows()

    def _set_selected_file_categories(self, categories: Iterable[str], *, schedule: bool = True) -> None:
        wanted = {str(value or "").strip().casefold() for value in categories if str(value or "").strip()}
        allowed = {
            str(value).casefold()
            for row in range(1, self.category.count())
            for value in (self.category.itemData(row) or ())
        }
        wanted.intersection_update(allowed)
        changed = False
        for row in range(self.category.count()):
            data = self.category.itemData(row)
            values = {str(value).casefold() for value in data} if isinstance(data, (tuple, list)) else set()
            checked = not wanted if row == 0 else bool(values & wanted)
            desired = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            if self.category.itemData(row, Qt.ItemDataRole.CheckStateRole) != desired:
                self.category.setItemData(row, desired, Qt.ItemDataRole.CheckStateRole)
                changed = True
        self._sync_category_filter_summary()
        self._sync_clear_filters_button()
        if changed and schedule:
            self._schedule_fresh_search()

    def _toggle_category_filter_row(self, row: int) -> bool:
        if row < 0 or row >= self.category.count():
            return False
        if row == 0:
            self._set_selected_file_categories(())
            return True
        data = self.category.itemData(row)
        values = tuple(str(value) for value in data) if isinstance(data, (tuple, list)) else ()
        if not values:
            return False
        selected = list(self._selected_file_categories())
        value = values[0]
        if value in selected:
            selected.remove(value)
        else:
            selected.append(value)
        self._set_selected_file_categories(selected)
        return True

    def _category_current_index_changed(self, row: int) -> None:
        # Closed-combo keyboard/programmatic selection keeps legacy single-type
        # behavior. Popup mouse/keyboard toggles are intercepted below and can
        # keep multiple types active at once.
        if row < 0 or row >= self.category.count():
            return
        data = self.category.itemData(row)
        values = tuple(str(value) for value in data) if isinstance(data, (tuple, list)) else ()
        self._set_selected_file_categories(values)

    def _active_sidebar_filter_tokens(self) -> set[str]:
        active_tokens = {
            self._normalized_filter_token(raw)
            for _start, _end, raw, contains_quote in self._query_token_spans(self.search.text())
            if not contains_quote
        }
        return {
            self._normalized_filter_token(token)
            for _label, token, _description in self._filter_choices()
            if self._normalized_filter_token(token) in active_tokens
        }

    def _clearable_filter_spans(self) -> tuple[tuple[int, int], ...]:
        canonical = {
            self._normalized_filter_token(token)
            for _label, token, _description in self._filter_choices()
        }
        spans: list[tuple[int, int]] = []
        for start, end, raw, contains_quote in self._query_token_spans(self.search.text()):
            if contains_quote:
                continue
            normalized = self._normalized_filter_token(raw)
            clearable = normalized in canonical
            if not self._is_content_mode():
                candidate = normalized[1:] if normalized.startswith("-") else normalized
                clearable = clearable or candidate in {
                    "folder:",
                    "file:",
                    "doc:",
                    "pic:",
                    "video:",
                    "audio:",
                }
                clearable = clearable or candidate.startswith(("ext:", "date:", "size:", "*."))
            if clearable:
                spans.append((start, end))
        return tuple(spans)

    def _sync_clear_filters_button(self, *_args: Any) -> None:
        if not hasattr(self, "clear_filters_button"):
            return
        active = bool(self._clearable_filter_spans())
        if not self._is_content_mode():
            active = active or bool(self._selected_file_categories()) or self.modified_time.currentData() is not None
        self.clear_filters_button.setEnabled(active)
        self.clear_filters_button.setVisible(active)

    @staticmethod
    def _text_without_filter_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
        current = str(text or "")
        for start, end in sorted(((int(start), int(end)) for start, end in spans), reverse=True):
            remove_start = max(0, min(start, len(current)))
            remove_end = max(remove_start, min(end, len(current)))
            if remove_start == 0 and remove_end < len(current) and current[remove_end].isspace():
                remove_end += 1
            elif remove_end == len(current) and remove_start > 0 and current[remove_start - 1].isspace():
                remove_start -= 1
            elif (
                remove_start > 0
                and remove_end < len(current)
                and current[remove_start - 1].isspace()
                and current[remove_end].isspace()
            ):
                remove_end += 1
            current = f"{current[:remove_start]}{current[remove_end:]}"
        return current

    def _clear_current_filters(self) -> bool:
        token_spans = self._clearable_filter_spans()
        cleaned_query = self._text_without_filter_spans(self.search.text(), token_spans)
        controls_changed = False
        if not self._is_content_mode():
            if self._selected_file_categories():
                self._set_selected_file_categories((), schedule=False)
                controls_changed = True
            if self.modified_time.currentData() is not None:
                blocked = self.modified_time.blockSignals(True)
                self.modified_time.setCurrentIndex(0)
                self.modified_time.blockSignals(blocked)
                controls_changed = True
        query_changed = cleaned_query != self.search.text()
        if query_changed:
            self.search.setText(cleaned_query)
        elif controls_changed:
            self._schedule_fresh_search()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._sync_filter_chips()
        self._sync_reference_filter_rows()
        self._sync_clear_filters_button()
        return query_changed or controls_changed

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.results.viewport() and event.type() == QEvent.Type.Resize:
            self.empty_state_label.setGeometry(self.results.viewport().rect())
        if watched is self.category.view().viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            position = event.position().toPoint() if hasattr(event, "position") else event.pos()
            index = self.category.view().indexAt(position)
            if index.isValid():
                return self._toggle_category_filter_row(index.row())
        if watched is self.category.view() and event.type() == QEvent.Type.KeyPress:
            if event.key() in {Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                return self._toggle_category_filter_row(self.category.view().currentIndex().row())
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if self.filter_discovery.isVisible():
                if key == Qt.Key.Key_Down:
                    return self._move_filter_discovery(1)
                if key == Qt.Key.Key_Up:
                    return self._move_filter_discovery(-1)
                if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                    return self._accept_filter_discovery()
                if key == Qt.Key.Key_Escape:
                    self._dismiss_filter_discovery()
                    return True
            if (
                not self.filter_discovery.isVisible()
                and key == Qt.Key.Key_Right
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.results.selectionModel().selectedRows()
            ):
                return self._show_selected_actions_menu()
            if key == Qt.Key.Key_Down:
                return self._move_result_selection(1)
            if key == Qt.Key.Key_Up:
                return self._move_result_selection(-1)
            if key == Qt.Key.Key_PageDown:
                return self._move_result_selection(10)
            if key == Qt.Key.Key_PageUp:
                return self._move_result_selection(-10)
        if watched is self.search and event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._dismiss_filter_discovery_if_unfocused)
        if watched is self.filter_discovery and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                return self._accept_filter_discovery()
            if key == Qt.Key.Key_Escape:
                self._dismiss_filter_discovery()
                self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
                return True
        if watched is self.results and event.type() == QEvent.Type.KeyPress:
            if (
                event.key() == Qt.Key.Key_Right
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.results.selectionModel().selectedRows()
            ):
                return self._show_selected_actions_menu()
        return super().eventFilter(watched, event)

    def _filter_choices(self) -> tuple[tuple[str, str, str], ...]:
        if self._is_content_mode():
            return self.CONTENT_FILTER_CHOICES
        saved = tuple(
            (
                f"@{str(preset.get('keyword') or '').strip()}",
                f"{str(preset.get('keyword') or '').strip()}:",
                f"Saved preset: {str(preset.get('name') or '').strip()}",
            )
            for preset in self._saved_filters
            if str(preset.get("keyword") or "").strip() and str(preset.get("name") or "").strip()
        )
        return self.FILE_FILTER_CHOICES + saved

    def _sync_saved_filter_presentation(self) -> None:
        if not hasattr(self, "saved_filter_group"):
            return
        visible = not self._is_content_mode() and self._saved_filters_supported is True
        self.saved_filter_group.setVisible(visible)
        if not visible:
            return
        self.saved_filter_refresh_button.setEnabled(not self._saved_filters_loading)
        if self._saved_filters_loading:
            self.saved_filter_combo.setEnabled(False)
            return
        if self._saved_filters_error:
            self.saved_filter_combo.setEnabled(False)
            return
        self.saved_filter_combo.setEnabled(bool(self._saved_filters))

    def _set_saved_filter_combo_rows(self, label: str = "Choose preset…") -> None:
        blocked = self.saved_filter_combo.blockSignals(True)
        try:
            self.saved_filter_combo.clear()
            self.saved_filter_combo.addItem(label, None)
            for preset in self._saved_filters:
                name = str(preset.get("name") or "").strip()
                keyword = str(preset.get("keyword") or "").strip()
                if not name or not keyword:
                    continue
                self.saved_filter_combo.addItem(name, dict(preset))
                row = self.saved_filter_combo.count() - 1
                self.saved_filter_combo.setItemData(
                    row,
                    f"Insert {keyword}: — Core expands this saved preset at search time.",
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.saved_filter_combo.setCurrentIndex(0)
        finally:
            self.saved_filter_combo.blockSignals(blocked)

    def _refresh_saved_filters(self) -> bool:
        if self._is_content_mode():
            self._sync_saved_filter_presentation()
            return False
        list_filters = getattr(self.client, "list_filters", None)
        if not callable(list_filters):
            self._saved_filter_generation += 1
            self._saved_filters = ()
            self._saved_filters_supported = False
            self._saved_filters_loading = False
            self._saved_filters_error = "Saved presets are unavailable in this Listary client."
            self._update_filter_ui()
            self._sync_saved_filter_presentation()
            return False
        self._saved_filter_generation += 1
        generation = self._saved_filter_generation
        self._saved_filters = ()
        self._saved_filters_loading = True
        self._saved_filters_error = ""
        if self._saved_filters_supported is True:
            self._set_saved_filter_combo_rows("Loading presets…")
        self._update_filter_ui()
        self._sync_saved_filter_presentation()
        worker = _ResultActionWorker(generation, lambda: tuple(list_filters()))
        self._saved_filter_workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, result, error, owned=worker: self._saved_filters_finished(
                owned,
                finished_generation,
                result,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _saved_filters_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        result: object,
        error: object,
    ) -> None:
        self._saved_filter_workers.discard(worker)
        if generation != self._saved_filter_generation:
            return
        self._saved_filters_loading = False
        if error is not None:
            unsupported = (
                isinstance(error, ListaryAgentError)
                and error.code == "invalid_request"
                and "unknown integration method: list_filters" in str(error).casefold()
            )
            self._saved_filters = ()
            self._saved_filters_supported = False if unsupported else True
            self._saved_filters_error = (
                "Saved presets require an updated Listary service."
                if unsupported
                else f"Saved presets unavailable: {error}"
            )
            if self._saved_filters_supported:
                self._set_saved_filter_combo_rows("Presets unavailable")
            self._update_filter_ui()
            self._sync_saved_filter_presentation()
            return
        if not isinstance(result, (tuple, list)) or not all(isinstance(row, dict) for row in result):
            self._saved_filters = ()
            self._saved_filters_supported = True
            self._saved_filters_error = "Saved presets returned an invalid response."
            self._set_saved_filter_combo_rows("Presets unavailable")
            self._update_filter_ui()
            self._sync_saved_filter_presentation()
            return
        self._saved_filters = tuple(dict(row) for row in result)
        self._saved_filters_supported = True
        self._saved_filters_error = ""
        self._set_saved_filter_combo_rows()
        self._update_filter_ui()
        self._sync_saved_filter_presentation()

    def _activate_saved_filter(self, index: int) -> bool:
        if self._is_content_mode() or self._saved_filters_supported is not True:
            return False
        preset = self.saved_filter_combo.itemData(int(index))
        if not isinstance(preset, dict):
            return False
        keyword = str(preset.get("keyword") or "").strip()
        name = str(preset.get("name") or keyword).strip()
        if not keyword:
            return False
        self._insert_filter_token(f"{keyword}:")
        blocked = self.saved_filter_combo.blockSignals(True)
        self.saved_filter_combo.setCurrentIndex(0)
        self.saved_filter_combo.blockSignals(blocked)
        self.status.setText(f"Applied saved preset {name}. Core will resolve {keyword}: at search time.")
        return True

    @staticmethod
    def _query_token_spans(query: str) -> tuple[tuple[int, int, str, bool], ...]:
        """Mirror only the core token boundaries for UI state; never interpret query grammar."""

        text = str(query or "")
        spans: list[tuple[int, int, str, bool]] = []
        start: int | None = None
        in_quote = False
        escaped = False
        contains_quote = False
        for index, ch in enumerate(text):
            if start is None:
                if ch.isspace():
                    continue
                start = index
                contains_quote = False
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    contains_quote = True
                    in_quote = False
                continue
            if ch == '"':
                contains_quote = True
                in_quote = True
                continue
            if ch.isspace():
                token_start = start
                spans.append((token_start, index, text[token_start:index], contains_quote))
                start = None
        if start is not None:
            spans.append((start, len(text), text[start:], contains_quote))
        return tuple(spans)

    def _normalized_filter_token(self, token: str) -> str:
        normalized = str(token or "").casefold()
        if not self._is_content_mode():
            normalized = normalized.replace("：", ":")
            negative = normalized.startswith("-")
            candidate = normalized[1:] if negative else normalized
            if (
                candidate.startswith(".")
                and 1 < len(candidate) <= 65
                and all(character.isalnum() or character in "._-" for character in candidate[1:])
            ):
                normalized = f"{'-' if negative else ''}ext:{candidate[1:]}"
        return normalized

    def _canonical_filter_spans(self, token: str) -> tuple[tuple[int, int], ...]:
        wanted = self._normalized_filter_token(token)
        return tuple(
            (start, end)
            for start, end, raw, contains_quote in self._query_token_spans(self.search.text())
            if not contains_quote and self._normalized_filter_token(raw) == wanted
        )

    def _sync_filter_chips(self) -> None:
        active_tokens = {
            self._normalized_filter_token(raw)
            for _start, _end, raw, contains_quote in self._query_token_spans(self.search.text())
            if not contains_quote
        }
        content = self._is_content_mode()
        for chip, enabled in (
            *((chip, not content) for chip in self.filter_chips),
            *((chip, content and self._file_dialog_context is None) for chip in self.content_filter_chips),
        ):
            token = str(chip.property("filterToken") or "")
            description = str(chip.property("filterDescription") or chip.text())
            active = self._normalized_filter_token(token) in active_tokens
            blocked = chip.blockSignals(True)
            chip.setChecked(active)
            chip.blockSignals(blocked)
            chip.setEnabled(enabled)
            if active:
                chip.setAccessibleName(f"Remove active filter: {description}")
                chip.setToolTip(f"Active: {description} · activate to remove {token}")
            else:
                chip.setAccessibleName(
                    f"Add {'Content' if chip in self.content_filter_chips else 'File Search'} filter: {description}"
                )
                chip.setToolTip(f"{description} · inserts {token}")
        self._sync_clear_filters_button()

    def _at_filter_fragment(self) -> tuple[int, int, str] | None:
        cursor = max(0, min(self.search.cursorPosition(), len(self.search.text())))
        for start, end, raw, contains_quote in self._query_token_spans(self.search.text()):
            if start <= cursor <= end and not contains_quote and raw.startswith("@"):
                return start, end, raw
        return None

    def _update_filter_ui(self, *_args: Any) -> None:
        self._sync_filter_chips()
        self._update_filter_discovery()

    def _update_filter_discovery(self) -> None:
        fragment = self._at_filter_fragment()
        if fragment is None:
            self._dismiss_filter_discovery()
            return
        _start, _end, raw = fragment
        folded = raw.casefold()
        matches = [choice for choice in self._filter_choices() if choice[0].casefold().startswith(folded)]
        self.filter_discovery.clear()
        if matches:
            for alias, token, description in matches:
                item = QListWidgetItem(f"{alias}  —  {description}  →  {token}")
                item.setData(self.FILTER_SUGGESTION_ROLE, token)
                item.setToolTip(f"Replace {raw} with {token}")
                self.filter_discovery.addItem(item)
            self.filter_discovery.setCurrentRow(0)
        else:
            item = QListWidgetItem(f'No supported @ filter matches "{raw}" — text remains literal.')
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.filter_discovery.addItem(item)
            self.filter_discovery.setCurrentRow(-1)
        self.filter_discovery.setVisible(True)

    def _dismiss_filter_discovery(self) -> None:
        self.filter_discovery.setVisible(False)
        self.filter_discovery.clearSelection()

    def _dismiss_filter_discovery_if_unfocused(self) -> None:
        if not self.search.hasFocus() and not self.filter_discovery.hasFocus():
            self._dismiss_filter_discovery()

    def _move_filter_discovery(self, delta: int) -> bool:
        count = self.filter_discovery.count()
        selectable_rows = [
            row
            for row in range(count)
            if self.filter_discovery.item(row).data(self.FILTER_SUGGESTION_ROLE) is not None
        ]
        if not selectable_rows:
            return True
        current = self.filter_discovery.currentRow()
        try:
            position = selectable_rows.index(current)
        except ValueError:
            position = 0
        position = max(0, min(len(selectable_rows) - 1, position + int(delta)))
        self.filter_discovery.setCurrentRow(selectable_rows[position])
        return True

    def _accept_filter_discovery(self) -> bool:
        item = self.filter_discovery.currentItem()
        token = item.data(self.FILTER_SUGGESTION_ROLE) if item is not None else None
        if not isinstance(token, str) or not token.strip():
            return False
        self._insert_filter_token(token)
        self._dismiss_filter_discovery()
        return True

    def _remove_filter_span(self, start: int, end: int) -> None:
        current = self.search.text()
        remove_start = max(0, min(int(start), len(current)))
        remove_end = max(remove_start, min(int(end), len(current)))
        if remove_start == 0 and remove_end < len(current) and current[remove_end].isspace():
            remove_end += 1
        elif remove_end == len(current) and remove_start > 0 and current[remove_start - 1].isspace():
            remove_start -= 1
        elif (
            remove_start > 0
            and remove_end < len(current)
            and current[remove_start - 1].isspace()
            and current[remove_end].isspace()
        ):
            remove_end += 1
        self.search.setText(f"{current[:remove_start]}{current[remove_end:]}")
        self.search.setCursorPosition(remove_start)
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _toggle_filter_token(self, token: str) -> None:
        spans = self._canonical_filter_spans(token)
        if spans:
            cursor = self.search.cursorPosition()
            start, end = min(
                spans,
                key=lambda span: 0 if span[0] <= cursor <= span[1] else min(abs(cursor - span[0]), abs(cursor - span[1])),
            )
            self._remove_filter_span(start, end)
            return
        self._insert_filter_token(token)

    def _insert_filter_token(self, token: str) -> None:
        """Insert one discoverability chip as canonical query syntax."""

        token = str(token or "").strip()
        if not token:
            return
        current = self.search.text()
        fragment = self._at_filter_fragment()
        if fragment is not None:
            start, end, _raw = fragment
            self.search.setText(f"{current[:start]}{token}{current[end:]}")
            self.search.setCursorPosition(start + len(token))
            self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return
        cursor = self.search.cursorPosition()
        before = current[:cursor]
        after = current[cursor:]
        prefix = "" if not before or before[-1].isspace() else " "
        suffix = "" if not after or after[0].isspace() else " "
        inserted = f"{prefix}{token}{suffix}"
        self.search.setText(f"{before}{inserted}{after}")
        self.search.setCursorPosition(len(before) + len(inserted))
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def open_for_query(self, query: str = "") -> None:
        self._offset = 0
        self._catalog_generation = None
        self._saved_filters = ()
        self._update_filter_ui()
        self.search.setText(str(query))
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()
        self._refresh_saved_filters()
        self._schedule_fresh_search()

    def _schedule_fresh_search(self, *_args: Any) -> None:
        # Query/filter edits retire the current page immediately.  Waiting for
        # the debounce to fire leaves old rows actionable under the new query
        # text and can misattribute a native Open/Save action to stale results.
        self._generation += 1
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._cancel_content_action()
        self._retire_pin_state()
        self._cancel_active()
        self._preview_generation += 1
        self._preview_timer.stop()
        self._cancel_preview_request()
        self._preview_item_id = None
        self._offset = 0
        self._catalog_generation = None
        self._page_session_valid = True
        self._has_more = False
        self._total_matches = 0
        self._reset_page_session_clock()
        self.results.setRowCount(0)
        self.results.clearSelection()
        self._sync_initial_empty_columns()
        if self._is_content_mode():
            self.details.setText("Select a Content result to see its bounded source provenance.")
            self._preview_placeholder(None, "Content preview waits for explicit source verification.")
        else:
            self.details.setText("Select a result to see indexed details.")
            self._preview_placeholder(None, "Searching before preview selection...")
        self._set_result_buttons_enabled(False)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.page_label.setText("Page 1 of 1")
        self.status.setText(
            f"Searching {self._content_provider_display_name()} Content..."
            if self._is_content_mode()
            else "Searching exact catalog..."
        )
        if not self._is_content_mode():
            self._set_file_facet_count_state("Loading counts…", quick_text="…")
        self.sidebar_state.setText("Searching...")
        self._debounce.start()

    def _create_results_context_menu(self) -> QMenu:
        menu = QMenu(self.results)
        for label, button, callback, object_name in (
            (self.open_button.text(), self.open_button, self._open_current, "spiffyFileSearchContextOpen"),
            (self.reveal_button.text(), self.reveal_button, self._reveal_current, "spiffyFileSearchContextReveal"),
            (self.copy_path_button.text(), self.copy_path_button, self._copy_current_path, "spiffyFileSearchContextCopyPath"),
        ):
            action = menu.addAction(label)
            action.setObjectName(object_name)
            action.setEnabled(button.isEnabled())
            action.setToolTip(button.toolTip())
            shortcut = button.shortcut().toString() if hasattr(button, "shortcut") else ""
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
        copy_name_action = menu.addAction("Copy File Name")
        copy_name_action.setObjectName("spiffyFileSearchContextCopyFileName")
        copy_name_action.setEnabled(not self._is_content_mode() and self.copy_path_button.isEnabled())
        copy_name_action.setToolTip("Copy the current file or folder name for every selected result.")
        copy_name_action.setStatusTip(copy_name_action.toolTip())
        copy_name_action.triggered.connect(self._copy_current_file_name)
        copy_items_action = menu.addAction("Copy Items")
        copy_items_action.setObjectName("spiffyFileSearchContextCopyItems")
        copy_items_action.setEnabled(not self._is_content_mode() and self.copy_path_button.isEnabled())
        copy_items_action.setToolTip("Copy the current files or folders to the system clipboard.")
        copy_items_action.setStatusTip(copy_items_action.toolTip())
        copy_items_action.triggered.connect(self._copy_current_items)
        menu.addSeparator()
        pin_action = menu.addAction(self.pin_button.text())
        pin_action.setObjectName("spiffyFileSearchContextPin")
        pin_action.setEnabled(self.pin_button.isEnabled())
        pin_action.setToolTip(self.pin_button.toolTip())
        pin_action.setStatusTip(self.pin_button.toolTip())
        pin_action.triggered.connect(self._toggle_current_pin)
        return menu

    def _show_results_context_menu(self, position: Any) -> None:
        index = self.results.indexAt(position)
        if index.isValid():
            selected_rows = {selected.row() for selected in self.results.selectionModel().selectedRows()}
            if index.row() not in selected_rows:
                self._select_result_row(index.row())
        menu = self._create_results_context_menu()
        menu.exec(self.results.viewport().mapToGlobal(position))

    def _show_selected_actions_menu(self) -> bool:
        selected_rows = self.results.selectionModel().selectedRows()
        if not selected_rows:
            self.status.setText("Select a result before opening Actions.")
            return False
        row = self.results.currentRow()
        if row < 0:
            row = selected_rows[0].row()
        item = self.results.item(row, 0)
        if item is None:
            self.status.setText("Select a result before opening Actions.")
            return False
        menu = self._create_results_context_menu()
        self._keyboard_results_menu = menu
        anchor = self.results.visualItemRect(item).bottomRight()
        menu.popup(self.results.viewport().mapToGlobal(anchor))
        return True

    def _reset_page_session_clock(self) -> None:
        now = time.time()
        self._session_now_unix_ms = int(now * 1000)
        self._session_timezone_offset_minutes = int(datetime_offset_seconds(now) // 60)

    def _attach_preview_broker(self, broker: Any) -> None:
        broker.imageReady.connect(self._preview_ready)
        broker.failed.connect(self._preview_failed)

    def _ensure_preview_broker(self) -> Any | None:
        if self._preview_broker is not None:
            return self._preview_broker
        if not self._auto_preview_broker or not isinstance(self.client, ListaryAgentClient):
            return None
        try:
            from .media_surface import SharedThumbnailBroker

            self._preview_broker = SharedThumbnailBroker(
                self.client.state_dir.parent / "thumbnail-cache",
                self,
                memory_limit=64,
                disk_limit_bytes=32 * 1024 * 1024,
                disk_cache_enabled=False,
            )
            self._owns_preview_broker = True
            self._attach_preview_broker(self._preview_broker)
        except Exception:
            self._preview_broker = None
        return self._preview_broker

    def _cancel_preview_request(self) -> None:
        self._preview_key = ""
        cancel_event, self._preview_cancel_event = self._preview_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()
        broker = self._preview_broker
        if broker is None:
            return
        try:
            broker.cancel_consumer(self._preview_consumer_id)
        except Exception:
            pass

    def _preview_placeholder(self, result: ListarySearchResult | None, message: str) -> None:
        quiet_empty = result is None and message in {
            "Preview loads after selection settles.",
            "Searching before preview selection...",
        }
        self.preview_image.clear()
        self.preview_image.setVisible(True)
        self.preview_text.clear()
        self.preview_text.setVisible(False)
        self.preview_name.setVisible(not quiet_empty)
        self.preview_status.setVisible(not quiet_empty)
        if result is not None:
            icon = library_icon(_result_icon_name(result), color=BROADCAST_DARK.text_muted, size=64)
            pixmap = icon.pixmap(QSize(64, 64))
            if not pixmap.isNull():
                self.preview_image.setPixmap(pixmap)
            self.preview_name.setText(_result_presentation(result).name)
        else:
            self.preview_name.setText("" if quiet_empty else "Preview")
            self.preview_image.setText("" if quiet_empty else "Select one previewable file.")
        self.preview_status.setText("" if quiet_empty else message)
        self.preview_status.setToolTip("")

    def _schedule_preview(self, selected: tuple[ListarySearchResult, ...]) -> None:
        self._preview_generation += 1
        self._preview_timer.stop()
        self._cancel_preview_request()
        self._preview_item_id = None
        if not selected:
            self._preview_placeholder(None, "Preview loads after selection settles.")
            return
        if len(selected) != 1:
            self._preview_placeholder(None, f"Preview unavailable for {len(selected)} selected items.")
            return
        result = selected[0]
        presentation = _result_presentation(result)
        self._preview_placeholder(result, "Preview unavailable for this item type.")
        if presentation.workspace_application:
            self.preview_status.setText("Workspace applications use their validated launcher; no media preview is requested.")
            return
        hint = result.preview_hint.strip().casefold()
        if result.kind != "file" or hint not in _LISTARY_PREVIEW_HINTS:
            return
        if not result.available:
            self.preview_status.setText("Preview unavailable while this file is offline.")
            return
        # The short selection debounce keeps rapid keyboard/query movement from
        # admitting preview work on every intermediate row. The worker first
        # revalidates the canonical Listary item; image/video decode then stays
        # delegated to SharedThumbnailBroker.
        self._preview_item_id = result.item_id
        self.preview_status.setText("Loading preview...")
        self._preview_timer.start()

    def _start_preview(self) -> None:
        selected = self._selected_results()
        if len(selected) != 1:
            return
        result = selected[0]
        if self._preview_item_id != result.item_id:
            return
        self._preview_workers.intersection_update(worker for worker in self._preview_workers if not worker.done.is_set())
        cancel_event = threading.Event()
        self._preview_cancel_event = cancel_event
        worker = _FilePreviewWorker(self._preview_generation, self.client, result, cancel_event)
        self._preview_workers.add(worker)
        worker.signals.finished.connect(
            lambda generation, item_id, payload, error, worker=worker: self._file_preview_finished(
                worker,
                generation,
                item_id,
                payload,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _file_preview_finished(
        self,
        worker: _FilePreviewWorker,
        generation: int,
        item_id: int,
        payload: object,
        error: object,
    ) -> None:
        self._preview_workers.discard(worker)
        if generation != self._preview_generation or item_id != self._preview_item_id:
            return
        selected = self._selected_results()
        if len(selected) != 1 or selected[0].item_id != item_id:
            return
        if self._preview_cancel_event is worker.cancel_event:
            self._preview_cancel_event = None
        if error is not None:
            if isinstance(error, _PreviewCancelled):
                return
            if isinstance(error, UnicodeError):
                message = "Text preview supports UTF-8 or BOM-marked UTF-16 text; this file was not decoded."
            elif isinstance(error, FileNotFoundError):
                message = "Preview source is no longer available."
            elif isinstance(error, PermissionError):
                message = "Preview source could not be read."
            elif isinstance(error, ValueError):
                message = str(error) or "Preview unavailable."
            elif isinstance(error, OSError):
                message = "Preview source could not be read."
            else:
                message = "Preview unavailable."
            self._preview_placeholder(selected[0], message)
            self.preview_status.setToolTip(type(error).__name__)
            return
        if not isinstance(payload, dict):
            self._preview_placeholder(selected[0], "Preview owner returned an invalid result.")
            return
        kind = str(payload.get("kind") or "")
        if kind == "thumbnail":
            current = payload.get("result")
            if not isinstance(current, ListarySearchResult) or current.item_id != item_id:
                self._preview_placeholder(selected[0], "Preview source changed; refresh search results.")
                return
            broker = self._ensure_preview_broker()
            if broker is None:
                self.preview_status.setText("Preview owner is unavailable in this host.")
                return
            try:
                key = broker.request(
                    current.path,
                    consumer_id=self._preview_consumer_id,
                    source_identity=f"listary:{current.item_id}",
                    source_revision=f"{current.modified_unix_ms}:{current.size_bytes}",
                    resolve_source=False,
                    probe_source=False,
                    width=260,
                    height=190,
                    priority="selected",
                )
            except Exception as exc:
                self.preview_status.setText("Preview unavailable.")
                self.preview_status.setToolTip(type(exc).__name__)
                return
            self._preview_key = str(key or "")
            if not self._preview_key:
                self.preview_status.setText("Preview unavailable.")
            return

        self.preview_text.setPlainText(str(payload.get("text") or ""))
        self.preview_text.setVisible(True)
        self.preview_status.setText(str(payload.get("status") or "Preview ready."))
        self.preview_status.setToolTip("")
        if kind == "pdf":
            image = payload.get("image")
            if isinstance(image, QImage) and not image.isNull():
                size = self.preview_image.size()
                pixmap = QPixmap.fromImage(image).scaled(
                    max(64, size.width()),
                    max(64, size.height()),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.preview_image.setVisible(True)
                self.preview_image.clear()
                self.preview_image.setPixmap(pixmap)
            if not self.preview_text.toPlainText():
                self.preview_text.setPlainText("No embedded first-page text. Visual preview shown; OCR was not started.")
        elif kind == "text":
            self.preview_image.setVisible(False)
        elif kind == "audio":
            self.preview_image.setVisible(True)

    def _preview_ready(self, key: str, image: QImage) -> None:
        if str(key) != self._preview_key or image.isNull():
            return
        selected = self._selected_results()
        if len(selected) != 1 or selected[0].item_id != self._preview_item_id:
            return
        size = self.preview_image.size()
        pixmap = QPixmap.fromImage(image).scaled(
            max(64, size.width()),
            max(64, size.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_image.clear()
        self.preview_image.setPixmap(pixmap)
        self.preview_text.clear()
        self.preview_text.setVisible(False)
        self.preview_status.setText(f"Preview ready • {image.width()}×{image.height()}")
        self.preview_status.setToolTip("")

    def _preview_failed(self, key: str, error: str) -> None:
        if str(key) != self._preview_key:
            return
        selected = self._selected_results()
        if len(selected) != 1 or selected[0].item_id != self._preview_item_id:
            return
        self._preview_placeholder(selected[0], "Preview unavailable.")
        self.preview_status.setToolTip(str(error or "Preview decode failed."))

    def _cancel_active(self) -> None:
        request_id, self._active_request_id = self._active_request_id, None
        kind, self._active_search_kind = self._active_search_kind, None
        cancel_event, self._content_cancel_event = self._content_cancel_event, None
        if kind == "content":
            if cancel_event is not None:
                cancel_event.set()
            return
        if request_id:
            QThreadPool.globalInstance().start(_OneShotWorker(lambda: self.client.cancel(request_id)))

    def _start_search(self) -> None:
        if self._is_content_mode():
            self._start_content_search()
            return
        self._cancel_active()
        self._set_root_recovery_action(None)
        self._preview_generation += 1
        self._preview_timer.stop()
        self._cancel_preview_request()
        self._preview_item_id = None
        self._preview_placeholder(None, "Searching before preview selection...")
        self._generation += 1
        generation = self._generation
        request_id = secrets.randbits(63) or 1
        self._active_request_id = request_id
        self._active_search_kind = "files"
        categories = self._selected_file_categories()
        expected_generation = self._catalog_generation if self._offset else None
        self.status.setText("Searching exact catalog...")
        self._set_file_facet_count_state("Loading counts…", quick_text="…")
        self._set_result_buttons_enabled(False)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        worker = _FileSearchWorker(
            generation,
            request_id,
            self.client,
            query=self.search.text().strip(),
            categories=categories,
            modified_time=(str(self.modified_time.currentData()) if self.modified_time.currentData() else None),
            offset=self._offset,
            limit=self.PAGE_LIMIT,
            sort_field=str(self.sort_field.currentData() or "relevance"),
            sort_direction=str(self.sort_direction.currentData() or "descending"),
            expected_catalog_generation=expected_generation,
            now_unix_ms=self._session_now_unix_ms,
            timezone_offset_minutes=self._session_timezone_offset_minutes,
        )
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, page, error, worker=worker: self._search_finished(
                worker, got_generation, page, error
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _set_root_recovery_action(self, action: str | None) -> None:
        self._root_recovery_action = action if action in {"index", "manage"} else None
        if self._root_recovery_action == "index":
            label = "Add folder..."
            accessible_name = "Open Indexed Folders to add a folder"
        elif self._root_recovery_action == "manage":
            label = "Review folders..."
            accessible_name = "Review disabled or unavailable indexed folders"
        else:
            self.root_recovery_button.hide()
            return
        self.root_recovery_button.setText(label)
        self.root_recovery_button.setAccessibleName(accessible_name)
        self.root_recovery_button.setToolTip(
            "Opens the existing Indexed Folders manager. No folder is added automatically."
        )
        self.root_recovery_button.show()

    def _activate_root_recovery(self) -> None:
        if self._root_recovery_action in {"index", "manage"}:
            self._open_root_management()

    def _open_root_management(self) -> None:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, ListarySearchDialog):
                parent._open_root_management()
                return
            parent = parent.parentWidget()
        if self._root_management_dialog is None:
            self._root_management_dialog = ListaryRootManagementDialog(
                self, client=self.client, roots_changed=self.refresh_after_root_change
            )
        self._root_management_dialog.open_manager()

    def refresh_after_root_change(self) -> None:
        if self._is_content_mode():
            return
        self._schedule_fresh_search()
        self._debounce.stop()
        self._start_search()

    def _search_finished(self, worker: _FileSearchWorker, generation: int, page: object, error: object) -> None:
        self._workers.discard(worker)
        if generation != self._generation:
            return
        self._active_request_id = None
        self._active_search_kind = None
        if error is not None:
            self._set_root_recovery_action(None)
            if isinstance(error, ListaryAgentError) and error.code == "stale_catalog":
                self._offset = 0
                self._catalog_generation = None
                self._page_session_valid = True
                self._reset_page_session_clock()
                self.status.setText("Index changed while paging. Refreshing from page 1...")
                self._set_file_facet_count_state("Refreshing counts…", quick_text="…")
                QTimer.singleShot(0, self._start_search)
                return
            self.results.setRowCount(0)
            self.details.setText("Select a result to see indexed details.")
            self.status.setText(str(error) or "Deep File Search is unavailable.")
            self._set_file_facet_count_state("Counts unavailable.")
            code = str(getattr(error, "code", "") or "").casefold()
            self.sidebar_state.setText(
                "Permission blocked; filters are preserved."
                if "permission" in code
                else "Search unavailable; filters are preserved."
            )
            self._has_more = False
            self._total_matches = 0
            self._sync_navigation()
            self._sync_result_state()
            return
        if not isinstance(page, ListaryFileSearchPage):
            self._set_root_recovery_action(None)
            self.status.setText("Deep File Search returned an invalid result page.")
            self._set_file_facet_count_state("Counts unavailable.")
            self.sidebar_state.setText("Result state unavailable; filters are preserved.")
            self._sync_navigation()
            return
        self._catalog_generation = page.catalog_generation
        self._offset = page.offset
        self._has_more = page.has_more
        self._total_matches = page.total_matches
        self.results.setRowCount(len(page.results))
        for row_index, result in enumerate(page.results):
            presentation = _result_presentation(result)
            name_item = QTableWidgetItem(presentation.name)
            name_item.setIcon(library_icon(_result_icon_name(result), color=BROADCAST_DARK.text_muted, size=16))
            name_item.setData(self.RESULT_ROLE, result)
            name_item.setToolTip(presentation.tooltip)
            path_item = QTableWidgetItem(presentation.parent_path)
            type_item = QTableWidgetItem(result.kind.replace("_", " ").title())
            modified_text = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(result.modified_unix_ms / 1000.0))
                if result.modified_unix_ms > 0
                else ""
            )
            modified_item = QTableWidgetItem(modified_text)
            size_item = QTableWidgetItem(_format_size(result.size_bytes) if result.kind != "folder" else "")
            for column, item in enumerate((name_item, path_item, type_item, modified_item, size_item)):
                self.results.setItem(row_index, column, item)
        if self.results.rowCount():
            self._select_result_row(0)
        else:
            self.results.clearSelection()
        self._sync_initial_empty_columns()
        first = page.offset + 1 if page.results else 0
        last = page.offset + len(page.results)
        elapsed_ms = page.elapsed_micros / 1000.0
        range_text = f"{first}-{last}" if page.results else "0"
        status_text = f"Showing {range_text} of {page.total_matches} exact matches"
        if page.elapsed_micros:
            status_text += f" in {elapsed_ms:.1f} ms"
        facets_ready = self._apply_file_facet_projection(page)
        root_state = getattr(worker, "indexed_root_state", None)
        if not page.results and page.total_matches == 0 and root_state == (0, 0):
            self._set_root_recovery_action("index")
            self.sidebar_state.setText("No indexed folders yet. Add a folder to enable file search.")
            status_text = "No indexed folders yet — use Add folder... to enable file search."
        elif (
            not page.results
            and page.total_matches == 0
            and root_state is not None
            and root_state[0] > 0
            and root_state[1] == 0
        ):
            self._set_root_recovery_action("manage")
            self.sidebar_state.setText(
                "Indexed folders are disabled or unavailable. Review indexed folders to restore file search."
            )
            status_text = "Indexed folders are disabled or unavailable — use Review folders... to restore file search."
        else:
            self._set_root_recovery_action(None)
            self.sidebar_state.setText(
                "No matching files."
                if page.total_matches == 0
                else (
                    f"{page.total_matches:,} exact match{'es' if page.total_matches != 1 else ''} · live counts"
                    if facets_ready
                    else f"{page.total_matches:,} exact match{'es' if page.total_matches != 1 else ''}."
                )
            )
        self.status.setText(status_text)
        self._sync_navigation()
        self._sync_result_state()
        self._start_pin_state_refresh()

    def _content_provider_display_name(self) -> str:
        provider = self._content_provider
        if provider is None:
            return "MemoryCore"
        name = str(getattr(provider, "display_name", "") or "").strip()
        return name or "Content"

    def _ensure_content_provider(self) -> ContentSearchProvider:
        provider = self._content_provider
        if provider is None:
            provider = MemoryCoreContentSearchProvider()
            self._content_provider = provider
        return provider

    def _start_content_search(self) -> None:
        self._set_root_recovery_action(None)
        self._cancel_active()
        self._preview_generation += 1
        self._preview_timer.stop()
        self._cancel_preview_request()
        self._preview_item_id = None
        self._preview_placeholder(None, "Content preview waits for explicit source verification.")
        self._generation += 1
        generation = self._generation
        query = self.search.text().strip()
        if not query:
            self.results.setRowCount(0)
            self.status.setText(f"Type to search {self._content_provider_display_name()} Content.")
            self.sidebar_state.setText("Type a Content query; filters are preserved.")
            self._sync_navigation()
            self._sync_result_state()
            return
        request_id = secrets.randbits(63) or 1
        cancel_event = threading.Event()
        self._active_request_id = request_id
        self._active_search_kind = "content"
        self._content_cancel_event = cancel_event
        self.status.setText(f"Searching {self._content_provider_display_name()} Content...")
        self._set_result_buttons_enabled(False)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        worker = _ContentSearchWorker(
            generation,
            request_id,
            self._ensure_content_provider(),
            query=query,
            limit=self.CONTENT_LIMIT,
            cancel_event=cancel_event,
        )
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, page, error, worker=worker: self._content_search_finished(
                worker, got_generation, page, error
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _content_search_finished(
        self,
        worker: _ContentSearchWorker,
        generation: int,
        page: object,
        error: object,
    ) -> None:
        self._workers.discard(worker)
        if generation != self._generation:
            return
        self._active_request_id = None
        self._active_search_kind = None
        self._content_cancel_event = None
        if error is not None:
            self.results.setRowCount(0)
            if isinstance(error, ContentSearchError) and error.code == "cancelled":
                self.status.setText("Content search cancelled.")
                self.sidebar_state.setText("Content search cancelled; filters are preserved.")
            else:
                self.status.setText(str(error) or "Content search is unavailable.")
                code = str(getattr(error, "code", "") or "").casefold()
                self.sidebar_state.setText(
                    "Content permission blocked; filters are preserved."
                    if "permission" in code
                    else "Content search unavailable; filters are preserved."
                )
            self._sync_navigation()
            self._sync_result_state()
            return
        if not isinstance(page, ContentSearchPage):
            self.results.setRowCount(0)
            self.status.setText("Content search returned an invalid result page.")
            self.sidebar_state.setText("Content result state unavailable; filters are preserved.")
            self._sync_navigation()
            self._sync_result_state()
            return
        self.results.setRowCount(len(page.results))
        for row_index, result in enumerate(page.results):
            title_item = QTableWidgetItem(result.title or "Content result")
            title_item.setIcon(library_icon("spiffy-item-project", color=BROADCAST_DARK.text_muted, size=16))
            title_item.setData(self.RESULT_ROLE, result)
            title_item.setToolTip(result.snippet)
            provider_name = self._content_provider_display_name()
            source_label = result.source_table or (f"{provider_name} media" if result.media_id else provider_name)
            source_item = QTableWidgetItem(source_label)
            type_item = QTableWidgetItem(result.doc_type.replace("_", " ").title())
            match_item = QTableWidgetItem(", ".join(result.matched_via) if result.matched_via else "lexical")
            range_text = ""
            if result.start_ms is not None and result.end_ms is not None:
                range_text = f"{result.start_ms / 1000.0:.1f}s–{result.end_ms / 1000.0:.1f}s"
            range_item = QTableWidgetItem(range_text)
            for column, item in enumerate((title_item, source_item, type_item, match_item, range_item)):
                self.results.setItem(row_index, column, item)
        if self.results.rowCount():
            self._select_result_row(0)
        else:
            self.results.clearSelection()
        self._sync_initial_empty_columns()
        self._total_matches = len(page.results)
        self._offset = 0
        self._has_more = False
        scope = str(page.privacy_scope or "").strip()
        scope_prefix = f"{scope}-scope " if scope else ""
        self.status.setText(
            f"Showing {len(page.results)} {scope_prefix}Content result"
            f"{'s' if len(page.results) != 1 else ''} in {page.elapsed_ms:.1f} ms"
            + (f" • {page.structured_filter_count} filter{'s' if page.structured_filter_count != 1 else ''}" if page.structured_filter_count else "")
        )
        self.sidebar_state.setText(
            "No matching Content."
            if not page.results
            else f"{len(page.results):,} Content result{'s' if len(page.results) != 1 else ''}."
        )
        self._sync_navigation()
        self._sync_result_state()

    def _sync_navigation(self) -> None:
        idle = self._active_request_id is None
        if self._is_content_mode():
            self.previous_button.setVisible(False)
            self.next_button.setVisible(False)
            self.page_label.setVisible(True)
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.refresh_button.setEnabled(idle)
            self.page_label.setText(f"Content • top {self.CONTENT_LIMIT}")
            return
        show_paging = bool(self._offset > 0 or self._total_matches > self.PAGE_LIMIT or not self._page_session_valid)
        self.previous_button.setVisible(show_paging)
        self.next_button.setVisible(show_paging)
        self.page_label.setVisible(show_paging)
        self.previous_button.setEnabled(self._page_session_valid and self._offset > 0 and idle)
        self.next_button.setEnabled(self._page_session_valid and self._has_more and idle)
        self.refresh_button.setEnabled(idle)
        page_number = (self._offset // self.PAGE_LIMIT) + 1
        total_pages = max(1, (self._total_matches + self.PAGE_LIMIT - 1) // self.PAGE_LIMIT)
        suffix = " • Refresh required" if not self._page_session_valid else ""
        self.page_label.setText(f"Page {page_number} of {total_pages}{suffix}")

    def _refresh_current(self) -> None:
        if self._active_request_id is not None:
            return
        self._offset = 0
        self._catalog_generation = None
        self._page_session_valid = True
        self._reset_page_session_clock()
        self._start_search()

    def _previous_page(self) -> None:
        if self._is_content_mode():
            return
        if not self._page_session_valid or self._offset <= 0 or self._active_request_id is not None:
            return
        self._offset = max(0, self._offset - self.PAGE_LIMIT)
        self._start_search()

    def _next_page(self) -> None:
        if self._is_content_mode():
            return
        if not self._page_session_valid or not self._has_more or self._active_request_id is not None:
            return
        self._offset += self.PAGE_LIMIT
        self._start_search()

    def _current_result(self) -> ListarySearchResult | None:
        return self._result_at_row(self.results.currentRow())

    def _result_at_row(self, row: int) -> ListarySearchResult | None:
        if row < 0 or row >= self.results.rowCount():
            return None
        item = self.results.item(row, 0)
        value = item.data(self.RESULT_ROLE) if item is not None else None
        return value if isinstance(value, ListarySearchResult) else None

    def _selected_results(self) -> tuple[ListarySearchResult, ...]:
        selection_model = self.results.selectionModel()
        selected_rows = sorted({index.row() for index in selection_model.selectedRows(0)}) if selection_model else []
        selected = tuple(
            result
            for row in selected_rows
            if (result := self._result_at_row(row)) is not None
        )
        return selected

    def _content_result_at_row(self, row: int) -> ContentResult | None:
        if row < 0 or row >= self.results.rowCount():
            return None
        item = self.results.item(row, 0)
        value = item.data(self.RESULT_ROLE) if item is not None else None
        return value if isinstance(value, ContentResult) else None

    def _selected_content_results(self) -> tuple[ContentResult, ...]:
        selection_model = self.results.selectionModel()
        selected_rows = sorted({index.row() for index in selection_model.selectedRows(0)}) if selection_model else []
        return tuple(
            result
            for row in selected_rows
            if (result := self._content_result_at_row(row)) is not None
        )

    def _selected_action_entries(
        self,
        results: tuple[ListarySearchResult, ...] | None = None,
        *,
        revalidate: bool = False,
    ) -> tuple[tuple[tuple[ListarySearchResult, _ResultPresentation], ...], str]:
        if self._active_request_id is not None:
            return (), "Wait for the current search to finish before using result actions."
        selected = results if results is not None else self._selected_results()
        if not selected:
            return (), "Select a result first."
        entries: list[tuple[ListarySearchResult, _ResultPresentation]] = []
        for result in selected:
            if revalidate:
                current, presentation, error = _revalidate_result_for_action(
                    self.client,
                    result,
                    refresh_label="Refresh File Search.",
                )
                if current is None or presentation is None:
                    return (), error
                result = current
            else:
                presentation = _result_presentation(result)
                if presentation.action_path is None:
                    return (), presentation.action_error or "The selected result is unavailable."
            entries.append((result, presentation))
        return tuple(entries), ""

    def _move_result_selection(self, delta: int) -> bool:
        count = self.results.rowCount()
        if count <= 0:
            return False
        current = self.results.currentRow()
        if current < 0:
            current = 0
        target = max(0, min(count - 1, current + int(delta)))
        self._select_result_row(target)
        self.results.scrollToItem(self.results.item(target, 0))
        return True

    def _select_result_row(self, row: int) -> None:
        if row < 0 or row >= self.results.rowCount():
            self.results.clearSelection()
            return
        selection_model = self.results.selectionModel()
        index = self.results.model().index(row, 0)
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

    @staticmethod
    def _pin_path_key(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))

    @classmethod
    def _pin_state_from_favorites(cls, favorites: dict[str, Any], path: str) -> bool:
        expected = cls._pin_path_key(path)
        for raw_pin in favorites.get("pins") if isinstance(favorites.get("pins"), list) else ():
            if not isinstance(raw_pin, dict):
                continue
            raw_path = str(raw_pin.get("path") or "").strip()
            if raw_path and cls._pin_path_key(raw_path) == expected:
                return True
        return False

    def _pin_selection(self) -> tuple[ListarySearchResult | None, str]:
        if self._is_content_mode():
            return None, "Pins are available only in Files mode."
        selected = self._selected_results()
        if len(selected) != 1:
            if selected:
                return None, "Pin/Unpin works with one indexed file or folder at a time."
            return None, "Select one indexed file or folder to pin."
        result = selected[0]
        if result.kind not in {"file", "folder"}:
            return None, "Only indexed files and folders can be pinned."
        presentation = _result_presentation(result)
        if presentation.action_path is None:
            return None, presentation.action_error or "The selected result cannot be pinned."
        return result, ""

    def _pin_action_state(self) -> tuple[str, bool, str]:
        result, reason = self._pin_selection()
        if result is None:
            return "Pin / Unpin", False, reason
        if self._active_request_id is not None:
            return "Pin / Unpin", False, "Wait for the current File Search request to finish."
        if self._pin_busy:
            return "Pin / Unpin", False, "A pin change is already in progress."
        if self._pin_state_loading:
            return "Pin / Unpin", False, "Checking the current pin state..."
        if self._pin_state_item_id != result.item_id or self._pin_state is None:
            return "Pin / Unpin", False, self._pin_state_reason or "Refresh the current pin state before changing it."
        label = "Unpin" if self._pin_state else "Pin"
        return label, True, f"{label} selected result in zero-query recommendations"

    def _sync_pin_button(self) -> None:
        label, enabled, reason = self._pin_action_state()
        self.pin_button.setText(label)
        self.pin_button.setEnabled(enabled)
        self.pin_button.setAccessibleName(f"{label} selected File Search result")
        self.pin_button.setToolTip(reason)

    def _pin_detail_text(self, result: ListarySearchResult) -> str:
        if result.kind not in {"file", "folder"}:
            return ""
        if self._pin_state_item_id != result.item_id:
            return "Checking pin state..." if self._pin_state_loading else self._pin_state_reason
        if self._pin_state is True:
            return "Pinned in zero-query recommendations."
        if self._pin_state is False:
            return "Not pinned in zero-query recommendations."
        return self._pin_state_reason

    def _retire_pin_state(self) -> None:
        self._pin_generation += 1
        self._pin_state_loading = False
        self._pin_state_item_id = None
        self._pin_state = None
        self._pin_state_reason = "Refresh the current pin state before changing it."

    def _read_pin_state(self, result: ListarySearchResult) -> dict[str, Any]:
        presentation = _result_presentation(result)
        if result.kind not in {"file", "folder"} or presentation.action_path is None:
            return {
                "outcome": "blocked",
                "item_id": result.item_id,
                "pinned": None,
                "detail": "Only indexed files and folders can be pinned.",
            }
        favorites = self.client.get_favorites()
        return {
            "outcome": "ready",
            "item_id": result.item_id,
            "pinned": self._pin_state_from_favorites(favorites, presentation.action_path),
            "detail": "",
        }

    def _sync_pin_presentation(self) -> None:
        self._sync_pin_button()
        if self._is_content_mode():
            return
        selected = self._selected_results()
        if len(selected) != 1:
            return
        detail = _result_details(selected[0])
        pin_detail = self._pin_detail_text(selected[0])
        self.details.setText(f"{detail}\n{pin_detail}" if pin_detail else detail)

    def _start_pin_state_refresh(self) -> None:
        self._pin_generation += 1
        generation = self._pin_generation
        self._pin_state_loading = False
        self._pin_state_item_id = None
        self._pin_state = None
        result, reason = self._pin_selection()
        if result is None:
            self._pin_state_reason = reason
            self._sync_pin_button()
            return
        if self._active_request_id is not None:
            self._pin_state_reason = "Wait for the current File Search request to finish."
            self._sync_pin_button()
            return
        self._pin_state_loading = True
        self._pin_state_reason = "Checking the current pin state..."
        self._sync_pin_button()
        worker = _ResultActionWorker(generation, lambda result=result: self._read_pin_state(result))
        self._pin_workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, state, error, owned=worker: self._pin_state_finished(
                owned,
                finished_generation,
                state,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _pin_state_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        state: object,
        error: object,
    ) -> None:
        self._pin_workers.discard(worker)
        if generation != self._pin_generation:
            return
        self._pin_state_loading = False
        if error is not None:
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = f"Pin state unavailable: {error}. Refresh or reconnect before changing it."
        elif not isinstance(state, dict) or not isinstance(state.get("item_id"), int):
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = "Pin state unavailable: invalid response. Refresh before changing it."
        elif state.get("outcome") != "ready" or not isinstance(state.get("pinned"), bool):
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = str(state.get("detail") or "The selected result is no longer current.")
        else:
            self._pin_state_item_id = int(state["item_id"])
            self._pin_state = bool(state["pinned"])
            self._pin_state_reason = ""
        self._sync_pin_presentation()

    def _set_result_buttons_enabled(self, enabled: bool) -> None:
        self.open_button.setEnabled(enabled)
        self.reveal_button.setEnabled(enabled)
        self.copy_path_button.setEnabled(enabled)
        if enabled:
            self._sync_pin_button()
        else:
            self.pin_button.setEnabled(False)

    def _sync_result_action_visibility(self, selected_count: int, *, content: bool = False) -> None:
        # Match the installed File Search: selection changes action state and the
        # row/context/keyboard routes, but does not grow a persistent button bar.
        self.open_button.setVisible(False)
        self.copy_path_button.setVisible(False)
        self.reveal_button.setVisible(False)
        self.pin_button.setVisible(False)

    def _sync_result_state(self) -> None:
        self._sync_pin_button()
        if self._is_content_mode():
            selected = self._selected_content_results()
            self._sync_result_action_visibility(len(selected), content=True)
            result = selected[0] if len(selected) == 1 else None
            busy = self._active_request_id is not None or self._content_action_cancel_event is not None
            actionable = bool(result is not None and result.actionable and not busy and self._file_dialog_context is None)
            self.open_button.setText("Open Source")
            self.copy_path_button.setText("Copy Source Path")
            self.reveal_button.setText("Reveal Source")
            self.open_button.setEnabled(actionable)
            self.copy_path_button.setEnabled(actionable)
            self.reveal_button.setEnabled(actionable)
            self._preview_timer.stop()
            self._cancel_preview_request()
            self._preview_placeholder(None, "Content preview is disabled until an explicit source action is verified.")
            if result is not None:
                provenance = " • ".join(
                    value for value in (
                        result.doc_type.replace("_", " "),
                        result.source_table,
                        ", ".join(result.matched_via),
                    ) if value
                )
                suffix = "" if result.actionable else " • Source action unavailable for this result"
                self.details.setText(f"{result.snippet or 'No snippet available.'}\n{provenance}{suffix}")
            else:
                self.details.setText("Select a Content result to see its bounded source provenance.")
            return
        selected = self._selected_results()
        count = len(selected)
        self._sync_result_action_visibility(count)
        if self._file_dialog_context is not None and count == 1:
            kind = selected[0].kind
            self.open_button.setText(_native_dialog_action_label(kind, self._file_dialog_context.dialog_kind))
        else:
            self.open_button.setText("Open" if count <= 1 else f"Open {count}")
        self.open_button.setAccessibleName(self.open_button.text())
        self.copy_path_button.setText("Copy Path" if count <= 1 else f"Copy {count} Paths")
        self.reveal_button.setText("Reveal")
        entries, action_error = self._selected_action_entries(selected)
        actions_enabled = bool(entries) and not action_error
        file_manager_busy = self._file_manager_cancel_event is not None
        shortcut_batch = bool(
            sys.platform == "win32"
            and count > 1
            and any(_is_windows_shortcut_file_result(result) for result in selected)
        )
        direct_shortcut_batch = bool(
            sys.platform == "win32"
            and count > 1
            and all(_is_verifiable_windows_shortcut_result(result) for result in selected)
        )
        direct_app_batch = bool(
            sys.platform == "win32"
            and count > 1
            and all(_is_direct_windows_executable_app_result(result) for result in selected)
        )
        mixed_verified_operations = tuple(
            _verified_windows_mixed_batch_item_operation(result) for result in selected
        ) if sys.platform == "win32" and count > 1 else ()
        mixed_verified_batch = bool(
            mixed_verified_operations
            and all(mixed_verified_operations)
            and len(set(mixed_verified_operations)) > 1
        )
        verified_file_batch = bool(
            sys.platform != "win32"
            or count <= 1
            or all(result.kind == "file" and not _is_windows_shortcut_file_result(result) for result in selected)
        )
        verified_batch_open = verified_file_batch or direct_app_batch or direct_shortcut_batch or mixed_verified_batch
        quick_switch_enabled = (
            self._file_dialog_context is None
            or (count == 1 and selected[0].kind in {"file", "folder"})
        )
        self.open_button.setEnabled(actions_enabled and quick_switch_enabled and verified_batch_open and not file_manager_busy)
        if _native_save_confirmation_pending(self._file_dialog_context, self._last_file_dialog_failure_receipt):
            self.open_button.setEnabled(False)
            self.open_button.setToolTip("Windows is awaiting a human overwrite decision; do not repeat Save.")
        elif self._file_dialog_context is not None and self._file_dialog_context.dialog_kind == "save":
            self.open_button.setToolTip("Commit Save in the captured Windows dialog; Windows requires human approval for replacement.")
        else:
            self.open_button.setToolTip("Open the selected indexed result.")
        self.copy_path_button.setEnabled(actions_enabled)
        self.reveal_button.setEnabled(actions_enabled and count == 1 and not file_manager_busy)
        if count == 1:
            detail = _result_details(selected[0])
            pin_detail = self._pin_detail_text(selected[0])
            self.details.setText(f"{detail}\n{pin_detail}" if pin_detail else detail)
        elif count > 1:
            if action_error:
                self.details.setText(f"{count} items selected  •  Batch actions disabled: {action_error}")
            elif not verified_batch_open:
                if any(result.kind == "folder" for result in selected):
                    reason = "Folder Open requires one verified Explorer destination at a time"
                elif shortcut_batch:
                    reason = "Windows shortcut batch Open requires every selected row to be a verifiable direct-executable shortcut"
                else:
                    reason = (
                        "Verified Windows batch actions support file-only Open, direct-executable app-only launch, "
                        "or direct-executable shortcut-only launch"
                    )
                self.details.setText(
                    f"{count} items selected  •  {reason}  •  Copy Path can still apply to every selected row"
                )
            elif direct_app_batch:
                self.details.setText(
                    f"{count} apps selected  •  Open runs ordered verified executable launches  •  "
                    "stops on the first failed or ambiguous destination"
                )
            elif direct_shortcut_batch:
                self.details.setText(
                    f"{count} shortcuts selected  •  Open runs ordered verified executable shortcut launches  •  "
                    "stops on the first failed or ambiguous destination"
                )
            elif mixed_verified_batch:
                self.details.setText(
                    f"{count} mixed verified items selected  •  Open uses each row's existing verified file/app/shortcut owner  •  "
                    "stops on the first failed or ambiguous destination"
                )
            else:
                self.details.setText(
                    f"{count} items selected  •  Open and Copy Path apply to every selected row on this page  •  "
                    "Reveal works one item at a time"
                )
        else:
            self.details.setText("Select a result to see indexed details.")
        self._schedule_preview(selected)

    def _cancel_content_action(self) -> None:
        self._content_action_generation += 1
        cancel_event, self._content_action_cancel_event = self._content_action_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()

    def _record_content_action_receipt(self, receipt: ContentActionReceipt) -> None:
        self._content_action_receipts.append(receipt)
        if len(self._content_action_receipts) > 32:
            del self._content_action_receipts[:-32]
        if receipt.action_generation == self._content_action_started_generation:
            self._last_content_action_receipt = receipt

    def _start_content_action(self, action: str, result: ContentResult) -> bool:
        if self._file_dialog_context is not None:
            self.status.setText("Content results are not available in Open/Save Quick Switch.")
            return False
        if not result.actionable:
            self.status.setText("This Content result has no exactly verifiable source identity.")
            return False
        if action not in {"open", "reveal", "copy"}:
            return False
        self._cancel_content_action()
        self._content_action_generation += 1
        generation = self._content_action_generation
        self._content_action_started_generation = generation
        search_generation = self._generation
        self._last_content_action_receipt = None
        cancel_event = threading.Event()
        self._content_action_cancel_event = cancel_event
        self._set_result_buttons_enabled(False)
        verb = {"open": "Opening", "reveal": "Revealing", "copy": "Verifying"}[action]
        self.status.setText(f"{verb} the current {self._content_provider_display_name()} source...")
        provider = self._ensure_content_provider()

        def perform() -> ContentSourceTarget:
            target = provider.resolve_source(result, cancel_event=cancel_event)
            if cancel_event.is_set():
                raise ContentSearchError("cancelled", "Content source action was cancelled.")
            return target

        worker = _ResultActionWorker(generation, perform)
        self._content_action_workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, payload, error, owned=worker, action=action, search_generation=search_generation, result=result: self._content_action_finished(
                owned,
                got_generation,
                search_generation,
                action,
                result,
                payload,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _content_action_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        search_generation: int,
        action: str,
        result: ContentResult,
        payload: object,
        error: object,
    ) -> None:
        self._content_action_workers.discard(worker)
        if generation != self._content_action_generation or search_generation != self._generation:
            return
        if error is not None:
            self._content_action_cancel_event = None
            code = error.code if isinstance(error, ContentSearchError) else "source_verification_failed"
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="failed",
                code=str(code),
                action=action,
                provider_name=self._content_provider_display_name(),
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path="",
                source_sha256="",
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=False,
                ambiguous=False,
                detail=str(error) or "The Content source changed or is unavailable.",
            ))
            if not (isinstance(error, ContentSearchError) and error.code == "cancelled"):
                self.status.setText(str(error) or "The Content source changed or is unavailable.")
            self._sync_result_state()
            return
        if not isinstance(payload, ContentSourceTarget):
            self._content_action_cancel_event = None
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="failed",
                code="invalid_source_target",
                action=action,
                provider_name=self._content_provider_display_name(),
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path="",
                source_sha256="",
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=False,
                ambiguous=False,
                detail="Content source verification returned an invalid target.",
            ))
            self.status.setText("Content source verification returned an invalid target.")
            self._sync_result_state()
            return
        source_path = str(payload.path or "").strip()
        if not source_path or not Path(source_path).expanduser().is_absolute():
            self._content_action_cancel_event = None
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="failed",
                code="invalid_source_target",
                action=action,
                provider_name=self._content_provider_display_name(),
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path=source_path,
                source_sha256=str(payload.source_sha256 or ""),
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=False,
                ambiguous=False,
                detail="Content source verification rejected an empty or non-absolute source path.",
            ))
            self.status.setText("Content source verification rejected an empty or non-absolute source path.")
            self._sync_result_state()
            return
        source_digest = str(payload.source_sha256 or "").strip().casefold()
        if len(source_digest) != 64 or any(character not in "0123456789abcdef" for character in source_digest):
            self._content_action_cancel_event = None
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="failed",
                code="source_digest_missing",
                action=action,
                provider_name=self._content_provider_display_name(),
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path=source_path,
                source_sha256="",
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=False,
                ambiguous=False,
                detail="Content source verification did not return an exact SHA-256 source digest.",
            ))
            self.status.setText("Content source verification did not return an exact SHA-256 source digest.")
            self._sync_result_state()
            return
        if action == "copy":
            self._content_action_cancel_event = None
            QApplication.clipboard().setText(source_path)
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="verified",
                code="ok",
                action=action,
                provider_name=self._content_provider_display_name(),
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path=source_path,
                source_sha256=source_digest,
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=True,
                ambiguous=False,
                detail="Copied the currently verified Content source path.",
            ))
            self.status.setText("Copied the currently verified Content source path.")
        else:
            self._start_content_native_dispatch(
                action,
                result,
                ContentSourceTarget(
                    path=source_path,
                    source_state=payload.source_state,
                    reason_code=payload.reason_code,
                    source_sha256=source_digest,
                    target_kind=payload.target_kind,
                ),
                generation=generation,
                search_generation=search_generation,
            )
            return
        self._sync_result_state()

    def _start_content_native_dispatch(
        self,
        action: str,
        result: ContentResult,
        expected: ContentSourceTarget,
        *,
        generation: int,
        search_generation: int,
    ) -> None:
        provider = self._ensure_content_provider()
        cancel_event = self._content_action_cancel_event
        if cancel_event is None:
            cancel_event = threading.Event()
            self._content_action_cancel_event = cancel_event
        provider_name = self._content_provider_display_name()
        started = time.monotonic()

        def receipt(
            *,
            outcome: str,
            code: str,
            detail: str,
            dispatch_happened: bool,
            ambiguous: bool,
            destination: dict[str, object] | None = None,
        ) -> ContentActionReceipt:
            return ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome=outcome,
                code=code,
                action=action,
                provider_name=provider_name,
                document_id=result.document_id,
                media_id=result.media_id,
                representation_revision=result.representation_revision,
                target_path=expected.path,
                source_sha256=expected.source_sha256,
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=max(0.0, (time.monotonic() - started) * 1000.0),
                dispatch_happened=dispatch_happened,
                ambiguous=ambiguous,
                detail=detail,
                destination=destination,
            )

        def fail(
            code: str,
            detail: str,
            *,
            dispatch_happened: bool = False,
            ambiguous: bool = False,
            destination: dict[str, object] | None = None,
        ) -> ContentActionError:
            return ContentActionError(
                receipt(
                    outcome="failed",
                    code=code,
                    detail=detail,
                    dispatch_happened=dispatch_happened,
                    ambiguous=ambiguous,
                    destination=destination,
                )
            )

        def require_current_generation() -> None:
            if cancel_event.is_set():
                raise fail("cancelled", "Content source action was cancelled before the next native side effect.")
            if generation != self._content_action_generation or search_generation != self._generation:
                raise fail(
                    "stale_generation",
                    "The Content result/query generation changed before the native source action could run.",
                )

        def revalidate_expected() -> ContentSourceTarget:
            require_current_generation()
            current = provider.resolve_source(result, cancel_event=cancel_event)
            require_current_generation()
            same_path = os.path.normcase(os.path.abspath(os.path.normpath(current.path))) == os.path.normcase(
                os.path.abspath(os.path.normpath(expected.path))
            )
            same_digest = current.source_sha256.casefold() == expected.source_sha256.casefold()
            same_kind = current.target_kind == expected.target_kind
            if not (same_path and same_digest and same_kind):
                raise fail(
                    "source_identity_changed",
                    "The Content source identity changed after initial verification; no native action was started.",
                )
            return current

        def perform() -> ContentActionReceipt:
            dispatched = False
            try:
                current = revalidate_expected()
                if action == "reveal":
                    if sys.platform == "win32":
                        target_kind = "folder" if Path(current.path).is_dir() else "file"

                        def platform_revalidate() -> None:
                            revalidate_expected()

                        platform_receipt = reveal_in_file_manager_verified(
                            current.path,
                            target_kind=target_kind,
                            cancel_event=cancel_event,
                            timeout_seconds=8.0,
                            revalidate_target=platform_revalidate,
                            operation="content_reveal",
                        )
                        dispatched = True
                        return receipt(
                            outcome="verified",
                            code="ok",
                            detail="Revealed the currently verified Content source.",
                            dispatch_happened=True,
                            ambiguous=False,
                            destination=platform_receipt.to_dict(),
                        )
                    require_current_generation()
                    if not reveal_in_file_manager(current.path):
                        raise fail("native_reveal_failed", "Could not reveal the verified Content source.")
                    dispatched = True
                    try:
                        revalidate_expected()
                    except (ContentSearchError, ContentActionError) as exc:
                        detail = str(exc) or "The Content source changed while it was being revealed."
                        raise fail(
                            "source_changed_after_dispatch",
                            detail,
                            dispatch_happened=True,
                            ambiguous=True,
                        ) from exc
                    return receipt(
                        outcome="verified",
                        code="ok",
                        detail="Revealed the currently verified Content source.",
                        dispatch_happened=True,
                        ambiguous=False,
                    )

                require_current_generation()
                if not open_path(current.path):
                    raise fail("native_open_failed", "Could not open the verified Content source.")
                dispatched = True
                try:
                    revalidate_expected()
                except (ContentSearchError, ContentActionError) as exc:
                    detail = str(exc) or "The Content source changed while it was being opened."
                    raise fail(
                        "source_changed_after_dispatch",
                        detail,
                        dispatch_happened=True,
                        ambiguous=True,
                    ) from exc
                return receipt(
                    outcome="verified",
                    code="ok",
                    detail="Opened the currently verified Content source.",
                    dispatch_happened=True,
                    ambiguous=False,
                )
            except ContentActionError:
                raise
            except ContentSearchError as exc:
                raise fail(
                    exc.code,
                    str(exc) or "The Content source changed or is unavailable.",
                    dispatch_happened=dispatched,
                    ambiguous=dispatched,
                ) from exc
            except FileManagerActionError as exc:
                destination = exc.destination.to_dict() if exc.destination is not None else None
                raise fail(
                    exc.code,
                    str(exc) or "The verified Explorer action failed.",
                    dispatch_happened=exc.dispatch_happened,
                    ambiguous=exc.ambiguous,
                    destination=destination,
                ) from exc
            except Exception as exc:
                raise fail(
                    "native_action_failed",
                    str(exc) or "The Content source action failed.",
                    dispatch_happened=dispatched,
                    ambiguous=dispatched,
                ) from exc

        worker = _ResultActionWorker(generation, perform)
        self._content_action_workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, payload, error, owned=worker, search_generation=search_generation: self._content_native_action_finished(
                owned,
                got_generation,
                search_generation,
                payload,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _content_native_action_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        search_generation: int,
        payload: object,
        error: object,
    ) -> None:
        self._content_action_workers.discard(worker)
        typed_receipt: ContentActionReceipt | None = None
        if isinstance(error, ContentActionError):
            typed_receipt = error.receipt
        elif isinstance(payload, ContentActionReceipt):
            typed_receipt = payload
        if typed_receipt is not None:
            self._record_content_action_receipt(typed_receipt)
        if generation != self._content_action_generation or search_generation != self._generation:
            return
        self._content_action_cancel_event = None
        if isinstance(error, ContentActionError):
            if error.code != "cancelled":
                self.status.setText(str(error) or "The Content source action failed.")
            self._sync_result_state()
            return
        if error is not None:
            self._record_content_action_receipt(ContentActionReceipt(
                schema="spiffy_listary_content_action_v1",
                outcome="failed",
                code="native_action_failed",
                action="unknown",
                provider_name=self._content_provider_display_name(),
                document_id="",
                media_id="",
                representation_revision="",
                target_path="",
                source_sha256="",
                action_generation=generation,
                search_generation=search_generation,
                action_latency_ms=0.0,
                dispatch_happened=False,
                ambiguous=False,
                detail=str(error) or "The Content source action failed.",
            ))
            self.status.setText(str(error) or "The Content source action failed.")
            self._sync_result_state()
            return
        if not isinstance(payload, ContentActionReceipt):
            self.status.setText("Content source action returned an invalid receipt.")
            self._sync_result_state()
            return
        self.status.setText(payload.detail)
        self._sync_result_state()

    def _record_file_search_activations(self, item_ids: tuple[int, ...]) -> None:
        if not item_ids:
            return

        def record_all() -> None:
            for item_id in item_ids:
                try:
                    self.client.record_activation(item_id, source="file_search")
                except Exception:
                    continue

        _start_tracked_one_shot(self._activation_workers, record_all)

    def _invalidate_relevance_paging_after_activation(self) -> bool:
        if str(self.sort_field.currentData() or "relevance") != "relevance":
            return False
        self._catalog_generation = None
        self._page_session_valid = False
        self._sync_navigation()
        return True

    def _start_file_dialog_action(self, displayed: ListarySearchResult) -> bool:
        adapter = self._file_dialog_adapter
        context = self._file_dialog_context
        if adapter is None or context is None:
            return False
        if _native_save_confirmation_pending(context, self._last_file_dialog_failure_receipt):
            self.status.setText(
                "Windows Save needs your overwrite decision. Confirm or cancel in Windows; "
                "a second Save will not be sent to this dialog."
            )
            return True
        if self._active_request_id is not None:
            self.status.setText("Wait for the current search to finish before using result actions.")
            return True
        if displayed.kind not in {"file", "folder"}:
            self.status.setText("Open/Save Quick Switch accepts only indexed files and folders.")
            return True

        self._cancel_file_dialog_action()
        self._file_dialog_action_generation += 1
        generation = self._file_dialog_action_generation
        self._last_file_dialog_receipt = None
        self._last_file_dialog_failure_receipt = None
        cancel_event = threading.Event()
        self._file_dialog_cancel_event = cancel_event
        query_text = self.search.text().strip()
        activation_generation = self._generation
        self.open_button.setEnabled(False)
        self.status.setText(
            f"{'Navigating' if displayed.kind == 'folder' else 'Selecting'} in the captured "
            f"{context.dialog_kind.title()} dialog..."
        )
        try:
            adapter.prepare_context_for_action(context)
        except WindowsFileDialogError as exc:
            self._file_dialog_cancel_event = None
            self._last_file_dialog_failure_receipt = _attach_file_dialog_failure_receipt(
                adapter,
                context,
                exc,
                displayed,
                query_text=query_text,
                activation_generation=activation_generation,
                action_generation=generation,
            ).receipt
            self.status.setText(str(exc) or "Open/Save Quick Switch could not re-establish the captured dialog.")
            self._sync_result_state()
            return True

        def perform() -> tuple[ListarySearchResult, WindowsFileDialogReceipt]:
            current_for_receipt = displayed
            target_for_receipt = displayed.path

            def require_current_generation() -> None:
                if cancel_event.is_set():
                    raise WindowsFileDialogError(
                        "cancelled",
                        "The File Search Open/Save action was cancelled before the next native change.",
                    )
                if (
                    generation != self._file_dialog_action_generation
                    or activation_generation != self._generation
                ):
                    raise WindowsFileDialogError(
                        "stale_generation",
                        "The File Search result/query generation changed before the Open/Save action could run.",
                    )

            try:
                require_current_generation()
                current, presentation, error = _revalidate_result_for_action(
                    self.client,
                    displayed,
                    refresh_label="Refresh File Search.",
                )
                if current is None or presentation is None:
                    raise WindowsFileDialogError("stale_result", error)
                current_for_receipt = current
                target_path = presentation.action_path
                target_for_receipt = target_path

                def revalidate_target() -> None:
                    require_current_generation()
                    _require_result_target_unchanged(
                        self.client,
                        current,
                        target_path,
                        refresh_label="Refresh File Search.",
                    )

                require_current_generation()
                receipt = adapter.apply_result(
                    context,
                    target_path=target_path,
                    item_kind=current.kind,
                    item_id=current.item_id,
                    root_id=current.root_id,
                    stable_file_id=current.stable_file_id,
                    displayed_path=displayed.path,
                    query_text=query_text,
                    activation_generation=activation_generation,
                    cancel_event=cancel_event,
                    revalidate_target=revalidate_target,
                    accept_open=context.dialog_kind == "open" and current.kind == "file",
                    accept_save=context.dialog_kind == "save" and current.kind in {"file", "folder"},
                    action_generation=generation,
                    result_generation=activation_generation,
                )
                return current, receipt
            except WindowsFileDialogError as exc:
                raise _attach_file_dialog_failure_receipt(
                    adapter,
                    context,
                    exc,
                    current_for_receipt,
                    query_text=query_text,
                    activation_generation=activation_generation,
                    action_generation=generation,
                    target_path=target_for_receipt,
                )

        worker = _ResultActionWorker(generation, perform)
        self._file_dialog_action_workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, payload, error, owned=worker: self._file_dialog_action_finished(
                owned, got_generation, payload, error
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _file_dialog_action_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        payload: object,
        error: object,
    ) -> None:
        self._file_dialog_action_workers.discard(worker)
        if generation != self._file_dialog_action_generation:
            if (
                self._file_dialog_cancel_event is None
                and isinstance(error, WindowsFileDialogError)
                and error.ambiguous
                and isinstance(error.receipt, WindowsFileDialogFailureReceipt)
            ):
                self._last_file_dialog_failure_receipt = error.receipt
            return
        if isinstance(error, WindowsFileDialogError) and isinstance(
            error.receipt,
            WindowsFileDialogFailureReceipt,
        ):
            self._last_file_dialog_failure_receipt = error.receipt
        if (
            isinstance(payload, tuple)
            and len(payload) == 2
            and isinstance(payload[1], WindowsFileDialogReceipt)
        ):
            self._last_file_dialog_receipt = payload[1]
        self._file_dialog_cancel_event = None
        if error is not None:
            message = str(error) or "Open/Save Quick Switch failed."
            if isinstance(error, WindowsFileDialogError) and error.ambiguous:
                message = f"Dialog state may have changed; no retry was attempted. {message}"
            self.status.setText(message)
            self._sync_result_state()
            return
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or not isinstance(payload[0], ListarySearchResult)
            or not isinstance(payload[1], WindowsFileDialogReceipt)
        ):
            self.status.setText("Open/Save Quick Switch returned an invalid receipt.")
            self._sync_result_state()
            return
        current, receipt = payload
        save_context = self._file_dialog_context is not None and self._file_dialog_context.dialog_kind == "save"
        if receipt.dialog_kind == "save" or save_context:
            if not receipt.accepted_by_dialog:
                self.status.setText("The Save dialog state was verified, but the dialog did not confirm Save acceptance.")
                self._sync_result_state()
                return
            if not _native_save_receipt_proves_delivery(
                receipt,
                current,
                self._file_dialog_context,
                generation,
                self._generation,
            ):
                self.status.setText(
                    "Save did not provide matching, stable destination identity and SHA-256 proof; "
                    "no successful Save was claimed."
                )
                self._sync_result_state()
                return
        elif current.kind == "file" and not receipt.accepted_by_dialog:
            self.status.setText(
                f"The {receipt.dialog_kind.title()} dialog state was verified, but the dialog did not confirm "
                f"{receipt.dialog_kind.title()} acceptance."
            )
            self._sync_result_state()
            return
        _start_tracked_one_shot(
            self._activation_workers,
            lambda item_id=current.item_id: self.client.record_activation(item_id, source="file_search"),
        )
        self.status.setText(
            f"Verified {receipt.action} in the {receipt.dialog_kind.title()} dialog "
            f"in {receipt.action_latency_ms:.1f} ms."
        )
        self.accept()
        parent = self.parentWidget()
        if isinstance(parent, QDialog) and parent.objectName() == "spiffyListarySearch":
            parent.accept()

    def _open_results(self, results: tuple[ListarySearchResult, ...]) -> bool:
        if self._file_dialog_context is not None:
            if len(results) != 1:
                self.status.setText("Open/Save Quick Switch works with one result at a time.")
                return False
            return self._start_file_dialog_action(results[0])
        if sys.platform == "win32" and len(results) > 1:
            presentation_entries, action_error = self._selected_action_entries(results, revalidate=False)
            if not presentation_entries:
                self.status.setText(action_error)
                return False
        if sys.platform == "win32" and len(results) > 1 and any(result.kind == "folder" for result in results):
            self.status.setText(
                "Open folders one at a time so Explorer destination identity and semantic readback can be verified."
            )
            return False
        if sys.platform == "win32" and len(results) > 1:
            admitted_operations = tuple(
                _verified_windows_mixed_batch_item_operation(result) for result in results
            )
            if all(admitted_operations) and len(set(admitted_operations)) > 1:
                entries, action_error = self._selected_action_entries(results, revalidate=True)
                if not entries:
                    self.status.setText(action_error)
                    return False
                current_results = tuple(result for result, _presentation in entries)
                current_operations = tuple(
                    _verified_windows_mixed_batch_item_operation(result) for result in current_results
                )
                if current_operations != admitted_operations:
                    self.status.setText(
                        "One or more selected rows changed verified delivery owner before the mixed batch could start."
                    )
                    return False
                return _start_listary_file_manager_batch_open(
                    self,
                    current_results,
                    result_generation=self._generation,
                    current_generation=lambda: self._generation,
                    refresh_label="Refresh File Search.",
                    operation="mixed_verified",
                )
        if (
            sys.platform == "win32"
            and len(results) > 1
            and all(_is_verifiable_windows_shortcut_result(result) for result in results)
        ):
            entries, action_error = self._selected_action_entries(results, revalidate=True)
            if not entries:
                self.status.setText(action_error)
                return False
            current_results = tuple(result for result, _presentation in entries)
            if not all(_is_verifiable_windows_shortcut_result(result) for result in current_results):
                self.status.setText(
                    "One or more selected shortcuts no longer resolve to direct executable targets."
                )
                return False
            return _start_listary_file_manager_batch_open(
                self,
                current_results,
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                operation="launch_shortcut",
            )
        if sys.platform == "win32" and len(results) > 1 and any(
            _is_windows_shortcut_file_result(result) for result in results
        ):
            self.status.setText(
                "Batch Open with Windows shortcuts requires every selected row to be a verifiable direct-executable shortcut."
            )
            return False
        if (
            sys.platform == "win32"
            and len(results) > 1
            and all(_is_direct_windows_executable_app_result(result) for result in results)
        ):
            entries, action_error = self._selected_action_entries(results, revalidate=True)
            if not entries:
                self.status.setText(action_error)
                return False
            current_results = tuple(result for result, _presentation in entries)
            if not all(_is_direct_windows_executable_app_result(result) for result in current_results):
                self.status.setText("One or more selected applications no longer resolve to direct executable targets.")
                return False
            return _start_listary_file_manager_batch_open(
                self,
                current_results,
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                operation="launch_app",
            )
        if sys.platform == "win32" and len(results) > 1 and any(result.kind != "file" for result in results):
            self.status.setText(
                "Verified Windows batch actions support file-only Open, direct-executable app-only launch, "
                "or direct-executable shortcut-only launch."
            )
            return False
        if sys.platform == "win32" and len(results) == 1 and results[0].kind == "folder":
            return _start_listary_file_manager_action(
                self,
                results[0],
                operation="open_folder",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                activation_source="file_search",
            )
        if (
            sys.platform == "win32"
            and len(results) == 1
            and _workspace_application_contract(results[0]) is not None
        ):
            return _start_listary_file_manager_action(
                self,
                results[0],
                operation="launch_workspace_app",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                activation_source="file_search",
            )
        if (
            sys.platform == "win32"
            and len(results) == 1
            and _is_verifiable_windows_shortcut_result(results[0])
        ):
            return _start_listary_file_manager_action(
                self,
                results[0],
                operation="launch_shortcut",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                activation_source="file_search",
            )
        if (
            sys.platform == "win32"
            and len(results) == 1
            and results[0].kind == "file"
            and not _is_windows_shortcut_file_result(results[0])
        ):
            return _start_listary_file_manager_action(
                self,
                results[0],
                operation="open_file",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                activation_source="file_search",
            )
        if sys.platform == "win32" and len(results) == 1 and _is_direct_windows_executable_app_result(results[0]):
            return _start_listary_file_manager_action(
                self,
                results[0],
                operation="launch_app",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
                activation_source="file_search",
            )
        if sys.platform == "win32" and len(results) > 1:
            entries, action_error = self._selected_action_entries(results, revalidate=True)
            if not entries:
                self.status.setText(action_error)
                return False
            return _start_listary_file_manager_batch_open(
                self,
                results,
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
            )
        if sys.platform == "win32" and len(results) == 1:
            entries, action_error = self._selected_action_entries(results, revalidate=True)
            if not entries:
                self.status.setText(action_error)
                return False
            _result, presentation = entries[0]
            if not open_path(presentation.action_path):
                self.status.setText(f"Could not open {presentation.name}.")
                return False
            self.status.setText(
                f"Opened {presentation.name} through Windows without a verifiable destination receipt; "
                "activation history was not changed."
            )
            return True
        entries, action_error = self._selected_action_entries(results, revalidate=True)
        if not entries:
            self.status.setText(action_error)
            return False

        opened_ids: list[int] = []
        for result, presentation in entries:
            if not open_path(presentation.action_path):
                self._record_file_search_activations(tuple(opened_ids))
                relevance_changed = bool(opened_ids) and self._invalidate_relevance_paging_after_activation()
                if len(entries) == 1:
                    self.status.setText(f"Could not open {presentation.name}.")
                else:
                    message = f"Opened {len(opened_ids)} of {len(entries)} selected items; could not open {presentation.name}."
                    if relevance_changed:
                        message += " Relevance history changed; refresh before continuing paging."
                    self.status.setText(message)
                return False
            opened_ids.append(result.item_id)

        self._record_file_search_activations(tuple(opened_ids))
        relevance_changed = self._invalidate_relevance_paging_after_activation()
        if len(entries) == 1:
            presentation = entries[0][1]
            if relevance_changed:
                self.status.setText(
                    f"Opened {presentation.name}. Relevance history changed; refresh before continuing paging."
                )
            else:
                self.status.setText(f"Opened {presentation.name}.")
        elif relevance_changed:
            self.status.setText(
                f"Opened {len(entries)} selected items. Relevance history changed; refresh before continuing paging."
            )
        else:
            self.status.setText(f"Opened {len(entries)} selected items.")
        return True

    def _open_double_clicked(self, item: QTableWidgetItem) -> None:
        if self._is_content_mode():
            result = self._content_result_at_row(item.row())
            if result is not None:
                self._start_content_action("open", result)
            return
        result = self._result_at_row(item.row())
        if result is not None:
            self._open_results((result,))

    def _open_current(self) -> bool:
        if self._is_content_mode():
            selected = self._selected_content_results()
            if len(selected) != 1:
                self.status.setText("Select one Content result first.")
                return False
            return self._start_content_action("open", selected[0])
        return self._open_results(self._selected_results())

    def _reveal_current(self) -> bool:
        if self._is_content_mode():
            selected = self._selected_content_results()
            if len(selected) != 1:
                self.status.setText("Reveal Source works with one Content result at a time.")
                return False
            return self._start_content_action("reveal", selected[0])
        selected = self._selected_results()
        if len(selected) != 1:
            self.status.setText("Reveal works with one selected item at a time.")
            return False
        if self._file_dialog_context is not None:
            self.status.setText("Reveal is disabled while Open/Save Quick Switch owns a native dialog.")
            return False
        if sys.platform == "win32":
            return _start_listary_file_manager_action(
                self,
                selected[0],
                operation="reveal",
                result_generation=self._generation,
                current_generation=lambda: self._generation,
                refresh_label="Refresh File Search.",
            )
        entries, action_error = self._selected_action_entries(selected, revalidate=True)
        if not entries:
            self.status.setText(action_error)
            return False
        _result, presentation = entries[0]
        if not reveal_in_file_manager(presentation.action_path):
            self.status.setText(f"Could not reveal {presentation.name}.")
            return False
        self.status.setText(f"Revealed {presentation.name}.")
        return True

    def _copy_current_path(self) -> bool:
        if self._is_content_mode():
            selected = self._selected_content_results()
            if len(selected) != 1:
                self.status.setText("Copy Source Path works with one Content result at a time.")
                return False
            return self._start_content_action("copy", selected[0])
        entries, action_error = self._selected_action_entries(revalidate=True)
        if not entries:
            self.status.setText(action_error)
            return False
        paths = tuple(presentation.action_path for _result, presentation in entries)
        QApplication.clipboard().setText("\n".join(paths))
        if len(entries) == 1:
            self.status.setText(f"Copied path for {entries[0][1].name}.")
        else:
            self.status.setText(f"Copied {len(entries)} paths.")
        return True

    def _copy_current_file_name(self) -> bool:
        if self._is_content_mode():
            self.status.setText("Copy File Name is available for File Search results.")
            return False
        entries, action_error = self._selected_action_entries(revalidate=True)
        if not entries:
            self.status.setText(action_error)
            return False
        names = tuple(
            Path(presentation.action_path).name or presentation.name
            for _result, presentation in entries
        )
        QApplication.clipboard().setText("\n".join(names))
        if len(names) == 1:
            self.status.setText(f"Copied file name: {names[0]}.")
        else:
            self.status.setText(f"Copied {len(names)} file names.")
        return True

    def _copy_current_items(self) -> bool:
        if self._is_content_mode():
            self.status.setText("Copy Items is available for File Search results.")
            return False
        entries, action_error = self._selected_action_entries(revalidate=True)
        if not entries:
            self.status.setText(action_error)
            return False
        paths = tuple(presentation.action_path for _result, presentation in entries)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        mime.setText("\n".join(paths))
        QApplication.clipboard().setMimeData(mime)
        if len(paths) == 1:
            self.status.setText(f"Copied {entries[0][1].name} to the system clipboard.")
        else:
            self.status.setText(f"Copied {len(paths)} items to the system clipboard.")
        return True

    def _toggle_pin_once_with_readback(self, displayed: ListarySearchResult) -> dict[str, Any]:
        started = time.perf_counter()
        current, presentation, error = _revalidate_result_for_action(
            self.client,
            displayed,
            refresh_label="Refresh File Search.",
        )
        if current is None or presentation is None:
            return {
                "outcome": "blocked",
                "item_id": displayed.item_id,
                "pinned": None,
                "mutation_attempted": False,
                "no_retry": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "detail": error or "The selected result is no longer current.",
            }
        if current.kind not in {"file", "folder"} or presentation.action_path is None:
            return {
                "outcome": "blocked",
                "item_id": current.item_id,
                "pinned": None,
                "mutation_attempted": False,
                "no_retry": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "detail": "Only indexed files and folders can be pinned.",
            }

        toggle_response: bool | None = None
        mutation_error: Exception | None = None
        try:
            toggle_response = self.client.toggle_pin(current.item_id)
        except Exception as exc:
            mutation_error = exc

        try:
            favorites = self.client.get_favorites()
            pinned = self._pin_state_from_favorites(favorites, presentation.action_path)
        except Exception as readback_error:
            detail = (
                f"Pin change result is unknown: {mutation_error or 'authoritative readback failed'}. "
                f"Readback unavailable: {readback_error}. No retry was sent."
            )
            return {
                "outcome": "unknown",
                "item_id": current.item_id,
                "pinned": None,
                "toggle_response": toggle_response,
                "mutation_attempted": True,
                "no_retry": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "detail": detail,
            }

        if mutation_error is not None:
            return {
                "outcome": "reconciled_after_error",
                "item_id": current.item_id,
                "pinned": pinned,
                "toggle_response": None,
                "mutation_attempted": True,
                "no_retry": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "detail": (
                    f"The pin request did not return a confirmed response ({mutation_error}); "
                    f"authoritative readback shows the item is {'pinned' if pinned else 'not pinned'}. No retry was sent."
                ),
            }

        if toggle_response != pinned:
            return {
                "outcome": "readback_mismatch",
                "item_id": current.item_id,
                "pinned": pinned,
                "toggle_response": toggle_response,
                "mutation_attempted": True,
                "no_retry": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "detail": (
                    "Pin response and Favorites readback differed; the displayed state follows authoritative readback. "
                    "No retry was sent."
                ),
            }

        return {
            "outcome": "success",
            "item_id": current.item_id,
            "pinned": pinned,
            "toggle_response": toggle_response,
            "mutation_attempted": True,
            "no_retry": True,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "detail": "",
        }

    def _toggle_current_pin(self) -> bool:
        result, selection_error = self._pin_selection()
        label, enabled, reason = self._pin_action_state()
        if result is None or not enabled:
            self.status.setText(selection_error or reason or "Pin state is not ready.")
            return False
        self._pin_generation += 1
        generation = self._pin_generation
        self._pin_busy = True
        self._pin_busy_generation = generation
        self._pin_state_loading = False
        self._pin_state_reason = "A pin change is already in progress."
        self._last_pin_receipt = None
        self._sync_pin_presentation()
        self.status.setText(f"{label}ning {result.name}..." if label == "Pin" else f"Unpinning {result.name}...")
        worker = _ResultActionWorker(
            generation,
            lambda result=result: self._toggle_pin_once_with_readback(result),
        )
        self._pin_workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, receipt, error, owned=worker, name=result.name: self._pin_finished(
                owned,
                finished_generation,
                receipt,
                error,
                name,
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _pin_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        receipt: object,
        error: object,
        name: str,
    ) -> None:
        self._pin_workers.discard(worker)
        if generation != self._pin_generation:
            if self._pin_busy_generation == generation:
                self._pin_busy = False
                self._pin_busy_generation = None
                self._sync_pin_presentation()
            return
        self._pin_busy = False
        self._pin_busy_generation = None
        if error is not None:
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = (
                f"Pin change result is unknown: {error}. Refresh or reconnect before changing it again; no retry was sent."
            )
            self.status.setText(self._pin_state_reason)
            self._sync_pin_presentation()
            return
        if not isinstance(receipt, dict) or not isinstance(receipt.get("item_id"), int):
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = (
                "Pin change returned an invalid receipt. Refresh before changing it again; no retry was sent."
            )
            self.status.setText(self._pin_state_reason)
            self._sync_pin_presentation()
            return

        self._last_pin_receipt = dict(receipt)
        pinned = receipt.get("pinned")
        if isinstance(pinned, bool):
            self._pin_state_item_id = int(receipt["item_id"])
            self._pin_state = pinned
            self._pin_state_reason = ""
        else:
            self._pin_state_item_id = None
            self._pin_state = None
            self._pin_state_reason = str(
                receipt.get("detail")
                or "Pin state is unknown. Refresh or reconnect before changing it again; no retry was sent."
            )
        outcome = str(receipt.get("outcome") or "unknown")
        if outcome == "success" and isinstance(pinned, bool):
            self.status.setText(f"{'Pinned' if pinned else 'Unpinned'} {name}.")
        else:
            self.status.setText(str(receipt.get("detail") or self._pin_state_reason))
        self._sync_pin_presentation()
        if isinstance(pinned, bool) and self._recommendations_changed is not None:
            self._recommendations_changed()
        if isinstance(pinned, bool) and not self.search.text().strip() and self._active_request_id is None:
            self._refresh_current()

    def reject(self) -> None:
        self._generation += 1
        self._preview_generation += 1
        self._saved_filter_generation += 1
        self._saved_filters_loading = False
        self._retire_pin_state()
        self._dismiss_filter_discovery()
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._cancel_content_action()
        self._debounce.stop()
        self._preview_timer.stop()
        self._cancel_preview_request()
        self._cancel_active()
        super().reject()

    def shutdown(self, timeout_ms: int = 2_000) -> bool:
        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        self.reject()
        activation_shutdown_ok = _wait_tracked_one_shots(
            self._activation_workers,
            max(0, int((deadline - time.monotonic()) * 1000)),
        )
        file_dialog_shutdown_ok = True
        for worker in tuple(self._file_dialog_action_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                file_dialog_shutdown_ok = False
                break
        self._file_dialog_action_workers.intersection_update(
            worker for worker in self._file_dialog_action_workers if not worker.done.is_set()
        )
        file_manager_shutdown_ok = True
        for worker in tuple(self._file_manager_action_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                file_manager_shutdown_ok = False
                break
        self._file_manager_action_workers.intersection_update(
            worker for worker in self._file_manager_action_workers if not worker.done.is_set()
        )
        content_action_shutdown_ok = True
        for worker in tuple(self._content_action_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                content_action_shutdown_ok = False
                break
        self._content_action_workers.intersection_update(
            worker for worker in self._content_action_workers if not worker.done.is_set()
        )
        content_search_shutdown_ok = True
        for worker in tuple(self._workers):
            if isinstance(worker, _ContentSearchWorker) and not worker.done.wait(max(0.0, deadline - time.monotonic())):
                content_search_shutdown_ok = False
                break
        pin_shutdown_ok = True
        for worker in tuple(self._pin_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                pin_shutdown_ok = False
                break
        self._pin_workers.intersection_update(
            worker for worker in self._pin_workers if not worker.done.is_set()
        )
        saved_filter_shutdown_ok = True
        for worker in tuple(self._saved_filter_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                saved_filter_shutdown_ok = False
                break
        self._saved_filter_workers.intersection_update(
            worker for worker in self._saved_filter_workers if not worker.done.is_set()
        )
        file_preview_shutdown_ok = True
        for worker in tuple(self._preview_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                file_preview_shutdown_ok = False
                break
        self._preview_workers.intersection_update(
            worker for worker in self._preview_workers if not worker.done.is_set()
        )
        broker, self._preview_broker = self._preview_broker, None
        preview_shutdown_ok = True
        if broker is not None and not self._owns_preview_broker:
            try:
                broker.cancel_consumer(self._preview_consumer_id)
            except Exception:
                pass
        elif broker is not None:
            self._owns_preview_broker = False
            try:
                preview_shutdown_ok = bool(
                    broker.shutdown(max(0, int((deadline - time.monotonic()) * 1000)))
                )
            except Exception:
                preview_shutdown_ok = False
        if self._owns_client:
            self.client.close()
        return (
            activation_shutdown_ok
            and file_dialog_shutdown_ok
            and file_manager_shutdown_ok
            and content_action_shutdown_ok
            and content_search_shutdown_ok
            and pin_shutdown_ok
            and saved_filter_shutdown_ok
            and file_preview_shutdown_ok
            and preview_shutdown_ok
        )


class ListaryRootPolicyDialog(QDialog):
    """Edit one canonical RootRecord configuration without owning persistence."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        path: str,
        root: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ListaryRootPolicyDialog")
        self.setWindowTitle("Edit Indexed Folder" if root is not None else "Add Indexed Folder")
        self.setModal(True)
        self.resize(520, 330)
        apply_broadcast_dark(self)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.path_edit = QLineEdit(str(path), self)
        self.path_edit.setReadOnly(True)
        self.path_edit.setAccessibleName("Indexed folder path")
        form.addRow("Folder", self.path_edit)

        self.enabled = QCheckBox("Include this folder in Listary search", self)
        self.enabled.setChecked(bool(root.get("enabled", True)) if root is not None else True)
        self.enabled.setAccessibleName("Indexed folder enabled")
        form.addRow("Enabled", self.enabled)

        self.policy = QComboBox(self)
        for key in ("automatic", "never", "interval", "daily", "weekly"):
            self.policy.addItem(_REFRESH_POLICY_LABELS[key], key)
        self.policy.setAccessibleName("Refresh policy")
        form.addRow("Refresh", self.policy)

        self.interval_seconds = QSpinBox(self)
        self.interval_seconds.setRange(1, 2_147_483_647)
        self.interval_seconds.setValue(3600)
        self.interval_seconds.setSuffix(" seconds")
        self.interval_seconds.setAccessibleName("Refresh interval seconds")
        form.addRow("Interval", self.interval_seconds)

        self.weekday = QComboBox(self)
        for index, label in enumerate(_WEEKDAY_LABELS):
            self.weekday.addItem(label, index)
        self.weekday.setAccessibleName("Weekly refresh day")
        form.addRow("Weekday", self.weekday)

        time_row = QWidget(self)
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        self.hour = QSpinBox(time_row)
        self.hour.setRange(0, 23)
        self.hour.setValue(6)
        self.hour.setSuffix(" h")
        self.hour.setAccessibleName("Refresh hour")
        self.minute = QSpinBox(time_row)
        self.minute.setRange(0, 59)
        self.minute.setValue(0)
        self.minute.setSuffix(" min")
        self.minute.setAccessibleName("Refresh minute")
        time_layout.addWidget(self.hour)
        time_layout.addWidget(self.minute)
        form.addRow("Local time", time_row)
        layout.addLayout(form)

        self.status = QLabel("", self)
        self.status.setObjectName("PanelStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setProperty("spiffyRole", "primary")
        self.save_button.setAccessibleName("Save indexed folder settings")
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.policy.currentIndexChanged.connect(self._sync_policy_controls)
        if root is not None:
            self._load_policy(root.get("refresh_policy", "automatic"))
        else:
            self.policy.setCurrentIndex(self.policy.findData("automatic"))
        self._sync_policy_controls()

    def _load_policy(self, value: Any) -> None:
        try:
            policy = normalize_refresh_policy(value)
        except ValueError as exc:
            self.policy.setCurrentIndex(-1)
            self.status.setText(f"Stored refresh policy is invalid: {exc}")
            return
        if isinstance(policy, str):
            self.policy.setCurrentIndex(self.policy.findData(policy))
            return
        mode, payload = next(iter(policy.items()))
        self.policy.setCurrentIndex(self.policy.findData(mode))
        if mode == "interval":
            self.interval_seconds.setValue(payload["seconds"])
        else:
            self.hour.setValue(payload["hour"])
            self.minute.setValue(payload["minute"])
            if mode == "weekly":
                self.weekday.setCurrentIndex(self.weekday.findData(payload["weekday_from_monday"]))

    def refresh_policy(self) -> str | dict[str, dict[str, int]]:
        mode = self.policy.currentData()
        if mode == "automatic" or mode == "never":
            return normalize_refresh_policy(mode)
        if mode == "interval":
            return normalize_refresh_policy({"interval": {"seconds": self.interval_seconds.value()}})
        if mode == "daily":
            return normalize_refresh_policy({"daily": {"hour": self.hour.value(), "minute": self.minute.value()}})
        if mode == "weekly":
            return normalize_refresh_policy(
                {
                    "weekly": {
                        "weekday_from_monday": self.weekday.currentData(),
                        "hour": self.hour.value(),
                        "minute": self.minute.value(),
                    }
                }
            )
        raise ValueError("Choose a valid refresh policy")

    def configuration(self) -> dict[str, Any]:
        return {"refresh_policy": self.refresh_policy(), "enabled": self.enabled.isChecked()}

    def _sync_policy_controls(self) -> None:
        mode = self.policy.currentData()
        self.interval_seconds.setEnabled(mode == "interval")
        self.weekday.setEnabled(mode == "weekly")
        timed = mode in {"daily", "weekly"}
        self.hour.setEnabled(timed)
        self.minute.setEnabled(timed)
        try:
            self.refresh_policy()
        except ValueError as exc:
            self.status.setText(str(exc))
            self.save_button.setEnabled(False)
        else:
            if self.status.text().startswith("Stored refresh policy is invalid"):
                self.status.clear()
            self.save_button.setEnabled(True)

    def set_busy(self, busy: bool) -> None:
        for control in (
            self.enabled,
            self.policy,
            self.interval_seconds,
            self.weekday,
            self.hour,
            self.minute,
        ):
            control.setEnabled(not busy)
        if not busy:
            self._sync_policy_controls()
        else:
            self.save_button.setEnabled(False)

    def _accept_if_valid(self) -> None:
        try:
            self.configuration()
        except ValueError as exc:
            self.status.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        self.accept()


class ListaryFilterManagementDialog(QDialog):
    """Edit canonical Core saved filters without adding a second settings owner."""

    FILTER_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        client: ListaryAgentClient,
        filters_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self._filters_changed = filters_changed
        self._generation = 0
        self._busy = False
        self._load_failed = False
        self._requires_reconcile = False
        self._workers: set[_ResultActionWorker] = set()
        self._filters: tuple[dict[str, Any], ...] = ()
        self._draft_id: str | None = None
        self.setObjectName("ListaryFilterManagementDialog")
        self.setWindowTitle("File Search Filters")
        self.resize(820, 620)
        apply_broadcast_dark(self)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Saved filters use the canonical Listary Core settings owner. Select a filter to edit it, or add a new filter.",
            self,
        )
        intro.setObjectName("PanelStatus")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(("Keyword", "Name", "Search for"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        editor = QWidget(self)
        form = QFormLayout(editor)
        form.setContentsMargins(0, 0, 0, 0)
        self.keyword = QLineEdit(editor)
        self.keyword.setAccessibleName("Filter keyword")
        self.keyword.setPlaceholderText("review")
        self.name = QLineEdit(editor)
        self.name.setAccessibleName("Filter name")
        self.name.setPlaceholderText("Review files")
        self.extra_query = QLineEdit(editor)
        self.extra_query.setAccessibleName("Filter extra query")
        self.extra_query.setPlaceholderText("Optional extra search terms")
        self.search_paths = QPlainTextEdit(editor)
        self.search_paths.setAccessibleName("Filter search folders")
        self.search_paths.setPlaceholderText("Optional folders, one per line")
        self.search_paths.setMaximumHeight(84)
        self.target = QComboBox(editor)
        self.target.setAccessibleName("Filter file or folder target")
        self.target.addItem("Files and folders", "any")
        self.target.addItem("Files only", "files")
        self.target.addItem("Folders only", "folders")
        self.extensions = QLineEdit(editor)
        self.extensions.setAccessibleName("Filter file extensions")
        self.extensions.setPlaceholderText("Optional: mp4;mov;png")
        self.extensions.setToolTip("Separate file extensions with semicolons; no leading dot is required.")
        form.addRow("Keyword", self.keyword)
        form.addRow("Name", self.name)
        form.addRow("Extra query", self.extra_query)
        form.addRow("Search in", self.search_paths)
        form.addRow("Search for", self.target)
        form.addRow("Extensions", self.extensions)
        layout.addWidget(editor)

        self.status = QLabel("Loading saved filters...", self)
        self.status.setObjectName("PanelStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Add", self)
        self.add_button.setAccessibleName("Add saved filter")
        self.save_button = QPushButton("Save", self)
        self.save_button.setProperty("spiffyRole", "primary")
        self.save_button.setAccessibleName("Save saved filter")
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setProperty("spiffyRole", "danger")
        self.delete_button.setAccessibleName("Delete saved filter")
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setAccessibleName("Reload saved filters")
        self.close_button = QPushButton("Close", self)
        for button in (self.add_button, self.save_button, self.delete_button, self.refresh_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.add_button.clicked.connect(self._new_filter)
        self.save_button.clicked.connect(self._save_current)
        self.delete_button.clicked.connect(self._delete_selected)
        self.refresh_button.clicked.connect(self.reload)
        self.close_button.clicked.connect(self.reject)
        for control in (self.keyword, self.name, self.extra_query, self.extensions):
            control.textChanged.connect(self._sync_actions)
        self.search_paths.textChanged.connect(self._sync_actions)
        self.target.currentIndexChanged.connect(self._sync_actions)
        self._sync_actions()

    def open_manager(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.reload()

    def _selected_filter(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(self.FILTER_ROLE) if item is not None else None
        return dict(value) if isinstance(value, dict) else None

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.table.setEnabled(not busy)
        for control in (self.keyword, self.name, self.extra_query, self.search_paths, self.target, self.extensions):
            control.setEnabled(not busy)
        self._sync_actions()

    def _sync_actions(self, *_args: Any) -> None:
        selected = self._selected_filter()
        can_mutate = not self._busy and not self._load_failed and not self._requires_reconcile
        has_required = bool(self._draft_id and self.keyword.text().strip() and self.name.text().strip())
        self.add_button.setEnabled(can_mutate)
        self.save_button.setEnabled(can_mutate and has_required)
        self.delete_button.setEnabled(
            can_mutate and selected is not None and self._draft_id == str(selected.get("id") or "")
        )
        self.refresh_button.setEnabled(not self._busy)

    def _start_worker(self, operation: str, callback: Callable[[], Any], message: str) -> None:
        if self._busy:
            return
        self._generation += 1
        generation = self._generation
        self._set_busy(True)
        self.status.setText(message)
        worker = _ResultActionWorker(generation, callback)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, result, error, owned=worker, op=operation: self._worker_finished(
                owned, finished_generation, op, result, error
            )
        )
        QThreadPool.globalInstance().start(worker)

    def reload(self) -> None:
        self._start_worker("load", self.client.list_filters, "Loading saved filters...")

    @staticmethod
    def _target_label(value: object) -> str:
        return {"files": "Files", "folders": "Folders"}.get(str(value or "any"), "Files + folders")

    def _populate(self, filters: Iterable[dict[str, Any]], *, select_id: str | None = None) -> None:
        rows = tuple(dict(row) for row in filters)
        self._filters = rows
        desired_id = select_id or self._draft_id or str((self._selected_filter() or {}).get("id") or "")
        self.table.setRowCount(0)
        selected_row = -1
        for preset in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            keyword_item = QTableWidgetItem(str(preset.get("keyword") or ""))
            keyword_item.setData(self.FILTER_ROLE, dict(preset))
            self.table.setItem(row, 0, keyword_item)
            self.table.setItem(row, 1, QTableWidgetItem(str(preset.get("name") or "")))
            self.table.setItem(row, 2, QTableWidgetItem(self._target_label(preset.get("target"))))
            if str(preset.get("id") or "") == desired_id:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif rows and self._draft_id is None:
            self.table.selectRow(0)
        elif not rows and self._draft_id is None:
            self._clear_editor()
        self._sync_actions()

    def _selection_changed(self) -> None:
        preset = self._selected_filter()
        if preset is None or self._busy:
            return
        self._load_editor(preset)

    def _clear_editor(self) -> None:
        self._draft_id = None
        self.keyword.clear()
        self.name.clear()
        self.extra_query.clear()
        self.search_paths.clear()
        self.target.setCurrentIndex(max(0, self.target.findData("any")))
        self.extensions.clear()
        self._sync_actions()

    def _load_editor(self, preset: dict[str, Any]) -> None:
        self._draft_id = str(preset.get("id") or "") or None
        self.keyword.setText(str(preset.get("keyword") or ""))
        self.name.setText(str(preset.get("name") or ""))
        self.extra_query.setText(str(preset.get("extra_query") or ""))
        self.search_paths.setPlainText("\n".join(str(path) for path in preset.get("search_paths") or ()))
        target_index = self.target.findData(str(preset.get("target") or "any"))
        self.target.setCurrentIndex(target_index if target_index >= 0 else 0)
        self.extensions.setText(";".join(str(value) for value in preset.get("extensions") or ()))
        self._sync_actions()

    def _new_filter(self) -> None:
        if self._busy or self._load_failed or self._requires_reconcile:
            return
        self.table.clearSelection()
        self._draft_id = str(uuid4())
        self.keyword.clear()
        self.name.clear()
        self.extra_query.clear()
        self.search_paths.clear()
        self.target.setCurrentIndex(max(0, self.target.findData("any")))
        self.extensions.clear()
        self.status.setText("New filter. Keyword and Name are required; Save writes through Listary Core.")
        self.keyword.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._sync_actions()

    def _editor_preset(self) -> dict[str, Any]:
        if not self._draft_id:
            raise ValueError("Choose Add or select an existing filter first.")
        keyword = self.keyword.text().strip()
        name = self.name.text().strip()
        if not keyword:
            raise ValueError("Keyword is required.")
        if not name:
            raise ValueError("Name is required.")
        search_paths = [line.strip() for line in self.search_paths.toPlainText().splitlines() if line.strip()]
        extensions = [value.strip() for value in self.extensions.text().split(";") if value.strip()]
        return {
            "id": self._draft_id,
            "keyword": keyword,
            "name": name,
            "extra_query": self.extra_query.text().strip(),
            "search_paths": search_paths,
            "target": str(self.target.currentData() or "any"),
            "extensions": extensions,
        }

    def _put_once_with_readback(self, preset: dict[str, Any]) -> dict[str, Any]:
        mutation_error: Exception | None = None
        response: dict[str, Any] | None = None
        try:
            response = self.client.put_filter(preset)
        except Exception as exc:
            mutation_error = exc
        try:
            filters = tuple(self.client.list_filters())
        except Exception as readback_error:
            return {
                "outcome": "unknown",
                "filters": None,
                "filter_id": str(preset["id"]),
                "no_retry": True,
                "detail": (
                    f"Filter save result is unknown: {mutation_error or 'authoritative readback failed'}. "
                    f"Readback unavailable: {readback_error}. No retry was sent."
                ),
            }
        canonical = next((dict(row) for row in filters if str(row.get("id") or "") == str(preset["id"])), None)
        if mutation_error is not None:
            return {
                "outcome": "reconciled_after_error" if canonical is not None else "not_committed_after_error",
                "filters": filters,
                "filter_id": str(preset["id"]),
                "canonical": canonical,
                "no_retry": True,
                "detail": (
                    f"The save request did not return a confirmed response ({mutation_error}); authoritative readback "
                    + ("contains the saved filter. No retry was sent." if canonical is not None else "does not contain the filter. No retry was sent.")
                ),
            }
        if canonical is None:
            return {
                "outcome": "readback_mismatch",
                "filters": filters,
                "filter_id": str(preset["id"]),
                "canonical": None,
                "no_retry": True,
                "detail": "Save returned success but authoritative readback did not contain the filter. No retry was sent.",
            }
        return {
            "outcome": "success" if response is not None else "readback_only",
            "filters": filters,
            "filter_id": str(preset["id"]),
            "canonical": canonical,
            "no_retry": True,
            "detail": "",
        }

    def _remove_once_with_readback(self, filter_id: str) -> dict[str, Any]:
        mutation_error: Exception | None = None
        try:
            self.client.remove_filter(filter_id)
        except Exception as exc:
            mutation_error = exc
        try:
            filters = tuple(self.client.list_filters())
        except Exception as readback_error:
            return {
                "outcome": "unknown",
                "filters": None,
                "filter_id": filter_id,
                "no_retry": True,
                "detail": (
                    f"Filter delete result is unknown: {mutation_error or 'authoritative readback failed'}. "
                    f"Readback unavailable: {readback_error}. No retry was sent."
                ),
            }
        still_present = any(str(row.get("id") or "") == filter_id for row in filters)
        if mutation_error is not None:
            return {
                "outcome": "reconciled_after_error" if not still_present else "not_committed_after_error",
                "filters": filters,
                "filter_id": filter_id,
                "no_retry": True,
                "detail": (
                    f"The delete request did not return a confirmed response ({mutation_error}); authoritative readback "
                    + ("still contains the filter. No retry was sent." if still_present else "confirms the filter is gone. No retry was sent.")
                ),
            }
        if still_present:
            return {
                "outcome": "readback_mismatch",
                "filters": filters,
                "filter_id": filter_id,
                "no_retry": True,
                "detail": "Delete returned success but authoritative readback still contains the filter. No retry was sent.",
            }
        return {
            "outcome": "success",
            "filters": filters,
            "filter_id": filter_id,
            "no_retry": True,
            "detail": "",
        }

    def _save_current(self) -> None:
        if self._busy or self._requires_reconcile:
            return
        try:
            preset = self._editor_preset()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._start_worker(
            "save",
            lambda preset=dict(preset): self._put_once_with_readback(preset),
            f"Saving {preset['name']}...",
        )

    def _delete_selected(self) -> None:
        preset = self._selected_filter()
        if preset is None or self._busy or self._requires_reconcile:
            return
        filter_id = str(preset.get("id") or "")
        name = str(preset.get("name") or preset.get("keyword") or "filter")
        if not filter_id:
            return
        self._start_worker(
            "delete",
            lambda filter_id=filter_id: self._remove_once_with_readback(filter_id),
            f"Deleting {name}...",
        )

    def _worker_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        operation: str,
        result: Any,
        error: Exception | None,
    ) -> None:
        self._workers.discard(worker)
        if generation != self._generation:
            return
        self._set_busy(False)
        if error is not None:
            self._load_failed = operation == "load"
            self._requires_reconcile = operation != "load"
            suffix = " Refresh before changing filters again; no retry was sent." if operation != "load" else ""
            self.status.setText(f"Could not {operation} saved filters: {error}.{suffix}")
            self._sync_actions()
            return
        if operation == "load":
            filters = tuple(dict(row) for row in result) if isinstance(result, (tuple, list)) else ()
            self._load_failed = False
            self._requires_reconcile = False
            self._draft_id = None
            self._populate(filters)
            self.status.setText(f"{len(filters)} saved filter{'s' if len(filters) != 1 else ''}.")
            return
        if not isinstance(result, dict) or result.get("no_retry") is not True:
            self._requires_reconcile = True
            self.status.setText("Saved-filter mutation returned an invalid receipt. Refresh before changing filters again; no retry was sent.")
            self._sync_actions()
            return
        filters_value = result.get("filters")
        if isinstance(filters_value, (tuple, list)):
            filters = tuple(dict(row) for row in filters_value if isinstance(row, dict))
            self._load_failed = False
            self._requires_reconcile = False
            filter_id = str(result.get("filter_id") or "")
            if operation == "delete":
                self._draft_id = None
                self._populate(filters)
            else:
                self._draft_id = filter_id or self._draft_id
                self._populate(filters, select_id=self._draft_id)
            if self._filters_changed is not None:
                self._filters_changed()
        else:
            self._requires_reconcile = True
        outcome = str(result.get("outcome") or "unknown")
        detail = str(result.get("detail") or "")
        if outcome == "success":
            self.status.setText("Saved filter." if operation == "save" else "Deleted saved filter.")
        elif detail:
            self.status.setText(detail)
        else:
            self.status.setText("Saved-filter state requires Refresh before another change; no retry was sent.")
        self._sync_actions()

    def reject(self) -> None:
        self._generation += 1
        super().reject()

    def shutdown(self, timeout_ms: int = 2_000) -> bool:
        self._generation += 1
        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        ok = True
        for worker in tuple(self._workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                ok = False
                break
        self._workers.intersection_update(worker for worker in self._workers if not worker.done.is_set())
        return ok and not self._workers


class ListaryRootManagementDialog(QDialog):
    ROOT_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        client: ListaryAgentClient,
        roots_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self._roots_changed = roots_changed
        self._generation = 0
        self._busy = False
        self._load_failed = False
        self._workers: set[_RootAdminWorker] = set()
        self._success_after_reload = ""
        self.setObjectName("ListaryRootManagementDialog")
        self.setWindowTitle("Indexed Folders")
        self.resize(840, 460)
        apply_broadcast_dark(self)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Indexed folders use the same Listary catalog. Automatic reacts to filesystem changes; scheduled policies use the resident refresh owner.",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("PanelStatus")
        layout.addWidget(intro)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(("Folder", "Refresh", "Enabled", "State"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        layout.addWidget(self.table, 1)

        self.status = QLabel("Loading indexed folders...", self)
        self.status.setObjectName("PanelStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Add Folder...", self)
        self.add_button.setProperty("spiffyRole", "primary")
        self.add_button.setAccessibleName("Add indexed folder")
        self.edit_button = QPushButton("Edit...", self)
        self.edit_button.setAccessibleName("Edit indexed folder refresh settings")
        self.reindex_button = QPushButton("Reindex Now", self)
        self.reindex_button.setAccessibleName("Reindex selected folder now")
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.setProperty("spiffyRole", "danger")
        self.remove_button.setAccessibleName("Remove selected indexed folder")
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setAccessibleName("Reload indexed folders")
        self.close_button = QPushButton("Close", self)
        self.add_button.clicked.connect(self._choose_add_folder)
        self.edit_button.clicked.connect(self._edit_selected)
        self.reindex_button.clicked.connect(self._reindex_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.refresh_button.clicked.connect(self.reload)
        self.close_button.clicked.connect(self.reject)
        for button in (
            self.add_button,
            self.edit_button,
            self.reindex_button,
            self.remove_button,
            self.refresh_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._sync_actions()

    def open_manager(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.reload()

    def _selected_root(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(self.ROOT_ROLE) if item is not None else None
        return dict(value) if isinstance(value, dict) else None

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.table.setEnabled(not busy)
        self._sync_actions()

    def _sync_actions(self) -> None:
        selected = self._selected_root()
        can_mutate = not self._busy and not self._load_failed
        self.add_button.setEnabled(can_mutate)
        self.edit_button.setEnabled(can_mutate and selected is not None)
        self.remove_button.setEnabled(can_mutate and selected is not None)
        self.reindex_button.setEnabled(can_mutate and selected is not None and bool(selected.get("enabled")))
        self.refresh_button.setEnabled(not self._busy)

    def _start_admin(self, operation: str, callback: Callable[[], Any], message: str) -> None:
        if self._busy:
            return
        self._generation += 1
        generation = self._generation
        self._set_busy(True)
        self.status.setText(message)
        worker = _RootAdminWorker(generation, operation, callback)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, finished_operation, result, error, owned=worker: self._admin_finished(
                owned, finished_generation, finished_operation, result, error
            )
        )
        QThreadPool.globalInstance().start(worker)

    def reload(self) -> None:
        self._start_admin("load", self.client.list_roots, "Loading indexed folders...")

    def _admin_finished(
        self,
        worker: _RootAdminWorker,
        generation: int,
        operation: str,
        result: Any,
        error: Exception | None,
    ) -> None:
        self._workers.discard(worker)
        if generation != self._generation:
            return
        self._set_busy(False)
        if error is not None:
            self._load_failed = operation == "load"
            self.status.setText(f"Could not {operation} indexed folders: {error}")
            self._sync_actions()
            return
        if operation == "load":
            self._load_failed = False
            roots = tuple(result) if isinstance(result, (tuple, list)) else ()
            self._populate(roots)
            if self._success_after_reload:
                self.status.setText(self._success_after_reload)
                self._success_after_reload = ""
                if self._roots_changed is not None:
                    self._roots_changed()
            else:
                enabled_count = sum(1 for root in roots if bool(root.get("enabled", True)))
                if not roots:
                    self.status.setText("No indexed folders yet. Add a folder to enable file search.")
                elif enabled_count == 0:
                    self.status.setText(
                        f"{len(roots)} indexed folder{'s' if len(roots) != 1 else ''}, all disabled. "
                        "Enable one to search local files."
                    )
                else:
                    self.status.setText(f"{len(roots)} indexed folder{'s' if len(roots) != 1 else ''}.")
            self._sync_actions()
            return
        warning = ""
        if isinstance(result, dict):
            warning = str(result.get("warning") or "")
        self._success_after_reload = warning or {
            "add": "Indexed folder added.",
            "edit": "Indexed folder settings saved.",
            "remove": "Indexed folder removed.",
            "reindex": "Indexed folder refreshed.",
        }.get(operation, "Indexed folders updated.")
        self.reload()

    def _populate(self, roots: tuple[dict[str, Any], ...]) -> None:
        selected_id = (self._selected_root() or {}).get("id")
        self.table.setRowCount(0)
        selected_row = -1
        for root in roots:
            row = self.table.rowCount()
            self.table.insertRow(row)
            path_item = QTableWidgetItem(str(root.get("path") or ""))
            path_item.setData(self.ROOT_ROLE, dict(root))
            self.table.setItem(row, 0, path_item)
            try:
                policy_text = refresh_policy_label(root.get("refresh_policy", "automatic"))
            except ValueError:
                policy_text = "Invalid policy"
            self.table.setItem(row, 1, QTableWidgetItem(policy_text))
            self.table.setItem(row, 2, QTableWidgetItem("Yes" if root.get("enabled", True) else "No"))
            state = str(root.get("availability") or "unknown").replace("_", " ").title()
            if root.get("last_error"):
                state = f"{state} · {root['last_error']}"
            self.table.setItem(row, 3, QTableWidgetItem(state))
            if root.get("id") == selected_id:
                selected_row = row
        if self.table.rowCount() > 0:
            self.table.selectRow(selected_row if selected_row >= 0 else 0)
        self._sync_actions()

    def _choose_add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add Indexed Folder")
        if not path:
            return
        editor = ListaryRootPolicyDialog(self, path=path)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        config = editor.configuration()
        self._start_add_root(path, config["refresh_policy"], bool(config["enabled"]))

    def _start_add_root(self, path: str, refresh_policy: Any, enabled: bool) -> None:
        policy = normalize_refresh_policy(refresh_policy)

        def add() -> dict[str, Any]:
            root = self.client.add_root(path)
            configured = dict(root)
            configured["refresh_policy"] = policy
            configured["enabled"] = bool(enabled)
            try:
                canonical = self.client.put_root(configured)
            except Exception:
                try:
                    self.client.remove_root(str(root["id"]))
                except Exception:
                    pass
                raise
            if not canonical.get("enabled"):
                return {"root": canonical}
            try:
                report = self.client.reconcile_root(str(canonical["id"]))
                return {"root": canonical, "report": report}
            except Exception as exc:
                return {"root": canonical, "warning": f"Settings saved, but initial indexing failed: {exc}"}

        self._start_admin("add", add, "Adding indexed folder...")

    def _edit_selected(self) -> None:
        root = self._selected_root()
        if root is None or self._busy:
            return
        editor = ListaryRootPolicyDialog(self, path=str(root.get("path") or ""), root=root)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        config = editor.configuration()
        self._start_edit_root(root, config["refresh_policy"], bool(config["enabled"]))

    def _start_edit_root(self, root: dict[str, Any], refresh_policy: Any, enabled: bool) -> None:
        policy = normalize_refresh_policy(refresh_policy)
        original_policy = normalize_refresh_policy(root.get("refresh_policy", "automatic"))
        original_enabled = bool(root.get("enabled", True))

        def edit() -> dict[str, Any]:
            updated = dict(root)
            updated["refresh_policy"] = policy
            updated["enabled"] = bool(enabled)
            canonical = self.client.put_root(updated)
            should_reconcile = bool(enabled) and (not original_enabled or policy != original_policy)
            if not should_reconcile:
                return {"root": canonical}
            try:
                report = self.client.reconcile_root(str(canonical["id"]))
                return {"root": canonical, "report": report}
            except Exception as exc:
                return {"root": canonical, "warning": f"Settings saved, but catch-up indexing failed: {exc}"}

        self._start_admin("edit", edit, "Saving indexed folder settings...")

    def _reindex_selected(self) -> None:
        root = self._selected_root()
        if root is None or self._busy or not root.get("enabled", True):
            return

        def reindex() -> dict[str, Any]:
            return self.client.reconcile_root(str(root["id"]))

        self._start_admin("reindex", reindex, "Refreshing indexed folder...")

    def _remove_selected(self) -> None:
        root = self._selected_root()
        if root is None or self._busy:
            return
        answer = QMessageBox.question(
            self,
            "Remove Indexed Folder",
            f"Remove this folder from the Listary index?\n\n{root.get('path') or ''}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_remove_root(root)

    def _start_remove_root(self, root: dict[str, Any]) -> None:
        self._start_admin(
            "remove",
            lambda: self.client.remove_root(str(root["id"])),
            "Removing indexed folder...",
        )

    def shutdown(self, timeout_ms: int = 2_000) -> bool:
        self.reject()
        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        for worker in tuple(self._workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                return False
        self._workers.clear()
        return True


class ListarySearchDialog(QDialog):
    RESULT_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        client: ListaryAgentClient | None = None,
        file_dialog_adapter: WindowsFileDialogAdapter | None = None,
    ) -> None:
        super().__init__(parent)
        self._owns_client = client is None
        self.client = client or ListaryAgentClient()
        self._generation = 0
        self._search_generation = 0
        self._active_request_id: int | None = None
        self._active_root_request_id: int | None = None
        self._workers: set[_SearchWorker] = set()
        self._root_workers: set[_RootWorker] = set()
        self._activation_workers: set[_OneShotWorker] = set()
        self._pin_workers: set[_ResultActionWorker] = set()
        self._pin_generation = 0
        self._pin_busy = False
        self._file_dialog_adapter = file_dialog_adapter
        self._file_dialog_context: WindowsFileDialogContext | None = None
        self._file_dialog_action_workers: set[_ResultActionWorker] = set()
        self._file_dialog_action_generation = 0
        self._file_dialog_cancel_event: threading.Event | None = None
        self._last_file_dialog_receipt: WindowsFileDialogReceipt | None = None
        self._last_file_dialog_failure_receipt: WindowsFileDialogFailureReceipt | None = None
        self._file_manager_action_workers: set[_ResultActionWorker] = set()
        self._file_manager_action_generation = 0
        self._file_manager_cancel_event: threading.Event | None = None
        self._last_file_manager_receipt: ListaryFileManagerReceipt | None = None
        self._last_file_manager_failure_receipt: ListaryFileManagerFailureReceipt | None = None
        self._last_result_to_file_manager_ready_ms: float | None = None
        self._last_result_ready_monotonic: float | None = None
        self._last_result_to_dialog_ready_ms: float | None = None
        self._file_search_dialog: ListaryFileSearchDialog | None = None
        self._root_management_dialog: ListaryRootManagementDialog | None = None
        self._filter_management_dialog: ListaryFilterManagementDialog | None = None
        self._result_shortcuts: list[QShortcut] = []
        self.setObjectName("spiffyListarySearch")
        self.setWindowTitle("Search Files & Apps")
        self.setMinimumSize(QSize(640, 300))
        self.resize(760, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        top = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setObjectName("spiffyListarySearchBox")
        self.search.setAccessibleName("Search indexed files and applications")
        self.search.setPlaceholderText("Search files, folders, and apps...")
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        self.kind = QComboBox(self)
        self.kind.setAccessibleName("Result type")
        self.kind.addItem("All", ())
        self.kind.addItem("Apps", ("app", "package"))
        self.kind.addItem("Folders", ("folder",))
        self.kind.addItem("Files", ("file",))
        top.addWidget(self.search, 1)
        top.addWidget(self.kind)
        layout.addLayout(top)

        self.results = QListWidget(self)
        self.results.setObjectName("spiffyListarySearchResults")
        self.results.setAccessibleName("Indexed search results")
        self.results.setUniformItemSizes(True)
        layout.addWidget(self.results, 1)
        self.details = QLabel("Select a result to see indexed details.", self)
        self.details.setObjectName("PanelStatus")
        self.details.setAccessibleName("Selected result details")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.status = QLabel("Type to search your indexed files and applications.", self)
        self.status.setObjectName("PanelStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # Make an unconfigured index actionable in the launcher itself.  A
        # workspace-only catalog can legitimately return a few Spiffy apps
        # without any file roots, which otherwise looks like a complete index.
        self.root_recovery_button = QPushButton(self)
        self.root_recovery_button.setObjectName("spiffyListaryRootRecovery")
        self.root_recovery_button.setProperty("spiffyRole", "primary")
        self.root_recovery_button.hide()
        self._root_recovery_action: str | None = None
        layout.addWidget(self.root_recovery_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.shortcut_hints = QLabel(
            "Up/Down Navigate   Enter Open   Ctrl+1..9 Open",
            self,
        )
        self.shortcut_hints.setObjectName("PanelStatus")
        self.shortcut_hints.setAccessibleName("Listary keyboard shortcuts")
        self.shortcut_hints.setToolTip(
            "Up/Down navigate · Enter open · Ctrl+Enter reveal · Ctrl+Shift+C copy path · Ctrl+1..9 direct open · Esc close"
        )

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.index_button = QPushButton("Index Folder...", self)
        self.index_button.setAccessibleName("Add a folder to the Listary search index")
        self.manage_roots_button = QPushButton("Indexed Folders...", self)
        self.manage_roots_button.setAccessibleName("Manage indexed folders and refresh policies")
        self.file_search_button = QPushButton("File Search", self)
        self.file_search_button.setAccessibleName("Open exact pageable File Search")
        self.file_search_button.setProperty("spiffyRole", "quiet")
        self.open_button = QPushButton("Open", self)
        self.open_button.setProperty("spiffyRole", "primary")
        self.open_button.setEnabled(False)
        self.reveal_button = QPushButton("Reveal", self)
        self.reveal_button.setAccessibleName("Reveal selected result in the file manager")
        self.reveal_button.setToolTip("Reveal selected result (Ctrl+Enter)")
        self.reveal_button.setEnabled(False)
        self.copy_path_button = QPushButton("Copy Path", self)
        self.copy_path_button.setAccessibleName("Copy selected result path")
        self.copy_path_button.setToolTip("Copy selected path (Ctrl+Shift+C)")
        self.copy_path_button.setEnabled(False)
        self.pin_button = QPushButton("Pin / Unpin", self)
        self.pin_button.setAccessibleName("Pin or unpin selected search result")
        self.pin_button.setToolTip("Toggle selected result in zero-query recommendations (Ctrl+P)")
        self.pin_button.setEnabled(False)

        # Keep the incumbent buttons as the state/action owners, but move their
        # visible presentation into the compact Listary-style footer menus.
        for legacy_button in (
            self.index_button,
            self.manage_roots_button,
            self.open_button,
            self.reveal_button,
            self.copy_path_button,
            self.pin_button,
        ):
            legacy_button.setVisible(False)

        self.menu_button = QPushButton("Menu", self)
        self.menu_button.setAccessibleName("Listary menu")
        self.menu_button.setProperty("spiffyRole", "quiet")
        self.menu_menu = QMenu(self.menu_button)
        self.menu_file_search_action = self.menu_menu.addAction("File Search")
        self.menu_menu.addSeparator()
        self.menu_close_action = self.menu_menu.addAction("Close")
        self.menu_button.setMenu(self.menu_menu)

        self.options_button = QPushButton("Options", self)
        self.options_button.setAccessibleName("Listary options")
        self.options_button.setProperty("spiffyRole", "quiet")
        self.options_menu = QMenu(self.options_button)
        self.options_index_action = self.options_menu.addAction("Index Folder...")
        self.options_roots_action = self.options_menu.addAction("Indexed Folders...")
        self.options_filters_action = self.options_menu.addAction("File Search Filters...")
        self.options_menu.addSeparator()
        self.options_appearance_menu = self.options_menu.addMenu("Appearance")
        self.appearance_actions: dict[str, Any] = {}
        for profile_id, label in shared_appearance_profiles():
            action = self.options_appearance_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selected=profile_id: self._set_shared_appearance(selected)
            )
            self.appearance_actions[profile_id] = action
        self.options_menu.aboutToShow.connect(self._sync_appearance_actions)
        self.options_button.setMenu(self.options_menu)

        self.actions_button = QPushButton("Actions", self)
        self.actions_button.setAccessibleName("Actions for selected Listary result")
        self.actions_button.setProperty("spiffyRole", "primary")
        self.actions_menu = QMenu(self.actions_button)
        self.actions_open_action = self.actions_menu.addAction("Open")
        self.actions_reveal_action = self.actions_menu.addAction("Reveal")
        self.actions_copy_action = self.actions_menu.addAction("Copy Path")
        self.actions_menu.addSeparator()
        self.actions_pin_action = self.actions_menu.addAction("Pin / Unpin")
        self.actions_button.setMenu(self.actions_menu)

        footer.addWidget(self.menu_button)
        footer.addWidget(self.shortcut_hints, 1)
        footer.addWidget(self.file_search_button)
        footer.addWidget(self.options_button)
        footer.addWidget(self.actions_button)
        layout.addLayout(footer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_QUICK_SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_search)
        self.search.textChanged.connect(self._schedule_search)
        self.search.returnPressed.connect(self._open_current)
        self.kind.currentIndexChanged.connect(self._schedule_search)
        self.results.currentItemChanged.connect(self._quick_result_changed)
        self.results.itemActivated.connect(lambda _item: self._open_current())
        self.index_button.clicked.connect(self._choose_index_folder)
        self.manage_roots_button.clicked.connect(self._open_root_management)
        self.root_recovery_button.clicked.connect(self._activate_root_recovery)
        self.file_search_button.clicked.connect(self._open_file_search)
        self.copy_path_button.clicked.connect(self._copy_current_path)
        self.pin_button.clicked.connect(self._toggle_current_pin)
        self.reveal_button.clicked.connect(self._reveal_current)
        self.open_button.clicked.connect(self._open_current)
        self.menu_file_search_action.triggered.connect(self._open_file_search)
        self.menu_close_action.triggered.connect(self.reject)
        self.options_index_action.triggered.connect(self._choose_index_folder)
        self.options_roots_action.triggered.connect(self._open_root_management)
        self.options_filters_action.triggered.connect(self._open_filter_management)
        self.actions_open_action.triggered.connect(self._open_current)
        self.actions_reveal_action.triggered.connect(self._reveal_current)
        self.actions_copy_action.triggered.connect(self._copy_current_path)
        self.actions_pin_action.triggered.connect(self._toggle_current_pin)
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self._reveal_current)
            self._result_shortcuts.append(shortcut)
        copy_path_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        copy_path_shortcut.activated.connect(self._copy_current_path)
        pin_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        pin_shortcut.activated.connect(self._toggle_current_pin)
        self._result_shortcuts.extend((copy_path_shortcut, pin_shortcut))
        for index in range(9):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda index=index: self._open_result_row(index))
            self._result_shortcuts.append(shortcut)
        self._sync_appearance_actions()
        self._sync_launcher_footer_actions()
        apply_broadcast_dark(self)

    def _sync_appearance_actions(self) -> None:
        current = shared_appearance_profile_id()
        for profile_id, action in self.appearance_actions.items():
            action.setChecked(profile_id == current)

    def _set_shared_appearance(self, profile_id: str) -> None:
        focus_widget = QApplication.focusWidget()
        try:
            receipt = set_shared_appearance_profile(profile_id)
        except (OSError, ValueError) as exc:
            self.status.setText(f"Could not save appearance: {exc}")
            self._sync_appearance_actions()
            return
        self._sync_appearance_actions()
        label = dict(shared_appearance_profiles()).get(str(receipt.get("profile") or ""), profile_id)
        self.status.setText(f"Appearance: {label}.")
        if focus_widget is not None:
            QTimer.singleShot(
                0,
                lambda widget=focus_widget: widget.setFocus(Qt.FocusReason.OtherFocusReason)
                if widget is not None and widget.isVisible()
                else None,
            )

    def _sync_launcher_footer_actions(self) -> None:
        for action, button in (
            (self.actions_open_action, self.open_button),
            (self.actions_reveal_action, self.reveal_button),
            (self.actions_copy_action, self.copy_path_button),
            (self.actions_pin_action, self.pin_button),
        ):
            action.setText(button.text())
            action.setEnabled(button.isEnabled())
            action.setToolTip(button.toolTip())
        self.options_index_action.setEnabled(self.index_button.isEnabled())

    def _set_root_recovery_action(self, action: str | None) -> None:
        self._root_recovery_action = action if action in {"index", "manage"} else None
        if self._root_recovery_action == "index":
            label = "Index a folder..."
            accessible_name = "Add a folder to search files beyond workspace applications"
        elif self._root_recovery_action == "manage":
            label = "Review indexed folders..."
            accessible_name = "Review disabled or unavailable Listary indexed folders"
        else:
            self.root_recovery_button.hide()
            return
        self.root_recovery_button.setText(label)
        self.root_recovery_button.setAccessibleName(accessible_name)
        self.root_recovery_button.show()

    def _activate_root_recovery(self) -> None:
        if self._root_recovery_action == "index":
            self._choose_index_folder()
        elif self._root_recovery_action == "manage":
            self._open_root_management()

    def _sync_compact_launcher_height(self) -> None:
        """Fit the quick launcher to its bounded result set without dead space."""

        count = self.results.count()
        visible_rows = max(_QUICK_SEARCH_MIN_VISIBLE_ROWS, min(count, _QUICK_SEARCH_RESULT_LIMIT))
        row_height = self.results.sizeHintForRow(0) if count else -1
        if row_height <= 0:
            row_height = max(32, (self.fontMetrics().lineSpacing() * 2) + 6)
        result_height = (visible_rows * row_height) + (self.results.frameWidth() * 2) + 2
        self.results.setFixedHeight(result_height)
        if self.layout() is not None:
            self.layout().activate()
        target_height = max(self.minimumHeight(), self.sizeHint().height())
        self.resize(max(self.width(), 760), target_height)

    def _quick_result_changed(self, _current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if self._file_dialog_cancel_event is not None:
            self._cancel_file_dialog_action()
        if self._file_manager_cancel_event is not None:
            self._cancel_file_manager_action()
        self._sync_result_state()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                return self._move_result_selection(1)
            if key == Qt.Key.Key_Up:
                return self._move_result_selection(-1)
            if key == Qt.Key.Key_PageDown:
                return self._move_result_selection(5)
            if key == Qt.Key.Key_PageUp:
                return self._move_result_selection(-5)
        return super().eventFilter(watched, event)

    def _ensure_file_dialog_adapter(self) -> WindowsFileDialogAdapter | None:
        if self._file_dialog_adapter is not None:
            return self._file_dialog_adapter
        if sys.platform != "win32":
            return None
        try:
            self._file_dialog_adapter = WindowsFileDialogAdapter()
        except WindowsFileDialogError:
            return None
        return self._file_dialog_adapter

    def capture_foreground_file_dialog(self) -> WindowsFileDialogContext | None:
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        adapter = self._ensure_file_dialog_adapter()
        self._file_dialog_context = adapter.capture_foreground() if adapter is not None else None
        if self._file_search_dialog is not None:
            self._file_search_dialog.set_file_dialog_context(adapter, self._file_dialog_context)
        return self._file_dialog_context

    def clear_file_dialog_context(self) -> None:
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._file_dialog_context = None
        if self._file_search_dialog is not None:
            self._file_search_dialog.set_file_dialog_context(self._file_dialog_adapter, None)

    def _cancel_file_dialog_action(self) -> None:
        self._file_dialog_action_generation += 1
        cancel_event, self._file_dialog_cancel_event = self._file_dialog_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()

    def _cancel_file_manager_action(self) -> None:
        self._file_manager_action_generation += 1
        cancel_event, self._file_manager_cancel_event = self._file_manager_cancel_event, None
        if cancel_event is not None:
            cancel_event.set()

    def open_for_query(self, query: str = "") -> None:
        query_text = str(query)
        text_changed = self.search.text() != query_text
        self.search.setText(query_text)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()
        # Invocation supplies a complete query (or the intentional empty-query
        # recommendation request), so there is no typing burst to debounce.
        # Retire any previously actionable rows immediately, then dispatch now.
        if not text_changed:
            self._schedule_search()
        self._debounce.stop()
        self._start_search()

    def _schedule_search(self, *_args: Any) -> None:
        # Query/filter edits immediately retire both visible results and any
        # in-flight request.  Waiting until the debounce fires leaves a window
        # where an old completion can render against new query text and remain
        # actionable.  Search uses its own generation so typing never retires a
        # concurrent folder-index operation.
        self._search_generation += 1
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._cancel_active()
        self.results.clear()
        self.details.setText("Select a result to see indexed details.")
        self._set_root_recovery_action(None)
        self._set_result_buttons_enabled(False)
        self.status.setText("Searching...")
        self._debounce.start()

    def _cancel_active(self) -> None:
        request_id, self._active_request_id = self._active_request_id, None
        if request_id:
            QThreadPool.globalInstance().start(_OneShotWorker(lambda: self.client.cancel(request_id)))

    def _start_search(self) -> None:
        self._cancel_active()
        self._search_generation += 1
        generation = self._search_generation
        request_id = secrets.randbits(63) or 1
        self._active_request_id = request_id
        kinds = tuple(str(value) for value in (self.kind.currentData() or ()))
        self.status.setText("Searching...")
        self._set_result_buttons_enabled(False)
        worker = _SearchWorker(generation, request_id, self.client, self.search.text().strip(), kinds)
        self._workers.add(worker)
        worker.signals.finished.connect(lambda got_generation, page, error, worker=worker: self._search_finished(worker, got_generation, page, error))
        QThreadPool.globalInstance().start(worker)

    def _choose_index_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Index Folder")
        if path:
            self._start_index_folder(path)

    def _open_file_search(self) -> None:
        if self._file_search_dialog is None:
            self._file_search_dialog = ListaryFileSearchDialog(
                self,
                client=self.client,
                file_dialog_adapter=self._file_dialog_adapter,
                file_dialog_context=self._file_dialog_context,
                recommendations_changed=self._file_search_recommendations_changed,
            )
            apply_broadcast_dark(self._file_search_dialog)
        else:
            self._file_search_dialog.set_file_dialog_context(
                self._file_dialog_adapter,
                self._file_dialog_context,
            )
        self._file_search_dialog.open_for_query(self.search.text().strip())

    def _file_search_recommendations_changed(self) -> None:
        if self.search.text().strip():
            return
        self._debounce.stop()
        self._start_search()

    def _open_root_management(self) -> None:
        if self._root_management_dialog is None:
            self._root_management_dialog = ListaryRootManagementDialog(
                self, client=self.client, roots_changed=self._indexed_roots_changed
            )
        self._root_management_dialog.open_manager()

    def _indexed_roots_changed(self) -> None:
        # A committed root mutation invalidates rows from the previous catalog
        # scope. Refresh both open search surfaces after the manager reads back
        # the new root configuration.
        self._schedule_search()
        self._debounce.stop()
        self._start_search()
        if self._file_search_dialog is not None:
            self._file_search_dialog.refresh_after_root_change()

    def _open_filter_management(self) -> None:
        if self._filter_management_dialog is None:
            self._filter_management_dialog = ListaryFilterManagementDialog(
                self,
                client=self.client,
                filters_changed=self._file_search_filters_changed,
            )
        self._filter_management_dialog.open_manager()

    def _file_search_filters_changed(self) -> None:
        if self._file_search_dialog is not None:
            self._file_search_dialog._refresh_saved_filters()

    def _start_index_folder(self, path: str) -> None:
        self._search_generation += 1
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._cancel_active()
        self.results.clear()
        self.details.setText("Select a result to see indexed details.")
        self._set_root_recovery_action(None)
        self._set_result_buttons_enabled(False)
        self._generation += 1
        generation = self._generation
        request_id = secrets.randbits(63) or 1
        self._active_root_request_id = request_id
        self.index_button.setEnabled(False)
        self._sync_launcher_footer_actions()
        self.status.setText(f"Indexing {path}...")
        worker = _RootWorker(generation, request_id, self.client, path)
        self._root_workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, payload, error, worker=worker: self._index_finished(
                worker, got_generation, payload, error
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _index_finished(self, worker: _RootWorker, generation: int, payload: object, error: object) -> None:
        self._root_workers.discard(worker)
        if generation != self._generation:
            return
        self._active_root_request_id = None
        self.index_button.setEnabled(True)
        self._sync_launcher_footer_actions()
        if error is not None:
            self.status.setText(str(error) or "Folder indexing failed.")
            return
        report = payload.get("report", {}) if isinstance(payload, dict) else {}
        discovered = int(report.get("discovered") or 0) if isinstance(report, dict) else 0
        errors = int(report.get("errors") or 0) if isinstance(report, dict) else 0
        self.status.setText(f"Indexed {discovered} items" + (f" with {errors} errors." if errors else "."))
        self._schedule_search()

    def _search_finished(self, worker: _SearchWorker, generation: int, page: object, error: object) -> None:
        self._workers.discard(worker)
        if generation != self._search_generation:
            return
        self._active_request_id = None
        self.results.clear()
        self.details.setText("Select a result to see indexed details.")
        self._set_root_recovery_action(None)
        if error is not None:
            if isinstance(error, ListaryAgentError):
                self.details.setText(
                    f"Search owner unavailable: {error.code}. "
                    "Existing indexed search state was preserved; retry after recovery."
                )
                message = str(error)
            else:
                message = str(error)
            self.status.setText(message or "Indexed search is unavailable.")
            self._sync_compact_launcher_height()
            return
        if not isinstance(page, ListarySearchPage):
            self.status.setText("Indexed search returned an invalid result page.")
            self._sync_compact_launcher_height()
            return
        for result in page.results:
            presentation = _result_presentation(result)
            row = QListWidgetItem(f"{presentation.name}\n{presentation.parent_path}")
            row.setIcon(library_icon(_result_icon_name(result), color=BROADCAST_DARK.text_muted, size=16))
            row.setData(self.RESULT_ROLE, result)
            row.setToolTip(presentation.tooltip)
            self.results.addItem(row)
        if self.results.count():
            self.results.setCurrentRow(0)
            self._last_result_ready_monotonic = time.perf_counter()
        else:
            self._last_result_ready_monotonic = None
        elapsed_ms = page.elapsed_micros / 1000.0
        result_summary = f"{len(page.results)} result{'s' if len(page.results) != 1 else ''}"
        if page.elapsed_micros:
            result_summary += f" in {elapsed_ms:.1f} ms"
        root_state = getattr(worker, "indexed_root_state", None)
        if root_state == (0, 0):
            self._set_root_recovery_action("index")
            self.status.setText(
                result_summary
                + ". No indexed folders yet — use Options > Index Folder... to add one."
            )
        elif root_state is not None and root_state[0] > 0 and root_state[1] == 0:
            self._set_root_recovery_action("manage")
            self.status.setText(
                result_summary
                + ". Indexed folders are disabled or unavailable — use Options > Indexed Folders... to review them."
            )
        else:
            self.status.setText(result_summary)
        self._sync_result_state()
        if not page.results and root_state == (0, 0):
            self.details.setText("File search is not configured yet. Add an indexed folder to search local files.")
        elif not page.results and root_state is not None and root_state[0] > 0 and root_state[1] == 0:
            self.details.setText("No indexed folder is currently available. Review indexed folders to restore file search.")
        self._sync_compact_launcher_height()

    def _current_result(self) -> ListarySearchResult | None:
        row = self.results.currentItem()
        value = row.data(self.RESULT_ROLE) if row is not None else None
        return value if isinstance(value, ListarySearchResult) else None

    def _move_result_selection(self, delta: int) -> bool:
        count = self.results.count()
        if count <= 0:
            return False
        current = self.results.currentRow()
        if current < 0:
            current = 0
        target = max(0, min(count - 1, current + int(delta)))
        self.results.setCurrentRow(target)
        return True

    def _open_result_row(self, row: int) -> bool:
        if row < 0 or row >= self.results.count():
            return False
        self.results.setCurrentRow(row)
        return self._open_current()

    def _set_result_buttons_enabled(self, enabled: bool) -> None:
        self.open_button.setEnabled(enabled)
        self.reveal_button.setEnabled(enabled)
        self.copy_path_button.setEnabled(enabled)
        self.pin_button.setEnabled(enabled and not self._pin_busy)
        self._sync_launcher_footer_actions()

    def _sync_result_state(self) -> None:
        result = self._current_result()
        presentation = _result_presentation(result) if result is not None else None
        enabled = presentation is not None and presentation.action_path is not None
        self._set_result_buttons_enabled(enabled)
        file_manager_busy = self._file_manager_cancel_event is not None
        if file_manager_busy:
            self.open_button.setEnabled(False)
            self.reveal_button.setEnabled(False)
        if self._file_dialog_context is not None:
            supported = result is not None and result.kind in {"file", "folder"}
            self.open_button.setEnabled(enabled and supported)
            if result is not None:
                self.open_button.setText(_native_dialog_action_label(result.kind, self._file_dialog_context.dialog_kind))
        else:
            self.open_button.setText("Open")
        pending_save = _native_save_confirmation_pending(
            self._file_dialog_context, self._last_file_dialog_failure_receipt
        )
        if pending_save:
            self.open_button.setEnabled(False)
            self.open_button.setToolTip("Windows is awaiting a human overwrite decision; do not repeat Save.")
        elif self._file_dialog_context is not None and self._file_dialog_context.dialog_kind == "save":
            self.open_button.setToolTip("Commit Save in Windows; any overwrite confirmation is for you to decide.")
        else:
            self.open_button.setToolTip("Open the selected indexed result.")
        self.open_button.setAccessibleName(self.open_button.text())
        saving = self._file_dialog_context is not None and self._file_dialog_context.dialog_kind == "save"
        self.shortcut_hints.setText(
            "Windows overwrite decision pending · Enter Save disabled" if pending_save
            else "Up/Down Navigate   Enter Save   Ctrl+1..9 Save" if saving
            else "Up/Down Navigate   Enter Open   Ctrl+1..9 Open"
        )
        self.shortcut_hints.setToolTip(
            "Enter commits the native Save dialog. Choosing an existing file may request overwrite confirmation. Esc closes Listary."
            if saving else
            "Up/Down navigate · Enter open · Ctrl+Enter reveal · Ctrl+Shift+C copy path · Ctrl+1..9 direct open · Esc close"
        )
        self.details.setText(_result_details(result) if result is not None else "Select a result to see indexed details.")
        self._sync_launcher_footer_actions()

    def _start_file_dialog_action(self, displayed: ListarySearchResult) -> bool:
        adapter = self._file_dialog_adapter
        context = self._file_dialog_context
        if adapter is None or context is None:
            return False
        if _native_save_confirmation_pending(context, self._last_file_dialog_failure_receipt):
            self.status.setText(
                "Windows Save needs your overwrite decision. Confirm or cancel in Windows; "
                "a second Save will not be sent to this dialog."
            )
            return True
        if displayed.kind not in {"file", "folder"}:
            self.status.setText("Open/Save Quick Switch accepts only indexed files and folders.")
            return True

        self._cancel_file_dialog_action()
        self._file_dialog_action_generation += 1
        generation = self._file_dialog_action_generation
        self._last_file_dialog_receipt = None
        self._last_file_dialog_failure_receipt = None
        self._last_result_to_dialog_ready_ms = None
        cancel_event = threading.Event()
        self._file_dialog_cancel_event = cancel_event
        query_text = self.search.text().strip()
        activation_generation = self._search_generation
        result_ready = self._last_result_ready_monotonic
        self._set_result_buttons_enabled(False)
        self.status.setText(
            (
                "Saving to the selected destination via the captured Save dialog..."
                if context.dialog_kind == "save" else
                f"{'Navigating' if displayed.kind == 'folder' else 'Selecting'} in the captured Open dialog..."
            )
        )
        try:
            adapter.prepare_context_for_action(context)
        except WindowsFileDialogError as exc:
            self._file_dialog_cancel_event = None
            self._last_file_dialog_failure_receipt = _attach_file_dialog_failure_receipt(
                adapter,
                context,
                exc,
                displayed,
                query_text=query_text,
                activation_generation=activation_generation,
                action_generation=generation,
            ).receipt
            self.status.setText(str(exc) or "Open/Save Quick Switch could not re-establish the captured dialog.")
            self._sync_result_state()
            return True
        if result_ready is not None:
            self._last_result_to_dialog_ready_ms = max(
                0.0,
                (time.perf_counter() - result_ready) * 1000.0,
            )

        def perform() -> tuple[ListarySearchResult, WindowsFileDialogReceipt]:
            current_for_receipt = displayed
            target_for_receipt = displayed.path

            def require_current_generation() -> None:
                if cancel_event.is_set():
                    raise WindowsFileDialogError(
                        "cancelled",
                        "The launcher Open/Save action was cancelled before the next native change.",
                    )
                if (
                    generation != self._file_dialog_action_generation
                    or activation_generation != self._search_generation
                ):
                    raise WindowsFileDialogError(
                        "stale_generation",
                        "The launcher result/query generation changed before the Open/Save action could run.",
                    )

            try:
                require_current_generation()
                current, presentation, error = _revalidate_result_for_action(
                    self.client,
                    displayed,
                    refresh_label="Search again before using this result.",
                )
                if current is None or presentation is None:
                    raise WindowsFileDialogError("stale_result", error)
                current_for_receipt = current
                target_path = presentation.action_path
                target_for_receipt = target_path

                def revalidate_target() -> None:
                    require_current_generation()
                    _require_result_target_unchanged(
                        self.client,
                        current,
                        target_path,
                        refresh_label="Search again before using this result.",
                    )

                require_current_generation()
                receipt = adapter.apply_result(
                    context,
                    target_path=target_path,
                    item_kind=current.kind,
                    item_id=current.item_id,
                    root_id=current.root_id,
                    stable_file_id=current.stable_file_id,
                    displayed_path=displayed.path,
                    query_text=query_text,
                    activation_generation=activation_generation,
                    cancel_event=cancel_event,
                    revalidate_target=revalidate_target,
                    accept_open=context.dialog_kind == "open" and current.kind == "file",
                    accept_save=context.dialog_kind == "save" and current.kind in {"file", "folder"},
                    action_generation=generation,
                    result_generation=activation_generation,
                )
                return current, receipt
            except WindowsFileDialogError as exc:
                raise _attach_file_dialog_failure_receipt(
                    adapter,
                    context,
                    exc,
                    current_for_receipt,
                    query_text=query_text,
                    activation_generation=activation_generation,
                    action_generation=generation,
                    target_path=target_for_receipt,
                )

        worker = _ResultActionWorker(generation, perform)
        self._file_dialog_action_workers.add(worker)
        worker.signals.finished.connect(
            lambda got_generation, payload, error, owned=worker: self._file_dialog_action_finished(
                owned,
                got_generation,
                payload,
                error,
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _file_dialog_action_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        payload: object,
        error: object,
    ) -> None:
        self._file_dialog_action_workers.discard(worker)
        if generation != self._file_dialog_action_generation:
            if (
                self._file_dialog_cancel_event is None
                and isinstance(error, WindowsFileDialogError)
                and error.ambiguous
                and isinstance(error.receipt, WindowsFileDialogFailureReceipt)
            ):
                self._last_file_dialog_failure_receipt = error.receipt
            return
        if isinstance(error, WindowsFileDialogError) and isinstance(
            error.receipt,
            WindowsFileDialogFailureReceipt,
        ):
            self._last_file_dialog_failure_receipt = error.receipt
        if (
            isinstance(payload, tuple)
            and len(payload) == 2
            and isinstance(payload[1], WindowsFileDialogReceipt)
        ):
            self._last_file_dialog_receipt = payload[1]
        self._file_dialog_cancel_event = None
        if error is not None:
            message = str(error) or "Open/Save Quick Switch failed."
            if isinstance(error, WindowsFileDialogError) and error.ambiguous:
                message = f"Dialog state may have changed; no retry was attempted. {message}"
            self.status.setText(message)
            self._sync_result_state()
            return
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or not isinstance(payload[0], ListarySearchResult)
            or not isinstance(payload[1], WindowsFileDialogReceipt)
        ):
            self.status.setText("Open/Save Quick Switch returned an invalid receipt.")
            self._sync_result_state()
            return
        current, receipt = payload
        save_context = self._file_dialog_context is not None and self._file_dialog_context.dialog_kind == "save"
        if receipt.dialog_kind == "save" or save_context:
            if not receipt.accepted_by_dialog:
                self.status.setText("The Save dialog state was verified, but the dialog did not confirm Save acceptance.")
                self._sync_result_state()
                return
            if not _native_save_receipt_proves_delivery(
                receipt,
                current,
                self._file_dialog_context,
                generation,
                self._generation,
            ):
                self.status.setText(
                    "Save did not provide matching, stable destination identity and SHA-256 proof; "
                    "no successful Save was claimed."
                )
                self._sync_result_state()
                return
        elif current.kind == "file" and not receipt.accepted_by_dialog:
            self.status.setText(
                f"The {receipt.dialog_kind.title()} dialog state was verified, but the dialog did not confirm "
                f"{receipt.dialog_kind.title()} acceptance."
            )
            self._sync_result_state()
            return
        _start_tracked_one_shot(
            self._activation_workers,
            lambda item_id=current.item_id: self.client.record_activation(item_id, source="quick_switch"),
        )
        self.status.setText(
            f"Verified {receipt.action} in the {receipt.dialog_kind.title()} dialog "
            f"in {receipt.action_latency_ms:.1f} ms."
        )
        self.accept()

    def _open_current(self) -> bool:
        result = self._current_result()
        if result is None:
            return False
        if self._file_dialog_context is not None:
            return self._start_file_dialog_action(result)
        if sys.platform == "win32" and result.kind == "folder":
            return _start_listary_file_manager_action(
                self,
                result,
                operation="open_folder",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
                activation_source="launcher",
                close_on_success=True,
            )
        if sys.platform == "win32" and _workspace_application_contract(result) is not None:
            return _start_listary_file_manager_action(
                self,
                result,
                operation="launch_workspace_app",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
                activation_source="launcher",
                close_on_success=True,
            )
        if sys.platform == "win32" and _is_verifiable_windows_shortcut_result(result):
            return _start_listary_file_manager_action(
                self,
                result,
                operation="launch_shortcut",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
                activation_source="launcher",
                close_on_success=True,
            )
        if sys.platform == "win32" and result.kind == "file" and not _is_windows_shortcut_file_result(result):
            return _start_listary_file_manager_action(
                self,
                result,
                operation="open_file",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
                activation_source="launcher",
                close_on_success=True,
            )
        if sys.platform == "win32" and _is_direct_windows_executable_app_result(result):
            return _start_listary_file_manager_action(
                self,
                result,
                operation="launch_app",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
                activation_source="launcher",
                close_on_success=True,
            )
        current, presentation, error = _revalidate_result_for_action(
            self.client,
            result,
            refresh_label="Search again before using this result.",
        )
        if current is None or presentation is None:
            self.status.setText(error)
            return False
        if not open_path(presentation.action_path):
            self.status.setText(f"Could not open {presentation.name}.")
            return False
        if sys.platform == "win32":
            self.status.setText(
                f"Opened {presentation.name} through Windows without a verifiable destination receipt; "
                "activation history was not changed."
            )
            self.accept()
            return True
        _start_tracked_one_shot(
            self._activation_workers,
            lambda item_id=current.item_id: self.client.record_activation(item_id),
        )
        self.accept()
        return True

    def _reveal_current(self) -> bool:
        result = self._current_result()
        if result is None:
            return False
        if self._file_dialog_context is not None:
            self.status.setText("Reveal is disabled while Open/Save Quick Switch owns a native dialog.")
            return False
        if sys.platform == "win32" and result.kind in {"file", "folder"}:
            return _start_listary_file_manager_action(
                self,
                result,
                operation="reveal",
                result_generation=self._search_generation,
                current_generation=lambda: self._search_generation,
                refresh_label="Search again before using this result.",
                result_ready_monotonic=self._last_result_ready_monotonic,
            )
        current, presentation, error = _revalidate_result_for_action(
            self.client,
            result,
            refresh_label="Search again before using this result.",
        )
        if current is None or presentation is None:
            self.status.setText(error)
            return False
        if not reveal_in_file_manager(presentation.action_path):
            self.status.setText(f"Could not reveal {presentation.name}.")
            return False
        self.status.setText(f"Revealed {presentation.name}.")
        return True

    def _copy_current_path(self) -> bool:
        result = self._current_result()
        if result is None:
            return False
        current, presentation, error = _revalidate_result_for_action(
            self.client,
            result,
            refresh_label="Search again before using this result.",
        )
        if current is None or presentation is None:
            self.status.setText(error)
            return False
        QApplication.clipboard().setText(presentation.action_path)
        self.status.setText(f"Copied path for {presentation.name}.")
        return True

    def _toggle_current_pin(self) -> bool:
        result = self._current_result()
        if result is None or self._pin_busy:
            return False
        presentation = _result_presentation(result)
        if presentation.action_path is None:
            self.status.setText(presentation.action_error)
            return False
        self._pin_generation += 1
        generation = self._pin_generation
        self._pin_busy = True
        self._set_result_buttons_enabled(True)
        self.status.setText(f"Updating pin for {presentation.name}...")
        worker = _ResultActionWorker(generation, lambda: self.client.toggle_pin(result.item_id))
        self._pin_workers.add(worker)
        worker.signals.finished.connect(
            lambda finished_generation, pinned, error, owned=worker, name=presentation.name: self._pin_finished(
                owned, finished_generation, pinned, error, name
            )
        )
        QThreadPool.globalInstance().start(worker)
        return True

    def _pin_finished(
        self,
        worker: _ResultActionWorker,
        generation: int,
        pinned: object,
        error: object,
        name: str,
    ) -> None:
        self._pin_workers.discard(worker)
        if generation != self._pin_generation:
            return
        self._pin_busy = False
        if error is not None:
            self.status.setText(f"Could not update pin for {name}: {error}")
            self._sync_result_state()
            return
        if not isinstance(pinned, bool):
            self.status.setText(f"Could not update pin for {name}: invalid response")
            self._sync_result_state()
            return
        self.status.setText(f"{'Pinned' if pinned else 'Unpinned'} {name}.")
        if not self.search.text().strip():
            self._start_search()
        else:
            self._sync_result_state()

    def reject(self) -> None:
        self._generation += 1
        self._search_generation += 1
        self._pin_generation += 1
        self._cancel_file_dialog_action()
        self._cancel_file_manager_action()
        self._debounce.stop()
        self._cancel_active()
        if self._file_search_dialog is not None:
            self._file_search_dialog.reject()
        if self._root_management_dialog is not None:
            self._root_management_dialog.reject()
        if self._filter_management_dialog is not None:
            self._filter_management_dialog.reject()
        request_id, self._active_root_request_id = self._active_root_request_id, None
        if request_id:
            QThreadPool.globalInstance().start(_OneShotWorker(lambda: self.client.cancel(request_id)))
        super().reject()

    def shutdown(self, timeout_ms: int = 2_000) -> bool:
        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        file_search_shutdown_ok = True
        if self._file_search_dialog is not None:
            file_search_shutdown_ok = self._file_search_dialog.shutdown(
                max(0, int((deadline - time.monotonic()) * 1000))
            )
            self._file_search_dialog.close()
            self._file_search_dialog.deleteLater()
            self._file_search_dialog = None
        root_management_shutdown_ok = True
        if self._root_management_dialog is not None:
            root_management_shutdown_ok = self._root_management_dialog.shutdown(
                max(0, int((deadline - time.monotonic()) * 1000))
            )
            self._root_management_dialog.close()
            self._root_management_dialog.deleteLater()
            self._root_management_dialog = None
        filter_management_shutdown_ok = True
        if self._filter_management_dialog is not None:
            filter_management_shutdown_ok = self._filter_management_dialog.shutdown(
                max(0, int((deadline - time.monotonic()) * 1000))
            )
            self._filter_management_dialog.close()
            self._filter_management_dialog.deleteLater()
            self._filter_management_dialog = None
        self.reject()
        activation_shutdown_ok = _wait_tracked_one_shots(
            self._activation_workers,
            max(0, int((deadline - time.monotonic()) * 1000)),
        )
        pin_shutdown_ok = True
        for worker in tuple(self._pin_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                pin_shutdown_ok = False
                break
        self._pin_workers.intersection_update(worker for worker in self._pin_workers if not worker.done.is_set())
        file_dialog_action_shutdown_ok = True
        for worker in tuple(self._file_dialog_action_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                file_dialog_action_shutdown_ok = False
                break
        self._file_dialog_action_workers.intersection_update(
            worker for worker in self._file_dialog_action_workers if not worker.done.is_set()
        )
        file_manager_action_shutdown_ok = True
        for worker in tuple(self._file_manager_action_workers):
            if not worker.done.wait(max(0.0, deadline - time.monotonic())):
                file_manager_action_shutdown_ok = False
                break
        self._file_manager_action_workers.intersection_update(
            worker for worker in self._file_manager_action_workers if not worker.done.is_set()
        )
        if self._owns_client:
            self.client.close()
        return (
            file_search_shutdown_ok
            and root_management_shutdown_ok
            and filter_management_shutdown_ok
            and activation_shutdown_ok
            and pin_shutdown_ok
            and file_dialog_action_shutdown_ok
            and file_manager_action_shutdown_ok
        )


class ListarySearchController(QObject):
    """Expose one Listary-backed file/app search entry to every Spiffy palette."""

    def __init__(self, owner: QWidget, *, preview_broker: Any = _AUTO_PREVIEW_BROKER) -> None:
        super().__init__(owner)
        self._owner = owner
        self._preview_broker = preview_broker
        self._dialog: ListarySearchDialog | None = None
        self._file_dialog: ListaryFileSearchDialog | None = None

    def palette_items(self) -> tuple[CommandPaletteItem, ...]:
        return (
            CommandPaletteItem(
                "listary:search",
                "Search Files & Apps...",
                self.open,
                category="Files & Apps",
                keywords=("launcher", "file", "folder", "application", "app", "indexed", "listary"),
            ),
            CommandPaletteItem(
                "listary:file-search",
                "Deep File Search...",
                self.open_file_search,
                category="Files & Apps",
                keywords=("deep", "exact", "file search", "browse", "sort", "filter", "indexed"),
            ),
        )

    def open(self) -> None:
        if self._dialog is None:
            parent = self._owner.parentWidget() or self._owner
            self._dialog = ListarySearchDialog(parent)
        self._dialog.open_for_query()

    def open_file_search(self) -> None:
        if self._file_dialog is None:
            parent = self._owner.parentWidget() or self._owner
            self._file_dialog = ListaryFileSearchDialog(
                parent,
                preview_broker=self._preview_broker,
            )
            apply_broadcast_dark(self._file_dialog)
        self._file_dialog.open_for_query()

    def shutdown(self) -> None:
        if self._dialog is not None:
            self._dialog.shutdown()
            self._dialog.close()
            self._dialog.deleteLater()
            self._dialog = None
        if self._file_dialog is not None:
            self._file_dialog.shutdown()
            self._file_dialog.close()
            self._file_dialog.deleteLater()
            self._file_dialog = None


def create_standalone_dialog(initial_query: str = "") -> tuple[QApplication, ListarySearchDialog]:
    """Create the Windows-friendly standalone search surface using the resident owner."""

    app = QApplication.instance() or QApplication(sys.argv[:1])
    dialog = ListarySearchDialog()
    apply_broadcast_dark(dialog)
    dialog.open_for_query(initial_query)
    return app, dialog


def _show_standalone_dialog(
    dialog: ListarySearchDialog,
    query: str = "",
    *,
    capture_file_dialog: bool = False,
) -> None:
    if capture_file_dialog:
        dialog.capture_foreground_file_dialog()
    else:
        dialog.clear_file_dialog_context()
    dialog.open_for_query(query)


def _indexed_root_state(client: ListaryAgentClient) -> tuple[int, int] | None:
    try:
        roots = client.list_roots()
    except Exception:
        return None
    configured = tuple(
        root
        for root in roots
        if isinstance(root, dict) and bool(str(root.get("path") or "").strip())
    )
    return len(configured), sum(
        1 for root in configured
        if bool(root.get("enabled", True))
        and str(root.get("availability") or "available").casefold() == "available"
    )


def _explicit_root_setup_needed(client: ListaryAgentClient) -> bool:
    """Offer the incumbent root manager when explicit setup has no usable root."""

    state = _indexed_root_state(client)
    return state is not None and state[1] == 0


def _global_hotkey_activation_ready(client: ListaryAgentClient) -> bool:
    """Leave the user's Listary shortcut alone until an indexed root is usable."""

    try:
        roots = client.list_roots()
    except Exception:
        return False
    return any(
        isinstance(root, dict)
        and bool(str(root.get("path") or "").strip())
        and bool(root.get("enabled", True))
        and str(root.get("availability") or "").casefold() == "available"
        and root.get("last_reconciled_unix_ms") is not None
        for root in roots
    )


def _resident_server_name(state_dir: str | Path | None = None) -> str:
    path = Path(state_dir).expanduser() if state_dir is not None else listary_state_dir()
    normalized = os.path.normcase(os.path.abspath(str(path)))
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return f"spiffy-listary-ui-v1-{digest}"


def _ui_endpoint_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir is not None else listary_state_dir()
    return root / _UI_ENDPOINT_FILE


def _read_ui_endpoint(state_dir: str | Path | None = None) -> tuple[str, str, int] | None:
    path = _ui_endpoint_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    parts = raw.split("|")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    server_name, token, process_id_text = parts[1], parts[2], parts[3]
    if not server_name.startswith("spiffy-listary-ui-v1-") or len(token) < 32:
        return None
    try:
        process_id = int(process_id_text)
    except ValueError:
        return None
    if process_id <= 0:
        return None
    return server_name, token, process_id


def _write_ui_endpoint(state_dir: Path, server_name: str, token: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _UI_ENDPOINT_FILE
    staged = state_dir / f".{_UI_ENDPOINT_FILE}.{uuid4().hex}.tmp"
    staged.write_text(f"v1|{server_name}|{token}|{os.getpid()}\n", encoding="utf-8")
    os.replace(staged, path)
    return path


def _remove_ui_endpoint_if_owned(path: Path, server_name: str, token: str) -> None:
    current = _read_ui_endpoint(path.parent)
    if current is None or current[0] != server_name or current[1] != token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _send_resident_command(
    server_name: str,
    action: str,
    *,
    token: str = "",
    query: str = "",
    setup_if_rootless: bool = False,
    timeout_ms: int = 250,
) -> bool:
    if action not in {"show", "ping", "quit"}:
        raise ValueError(f"unsupported resident action: {action}")
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if not socket.waitForConnected(max(25, int(timeout_ms))):
        socket.abort()
        return False
    request = {"schema_version": 1, "token": str(token), "action": action, "query": str(query)}
    if action == "show" and setup_if_rootless:
        # An older running resident still understands show and ignores this flag.
        request["setup_if_rootless"] = True
    payload = json.dumps(
        request,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    queued = int(socket.write(payload))
    if queued != len(payload):
        socket.abort()
        return False
    socket.flush()
    if socket.bytesToWrite() > 0:
        socket.waitForBytesWritten(max(25, int(timeout_ms)))
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(max(25, int(timeout_ms)))
    return True


def _notify_existing_resident(
    server_name: str,
    query: str,
    *,
    token: str = "",
    show: bool = True,
    setup_if_rootless: bool = False,
    timeout_ms: int = 250,
) -> bool:
    return _send_resident_command(
        server_name,
        "show" if show else "ping",
        token=token,
        query=query,
        setup_if_rootless=setup_if_rootless if show else False,
        timeout_ms=timeout_ms,
    )


def _bind_resident_server(server: QLocalServer, dialog: ListarySearchDialog, token: str) -> None:
    buffers: dict[QLocalSocket, bytearray] = {}

    def cleanup(local_socket: QLocalSocket) -> None:
        buffers.pop(local_socket, None)
        local_socket.deleteLater()

    def consume(local_socket: QLocalSocket) -> None:
        buffer = buffers.setdefault(local_socket, bytearray())
        buffer.extend(bytes(local_socket.readAll()))
        if len(buffer) > 8192:
            local_socket.abort()
            cleanup(local_socket)
            return
        newline = buffer.find(b"\n")
        if newline < 0:
            return
        raw = bytes(buffer[:newline])
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if (
            isinstance(payload, dict)
            and int(payload.get("schema_version") or 0) == 1
            and secrets.compare_digest(str(payload.get("token") or ""), token)
        ):
            action = str(payload.get("action") or "")
            if action == "show":
                _show_standalone_dialog(dialog, str(payload.get("query") or ""))
                if payload.get("setup_if_rootless") is True and _explicit_root_setup_needed(dialog.client):
                    QTimer.singleShot(0, dialog._open_root_management)
            elif action == "quit":
                app = QApplication.instance()
                if app is not None:
                    QTimer.singleShot(0, app.quit)
        local_socket.disconnectFromServer()

    def accept_connections() -> None:
        while server.hasPendingConnections():
            local_socket = server.nextPendingConnection()
            if local_socket is None:
                continue
            buffers[local_socket] = bytearray()
            local_socket.readyRead.connect(lambda local_socket=local_socket: consume(local_socket))
            local_socket.disconnected.connect(lambda local_socket=local_socket: cleanup(local_socket))
            if local_socket.bytesAvailable():
                consume(local_socket)

    server.newConnection.connect(accept_connections)


def run_standalone(
    initial_query: str = "",
    *,
    enable_windows_hotkey: bool = False,
    start_hidden: bool = False,
    setup_if_rootless: bool = False,
) -> int:
    """Run Search Files & Apps as a small standalone desktop launcher."""

    if QApplication.instance() is not None:
        raise RuntimeError("run_standalone must own the QApplication event loop")
    if start_hidden and not enable_windows_hotkey:
        raise ValueError("start_hidden requires resident hotkey mode")
    app = QApplication(sys.argv[:1])
    resident_server = None
    resident_server_name = None
    resident_token = None
    resident_endpoint_path = None
    if enable_windows_hotkey and sys.platform == "win32":
        state_dir = listary_state_dir()
        resident_server_name = _resident_server_name(state_dir)
        existing_endpoint = _read_ui_endpoint(state_dir)
        if existing_endpoint is not None:
            existing_name, existing_token, _existing_pid = existing_endpoint
            if _notify_existing_resident(
                existing_name,
                initial_query,
                token=existing_token,
                show=not start_hidden,
                setup_if_rootless=setup_if_rootless,
            ):
                return 0
        elif _notify_existing_resident(resident_server_name, initial_query, show=not start_hidden):
            # Backward compatibility for a pre-descriptor resident shell.
            return 0
        QLocalServer.removeServer(resident_server_name)
        resident_server = QLocalServer(app)
        if not resident_server.listen(resident_server_name):
            retry_endpoint = _read_ui_endpoint(state_dir)
            retry_name = retry_endpoint[0] if retry_endpoint is not None else resident_server_name
            retry_token = retry_endpoint[1] if retry_endpoint is not None else ""
            if _notify_existing_resident(
                retry_name,
                initial_query,
                token=retry_token,
                show=not start_hidden,
                setup_if_rootless=setup_if_rootless,
            ):
                return 0
            raise RuntimeError(f"Could not own the resident Listary UI endpoint: {resident_server.errorString()}")
        resident_token = secrets.token_hex(24)
        resident_endpoint_path = _write_ui_endpoint(state_dir, resident_server_name, resident_token)

    dialog = ListarySearchDialog()
    apply_broadcast_dark(dialog)
    explicit_root_setup = setup_if_rootless and _explicit_root_setup_needed(dialog.client)
    if explicit_root_setup:
        dialog.open_for_query(initial_query)
        QTimer.singleShot(0, dialog._open_root_management)
    elif not start_hidden:
        dialog.open_for_query(initial_query)
    if resident_server is not None:
        _bind_resident_server(resident_server, dialog, resident_token or "")
    hook = None
    tray = None
    tray_menu = None
    resident_hotkey = False
    if enable_windows_hotkey:
        if sys.platform != "win32":
            dialog.status.setText("Global double-Control activation is currently available only on Windows.")
        else:
            from .listary_windows_hotkey import WindowsDoubleControlHook, installed_listary_owns_double_control

            hook = WindowsDoubleControlHook(dialog)

            def activate_from_global_hotkey() -> None:
                if installed_listary_owns_double_control():
                    dialog.status.setText(
                        "Installed Listary owns double-Control. "
                        "Use Ctrl+Alt+Space or the Spiffy Listary tray menu."
                    )
                    return
                if _global_hotkey_activation_ready(dialog.client):
                    _show_standalone_dialog(dialog, capture_file_dialog=True)

            def activate_from_explicit_fallback() -> None:
                if _global_hotkey_activation_ready(dialog.client):
                    _show_standalone_dialog(dialog, capture_file_dialog=True)

            hook.activated.connect(activate_from_global_hotkey)
            hook.fallback_activated.connect(activate_from_explicit_fallback)
            try:
                hook.start()
                resident_hotkey = True
            except RuntimeError as exc:
                hook = None
                dialog.status.setText(f"Double-Control activation is unavailable: {exc}")

    if resident_hotkey:
        app.setQuitOnLastWindowClosed(False)
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView), app)
            tray.setToolTip(
                ("Spiffy Listary — installed Listary keeps double-Control"
                 if installed_listary_owns_double_control() else "Spiffy Listary — double-Control")
                + ("; Spiffy fallback Ctrl+Alt+Space" if hook is not None and hook.fallback_registered else "")
            )
            tray_menu = QMenu()
            open_action = tray_menu.addAction("Open Search")
            quit_action = tray_menu.addAction("Quit")
            open_action.triggered.connect(lambda: _show_standalone_dialog(dialog))
            quit_action.triggered.connect(app.quit)
            tray.setContextMenu(tray_menu)

            def tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
                if reason in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                ):
                    _show_standalone_dialog(dialog)

            tray.activated.connect(tray_activated)
            tray.show()
    else:
        app.lastWindowClosed.connect(app.quit)
    try:
        return int(app.exec())
    finally:
        if hook is not None:
            hook.stop()
        if tray is not None:
            tray.hide()
        dialog.shutdown()
        if resident_server is not None:
            resident_server.close()
        if resident_endpoint_path is not None and resident_server_name is not None and resident_token is not None:
            _remove_ui_endpoint_if_owned(resident_endpoint_path, resident_server_name, resident_token)
        if resident_server_name is not None:
            QLocalServer.removeServer(resident_server_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Spiffy Listary file/app search surface.")
    parser.add_argument(
        "--resident-hotkey",
        action="store_true",
        help="Keep the Windows shell resident and open search on double-Control.",
    )
    parser.add_argument(
        "--start-hidden",
        action="store_true",
        help="Start the resident Windows hotkey shell without showing search immediately.",
    )
    parser.add_argument(
        "--quit-resident",
        action="store_true",
        help="Request a clean shutdown of the resident shell for this Listary profile.",
    )
    parser.add_argument(
        "--setup-if-rootless",
        action="store_true",
        help="On explicit setup, open Indexed Folders when no enabled filesystem root can search.",
    )
    parser.add_argument("query", nargs="*", help="Optional initial search text.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.quit_resident:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        endpoint = _read_ui_endpoint()
        if endpoint is None:
            return 1
        ok = _send_resident_command(endpoint[0], "quit", token=endpoint[1], timeout_ms=500)
        app.processEvents()
        return 0 if ok else 1
    return run_standalone(
        " ".join(args.query).strip(),
        enable_windows_hotkey=bool(args.resident_hotkey),
        start_hidden=bool(args.start_hidden),
        setup_if_rootless=bool(args.setup_if_rootless),
    )


if __name__ == "__main__":
    raise SystemExit(main())

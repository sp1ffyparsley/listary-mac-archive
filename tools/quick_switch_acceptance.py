#!/usr/bin/env python3
"""One-command real-Mac acceptance harness for native Open/Save Quick Switch.

This is an acceptance surface, not a second Quick Switch implementation. Folder
recommendations come only from the existing listary-ffi/listary-core search ABI;
the macOS helper is limited to Accessibility permission/panel detection,
navigation, Finder context, and verified dialog readback.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zlib
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
AX_HELPER = REPO_ROOT / "macos" / "QuickSwitchAcceptance" / "ax_file_dialog.js"
FIXTURE_SOURCE = REPO_ROOT / "macos" / "FileDialogFixture" / "main.m"
CORE_HEADER = REPO_ROOT / "rust" / "listary-ffi" / "include" / "listary_core.h"
SCHEMA = "listary_quick_switch_acceptance_v1"
FILE_CONTEXT_SCHEMA = "listary_file_context_v1"
FILE_ACTION_INTENT_SCHEMA = "listary_file_action_intent_v1"
DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "ListaryMac" / "state"
DEFAULT_LIMIT = 10
DEFAULT_CANDIDATE_LIMIT = 4096
EXPECTED_CORE_API_VERSION = 9
FILE_CONTEXT_SOURCES = {"finder", "open_dialog", "save_dialog"}
FILE_ACTION_KINDS = {"navigate", "select", "open", "reveal"}
CAPABILITY_STATES = {"unknown", "unavailable", "denied", "granted", "degraded"}
PUBLIC_CORE_SYMBOLS = (
    "listary_core_api_version",
    "listary_core_open",
    "listary_core_close",
    "listary_core_search",
    "listary_core_file_search",
    "listary_core_facet_counts",
    "listary_core_cancel",
    "listary_core_add_root",
    "listary_core_list_roots",
    "listary_core_put_root",
    "listary_core_remove_root",
    "listary_core_list_filters",
    "listary_core_put_filter",
    "listary_core_remove_filter",
    "listary_core_list_commands",
    "listary_core_put_command",
    "listary_core_remove_command",
    "listary_core_expand_command",
    "listary_core_list_actions",
    "listary_core_put_action",
    "listary_core_remove_action",
    "listary_core_expand_action",
    "listary_core_get_favorites",
    "listary_core_put_favorites",
    "listary_core_toggle_pin",
    "listary_core_resolve_favorites",
    "listary_core_list_web_search_providers",
    "listary_core_replace_web_search_providers",
    "listary_core_list_priority_rules",
    "listary_core_replace_priority_rules",
    "listary_core_resolve_web_search",
    "listary_core_reconcile_root",
    "listary_core_reconcile_paths",
    "listary_core_record_activation",
    "listary_core_recommendations",
    "listary_core_hide_recommendation",
    "listary_core_exclude_path",
    "listary_core_get_item",
    "listary_core_get_diagnostics",
    "listary_core_flush",
    "listary_core_rebuild_all",
    "listary_buffer_free",
)


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ListaryBuffer(ctypes.Structure):
    _fields_ = [("ptr", ctypes.POINTER(ctypes.c_uint8)), ("len", ctypes.c_size_t)]


def header_api_version(header_path: Path = CORE_HEADER) -> int:
    try:
        text = header_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError("core_header_unreadable", f"Cannot read canonical C header {header_path}: {exc}") from exc
    match = re.search(
        r"^\s*#define\s+LISTARY_CORE_API_VERSION\s+(\d+)[uUlL]*\s*$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        raise HarnessError(
            "core_header_version_missing",
            f"Canonical C header does not declare LISTARY_CORE_API_VERSION: {header_path}",
        )
    return int(match.group(1))


def validate_loaded_core_library(
    library: Any,
    *,
    library_path: Path,
    header_path: Path = CORE_HEADER,
) -> dict[str, Any]:
    header_version = header_api_version(header_path)
    if header_version != EXPECTED_CORE_API_VERSION:
        raise HarnessError(
            "core_header_version_mismatch",
            f"Canonical C header ABI is {header_version}; expected {EXPECTED_CORE_API_VERSION}.",
        )

    missing: list[str] = []
    for symbol in PUBLIC_CORE_SYMBOLS:
        try:
            getattr(library, symbol)
        except AttributeError:
            missing.append(symbol)
    if missing:
        raise HarnessError(
            "core_symbol_missing",
            f"Core library is missing ABI v{EXPECTED_CORE_API_VERSION} symbol(s): {', '.join(missing)}",
        )

    version_fn = library.listary_core_api_version
    version_fn.argtypes = []
    version_fn.restype = ctypes.c_uint32
    actual_version = int(version_fn())
    if actual_version != EXPECTED_CORE_API_VERSION:
        raise HarnessError(
            "core_api_version_mismatch",
            f"Core library ABI is {actual_version}; expected {EXPECTED_CORE_API_VERSION}. "
            f"Header: {header_path}. Library: {library_path}.",
        )
    return {
        "expected_api_version": EXPECTED_CORE_API_VERSION,
        "header_api_version": header_version,
        "library_api_version": actual_version,
        "symbol_count": len(PUBLIC_CORE_SYMBOLS),
        "header": str(header_path),
        "library": str(library_path),
    }


def load_and_validate_core_library(
    library_path: Path,
    *,
    loader: Any = ctypes.CDLL,
    header_path: Path = CORE_HEADER,
) -> tuple[Any, dict[str, Any]]:
    try:
        library = loader(str(library_path))
    except OSError as exc:
        raise HarnessError(
            "core_library_load_failed",
            f"Could not load Listary ABI library {library_path}: {exc}",
        ) from exc
    details = validate_loaded_core_library(
        library,
        library_path=library_path,
        header_path=header_path,
    )
    return library, details


def bridge_preflight_run(library_path: Path) -> dict[str, Any]:
    _, details = load_and_validate_core_library(library_path)
    return {
        "schema": SCHEMA,
        "mode": "bridge_preflight",
        "host_platform": platform.system().lower(),
        "real_mac_executed": False,
        "status": "success",
        "outcome": f"abi_v{EXPECTED_CORE_API_VERSION}_header_symbols_version_green",
        "core_owner": "listary-ffi -> listary-core",
        **details,
    }


def canonical_quick_switch_request(
    *,
    root_ids: Sequence[str] = (),
    limit: int = DEFAULT_LIMIT,
    now_unix_ms: int | None = None,
    timezone_offset_minutes: int | None = None,
) -> dict[str, Any]:
    parsed_root_ids = [str(uuid.UUID(value)) for value in root_ids]
    if limit <= 0 or limit > 200:
        raise HarnessError("invalid_limit", "Quick Switch result limit must be between 1 and 200.")
    if now_unix_ms is None:
        now_unix_ms = int(time.time() * 1000)
    if timezone_offset_minutes is None:
        offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
        timezone_offset_minutes = int(offset.total_seconds() // 60)
    return {
        "query": "",
        "limit": limit,
        "candidate_limit": DEFAULT_CANDIDATE_LIMIT,
        "include_unavailable": False,
        "root_ids": parsed_root_ids,
        "kinds": ["folder"],
        "context": {"current_folder": None, "contextual": False},
        "now_unix_ms": int(now_unix_ms),
        "timezone_offset_minutes": int(timezone_offset_minutes),
    }


def validate_core_candidates(
    response: Mapping[str, Any], *, root_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    results = response.get("results")
    if not isinstance(results, list):
        raise HarnessError("core_response_invalid", "Core search response has no results array.")
    allowed_roots = {str(uuid.UUID(value)) for value in root_ids}
    candidates: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if not isinstance(result, Mapping) or not isinstance(result.get("item"), Mapping):
            raise HarnessError("core_response_invalid", f"Core result {index} has no item object.")
        item = dict(result["item"])
        if item.get("kind") != "folder":
            raise HarnessError(
                "core_candidate_not_folder",
                f"Shared core returned non-folder result at position {index}.",
            )
        if item.get("available") is not True:
            raise HarnessError(
                "core_candidate_unavailable",
                f"Shared core returned unavailable folder at position {index}.",
            )
        root_id = str(item.get("root_id") or "")
        if allowed_roots and root_id not in allowed_roots:
            raise HarnessError(
                "core_candidate_wrong_root",
                f"Shared core returned folder outside the requested root scope at position {index}.",
            )
        path = str(item.get("path") or "").strip()
        if not path:
            raise HarnessError("core_candidate_missing_path", f"Core result {index} has no path.")
        candidates.append(
            {
                "index": index,
                "item_id": item.get("id"),
                "root_id": root_id,
                "path": path,
                "name": str(item.get("name") or ""),
                "kind": "folder",
                "available": True,
                "score": result.get("score"),
            }
        )
    return candidates


def select_core_candidate(candidates: Sequence[Mapping[str, Any]], index: int) -> dict[str, Any]:
    if not candidates:
        raise HarnessError("no_folder_candidate", "Shared core returned no eligible Quick Switch folder.")
    if index < 0 or index >= len(candidates):
        raise HarnessError(
            "candidate_index_out_of_range",
            f"Candidate index {index} is outside 0..{len(candidates) - 1}.",
        )
    # Preserve shared-core ordering exactly. Do not sort or re-rank here.
    return dict(candidates[index])


def normalized_path(value: str) -> str:
    expanded = os.path.expanduser(str(value or ""))
    normalized = os.path.normpath(os.path.abspath(expanded))
    return unicodedata.normalize("NFC", normalized)


def paths_match(expected: str, actual: str) -> bool:
    return normalized_path(expected) == normalized_path(actual)


def canonical_file_context(
    *,
    source: str,
    context_token: str,
    generation: int,
    capability: str,
    current_directory: str | None,
    selected_path: str | None = None,
    query_text: str = "",
    dialog_kind: str | None = None,
    process_id: int | None = None,
    window_id: str | None = None,
) -> dict[str, Any]:
    """Build the platform-neutral Finder/Open-Save context consumed by the Mac shell."""

    if source not in FILE_CONTEXT_SOURCES:
        raise HarnessError("context_source_invalid", f"Unsupported file context source: {source}")
    token = str(context_token or "").strip()
    if not token:
        raise HarnessError("context_token_missing", "File context requires a stable context token.")
    if int(generation) <= 0:
        raise HarnessError("context_generation_invalid", "File context generation must be positive.")
    if capability not in CAPABILITY_STATES:
        raise HarnessError("context_capability_invalid", f"Unsupported capability state: {capability}")
    if source == "open_dialog" and dialog_kind not in {None, "open"}:
        raise HarnessError("context_dialog_kind_invalid", "Open-dialog context cannot claim a non-open dialog kind.")
    if source == "save_dialog" and dialog_kind not in {None, "save"}:
        raise HarnessError("context_dialog_kind_invalid", "Save-dialog context cannot claim a non-save dialog kind.")
    return {
        "schema": FILE_CONTEXT_SCHEMA,
        "source": source,
        "context_token": token,
        "generation": int(generation),
        "capability": capability,
        "current_directory": str(current_directory) if current_directory else None,
        "selected_path": str(selected_path) if selected_path else None,
        "query_text": str(query_text),
        "dialog_kind": dialog_kind,
        "process_id": int(process_id) if process_id is not None else None,
        "window_id": str(window_id) if window_id is not None else None,
    }


def next_context_generation(
    previous: Mapping[str, Any] | None,
    *,
    next_context_token: str,
) -> int:
    """Keep one generation for a live context; advance when its stable identity changes."""

    if previous is None:
        return 1
    previous_generation = int(previous.get("generation") or 0)
    previous_token = str(previous.get("context_token") or "")
    if previous_generation <= 0:
        raise HarnessError("context_generation_invalid", "Previous file context has no positive generation.")
    return previous_generation if previous_token == next_context_token else previous_generation + 1


def context_from_panel(
    panel: Mapping[str, Any],
    *,
    generation: int,
    query_text: str = "",
) -> dict[str, Any]:
    kind = str(panel.get("kind") or panel.get("dialog_kind") or "unknown")
    if kind not in {"open", "save"}:
        raise HarnessError("context_dialog_kind_invalid", f"Unsupported dialog kind: {kind}")
    token = str(panel.get("context_token") or panel.get("panel_token") or "")
    return canonical_file_context(
        source=f"{kind}_dialog",
        context_token=token,
        generation=generation,
        capability="granted",
        current_directory=str(panel.get("current_directory") or panel.get("current_folder") or "") or None,
        selected_path=str(panel.get("selected_path") or "") or None,
        query_text=query_text,
        dialog_kind=kind,
        process_id=int(panel["pid"]) if panel.get("pid") is not None else None,
        window_id=str(panel.get("window_number") or "") or None,
    )


def context_from_finder(
    finder: Mapping[str, Any],
    *,
    generation: int,
    query_text: str = "",
) -> dict[str, Any]:
    token = str(finder.get("context_token") or "")
    if not token:
        pid = finder.get("process_id")
        window = finder.get("window_number")
        if pid is not None and window not in {None, ""}:
            token = f"finder:{int(pid)}:{window}"
    return canonical_file_context(
        source="finder",
        context_token=token,
        generation=generation,
        capability="granted",
        current_directory=str(finder.get("current_directory") or finder.get("current_folder") or "") or None,
        selected_path=str(finder.get("selected_path") or "") or None,
        query_text=query_text,
        process_id=int(finder["process_id"]) if finder.get("process_id") is not None else None,
        window_id=str(finder.get("window_number") or "") or None,
    )


def canonical_result_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    item = value.get("item") if isinstance(value.get("item"), Mapping) else value
    try:
        item_id = int(item.get("id") if item.get("id") is not None else item.get("item_id"))
        root_id = str(uuid.UUID(str(item.get("root_id") or "")))
    except (TypeError, ValueError) as exc:
        raise HarnessError("result_identity_invalid", "Result identity requires a valid item_id and root_id.") from exc
    path = str(item.get("path") or "")
    kind = str(item.get("kind") or "")
    if item_id <= 0 or not path or kind not in {"file", "folder", "app", "package"}:
        raise HarnessError("result_identity_invalid", "Result identity is missing a stable item/path/kind tuple.")
    return {"item_id": item_id, "root_id": root_id, "path": path, "kind": kind}


def canonical_action_intent(
    action: str,
    *,
    context: Mapping[str, Any],
    activation_generation: int,
    target_path: str,
    query_text: str,
    result_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if action not in FILE_ACTION_KINDS:
        raise HarnessError("action_kind_invalid", f"Unsupported file action: {action}")
    if context.get("schema") != FILE_CONTEXT_SCHEMA:
        raise HarnessError("context_schema_invalid", "Action intent requires a canonical file context.")
    if int(activation_generation) <= 0:
        raise HarnessError("activation_generation_invalid", "Action activation generation must be positive.")
    target = str(target_path or "")
    if not target:
        raise HarnessError("action_target_missing", "Action intent requires a target path.")
    return {
        "schema": FILE_ACTION_INTENT_SCHEMA,
        "action": action,
        "context_token": str(context.get("context_token") or ""),
        "context_generation": int(context.get("generation") or 0),
        "activation_generation": int(activation_generation),
        "query_text": str(query_text),
        "target_path": target,
        "result_identity": dict(result_identity) if result_identity is not None else None,
    }


def validate_action_intent(
    intent: Mapping[str, Any],
    *,
    current_context: Mapping[str, Any],
    current_activation_generation: int,
    live_item: Mapping[str, Any] | None = None,
    target_accessible: bool | None = None,
) -> None:
    """Fail closed before a native adapter performs a Finder/Open-Save side effect."""

    if intent.get("schema") != FILE_ACTION_INTENT_SCHEMA:
        raise HarnessError("action_schema_invalid", "File action intent schema is invalid.")
    if intent.get("action") not in FILE_ACTION_KINDS:
        raise HarnessError("action_kind_invalid", "File action intent kind is invalid.")
    if current_context.get("schema") != FILE_CONTEXT_SCHEMA:
        raise HarnessError("context_schema_invalid", "Current file context schema is invalid.")
    if current_context.get("capability") not in {"granted", "degraded"}:
        raise HarnessError("context_capability_unavailable", "Current file context is not actionable.")
    if (
        str(intent.get("context_token") or "") != str(current_context.get("context_token") or "")
        or int(intent.get("context_generation") or 0) != int(current_context.get("generation") or 0)
    ):
        raise HarnessError("stale_context", "File action targets a closed or replaced context.")
    if int(intent.get("activation_generation") or 0) != int(current_activation_generation):
        raise HarnessError("stale_activation", "File action came from a superseded query/result generation.")
    if str(intent.get("query_text") or "") != str(current_context.get("query_text") or ""):
        raise HarnessError("stale_activation", "File action query text no longer matches the live context.")
    if target_accessible is False:
        raise HarnessError("target_inaccessible", "File action target is missing or inaccessible.")

    expected_identity = intent.get("result_identity")
    if expected_identity is None:
        return
    if live_item is None:
        raise HarnessError("result_revalidation_required", "Indexed result must be revalidated before action.")
    if live_item.get("available") is not True:
        raise HarnessError("result_unavailable", "Indexed result is no longer available.")
    live_identity = canonical_result_identity(live_item)
    expected = dict(expected_identity)
    if live_identity != expected:
        raise HarnessError("stale_result_identity", "Indexed result identity changed before action.")
    if not paths_match(str(intent.get("target_path") or ""), live_identity["path"]):
        raise HarnessError("stale_result_path", "Indexed result path changed before action.")


def require_accessible_directory(path_text: str) -> Path:
    target = Path(path_text).expanduser()
    try:
        if not target.exists():
            raise HarnessError("target_folder_missing", f"Selected target folder does not exist: {target}")
        if not target.is_dir():
            raise HarnessError("target_not_folder", f"Selected target is not a folder: {target}")
        if not os.access(target, os.R_OK | os.X_OK):
            raise HarnessError("target_folder_inaccessible", f"Selected target folder is not accessible: {target}")
    except PermissionError as exc:
        raise HarnessError("target_folder_inaccessible", f"Selected target folder is not accessible: {target}") from exc
    except OSError as exc:
        raise HarnessError("target_folder_inaccessible", f"Selected target folder cannot be inspected: {target}: {exc}") from exc
    return target


def panel_readback_matches(expected: str, panel: Mapping[str, Any]) -> bool:
    values: list[str] = []
    current = str(panel.get("current_folder") or "")
    if current:
        values.append(current)
    evidence = panel.get("readback_evidence")
    if isinstance(evidence, list):
        for row in evidence:
            if isinstance(row, Mapping) and row.get("path"):
                values.append(str(row["path"]))
    expected_normalized = normalized_path(expected)
    for value in values:
        actual = normalized_path(value)
        if actual == expected_normalized:
            return True
        if normalized_path(str(Path(actual).parent)) == expected_normalized:
            # Save/Open panels may expose the selected/prospective file URL;
            # its parent is the actual current directory.
            return True
    return False


def assess_navigation(
    *,
    panel_before: Mapping[str, Any],
    navigation: Mapping[str, Any],
    panel_after: Mapping[str, Any],
    expected_path: str,
    fixture_state: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    before_token = str(panel_before.get("panel_token") or "")
    after_token = str(panel_after.get("panel_token") or "")
    nav_status = str(navigation.get("status") or "")
    if nav_status == "panel_closed" or not after_token:
        return "failed", "panel_closed"
    if nav_status == "panel_replaced" or before_token != after_token:
        return "failed", "panel_replaced"
    if nav_status == "readback_mismatch":
        return "failed", "readback_mismatch"
    if nav_status not in {"navigated", "already_at_target"}:
        return "failed", "navigation_failed"
    if not panel_readback_matches(expected_path, panel_after):
        return "failed", "readback_mismatch"
    if fixture_state is not None:
        fixture_path = str(fixture_state.get("current_directory") or "")
        if not fixture_path or not paths_match(expected_path, fixture_path):
            return "failed", "fixture_readback_mismatch"
    return "success", "success"


class CoreAbiClient:
    def __init__(self, library_path: Path, state_dir: Path) -> None:
        self.library_path = library_path
        self.state_dir = state_dir
        self.lib, self.bridge_preflight = load_and_validate_core_library(library_path)
        self.handle = ctypes.c_void_p()
        self._bind()
        self._open()

    def _bind(self) -> None:
        self.lib.listary_core_open.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_open.restype = ctypes.c_int32
        self.lib.listary_core_close.argtypes = [ctypes.c_void_p]
        self.lib.listary_core_close.restype = None
        self.lib.listary_core_search.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_search.restype = ctypes.c_int32
        self.lib.listary_core_file_search.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_file_search.restype = ctypes.c_int32
        self.lib.listary_core_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.lib.listary_core_cancel.restype = ctypes.c_int32
        self.lib.listary_core_add_root.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_add_root.restype = ctypes.c_int32
        self.lib.listary_core_list_roots.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_list_roots.restype = ctypes.c_int32
        self.lib.listary_core_remove_root.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_remove_root.restype = ctypes.c_int32
        self.lib.listary_core_reconcile_root.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_reconcile_root.restype = ctypes.c_int32
        self.lib.listary_core_reconcile_paths.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_reconcile_paths.restype = ctypes.c_int32
        self.lib.listary_core_record_activation.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_record_activation.restype = ctypes.c_int32
        self.lib.listary_core_get_item.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_get_item.restype = ctypes.c_int32
        self.lib.listary_core_toggle_pin.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_toggle_pin.restype = ctypes.c_int32
        self.lib.listary_core_get_diagnostics.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ListaryBuffer),
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_get_diagnostics.restype = ctypes.c_int32
        self.lib.listary_core_flush.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ListaryBuffer),
        ]
        self.lib.listary_core_flush.restype = ctypes.c_int32
        self.lib.listary_buffer_free.argtypes = [ListaryBuffer]
        self.lib.listary_buffer_free.restype = None

    @staticmethod
    def _payload_bytes(value: Mapping[str, Any]) -> tuple[bytes, Any]:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        array = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        return payload, array

    def _take_buffer(self, buffer: ListaryBuffer) -> bytes:
        if not buffer.ptr or not buffer.len:
            return b""
        try:
            return ctypes.string_at(buffer.ptr, buffer.len)
        finally:
            self.lib.listary_buffer_free(buffer)

    def _error_text(self, buffer: ListaryBuffer) -> str:
        raw = self._take_buffer(buffer)
        return raw.decode("utf-8", errors="replace") if raw else ""

    def _json_object(self, buffer: ListaryBuffer, *, code: str) -> dict[str, Any]:
        raw = self._take_buffer(buffer)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError(code, f"Core returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise HarnessError(code, "Core response is not an object.")
        return value

    def _json_array(self, buffer: ListaryBuffer, *, code: str) -> list[Any]:
        raw = self._take_buffer(buffer)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError(code, f"Core returned invalid JSON: {exc}") from exc
        if not isinstance(value, list):
            raise HarnessError(code, "Core response is not an array.")
        return value

    def _open(self) -> None:
        config = {
            "state_dir": str(self.state_dir),
            "log_max_bytes": 10 * 1024 * 1024,
            "log_generations": 3,
        }
        payload, array = self._payload_bytes(config)
        error = ListaryBuffer()
        status = self.lib.listary_core_open(
            array,
            len(payload),
            ctypes.byref(self.handle),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_open_failed",
                self._error_text(error) or f"listary_core_open returned status {status}.",
            )

    def search(self, request: Mapping[str, Any], *, request_id: int = 1) -> dict[str, Any]:
        payload, array = self._payload_bytes(request)
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_search(
            self.handle,
            request_id,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_search_failed",
                self._error_text(error) or f"listary_core_search returned status {status}.",
            )
        return self._json_object(response, code="core_response_invalid")

    def file_search(
        self,
        request: Mapping[str, Any],
        *,
        request_id: int,
    ) -> dict[str, Any]:
        payload, array = self._payload_bytes(request)
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_file_search(
            self.handle,
            request_id,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            code = {
                3: "core_file_search_cancelled",
                8: "core_file_search_stale_catalog",
            }.get(int(status), "core_file_search_failed")
            raise HarnessError(
                code,
                self._error_text(error)
                or f"listary_core_file_search returned status {status}.",
            )
        return self._json_object(response, code="core_file_search_invalid")

    def cancel_status(self, request_id: int) -> int:
        status = int(self.lib.listary_core_cancel(self.handle, int(request_id)))
        if status not in {0, 4}:
            raise HarnessError(
                "core_cancel_failed",
                f"listary_core_cancel returned unexpected status {status} for request {request_id}.",
            )
        return status

    def get_item(self, item_id: int) -> dict[str, Any]:
        payload, array = self._payload_bytes({"item_id": int(item_id)})
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_get_item(
            self.handle,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_item_not_found" if int(status) == 4 else "core_get_item_failed",
                self._error_text(error) or f"listary_core_get_item returned status {status}.",
            )
        return self._json_object(response, code="core_get_item_invalid")

    def toggle_pin(self, item_id: int) -> dict[str, Any]:
        payload, array = self._payload_bytes({"item_id": int(item_id)})
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_toggle_pin(
            self.handle,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_toggle_pin_failed",
                self._error_text(error) or f"listary_core_toggle_pin returned status {status}.",
            )
        value = self._json_object(response, code="core_toggle_pin_invalid")
        if int(value.get("item_id", -1)) != int(item_id) or not isinstance(value.get("pinned"), bool):
            raise HarnessError(
                "core_toggle_pin_invalid",
                "Core pin response did not bind the requested item id and resulting pin state.",
            )
        return value

    def add_root(self, path: Path, *, kind: str = "local") -> str:
        payload, array = self._payload_bytes({"path": str(path), "kind": kind})
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_add_root(
            self.handle,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_add_root_failed",
                self._error_text(error) or f"listary_core_add_root returned status {status}.",
            )
        value = self._json_object(response, code="core_add_root_invalid")
        try:
            return str(uuid.UUID(str(value.get("root_id") or "")))
        except ValueError as exc:
            raise HarnessError("core_add_root_invalid", "Core add-root response has no valid root_id.") from exc

    def list_roots(self) -> list[dict[str, Any]]:
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_list_roots(
            self.handle,
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_list_roots_failed",
                self._error_text(error) or f"listary_core_list_roots returned status {status}.",
            )
        rows = self._json_array(response, code="core_list_roots_invalid")
        if not all(isinstance(row, dict) for row in rows):
            raise HarnessError("core_list_roots_invalid", "Core root list contains a non-object row.")
        return [dict(row) for row in rows]

    def remove_root(self, root_id: str) -> None:
        payload, array = self._payload_bytes({"root_id": str(uuid.UUID(root_id))})
        error = ListaryBuffer()
        status = self.lib.listary_core_remove_root(
            self.handle,
            array,
            len(payload),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_remove_root_failed",
                self._error_text(error) or f"listary_core_remove_root returned status {status}.",
            )

    def reconcile_root(self, root_id: str, *, request_id: int = 2) -> dict[str, Any]:
        root_bytes = str(uuid.UUID(root_id)).encode("utf-8")
        root_array = (ctypes.c_uint8 * len(root_bytes)).from_buffer_copy(root_bytes)
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_reconcile_root(
            self.handle,
            request_id,
            root_array,
            len(root_bytes),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_reconcile_failed",
                self._error_text(error)
                or f"listary_core_reconcile_root returned status {status}.",
            )
        return self._json_object(response, code="core_reconcile_invalid")

    def reconcile_paths(
        self,
        root_id: str,
        paths: Sequence[Path],
        *,
        request_id: int,
        expected_catalog_generation: int | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "root_id": str(uuid.UUID(root_id)),
            "paths": [str(path) for path in paths],
        }
        if expected_catalog_generation is not None:
            request["expected_catalog_generation"] = int(expected_catalog_generation)
        payload, array = self._payload_bytes(request)
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_reconcile_paths(
            self.handle,
            request_id,
            array,
            len(payload),
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            code = {
                3: "core_reconcile_paths_cancelled",
                8: "core_reconcile_paths_stale_catalog",
            }.get(int(status), "core_reconcile_paths_failed")
            raise HarnessError(
                code,
                self._error_text(error)
                or f"listary_core_reconcile_paths returned status {status}.",
            )
        return self._json_object(response, code="core_reconcile_paths_invalid")

    def record_activation(
        self,
        item_id: int,
        *,
        now_unix_ms: int,
        source: str = "quick_switch",
    ) -> None:
        payload, array = self._payload_bytes(
            {"item_id": int(item_id), "now_unix_ms": int(now_unix_ms), "source": source}
        )
        error = ListaryBuffer()
        status = self.lib.listary_core_record_activation(
            self.handle,
            array,
            len(payload),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_activation_failed",
                self._error_text(error)
                or f"listary_core_record_activation returned status {status}.",
            )

    def diagnostics(self) -> dict[str, Any]:
        response = ListaryBuffer()
        error = ListaryBuffer()
        status = self.lib.listary_core_get_diagnostics(
            self.handle,
            ctypes.byref(response),
            ctypes.byref(error),
        )
        if status != 0:
            raise HarnessError(
                "core_diagnostics_failed",
                self._error_text(error)
                or f"listary_core_get_diagnostics returned status {status}.",
            )
        return self._json_object(response, code="core_diagnostics_invalid")

    def flush(self) -> None:
        error = ListaryBuffer()
        status = self.lib.listary_core_flush(self.handle, ctypes.byref(error))
        if status != 0:
            raise HarnessError(
                "core_flush_failed",
                self._error_text(error) or f"listary_core_flush returned status {status}.",
            )

    def close(self) -> None:
        if self.handle.value:
            self.lib.listary_core_close(self.handle)
            self.handle = ctypes.c_void_p()

    def __enter__(self) -> "CoreAbiClient":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


class AxHelperClient:
    def __init__(self, helper_path: Path = AX_HELPER) -> None:
        self.helper_path = helper_path

    def call(self, action: str, *args: str) -> dict[str, Any]:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
                str(self.helper_path),
                action,
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode != 0:
            raise HarnessError(
                "ax_helper_failed",
                (completed.stderr or completed.stdout or "osascript failed").strip(),
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError("ax_helper_invalid_json", f"AX helper returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise HarnessError("ax_helper_invalid_json", "AX helper response is not an object.")
        return value

    def probe(self) -> dict[str, Any]:
        return self.call("probe")

    def navigate(self, panel_token: str, target: str) -> dict[str, Any]:
        return self.call("navigate", panel_token, target)

    def finder_context(self) -> dict[str, Any]:
        return self.call("finder-context")


class FileDialogFixture:
    def __init__(self, mode: str, initial_directory: Path) -> None:
        self.mode = mode
        self.initial_directory = initial_directory
        self.temp = tempfile.TemporaryDirectory(prefix="listary-file-dialog-fixture-")
        root = Path(self.temp.name)
        self.binary = root / "FileDialogFixture"
        self.status_path = root / "status.json"
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        compile_result = subprocess.run(
            [
                "/usr/bin/xcrun",
                "clang",
                "-fobjc-arc",
                "-fblocks",
                "-framework",
                "AppKit",
                "-framework",
                "Foundation",
                str(FIXTURE_SOURCE),
                "-o",
                str(self.binary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if compile_result.returncode != 0:
            raise HarnessError(
                "fixture_build_failed",
                (compile_result.stderr or compile_result.stdout or "fixture compile failed").strip(),
            )
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "--mode",
                self.mode,
                "--initial",
                str(self.initial_directory),
                "--status",
                str(self.status_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            state = self.read_status()
            if state.get("visible") is True:
                return
            if self.process.poll() is not None:
                error = self.process.stderr.read().strip() if self.process.stderr else ""
                raise HarnessError("fixture_exited", error or "FileDialogFixture exited before showing a panel.")
            time.sleep(0.05)
        raise HarnessError("fixture_timeout", "FileDialogFixture did not show a panel within 8 seconds.")

    def read_status(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            return {}
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.temp.cleanup()

    def __enter__(self) -> "FileDialogFixture":
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def library_filename() -> str:
    if sys.platform == "darwin":
        return "liblistary_ffi.dylib"
    if os.name == "nt":
        return "listary_ffi.dll"
    return "liblistary_ffi.so"


def cargo_build_environment(
    base_env: Mapping[str, str] | None = None,
    *,
    system: str | None = None,
    llvm_bin: Path | None = None,
) -> dict[str, str]:
    """Return a build environment without requiring hidden per-shell LLVM state."""

    env = dict(base_env or os.environ)
    current_system = (system or platform.system()).casefold()
    if current_system != "windows":
        return env
    candidate = Path(llvm_bin or r"C:\Program Files\LLVM\bin")
    if not (candidate / "libclang.dll").is_file():
        return env
    if not env.get("LIBCLANG_PATH"):
        env["LIBCLANG_PATH"] = str(candidate)
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    if not any(Path(part) == candidate for part in path_parts):
        env["PATH"] = str(candidate) + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    return env


def resolve_core_library(explicit: Path | None, *, build_if_missing: bool) -> Path:
    path = explicit.expanduser().resolve() if explicit else REPO_ROOT / "target" / "release" / library_filename()
    if path.is_file():
        return path
    if not build_if_missing:
        raise HarnessError("core_library_missing", f"Listary FFI library is missing: {path}")
    completed = subprocess.run(
        ["cargo", "build", "-p", "listary-ffi", "--release"],
        cwd=REPO_ROOT,
        env=cargo_build_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0 or not path.is_file():
        raise HarnessError(
            "core_build_failed",
            (completed.stderr or completed.stdout or f"Cargo did not produce {path}").strip(),
        )
    return path


def _search_request(
    query: str,
    *,
    root_ids: Sequence[str],
    kinds: Sequence[str],
    now_unix_ms: int,
) -> dict[str, Any]:
    return {
        "query": query,
        "limit": 20,
        "candidate_limit": DEFAULT_CANDIDATE_LIMIT,
        "include_unavailable": False,
        "root_ids": [str(uuid.UUID(value)) for value in root_ids],
        "kinds": list(kinds),
        "context": {"current_folder": None, "contextual": False},
        "now_unix_ms": int(now_unix_ms),
        "timezone_offset_minutes": 0,
    }


def _file_search_request(
    query: str,
    *,
    root_ids: Sequence[str],
    now_unix_ms: int,
    offset: int = 0,
    limit: int = 100,
    sort_field: str = "relevance",
    sort_direction: str = "descending",
    expected_catalog_generation: int | None = None,
) -> dict[str, Any]:
    return {
        "query": query,
        "include_unavailable": False,
        "root_ids": [str(uuid.UUID(value)) for value in root_ids],
        "categories": [],
        "modified_time": None,
        "now_unix_ms": int(now_unix_ms),
        "timezone_offset_minutes": 0,
        "offset": int(offset),
        "limit": int(limit),
        "sort_field": sort_field,
        "sort_direction": sort_direction,
        "expected_catalog_generation": expected_catalog_generation,
    }


def _find_result_path(response: Mapping[str, Any], expected_path: Path) -> dict[str, Any]:
    expected = normalized_path(str(expected_path))
    results = response.get("results")
    if not isinstance(results, list):
        raise HarnessError("core_response_invalid", "Core search response has no results array.")
    for row in results:
        if not isinstance(row, Mapping) or not isinstance(row.get("item"), Mapping):
            continue
        item = dict(row["item"])
        if paths_match(expected, str(item.get("path") or "")):
            return {"item": item, "score": row.get("score")}
    raise HarnessError("core_expected_result_missing", f"Core did not return expected path: {expected}")


def core_smoke_run(library_path: Path) -> dict[str, Any]:
    """Exercise the real C ABI through index/search/reopen/cache-recovery on safe fixtures."""

    started = time.monotonic()
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "core_smoke",
        "host_platform": platform.system().lower(),
        "real_mac_executed": False,
        "status": "failed",
        "core_owner": "listary-ffi -> listary-core -> RocksDB",
        "temporary_fixture_only": True,
        "no_parallel_index_or_ranker": True,
        "core_library": str(library_path),
    }
    _, bridge_preflight = load_and_validate_core_library(library_path)
    receipt["bridge_preflight"] = bridge_preflight
    with tempfile.TemporaryDirectory(prefix="listary-core-smoke-") as temp_name:
        temp_root = Path(temp_name)
        state_dir = temp_root / "state"
        indexed_root = temp_root / "indexed"
        alpha = indexed_root / "ProjectAlpha"
        beta = indexed_root / "ProjectBeta"
        unicode_folder = indexed_root / "Résumé βeta"
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)
        unicode_folder.mkdir(parents=True)
        (alpha / "Scene.mov").write_bytes(b"scene")
        (beta / "Notes.txt").write_text("notes", encoding="utf-8")
        report_alpha = unicode_folder / "Report Alpha.txt"
        report_beta = unicode_folder / "Report Beta.txt"
        report_gamma = unicode_folder / "Report Gamma.txt"
        report_alpha.write_text("alpha", encoding="utf-8")
        report_beta.write_text("beta", encoding="utf-8")
        report_gamma.write_text("gamma", encoding="utf-8")
        outside = temp_root / "outside"
        outside.mkdir()
        outside_secret = outside / "SHOULD_NOT_LEAK.txt"
        outside_secret.write_text("outside", encoding="utf-8")
        symlink_supported = False
        link_path = indexed_root / "linked outside"
        try:
            os.symlink(outside, link_path, target_is_directory=True)
            symlink_supported = True
        except (NotImplementedError, OSError):
            pass
        now_ms = 1_800_000_000_000

        with CoreAbiClient(library_path, state_dir) as core:
            root_id = core.add_root(indexed_root)
            reconcile = core.reconcile_root(root_id, request_id=10)
            launcher_started = time.perf_counter_ns()
            unicode_launcher = core.search(
                _search_request(
                    "Résumé βeta",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms,
                ),
                request_id=11,
            )
            launcher_wall_ms = (time.perf_counter_ns() - launcher_started) / 1_000_000.0
            unicode_result = _find_result_path(unicode_launcher, unicode_folder)

            file_search_started = time.perf_counter_ns()
            first_file_page = core.file_search(
                _file_search_request(
                    "Report",
                    root_ids=[root_id],
                    now_unix_ms=now_ms,
                    offset=0,
                    limit=2,
                    sort_field="name",
                    sort_direction="ascending",
                ),
                request_id=12,
            )
            file_search_wall_ms = (time.perf_counter_ns() - file_search_started) / 1_000_000.0
            generation = int(first_file_page.get("catalog_generation") or 0)
            if generation <= 0 or first_file_page.get("has_more") is not True:
                raise HarnessError(
                    "core_file_search_paging_invalid",
                    "File Search first page did not return a live generation with a continuation page.",
                )
            second_file_page = core.file_search(
                _file_search_request(
                    "Report",
                    root_ids=[root_id],
                    now_unix_ms=now_ms,
                    offset=2,
                    limit=2,
                    sort_field="name",
                    sort_direction="ascending",
                    expected_catalog_generation=generation,
                ),
                request_id=13,
            )
            combined_file_rows = [
                *list(first_file_page.get("results") or []),
                *list(second_file_page.get("results") or []),
            ]
            combined_file_response = {"results": combined_file_rows}
            alpha_file_result = _find_result_path(combined_file_response, report_alpha)
            alpha_file_id = int(alpha_file_result["item"]["id"])
            live_alpha_item = core.get_item(alpha_file_id)
            if not paths_match(str(report_alpha), str(live_alpha_item.get("path") or "")):
                raise HarnessError(
                    "core_get_item_mismatch",
                    "Pre-action get_item revalidation returned a different path.",
                )
            core.record_activation(
                alpha_file_id,
                now_unix_ms=now_ms + 50,
                source="file_search",
            )
            if core.cancel_status(13) != 4:
                raise HarnessError(
                    "core_cancel_lifecycle_invalid",
                    "Completed File Search request remained active after response delivery.",
                )

            report_delta = unicode_folder / "Report Delta.txt"
            report_delta.write_text("delta", encoding="utf-8")
            core.reconcile_root(root_id, request_id=14)
            stale_generation_rejected = False
            try:
                core.file_search(
                    _file_search_request(
                        "Report",
                        root_ids=[root_id],
                        now_unix_ms=now_ms + 1,
                        offset=2,
                        limit=2,
                        sort_field="name",
                        sort_direction="ascending",
                        expected_catalog_generation=generation,
                    ),
                    request_id=15,
                )
            except HarnessError as exc:
                if exc.code != "core_file_search_stale_catalog":
                    raise
                stale_generation_rejected = True
            if not stale_generation_rejected:
                raise HarnessError(
                    "core_file_search_stale_catalog_not_rejected",
                    "File Search continuation accepted a stale catalog generation.",
                )

            report_alpha.unlink()
            core.reconcile_root(root_id, request_id=16)
            deleted_item_rejected = False
            try:
                core.get_item(alpha_file_id)
            except HarnessError as exc:
                if exc.code != "core_item_not_found":
                    raise
                deleted_item_rejected = True
            if not deleted_item_rejected:
                raise HarnessError(
                    "core_deleted_item_still_actionable",
                    "Deleted File Search result still revalidated as actionable.",
                )

            symlink_target_leaked = False
            if symlink_supported:
                symlink_search = core.search(
                    _search_request(
                        "SHOULD_NOT_LEAK",
                        root_ids=[root_id],
                        kinds=["file"],
                        now_unix_ms=now_ms + 2,
                    ),
                    request_id=17,
                )
                symlink_target_leaked = bool(symlink_search.get("results"))
                if symlink_target_leaked:
                    raise HarnessError(
                        "core_symlink_traversal_leak",
                        "Default crawl followed a symlink outside the indexed root.",
                    )

            exact = core.search(
                _search_request(
                    "ProjectAlpha",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms,
                ),
                request_id=18,
            )
            alpha_result = _find_result_path(exact, alpha)
            alpha_item_id = int(alpha_result["item"]["id"])
            core.record_activation(alpha_item_id, now_unix_ms=now_ms + 100, source="quick_switch")
            quick_response = core.search(
                canonical_quick_switch_request(
                    root_ids=[root_id],
                    limit=10,
                    now_unix_ms=now_ms + 200,
                    timezone_offset_minutes=0,
                ),
                request_id=19,
            )
            quick_candidates = validate_core_candidates(quick_response, root_ids=[root_id])
            selected = select_core_candidate(quick_candidates, 0)
            if not paths_match(str(alpha), str(selected["path"])):
                raise HarnessError(
                    "quick_switch_history_not_ranked",
                    "Activated ProjectAlpha was not the first zero-query Quick Switch folder.",
                )
            beta_response = core.search(
                _search_request(
                    "ProjectBeta",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms + 210,
                ),
                request_id=22,
            )
            beta_result = _find_result_path(beta_response, beta)
            beta_item_id = int(beta_result["item"]["id"])
            pin_result = core.toggle_pin(beta_item_id)
            if pin_result.get("pinned") is not True:
                raise HarnessError(
                    "core_pin_not_enabled",
                    "First-class pin toggle did not report the indexed ProjectBeta folder as pinned.",
                )
            pinned_quick = core.search(
                canonical_quick_switch_request(
                    root_ids=[root_id],
                    limit=10,
                    now_unix_ms=now_ms + 220,
                    timezone_offset_minutes=0,
                ),
                request_id=23,
            )
            pinned_candidates = validate_core_candidates(pinned_quick, root_ids=[root_id])
            pinned_selected = select_core_candidate(pinned_candidates, 0)
            if not paths_match(str(beta), str(pinned_selected["path"])):
                raise HarnessError(
                    "core_pin_not_ranked",
                    "Pinned ProjectBeta was not the first zero-query Quick Switch folder.",
                )
            diagnostics_before = core.diagnostics()
            core.flush()

        snapshot = state_dir / "cache" / "search-cache.bin"
        if not snapshot.is_file() or snapshot.stat().st_size <= 40:
            raise HarnessError("core_snapshot_missing", "Flush did not publish a usable derived cache snapshot.")

        with CoreAbiClient(library_path, state_dir) as reopened:
            reopened_exact = reopened.search(
                _search_request(
                    "ProjectAlpha",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms + 300,
                ),
                request_id=20,
            )
            _find_result_path(reopened_exact, alpha)
            reopened_quick = reopened.search(
                canonical_quick_switch_request(
                    root_ids=[root_id],
                    limit=10,
                    now_unix_ms=now_ms + 400,
                    timezone_offset_minutes=0,
                ),
                request_id=21,
            )
            reopened_candidates = validate_core_candidates(reopened_quick, root_ids=[root_id])
            reopened_pinned = select_core_candidate(reopened_candidates, 0)
            if not paths_match(str(beta), str(reopened_pinned["path"])):
                raise HarnessError(
                    "core_pin_not_persistent",
                    "First-class pin state did not survive a clean reopen.",
                )

        hostile = bytearray(snapshot.read_bytes())
        hostile[32:40] = (2**64 - 1).to_bytes(8, "little")
        hostile[28:32] = (zlib.crc32(hostile[32:]) & 0xFFFF_FFFF).to_bytes(4, "little")
        snapshot.write_bytes(hostile)

        with CoreAbiClient(library_path, state_dir) as recovered:
            recovered_exact = recovered.search(
                _search_request(
                    "ProjectAlpha",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms + 500,
                ),
                request_id=30,
            )
            _find_result_path(recovered_exact, alpha)
            recovered_quick = recovered.search(
                canonical_quick_switch_request(
                    root_ids=[root_id],
                    limit=10,
                    now_unix_ms=now_ms + 600,
                    timezone_offset_minutes=0,
                ),
                request_id=31,
            )
            recovered_candidates = validate_core_candidates(recovered_quick, root_ids=[root_id])
            recovered_pinned = select_core_candidate(recovered_candidates, 0)
            if not paths_match(str(beta), str(recovered_pinned["path"])):
                raise HarnessError(
                    "core_pin_lost_after_cache_recovery",
                    "First-class pin state was lost after derived-cache recovery.",
                )
            diagnostics_after = recovered.diagnostics()
            recovered.flush()

        republished = snapshot.is_file() and snapshot.read_bytes() != bytes(hostile)
        if not republished:
            raise HarnessError(
                "core_snapshot_not_republished",
                "Core recovered search state but did not replace the hostile derived snapshot.",
            )
        receipt.update(
            {
                "status": "success",
                "outcome": "index_search_reopen_cache_recovery_green",
                "root_id": root_id,
                "reconcile": reconcile,
                "exact_folder_result": alpha_result,
                "quick_switch_top_folder": selected,
                "first_class_pin_contract": {
                    "item_id": beta_item_id,
                    "toggle_response": pin_result,
                    "pinned_top_folder": pinned_selected,
                    "clean_reopen_top_folder": reopened_pinned,
                    "cache_recovery_top_folder": recovered_pinned,
                    "clean_reopen_persisted": True,
                    "cache_recovery_persisted": True,
                },
                "launcher_file_search_contract": {
                    "unicode_space_folder_result": unicode_result,
                    "launcher_wall_ms": round(launcher_wall_ms, 3),
                    "launcher_core_ms": round(float(unicode_launcher.get("elapsed_micros") or 0) / 1000.0, 3),
                    "file_search_first_page_wall_ms": round(file_search_wall_ms, 3),
                    "file_search_first_page_core_ms": round(float(first_file_page.get("elapsed_micros") or 0) / 1000.0, 3),
                    "file_search_total_matches_before_mutation": int(first_file_page.get("total_matches") or 0),
                    "file_search_catalog_generation": generation,
                    "file_search_second_page_count": len(second_file_page.get("results") or []),
                    "completed_request_cancel_status": 4,
                    "pre_action_get_item_verified": True,
                    "deleted_item_revalidation_rejected": True,
                    "stale_catalog_generation_rejected": stale_generation_rejected,
                    "symlink_fixture_supported": symlink_supported,
                    "symlink_target_traversal_rejected": symlink_supported and not symlink_target_leaked,
                },
                "diagnostics_before_reopen": diagnostics_before,
                "diagnostics_after_recovery": diagnostics_after,
                "clean_reopen_search_verified": True,
                "quick_switch_history_ranked_before_pin": True,
                "first_class_pin_persisted": True,
                "hostile_cache_variant": "u64_max_first_sequence_length_with_valid_crc32",
                "cache_recovery_search_verified": True,
                "cache_snapshot_republished": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
        )
        return receipt


def host_filesystem_smoke_run(
    library_path: Path,
    *,
    require_macos: bool = True,
) -> dict[str, Any]:
    """Exercise the shared recovery/currentness contract on the host filesystem.

    Production CLI use is Darwin-only. Tests may opt out of that host gate to
    exercise the same shared C-ABI workflow without promoting non-Mac results.
    Native FSEventStream delivery/event IDs are deliberately outside this smoke.
    """

    is_macos = sys.platform == "darwin"
    if require_macos and not is_macos:
        raise HarnessError(
            "non_macos_host",
            "Host filesystem qualification requires a real macOS host; use --core-smoke for cross-platform shared-core proof.",
        )

    started = time.monotonic()
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "host_filesystem_smoke",
        "host_platform": platform.system().lower(),
        "real_mac_executed": is_macos,
        "status": "failed",
        "qualification_scope": "mac_host_filesystem_and_shared_core" if is_macos else "shared_contract_only",
        "core_owner": "listary-ffi -> listary-core -> RocksDB",
        "temporary_fixture_only": True,
        "native_fseventstream_executed": False,
        "no_parallel_index_or_ranker": True,
        "core_library": str(library_path),
    }
    _, bridge_preflight = load_and_validate_core_library(library_path)
    receipt["bridge_preflight"] = bridge_preflight

    with tempfile.TemporaryDirectory(prefix="listary-mac-host-smoke-") as temp_name:
        temp_root = Path(temp_name)
        state_dir = temp_root / "state"
        indexed_root = temp_root / "Indexed Résumé Ω with spaces"
        indexed_root.mkdir(parents=True)
        rename_old = indexed_root / "Résumé rename old.txt"
        rename_new = indexed_root / "Résumé rename new.txt"
        modify_path = indexed_root / "modify me.txt"
        delete_path = indexed_root / "delete me.txt"
        create_path = indexed_root / "created Ω.txt"
        restricted_dir = indexed_root / "restricted folder"
        restricted_file = restricted_dir / "retained.txt"
        restricted_dir.mkdir()
        rename_old.write_text("rename", encoding="utf-8")
        modify_path.write_text("small", encoding="utf-8")
        delete_path.write_text("delete", encoding="utf-8")
        restricted_file.write_text("retain", encoding="utf-8")

        outside = temp_root / "outside"
        outside.mkdir()
        outside_secret = outside / "SHOULD_NOT_LEAK.txt"
        outside_secret.write_text("outside", encoding="utf-8")
        link_path = indexed_root / "linked outside"
        symlink_supported = False
        try:
            os.symlink(outside, link_path, target_is_directory=True)
            symlink_supported = True
        except (NotImplementedError, OSError):
            if is_macos:
                raise HarnessError(
                    "host_symlink_fixture_unavailable",
                    "Real-Mac host qualification could not create the required directory symlink fixture.",
                )

        restricted_original_mode = restricted_dir.stat().st_mode & 0o777
        restricted_mode_changed = False
        root_id = ""
        reopen_verified = False
        unavailable_after_loss = False
        available_after_return = False
        inaccessible_supported = False
        inaccessible_retained = False
        stale_generation_rejected = False
        duplicate_replay_generation_stable = False
        active_file_search_cancellation_verified: bool | None = None
        symlink_target_leaked = False
        full_recovery_started = 0
        full_recovery_elapsed_ms = 0.0
        try:
            with CoreAbiClient(library_path, state_dir) as core:
                root_id = core.add_root(indexed_root)
                initial_reconcile = core.reconcile_root(root_id, request_id=100)
                initial_generation = int(core.diagnostics().get("catalog_generation") or 0)
                if initial_generation <= 0:
                    raise HarnessError(
                        "core_generation_missing",
                        "Initial host reconcile did not publish a positive catalog generation.",
                    )

                if symlink_supported:
                    symlink_search = core.search(
                        _search_request(
                            "SHOULD_NOT_LEAK",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_000,
                        ),
                        request_id=101,
                    )
                    symlink_target_leaked = bool(symlink_search.get("results"))
                    if symlink_target_leaked:
                        raise HarnessError(
                            "core_symlink_traversal_leak",
                            "Host crawl followed a symlink outside the indexed root.",
                        )

                rename_old.rename(rename_new)
                modify_path.write_bytes(b"x" * 4096)
                delete_path.unlink()
                create_path.write_text("created", encoding="utf-8")
                incremental_started = time.perf_counter_ns()
                incremental = core.reconcile_paths(
                    root_id,
                    [create_path, rename_old, rename_new, modify_path, delete_path, create_path],
                    request_id=102,
                    expected_catalog_generation=initial_generation,
                )
                incremental_elapsed_ms = (time.perf_counter_ns() - incremental_started) / 1_000_000.0

                after_incremental_generation = int(core.diagnostics().get("catalog_generation") or 0)
                if after_incremental_generation <= initial_generation:
                    raise HarnessError(
                        "core_incremental_generation_missing",
                        "Dirty-path reconciliation did not advance catalog generation for real filesystem changes.",
                    )
                _find_result_path(
                    core.search(
                        _search_request(
                            "created Ω",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_100,
                        ),
                        request_id=103,
                    ),
                    create_path,
                )
                _find_result_path(
                    core.search(
                        _search_request(
                            "Résumé rename new",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_101,
                        ),
                        request_id=104,
                    ),
                    rename_new,
                )
                if core.search(
                    _search_request(
                        "Résumé rename old",
                        root_ids=[root_id],
                        kinds=["file"],
                        now_unix_ms=1_800_000_100_102,
                    ),
                    request_id=105,
                ).get("results"):
                    raise HarnessError("core_rename_stale_result", "Renamed source path remained searchable.")
                if core.search(
                    _search_request(
                        "delete me",
                        root_ids=[root_id],
                        kinds=["file"],
                        now_unix_ms=1_800_000_100_103,
                    ),
                    request_id=106,
                ).get("results"):
                    raise HarnessError("core_delete_stale_result", "Deleted path remained searchable.")
                modified_result = _find_result_path(
                    core.search(
                        _search_request(
                            "modify me",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_104,
                        ),
                        request_id=107,
                    ),
                    modify_path,
                )
                if int(modified_result["item"].get("size") or 0) != 4096:
                    raise HarnessError("core_modify_stale_metadata", "Modified file size did not refresh to 4096 bytes.")

                stale_candidate = indexed_root / "stale generation candidate.txt"
                stale_candidate.write_text("stale", encoding="utf-8")
                try:
                    core.reconcile_paths(
                        root_id,
                        [stale_candidate],
                        request_id=108,
                        expected_catalog_generation=initial_generation,
                    )
                except HarnessError as exc:
                    if exc.code != "core_reconcile_paths_stale_catalog":
                        raise
                    stale_generation_rejected = True
                if not stale_generation_rejected:
                    raise HarnessError(
                        "core_stale_generation_not_rejected",
                        "Host dirty-path handoff accepted an intentionally stale catalog generation.",
                    )
                if core.search(
                    _search_request(
                        "stale generation candidate",
                        root_ids=[root_id],
                        kinds=["file"],
                        now_unix_ms=1_800_000_100_105,
                    ),
                    request_id=109,
                ).get("results"):
                    raise HarnessError(
                        "core_stale_generation_mutated",
                        "Rejected stale dirty-path handoff still mutated searchable state.",
                    )

                full_recovery_started = time.perf_counter_ns()
                recovery = core.reconcile_root(root_id, request_id=110)
                full_recovery_elapsed_ms = (time.perf_counter_ns() - full_recovery_started) / 1_000_000.0
                _find_result_path(
                    core.search(
                        _search_request(
                            "stale generation candidate",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_106,
                        ),
                        request_id=111,
                    ),
                    stale_candidate,
                )

                replay_generation = int(core.diagnostics().get("catalog_generation") or 0)
                replay = core.reconcile_paths(
                    root_id,
                    [create_path, rename_new, create_path, modify_path, rename_new],
                    request_id=112,
                    expected_catalog_generation=replay_generation,
                )
                duplicate_replay_generation_stable = (
                    int(core.diagnostics().get("catalog_generation") or 0) == replay_generation
                )
                if not duplicate_replay_generation_stable:
                    raise HarnessError(
                        "core_noop_generation_churn",
                        "Duplicate/out-of-order no-op dirty replay advanced catalog generation.",
                    )

                restricted_result = _find_result_path(
                    core.search(
                        _search_request(
                            "retained",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_107,
                        ),
                        request_id=113,
                    ),
                    restricted_file,
                )
                restricted_item_id = int(restricted_result["item"]["id"])
                os.chmod(restricted_dir, 0)
                restricted_mode_changed = True
                inaccessible_supported = not os.access(restricted_dir, os.R_OK | os.X_OK)
                if is_macos and not inaccessible_supported:
                    raise HarnessError(
                        "host_inaccessible_fixture_unavailable",
                        "Real-Mac host qualification could not establish a genuinely inaccessible directory fixture.",
                    )
                if inaccessible_supported:
                    inaccessible_generation = int(core.diagnostics().get("catalog_generation") or 0)
                    inaccessible_report = core.reconcile_paths(
                        root_id,
                        [restricted_dir],
                        request_id=114,
                        expected_catalog_generation=inaccessible_generation,
                    )
                    if int(inaccessible_report.get("errors") or 0) <= 0:
                        raise HarnessError(
                            "core_inaccessible_path_not_reported",
                            "Inaccessible directory reconciliation did not report a localized crawl error.",
                        )
                    retained = core.get_item(restricted_item_id)
                    inaccessible_retained = paths_match(str(restricted_file), str(retained.get("path") or ""))
                    if not inaccessible_retained:
                        raise HarnessError(
                            "core_inaccessible_item_lost",
                            "Localized inaccessible-path recovery dropped the last committed indexed item.",
                        )
                    os.chmod(restricted_dir, restricted_original_mode)
                    restricted_mode_changed = False
                    core.reconcile_paths(
                        root_id,
                        [restricted_dir],
                        request_id=115,
                        expected_catalog_generation=int(core.diagnostics().get("catalog_generation") or 0),
                    )

                away_root = temp_root / "root temporarily absent"
                indexed_root.rename(away_root)
                try:
                    core.reconcile_root(root_id, request_id=116)
                except HarnessError as exc:
                    if exc.code != "core_reconcile_failed":
                        raise
                root_row = next((row for row in core.list_roots() if str(row.get("id")) == root_id), None)
                unavailable_after_loss = bool(root_row and root_row.get("availability") == "unavailable")
                if not unavailable_after_loss:
                    raise HarnessError(
                        "core_root_loss_not_unavailable",
                        "Missing root was not retained as unavailable after failed full reconciliation.",
                    )
                while_absent = away_root / "created while root absent.txt"
                while_absent.write_text("away", encoding="utf-8")
                away_root.rename(indexed_root)
                recovery_after_return = core.reconcile_root(root_id, request_id=117)
                root_row = next((row for row in core.list_roots() if str(row.get("id")) == root_id), None)
                available_after_return = bool(root_row and root_row.get("availability") == "available")
                if not available_after_return:
                    raise HarnessError(
                        "core_root_return_not_available",
                        "Returned root did not become available after canonical full reconciliation.",
                    )
                _find_result_path(
                    core.search(
                        _search_request(
                            "created while root absent",
                            root_ids=[root_id],
                            kinds=["file"],
                            now_unix_ms=1_800_000_100_108,
                        ),
                        request_id=118,
                    ),
                    indexed_root / "created while root absent.txt",
                )

                if is_macos:
                    cancellation_dir = indexed_root / "cancellation corpus"
                    cancellation_dir.mkdir()
                    for index in range(25_000):
                        (cancellation_dir / f"cancel-corpus-{index:05}.txt").write_text(
                            "cancel",
                            encoding="utf-8",
                        )
                    core.reconcile_paths(
                        root_id,
                        [cancellation_dir],
                        request_id=120,
                        expected_catalog_generation=int(core.diagnostics().get("catalog_generation") or 0),
                    )
                    cancellation_request_id = 121
                    cancellation_result: dict[str, Any] = {}

                    def run_cancellable_file_search() -> None:
                        try:
                            cancellation_result["response"] = core.file_search(
                                _file_search_request(
                                    "cancel-corpus-",
                                    root_ids=[root_id],
                                    now_unix_ms=1_800_000_100_110,
                                    offset=0,
                                    limit=200,
                                    sort_field="name",
                                    sort_direction="ascending",
                                ),
                                request_id=cancellation_request_id,
                            )
                        except HarnessError as exc:
                            cancellation_result["error"] = exc.code

                    cancellation_thread = threading.Thread(
                        target=run_cancellable_file_search,
                        name="listary-mac-host-file-search-cancel",
                        daemon=True,
                    )
                    cancellation_thread.start()
                    cancellation_sent = False
                    cancellation_deadline = time.monotonic() + 2.0
                    while cancellation_thread.is_alive() and time.monotonic() < cancellation_deadline:
                        if core.cancel_status(cancellation_request_id) == 0:
                            cancellation_sent = True
                            break
                        time.sleep(0.0005)
                    cancellation_thread.join(timeout=5.0)
                    if cancellation_thread.is_alive():
                        raise HarnessError(
                            "core_file_search_cancel_timeout",
                            "Active File Search cancellation did not drain within five seconds.",
                        )
                    active_file_search_cancellation_verified = (
                        cancellation_sent
                        and cancellation_result.get("error") == "core_file_search_cancelled"
                    )
                    if not active_file_search_cancellation_verified:
                        raise HarnessError(
                            "core_file_search_cancel_not_observed",
                            "Could not observe one active generation-bound File Search request being cancelled on the real Mac host.",
                        )
                core.flush()

            with CoreAbiClient(library_path, state_dir) as reopened:
                reopened_result = reopened.search(
                    _search_request(
                        "created while root absent",
                        root_ids=[root_id],
                        kinds=["file"],
                        now_unix_ms=1_800_000_100_109,
                    ),
                    request_id=119,
                )
                _find_result_path(reopened_result, indexed_root / "created while root absent.txt")
                reopen_verified = True
        finally:
            if restricted_mode_changed:
                try:
                    os.chmod(restricted_dir, restricted_original_mode)
                except OSError:
                    pass

        receipt.update(
            {
                "status": "success",
                "outcome": "mac_host_filesystem_contract_green" if is_macos else "shared_filesystem_contract_green",
                "root_id": root_id,
                "initial_reconcile": initial_reconcile,
                "incremental_reconcile": incremental,
                "simulated_untrusted_full_recovery": recovery,
                "root_return_reconcile": recovery_after_return,
                "unicode_spaces_verified": True,
                "create_modify_delete_rename_verified": True,
                "stale_generation_rejected": stale_generation_rejected,
                "duplicate_out_of_order_noop_generation_stable": duplicate_replay_generation_stable,
                "root_loss_retained_unavailable": unavailable_after_loss,
                "root_return_recovered_available": available_after_return,
                "clean_reopen_verified": reopen_verified,
                "active_file_search_cancellation_verified": active_file_search_cancellation_verified,
                "symlink_fixture_supported": symlink_supported,
                "symlink_target_traversal_rejected": symlink_supported and not symlink_target_leaked,
                "inaccessible_fixture_supported": inaccessible_supported,
                "inaccessible_last_committed_item_retained": inaccessible_retained if inaccessible_supported else None,
                "incremental_reconcile_wall_ms": round(incremental_elapsed_ms, 3),
                "full_recovery_wall_ms": round(full_recovery_elapsed_ms, 3),
                "native_fseventstream_executed": False,
                "not_proven": [
                    "AppKit Launcher/File Search activation, rendering, focus, or UI-query cancellation",
                    "native FSEventStream delivery, event IDs, drop flags, stream restart, eject, or remount",
                    "Quick Look rendering or cancellation",
                    "Accessibility/Finder/Open-Save navigation",
                    "signing/notarization/Gatekeeper",
                ],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
        )
        return receipt


def panel_or_error(probe: Mapping[str, Any]) -> dict[str, Any]:
    status = str(probe.get("status") or "")
    if status == "accessibility_denied":
        raise HarnessError("permission_denied", "Accessibility permission is not granted.")
    if status == "panel_not_found":
        raise HarnessError("panel_not_found", "No supported AX-visible native Open/Save panel is frontmost.")
    if status == "panel_identity_unstable":
        raise HarnessError("panel_identity_unstable", "The panel does not expose a stable window identity.")
    panel = probe.get("panel")
    if status != "ok" or not isinstance(panel, Mapping):
        raise HarnessError("panel_probe_failed", f"Unexpected panel probe status: {status or 'empty'}")
    token = str(panel.get("panel_token") or "")
    if not token:
        raise HarnessError("panel_identity_unstable", "Panel probe returned no stable token.")
    return dict(panel)


def write_receipt(path: Path | None, receipt: Mapping[str, Any]) -> None:
    if path is None:
        return
    target = path.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def json_for_stream(
    receipt: Mapping[str, Any],
    *,
    pretty: bool,
    encoding: str | None,
) -> str:
    """Render readable Unicode when the stream supports it, escaped JSON otherwise."""

    rendered = json.dumps(
        receipt,
        indent=2 if pretty else None,
        ensure_ascii=False,
    )
    if not encoding:
        return rendered
    try:
        rendered.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return json.dumps(
            receipt,
            indent=2 if pretty else None,
            ensure_ascii=True,
        )
    return rendered


def acceptance_run(args: argparse.Namespace) -> dict[str, Any]:
    request = canonical_quick_switch_request(
        root_ids=args.root_id,
        limit=args.limit,
    )
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "host_platform": platform.system().lower(),
        "real_mac_executed": sys.platform == "darwin" and not args.dry_run,
        "status": "blocked",
        "outcome": "not_started",
        "core_request": request,
        "core_owner": "listary-ffi -> listary-core",
        "navigation_owner": "macos/QuickSwitchAcceptance/ax_file_dialog.js",
        "no_parallel_index_or_ranker": True,
        "readback_required_for_success": True,
        "file_context_schema": FILE_CONTEXT_SCHEMA,
        "file_action_intent_schema": FILE_ACTION_INTENT_SCHEMA,
        "supported_action_intents": sorted(FILE_ACTION_KINDS),
        "stale_context_policy": "context_token+generation_and_activation_generation_must_match",
        "indexed_action_revalidation": "listary_core_get_item_before_native_side_effect",
        "fixture_mode": args.fixture,
    }
    if args.dry_run:
        receipt.update(
            {
                "status": "dry_run",
                "outcome": "static_plan",
                "planned_ax_helper": str(AX_HELPER),
                "planned_fixture_source": str(FIXTURE_SOURCE) if args.fixture else None,
            }
        )
        return receipt
    if sys.platform != "darwin":
        receipt["outcome"] = "non_macos_host"
        return receipt

    state_dir = args.state_dir.expanduser().resolve(strict=False)
    if not state_dir.is_dir():
        receipt["outcome"] = "core_state_missing"
        receipt["error"] = f"State directory does not exist: {state_dir}"
        return receipt

    fixture: FileDialogFixture | None = None
    try:
        library = resolve_core_library(args.core_lib, build_if_missing=not args.no_build_core)
        receipt["core_library"] = str(library)
        receipt["state_dir"] = str(state_dir)
        _, bridge_preflight = load_and_validate_core_library(library)
        receipt["bridge_preflight"] = bridge_preflight

        if args.fixture:
            fixture_initial = args.fixture_initial.expanduser().resolve(strict=False)
            if not fixture_initial.is_dir():
                raise HarnessError("fixture_initial_missing", f"Fixture initial folder does not exist: {fixture_initial}")
            fixture = FileDialogFixture(args.fixture, fixture_initial)
            fixture.start()
            receipt["fixture_before"] = fixture.read_status()

        ax = AxHelperClient()
        probe = ax.probe()
        receipt["permission"] = probe.get("permission")
        panel_before = panel_or_error(probe)
        receipt["panel_before"] = panel_before

        panel_context = context_from_panel(panel_before, generation=1, query_text="")
        receipt["context_before"] = panel_context

        with CoreAbiClient(library, state_dir) as core:
            core_response = core.search(request)
            candidates = validate_core_candidates(core_response, root_ids=args.root_id)
            receipt["core_considered_candidates"] = core_response.get("considered_candidates")
            receipt["core_elapsed_micros"] = core_response.get("elapsed_micros")
            receipt["folder_candidates"] = candidates

            result_identity: dict[str, Any] | None = None
            live_item: dict[str, Any] | None = None
            selected_item_id: int | None = None
            if args.finder_transfer:
                finder = ax.finder_context()
                receipt["finder_context"] = finder
                if str(finder.get("status") or "") != "ok" or not (
                    finder.get("current_directory") or finder.get("current_folder")
                ):
                    raise HarnessError(
                        "finder_context_unavailable",
                        "Finder current folder could not be read through Accessibility.",
                    )
                finder_context = context_from_finder(finder, generation=1, query_text="")
                receipt["finder_context_contract"] = finder_context
                target = str(finder_context["current_directory"])
                receipt["target_source"] = "finder_context"
            else:
                selected = select_core_candidate(candidates, args.candidate_index)
                receipt["selected_candidate"] = selected
                result_identity = canonical_result_identity(selected)
                selected_item_id = int(result_identity["item_id"])
                live_item = core.get_item(selected_item_id)
                target = str(selected["path"])
                receipt["target_source"] = "shared_core_zero_query"

            target_path = require_accessible_directory(target)
            target = normalized_path(str(target_path))
            receipt["target_folder"] = target
            action_intent = canonical_action_intent(
                "navigate",
                context=panel_context,
                activation_generation=1,
                target_path=target,
                query_text="",
                result_identity=result_identity,
            )
            validate_action_intent(
                action_intent,
                current_context=panel_context,
                current_activation_generation=1,
                live_item=live_item,
                target_accessible=True,
            )
            receipt["action_intent"] = action_intent

            navigation = ax.navigate(str(panel_before["panel_token"]), target)
            receipt["navigation"] = navigation
            nav_status = str(navigation.get("status") or "")
            if nav_status in {"panel_closed", "panel_replaced"}:
                receipt["status"] = "failed"
                receipt["outcome"] = nav_status
                receipt["verified_readback"] = False
                receipt["stale_object_recovered"] = bool(
                    navigation.get("stale_object_recovered", False)
                )
                return receipt
            after_probe = ax.probe()
            receipt["panel_probe_after"] = after_probe
            panel_after = panel_or_error(after_probe)
            receipt["panel_after"] = panel_after
            after_generation = next_context_generation(
                panel_context,
                next_context_token=str(panel_after.get("context_token") or panel_after.get("panel_token") or ""),
            )
            panel_context_after = context_from_panel(
                panel_after,
                generation=after_generation,
                query_text="",
            )
            receipt["context_after"] = panel_context_after
            if after_generation != int(panel_context["generation"]):
                raise HarnessError("stale_context", "Open/Save context was replaced before action verification.")
            fixture_after = fixture.read_status() if fixture is not None else None
            if fixture_after is not None:
                receipt["fixture_after"] = fixture_after

            status, outcome = assess_navigation(
                panel_before=panel_before,
                navigation=navigation,
                panel_after=panel_after,
                expected_path=target,
                fixture_state=fixture_after,
            )
            receipt["status"] = status
            receipt["outcome"] = outcome
            receipt["stale_object_recovered"] = bool(navigation.get("stale_object_recovered", False))
            receipt["verified_readback"] = outcome == "success"
            if outcome == "success" and selected_item_id is not None:
                core.record_activation(
                    selected_item_id,
                    now_unix_ms=int(time.time() * 1000),
                    source="quick_switch",
                )
                receipt["activation_recorded"] = True
            return receipt
    except HarnessError as exc:
        receipt["status"] = "blocked" if exc.code in {
            "permission_denied",
            "panel_not_found",
            "panel_identity_unstable",
            "core_state_missing",
            "core_open_failed",
        } else "failed"
        receipt["outcome"] = exc.code
        receipt["error"] = str(exc)
        receipt["verified_readback"] = False
        return receipt
    finally:
        if fixture is not None:
            fixture.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("LISTARY_MAC_STATE_DIR", DEFAULT_STATE_DIR)),
        help="Existing Listary core state directory. The harness never adds/reconciles roots or records history.",
    )
    parser.add_argument("--core-lib", type=Path, help="Optional explicit listary-ffi dynamic library path.")
    parser.add_argument("--no-build-core", action="store_true", help="Do not cargo-build listary-ffi when the release library is missing.")
    parser.add_argument("--root-id", action="append", default=[], help="Optional root UUID scope; may be supplied multiple times.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--candidate-index", type=int, default=0, help="Use this zero-based shared-core result without re-ranking it.")
    parser.add_argument("--finder-transfer", action="store_true", help="Use the current Finder folder as the navigation target while still recording shared-core folder suggestions.")
    parser.add_argument("--fixture", choices=("open", "save"), help="Compile/run the repo-owned native AppKit FileDialogFixture before acceptance.")
    parser.add_argument("--fixture-initial", type=Path, default=Path.home(), help="Initial directory for the native fixture panel.")
    parser.add_argument("--receipt", type=Path, help="Optional atomic JSON receipt path.")
    parser.add_argument(
        "--bridge-preflight",
        action="store_true",
        help="Verify the canonical ABI-v9 header, runtime API version, and complete exported symbol set without opening user state.",
    )
    parser.add_argument(
        "--core-smoke",
        action="store_true",
        help="Run a disposable C-ABI indexing/search/Quick-Switch/reopen/cache-recovery smoke without macOS UI or permissions.",
    )
    parser.add_argument(
        "--host-filesystem-smoke",
        action="store_true",
        help=(
            "On a real Mac, run the disposable shared recovery/currentness fixture against actual macOS filesystem semantics "
            "(Unicode/spaces, dirty reconciliation, stale generations, symlinks, inaccessible paths, root loss/return, reopen). "
            "This does not run or qualify native FSEventStream/AppKit/Quick Look."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Emit the canonical request/plan without touching macOS, the core, or a dialog.")
    parser.add_argument("--json", action="store_true", help="Pretty-print JSON to stdout.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.bridge_preflight:
            if args.core_smoke or args.host_filesystem_smoke or args.dry_run:
                raise HarnessError(
                    "invalid_argument",
                    "--bridge-preflight cannot be combined with --core-smoke, --host-filesystem-smoke, or --dry-run.",
                )
            library = resolve_core_library(args.core_lib, build_if_missing=not args.no_build_core)
            receipt = bridge_preflight_run(library)
        elif args.core_smoke:
            if args.host_filesystem_smoke or args.dry_run:
                raise HarnessError(
                    "invalid_argument",
                    "--core-smoke cannot be combined with --host-filesystem-smoke or --dry-run.",
                )
            library = resolve_core_library(args.core_lib, build_if_missing=not args.no_build_core)
            receipt = core_smoke_run(library)
        elif args.host_filesystem_smoke:
            if args.dry_run:
                raise HarnessError(
                    "invalid_argument",
                    "--host-filesystem-smoke executes a disposable real-host fixture and cannot be combined with --dry-run.",
                )
            library = resolve_core_library(args.core_lib, build_if_missing=not args.no_build_core)
            receipt = host_filesystem_smoke_run(library)
        else:
            receipt = acceptance_run(args)
    except (ValueError, HarnessError) as exc:
        code = exc.code if isinstance(exc, HarnessError) else "invalid_argument"
        receipt = {
            "schema": SCHEMA,
            "host_platform": platform.system().lower(),
            "real_mac_executed": False,
            "status": "failed",
            "outcome": code,
            "error": str(exc),
            "verified_readback": False,
        }
    write_receipt(args.receipt, receipt)
    print(
        json_for_stream(
            receipt,
            pretty=args.json,
            encoding=getattr(sys.stdout, "encoding", None),
        )
    )
    if receipt.get("status") in {"success", "dry_run"}:
        return 0
    return 2 if receipt.get("status") == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())

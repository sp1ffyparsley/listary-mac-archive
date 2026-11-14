#!/usr/bin/env python3
"""Coordinate the one bounded physical-macOS Listary/Directory Opus acceptance.

This is an acceptance orchestrator, not a product platform layer.  It composes
the existing bridge/bootstrap, shared Rust core, Quick Switch/AppKit fixture,
Spiffy appearance contract, and Directory Opus Mac verifier.  It never creates
another index, watcher, settings database, Finder adapter, or file-operation
owner.  A non-Mac host can emit/validate the plan, but can never produce a
physical acceptance success.

The production Swift/AppKit shell is intentionally required as external
evidence.  Current repository policy requires that first target to be created,
compiled, and linked on a real Mac; this script refuses to manufacture that
evidence on Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
MACOS = REPO_ROOT / "macos"
BOOTSTRAP = TOOLS / "macos_bridge_bootstrap.py"
QUICK_SWITCH = TOOLS / "quick_switch_acceptance.py"
AX_HELPER = MACOS / "QuickSwitchAcceptance" / "ax_file_dialog.js"
FILE_DIALOG_FIXTURE = MACOS / "FileDialogFixture" / "main.m"
WINDOW_FIXTURE_SOURCE = MACOS / "WindowFixture" / "main.m"
WINDOW_FIXTURE_AX = MACOS / "WindowFixture" / "ax_window_focus.js"

SCHEMA = "listary_physical_macos_acceptance_v1"
PLAN_SCHEMA = "listary_physical_macos_acceptance_plan_v1"
NATIVE_SCHEMA = "listary_macos_native_app_acceptance_v1"
APPEARANCE_SCHEMA = "spiffy_shared_appearance_v1"
OPUS_SCHEMA = "opusmac_macos_acceptance_v1"
DEFAULT_PROFILE = "listary_slate"
NATIVE_REQUIRED_CAPABILITIES = (
    "appkit_startup",
    "shared_settings_restore",
    "indexed_search",
    "exact_path",
    "finder_alias",
    "same_identity_move",
    "first_result_latency",
    "keyboard_focus",
    "native_open_panel",
    "native_save_panel",
    "authorized_file_action",
    "permission_recovery",
    "unavailable_root_recovery",
    "appearance_propagation",
    "appearance_persistence",
    "restart_survival",
    "fsevents_delivery_recovery",
    "quick_look",
    "lifecycle",
    "developer_id_signing",
    "notarization",
    "gatekeeper",
)


class AcceptanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_display(path: Path) -> str:
    try:
        return "$REPO/" + path.resolve(strict=False).relative_to(REPO_ROOT.resolve(strict=False)).as_posix()
    except ValueError:
        pass
    try:
        return "$HOME/" + path.resolve(strict=False).relative_to(Path.home().resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)


def _display_arg(value: str) -> str:
    text = str(value)
    for root, label in ((str(REPO_ROOT), "$REPO"), (str(Path.home()), "$HOME")):
        if text == root:
            return label
        if text.startswith(root + os.sep):
            return label + text[len(root):].replace("\\", "/")
    return text


def _sanitize_text(value: str) -> str:
    text = str(value or "")
    replacements = (
        (str(REPO_ROOT.resolve(strict=False)), "$REPO"),
        (str(Path.home().resolve(strict=False)), "$HOME"),
    )
    for source, replacement in replacements:
        text = text.replace(source, replacement)
        text = text.replace(source.replace("\\", "/"), replacement)
    return text[-4000:]


def command_record(argv: Sequence[str]) -> list[str]:
    return [_display_arg(str(value)) for value in argv]


def native_shell_inventory() -> dict[str, Any]:
    def admitted(path: Path) -> bool:
        try:
            relative = path.relative_to(REPO_ROOT)
        except ValueError:
            return False
        return not any(part in {"target", "_runtime", ".test_tmp", ".git"} for part in relative.parts)

    swift = sorted(path for path in REPO_ROOT.rglob("*.swift") if path.is_file() and admitted(path))
    xcode = sorted(path for path in REPO_ROOT.rglob("*.xcodeproj") if path.is_dir() and admitted(path))
    return {
        "production_shell_source_present": bool(swift and xcode),
        "swift_source_count": len(swift),
        "xcode_project_count": len(xcode),
        "swift_sources": [_repo_display(path) for path in swift[:32]],
        "xcode_projects": [_repo_display(path) for path in xcode[:8]],
        "policy": "first production Swift/AppKit target must be created/compiled on real Mac and linked to canonical ABI-v9 XCFramework",
    }


def build_plan() -> dict[str, Any]:
    python = "python3"
    state = "$SESSION/state"
    fixture = "$SESSION/fixture"
    commands = [
        [python, "tools/macos_bridge_bootstrap.py", "--plan", "--json"],
        [python, "tools/macos_bridge_bootstrap.py", "--output", "$SESSION/ListaryCore.xcframework", "--json"],
        [python, "tools/quick_switch_acceptance.py", "--state-dir", state, "--fixture", "open", "--fixture-initial", fixture, "--receipt", "$SESSION/quick-switch-open.json", "--json"],
        [python, "tools/quick_switch_acceptance.py", "--state-dir", state, "--fixture", "save", "--fixture-initial", fixture, "--receipt", "$SESSION/quick-switch-save.json", "--json"],
        ["$LISTARY_MAC_NATIVE_RUNNER", "--acceptance-session", "$SESSION", "--state-dir", state, "--fixture-root", fixture, "--appearance-settings", "$SESSION/shared-settings/appearance.json", "--receipt", "$SESSION/native-app.json", "--json"],
        ["$OPUS_PACKAGE/Verify-Mac.command", "--launch"],
    ]
    return {
        "schema": PLAN_SCHEMA,
        "status": "plan",
        "real_mac_executed": False,
        "acceptance_state_before": "5/6 agent-reported verified",
        "acceptance_may_advance": False,
        "owners": {
            "search_index": "listary-ffi -> listary-core -> RocksDB",
            "bridge": _repo_display(BOOTSTRAP),
            "file_dialog_navigation": _repo_display(AX_HELPER),
            "native_file_dialog_fixture": _repo_display(FILE_DIALOG_FIXTURE),
            "native_focus_fixture": _repo_display(WINDOW_FIXTURE_SOURCE),
            "file_action_contract": "listary_file_context_v1 + listary_file_action_intent_v1 with get_item pre-action revalidation",
            "shared_appearance": "Spiffy Workspace ThemeManager -> spiffy_shared_appearance_v1",
            "listary_state": "$HOME/Library/Application Support/ListaryMac/state",
            "shared_appearance_state": "$HOME/Library/Application Support/SpiffyScenes/settings/appearance.json (task run uses SS_WORKFLOW_SETTINGS_ROOT override)",
            "directory_opus_search": "Directory Opus Mac ListaryClient authenticated companion endpoint; never opens Listary RocksDB",
            "directory_opus_file_operations": "Directory Opus Mac FileOperationService + OperationJournal + DesktopFilePlatform",
        },
        "native_shell": native_shell_inventory(),
        "commands": commands,
        "required_native_capabilities": list(NATIVE_REQUIRED_CAPABILITIES),
        "native_receipt_template": native_receipt_template(),
        "finder_alias_boundary": "Finder alias evidence is native-macOS evidence; symlink/hard-link smoke is retained separately and must not be relabeled as Finder alias support.",
        "directory_opus_boundary": "Use the independent osx-arm64 Directory Opus Mac verifier/receipt only; never claim the Windows Directory Opus application ran natively on macOS and never claim full parity from automatic checks.",
        "privacy": "fixtures are generated under the acceptance session; receipts contain no clipboard/query history, credentials, or production file contents",
        "stop_conditions": [
            "not a real Darwin host",
            "source bytes change after bridge plan/build begins",
            "production Swift/AppKit shell receipt missing or incomplete",
            "Accessibility/Open-Save identity or readback is unstable",
            "pre-action indexed identity no longer matches",
            "permission/unavailable-root recovery does not converge",
            "FSEvents dropped-history recovery is not proven",
            "shared appearance is persisted outside the incumbent schema/owner",
            "authorized action cleanup/undo is incomplete or ambiguous",
            "Developer ID/signing/notarization/Gatekeeper evidence is incomplete",
        ],
    }


def native_receipt_template() -> dict[str, Any]:
    """Return the exact native product evidence shape required for 6/6."""

    return {
        "schema": NATIVE_SCHEMA,
        "status": "pending",
        "real_mac_executed": False,
        "platform": "macOS",
        "production_appkit_shell": False,
        "session_id": "<same physical acceptance session id>",
        "source_identity": {
            "build_id": "<native build id>",
            "app_sha256": "<SHA-256>",
        },
        "startup": {
            "started": False,
            "shared_settings_schema": APPEARANCE_SCHEMA,
            "restored_profile": DEFAULT_PROFILE,
            "startup_latency_ms": None,
        },
        "search": {
            "exact_path_verified": False,
            "finder_alias_verified": False,
            "same_identity_move_verified": False,
            "old_path_absent_after_move": False,
            "first_result_latency_ms": None,
        },
        "interaction": {
            "query_focus_verified": False,
            "keyboard_navigation_verified": False,
            "prior_focus_restored": False,
        },
        "file_dialog": {"open_verified": False, "save_verified": False},
        "file_action": {
            "kind": "reveal",
            "authorized": False,
            "pre_action_identity_verified": False,
            "receipt_id": "<native action receipt id>",
            "target_identity": {
                "item_id": None,
                "root_id": "<root UUID>",
                "stable_file_id": "<device/inode identity>",
                "relative_path": "<session-relative path>",
            },
            "cleanup_complete": False,
        },
        "recovery": {
            "permission_denial_observed": False,
            "permission_recovered": False,
            "unavailable_root_observed": False,
            "unavailable_root_recovered": False,
        },
        "appearance": {
            "live_propagation_verified": False,
            "persisted": False,
            "restart_restored": False,
            "settings_schema": APPEARANCE_SCHEMA,
            "settings_sha256": "<task appearance.json SHA-256>",
        },
        "restart": {"search_identity_survived": False, "settings_survived": False},
        "fsevents": {"native_delivery_verified": False, "dropped_history_reconciled": False},
        "quick_look": {"verified": False},
        "lifecycle": {
            "second_launch_reused_existing_process": False,
            "explicit_quit_clean": False,
        },
        "distribution": {
            "developer_id_signature_verified": False,
            "hardened_runtime": False,
            "notarization_accepted": False,
            "staple_valid": False,
            "gatekeeper_accepted": False,
        },
    }


def run_window_fixture_focus(session: Path) -> dict[str, Any]:
    """Compile/run the repo-owned AppKit focus fixture and verify it via AX."""

    binary = session / "WindowFixture"
    status_path = session / "window-fixture-status.json"
    compile_argv = [
        "/usr/bin/xcrun",
        "clang",
        "-fobjc-arc",
        "-framework",
        "AppKit",
        "-framework",
        "Foundation",
        str(WINDOW_FIXTURE_SOURCE),
        "-o",
        str(binary),
    ]
    started = utc_stamp()
    tick = time.monotonic()
    compiled = subprocess.run(
        compile_argv,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
    )
    compile_record = {
        "argv": command_record(compile_argv),
        "started_utc": started,
        "ended_utc": utc_stamp(),
        "elapsed_ms": round((time.monotonic() - tick) * 1000.0, 3),
        "exit_code": compiled.returncode,
        "stderr_tail": _sanitize_text(compiled.stderr),
    }
    if compiled.returncode != 0:
        raise AcceptanceError("window_fixture_build_failed", "WindowFixture failed to compile on the real Mac.")

    process = subprocess.Popen(
        [str(binary), "--status", str(status_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    launch_record = {
        "argv": command_record([str(binary), "--status", str(status_path)]),
        "started_utc": utc_stamp(),
        "pid": process.pid,
    }
    try:
        deadline = time.monotonic() + 10.0
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if status_path.is_file():
                try:
                    loaded = json.loads(status_path.read_text(encoding="utf-8"))
                    status = loaded if isinstance(loaded, dict) else {}
                except (OSError, json.JSONDecodeError):
                    status = {}
            if status.get("state") == "ready":
                break
            if process.poll() is not None:
                error = process.stderr.read().strip() if process.stderr else ""
                raise AcceptanceError("window_fixture_exited", error or "WindowFixture exited before focus verification.")
            time.sleep(0.05)
        else:
            raise AcceptanceError("window_fixture_timeout", "WindowFixture did not become ready within 10 seconds.")
        if status.get("query_focused") is not True:
            raise AcceptanceError("window_fixture_focus_mismatch", "WindowFixture did not make its query field first responder.")

        ax_argv = [
            "/usr/bin/osascript",
            "-l",
            "JavaScript",
            str(WINDOW_FIXTURE_AX),
            str(process.pid),
        ]
        ax_started = utc_stamp()
        ax_tick = time.monotonic()
        ax = subprocess.run(
            ax_argv,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        ax_record = {
            "argv": command_record(ax_argv),
            "started_utc": ax_started,
            "ended_utc": utc_stamp(),
            "elapsed_ms": round((time.monotonic() - ax_tick) * 1000.0, 3),
            "exit_code": ax.returncode,
            "stderr_tail": _sanitize_text(ax.stderr),
        }
        if ax.returncode != 0:
            raise AcceptanceError("window_fixture_ax_failed", "WindowFixture Accessibility readback failed.")
        try:
            ax_value = json.loads(ax.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceError("window_fixture_ax_invalid", "WindowFixture AX helper returned invalid JSON.") from exc
        if not isinstance(ax_value, dict) or ax_value.get("status") != "ok" or ax_value.get("query_focused") is not True:
            outcome = ax_value.get("status") if isinstance(ax_value, dict) else "invalid"
            raise AcceptanceError("window_fixture_focus_mismatch", f"WindowFixture AX focus readback failed: {outcome!r}.")
        return {
            "schema": "listary_window_fixture_acceptance_v1",
            "status": "success",
            "fixture_status": status,
            "ax_readback": ax_value,
            "compile": compile_record,
            "launch": launch_record,
            "ax_command": ax_record,
            "production_focus_claimed": False,
            "purpose": "deterministic AppKit/Accessibility focus-host qualification before production-shell focus proof",
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        launch_record["ended_utc"] = utc_stamp()
        launch_record["exit_code"] = process.returncode


def _load_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(code, f"Could not read JSON receipt {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(code, f"Receipt {path.name} must contain a JSON object.")
    return value


def _require(value: Mapping[str, Any], path: str, expected: Any = True) -> Any:
    cursor: Any = value
    for part in path.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            raise AcceptanceError("native_receipt_incomplete", f"Native receipt is missing {path}.")
        cursor = cursor[part]
    if expected is not None and (type(cursor) is not type(expected) or cursor != expected):
        raise AcceptanceError(
            "native_receipt_incomplete",
            f"Native receipt field {path} must be {expected!r}; observed {cursor!r}.",
        )
    return cursor


def validate_native_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_appearance_sha256: str | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    if receipt.get("schema") != NATIVE_SCHEMA:
        raise AcceptanceError("native_receipt_schema", f"Expected native receipt schema {NATIVE_SCHEMA}.")
    _require(receipt, "status", "success")
    _require(receipt, "real_mac_executed", True)
    _require(receipt, "platform", "macOS")
    _require(receipt, "production_appkit_shell", True)
    if expected_session_id is not None and receipt.get("session_id") != expected_session_id:
        raise AcceptanceError("native_receipt_incomplete", "Native receipt session_id does not match the coordinator session.")
    _require(receipt, "startup.started", True)
    _require(receipt, "startup.shared_settings_schema", APPEARANCE_SCHEMA)
    _require(receipt, "startup.restored_profile", DEFAULT_PROFILE)
    _require(receipt, "search.exact_path_verified", True)
    _require(receipt, "search.finder_alias_verified", True)
    _require(receipt, "search.same_identity_move_verified", True)
    _require(receipt, "search.old_path_absent_after_move", True)
    first_result = _require(receipt, "search.first_result_latency_ms", None)
    if type(first_result) not in (int, float) or not 0 <= first_result <= sys.float_info.max:
        raise AcceptanceError("native_receipt_incomplete", "search.first_result_latency_ms must be a finite nonnegative number.")
    _require(receipt, "interaction.query_focus_verified", True)
    _require(receipt, "interaction.keyboard_navigation_verified", True)
    _require(receipt, "interaction.prior_focus_restored", True)
    _require(receipt, "file_dialog.open_verified", True)
    _require(receipt, "file_dialog.save_verified", True)
    _require(receipt, "file_action.authorized", True)
    _require(receipt, "file_action.pre_action_identity_verified", True)
    action_receipt_id = _require(receipt, "file_action.receipt_id", None)
    if not isinstance(action_receipt_id, str) or not action_receipt_id.strip():
        raise AcceptanceError("native_receipt_incomplete", "file_action.receipt_id must be a non-empty string.")
    target_identity = _require(receipt, "file_action.target_identity", None)
    if not isinstance(target_identity, Mapping):
        raise AcceptanceError("native_receipt_incomplete", "file_action.target_identity must be an object.")
    for key in ("item_id", "root_id", "stable_file_id", "relative_path"):
        if target_identity.get(key) in (None, ""):
            raise AcceptanceError("native_receipt_incomplete", f"file_action.target_identity.{key} is required.")
    if _require(receipt, "file_action.kind", None) not in {"open", "reveal", "trash_restore_fixture"}:
        raise AcceptanceError("native_receipt_incomplete", "file_action.kind is not an admitted safe acceptance action.")
    _require(receipt, "file_action.cleanup_complete", True)
    _require(receipt, "recovery.permission_denial_observed", True)
    _require(receipt, "recovery.permission_recovered", True)
    _require(receipt, "recovery.unavailable_root_observed", True)
    _require(receipt, "recovery.unavailable_root_recovered", True)
    _require(receipt, "appearance.live_propagation_verified", True)
    _require(receipt, "appearance.persisted", True)
    _require(receipt, "appearance.restart_restored", True)
    _require(receipt, "appearance.settings_schema", APPEARANCE_SCHEMA)
    if expected_appearance_sha256 is not None:
        observed = str(_require(receipt, "appearance.settings_sha256", None)).upper()
        if observed != expected_appearance_sha256.upper():
            raise AcceptanceError("native_receipt_incomplete", "Native appearance receipt does not bind the acceptance settings bytes.")
    _require(receipt, "restart.search_identity_survived", True)
    _require(receipt, "restart.settings_survived", True)
    _require(receipt, "fsevents.native_delivery_verified", True)
    _require(receipt, "fsevents.dropped_history_reconciled", True)
    _require(receipt, "quick_look.verified", True)
    _require(receipt, "lifecycle.second_launch_reused_existing_process", True)
    _require(receipt, "lifecycle.explicit_quit_clean", True)
    _require(receipt, "distribution.developer_id_signature_verified", True)
    _require(receipt, "distribution.hardened_runtime", True)
    _require(receipt, "distribution.notarization_accepted", True)
    _require(receipt, "distribution.staple_valid", True)
    _require(receipt, "distribution.gatekeeper_accepted", True)
    return {
        "schema": NATIVE_SCHEMA,
        "validated": True,
        "first_result_latency_ms": float(first_result),
        "file_action_kind": receipt["file_action"]["kind"],
        "file_action_receipt_id": action_receipt_id,
        "shared_appearance_schema": APPEARANCE_SCHEMA,
        "finder_alias_is_native_evidence": True,
    }


def validate_opus_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if receipt.get("schema") != OPUS_SCHEMA:
        raise AcceptanceError("opus_receipt_schema", f"Expected Directory Opus Mac receipt schema {OPUS_SCHEMA}.")
    required = {
        "platform": "macOS",
        "architecture": "arm64",
        "package_hashes_verified": True,
        "native_architecture_verified": True,
        "core_suite_executed": True,
        "core_suite_passed": True,
        "all_core_checks_verified": True,
        "headless_ui_self_test_passed": True,
        "desktop_launch_request_accepted": True,
        "automatic_verification_completed": True,
        "exit_code": 0,
    }
    for key, expected in required.items():
        observed = receipt.get(key)
        if type(observed) is not type(expected) or observed != expected:
            raise AcceptanceError(
                "opus_receipt_incomplete",
                f"Directory Opus Mac receipt {key} must be {expected!r}; observed {receipt.get(key)!r}.",
            )
    if receipt.get("complete_directory_opus_parity_verified") is not False:
        raise AcceptanceError(
            "opus_receipt_overclaim",
            "Automatic Directory Opus Mac acceptance must not claim complete original Directory Opus parity.",
        )
    if receipt.get("manual_desktop_acceptance_complete") is not False:
        raise AcceptanceError(
            "opus_receipt_overclaim",
            "Automatic Directory Opus Mac acceptance must report manual desktop acceptance as incomplete.",
        )
    return {
        "schema": OPUS_SCHEMA,
        "validated_portability_boundary": True,
        "desktop_launch_request_accepted": True,
        "manual_desktop_acceptance_complete": False,
        "complete_directory_opus_parity_verified": False,
        "claim": "independent Directory Opus Mac app executed; Windows Directory Opus native execution is not claimed",
    }


def run_native_product_acceptance(
    runner: Path,
    *,
    session: Path,
    state_dir: Path,
    fixture_root: Path,
    appearance_path: Path,
    appearance_sha256: str,
    session_id: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt_path = session / "native-app.json"
    argv = [
        str(runner),
        "--acceptance-session",
        str(session),
        "--state-dir",
        str(state_dir),
        "--fixture-root",
        str(fixture_root),
        "--appearance-settings",
        str(appearance_path),
        "--receipt",
        str(receipt_path),
        "--json",
    ]
    value, record = _run_json(argv, cwd=REPO_ROOT, timeout=timeout)
    # The runner may print the same receipt it atomically writes; the file is
    # authoritative so a truncated console cannot become acceptance evidence.
    disk_value = _load_json(receipt_path, code="native_receipt_missing")
    if value != disk_value:
        raise AcceptanceError("native_receipt_mismatch", "Native runner stdout and persisted receipt disagree.")
    validated = validate_native_receipt(
        disk_value,
        expected_appearance_sha256=appearance_sha256,
        expected_session_id=session_id,
    )
    return validated, record


def opus_package_identity(package: Path) -> tuple[str, str, str]:
    verifier = package / "Verify-Mac.command"
    checksums = package / "SHA256SUMS"
    manifests = [package / name for name in ("release-manifest.json", "unsigned-build-manifest.json")]
    present = [path for path in manifests if path.exists() or path.is_symlink()]
    if len(present) != 1:
        raise AcceptanceError("opus_package_invalid", "Expected one Directory Opus build manifest in the extracted or signed package.")
    manifest = present[0]
    if any(not path.is_file() or path.is_symlink() for path in (verifier, manifest, checksums)):
        raise AcceptanceError("opus_package_invalid", "Keep Verify-Mac.command, the build manifest, and SHA256SUMS in the Opus package.")
    return manifest.name, sha256_file(manifest), sha256_file(checksums)


def preflight_opus_package(package: Path) -> tuple[str, str, str]:
    package = package.resolve()
    identity = opus_package_identity(package)
    manifest = _load_json(package / identity[0], code="opus_package_invalid")
    if (manifest.get("schema") != "opusmac_release_v1" or manifest.get("rid") != "osx-arm64"
            or manifest.get("self_contained") is not True):
        raise AcceptanceError("opus_package_invalid", "Expected a self-contained osx-arm64 Directory Opus release.")
    payload = "Directory Opus Mac.app/Contents/MacOS/"
    required = {identity[0], "Verify-Mac.command", "Directory Opus Mac.app/Contents/Info.plist",
                payload + "OpusMac", payload + "OpusMac.Checks"}
    if identity[0] == "unsigned-build-manifest.json":
        required.add("signing-receipt.json")
    seen: set[str] = set()
    for line in (package / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if match is None:
            raise AcceptanceError("opus_package_invalid", "The Opus SHA256SUMS inventory contains a malformed line.")
        expected, relative = match.groups()
        parts = relative.split("/")
        if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts) or relative in seen:
            raise AcceptanceError("opus_package_invalid", f"Unsafe or duplicate Opus checksum entry: {relative!r}.")
        seen.add(relative)
        path = package.joinpath(*parts)
        if not path.is_file() or path.resolve() != path or sha256_file(path) != expected.upper():
            raise AcceptanceError("opus_package_invalid", f"Missing, linked, or modified Opus package file: {relative}.")
    if not required.issubset(seen):
        raise AcceptanceError("opus_package_invalid", "Opus SHA256SUMS is missing required package files.")
    for directory, folders, files in os.walk(package):
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in folders + files):
            raise AcceptanceError("opus_package_invalid", "The Opus package contains a symbolic link.")
        for name in files:
            relative = (parent / name).relative_to(package).as_posix()
            if relative == "SHA256SUMS" or (name == ".DS_Store" and not relative.startswith("Directory Opus Mac.app/")):
                continue
            if relative not in seen:
                raise AcceptanceError("opus_package_invalid", f"Unexpected Opus package file: {relative}.")
    if sys.platform == "darwin" and any(not os.access(package / path, os.X_OK)
            for path in ("Verify-Mac.command", payload + "OpusMac", payload + "OpusMac.Checks")):
        raise AcceptanceError("opus_package_invalid", "Opus executable permissions are missing. Extract the archive again.")
    if opus_package_identity(package) != identity:
        raise AcceptanceError("opus_package_changed", "The Opus manifest or checksum inventory changed during preflight.")
    return identity


def run_opus_portability_acceptance(
    package: Path, *, timeout: int, expected_package: tuple[str, str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    verifier = package / "Verify-Mac.command"
    identity = opus_package_identity(package)
    if expected_package is not None and identity != expected_package:
        raise AcceptanceError("opus_package_changed", "The Directory Opus package changed after preflight.")
    verification_parent = (Path.home() / "Library/Logs/OpusMac/Verification").resolve()
    prior_runs = {path.name for path in verification_parent.iterdir()} if verification_parent.is_dir() else set()
    argv = [str(verifier), "--launch"]
    started = utc_stamp()
    tick = time.monotonic()
    completed = subprocess.run(
        argv,
        cwd=package,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    record = {
        "argv": command_record(argv),
        "started_utc": started,
        "ended_utc": utc_stamp(),
        "elapsed_ms": round((time.monotonic() - tick) * 1000.0, 3),
        "exit_code": completed.returncode,
        "stdout_tail": _sanitize_text(completed.stdout),
        "stderr_tail": _sanitize_text(completed.stderr),
    }
    if completed.returncode != 0:
        raise AcceptanceError("opus_verifier_failed", f"Directory Opus Mac verifier exited {completed.returncode}.")
    matches = re.findall(r"^Verification receipt and retained disposable fixtures: (.+)$", completed.stdout, re.MULTILINE)
    if len(matches) != 1:
        raise AcceptanceError("opus_receipt_missing", "Directory Opus Mac verifier did not report its retained receipt folder.")
    run_root = Path(matches[0].strip())
    if (not run_root.is_absolute() or run_root.parent.resolve() != verification_parent
            or not re.fullmatch(r"run-[A-Za-z0-9]{8}", run_root.name)
            or run_root.name in prior_runs or run_root.is_symlink() or not run_root.is_dir()):
        raise AcceptanceError("opus_receipt_not_fresh", "Expected a new physical verification folder created by this Opus verifier run.")
    receipt_path = run_root / "verification.json"
    if receipt_path.is_symlink():
        raise AcceptanceError("opus_receipt_not_fresh", "Directory Opus Mac reported a linked verification receipt.")
    receipt = _load_json(receipt_path, code="opus_receipt_missing")
    if opus_package_identity(package) != identity:
        raise AcceptanceError("opus_package_changed", "The Directory Opus Mac package manifest or checksum inventory changed during verification.")
    validated = validate_opus_receipt(receipt)
    validated["package_manifest"], validated["package_manifest_sha256"], validated["package_checksums_sha256"] = identity
    validated["verification_receipt_sha256"] = sha256_file(receipt_path)
    return validated, record, receipt_path


def _run_json(argv: Sequence[str], *, cwd: Path, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    started = utc_stamp()
    tick = time.monotonic()
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    ended = utc_stamp()
    record = {
        "argv": command_record(argv),
        "started_utc": started,
        "ended_utc": ended,
        "elapsed_ms": round((time.monotonic() - tick) * 1000.0, 3),
        "exit_code": completed.returncode,
        "stderr_tail": _sanitize_text(completed.stderr),
    }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("command_receipt_invalid", f"Command did not return JSON: {' '.join(record['argv'])}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError("command_receipt_invalid", "Command receipt must be a JSON object.")
    if completed.returncode != 0:
        raise AcceptanceError(
            "command_failed",
            f"Command failed with exit {completed.returncode}: {' '.join(record['argv'])}; outcome={value.get('outcome')!r}",
        )
    return value, record


def _load_quick_switch_module():
    spec = importlib.util.spec_from_file_location("physical_mac_quick_switch", QUICK_SWITCH)
    if spec is None or spec.loader is None:
        raise AcceptanceError("quick_switch_import_failed", "Could not load incumbent Quick Switch acceptance owner.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_disposable_state(core_library: Path, session: Path) -> dict[str, Any]:
    quick = _load_quick_switch_module()
    state = session / "state"
    root = session / "fixture"
    target_folder = root / "Résumé βeta"
    target_folder.mkdir(parents=True, exist_ok=False)
    exact = target_folder / "exact-path-probe.txt"
    exact.write_text("disposable physical Mac acceptance fixture\n", encoding="utf-8")
    moved_old = target_folder / "move-before.txt"
    moved_new = target_folder / "move-after.txt"
    moved_old.write_text("same identity move fixture\n", encoding="utf-8")
    hardlink = target_folder / "hard-link-alias.txt"
    os.link(exact, hardlink)
    with quick.CoreAbiClient(core_library, state) as core:
        root_id = core.add_root(root)
        core.reconcile_root(root_id, request_id=7001)
        search_started = time.perf_counter_ns()
        response = core.search(
            quick._search_request("exact-path-probe", root_ids=[root_id], kinds=["file"]),
            request_id=7002,
        )
        search_wall_ms = (time.perf_counter_ns() - search_started) / 1_000_000.0
        exact_result = quick._find_result_path(response, exact)
        item_id = int(exact_result["item"]["id"])
        stable_file_id = exact_result["item"].get("stable_file_id")
        moved_old_response = core.search(
            quick._search_request("move-before", root_ids=[root_id], kinds=["file"]),
            request_id=7003,
        )
        moved_before = quick._find_result_path(moved_old_response, moved_old)
        moved_item_id = int(moved_before["item"]["id"])
        moved_stable = moved_before["item"].get("stable_file_id")
        moved_old.rename(moved_new)
        core.reconcile_paths(root_id, [moved_old, moved_new], request_id=7004)
        moved_live = core.get_item(moved_item_id)
        if not quick.paths_match(str(moved_new), str(moved_live.get("path") or "")):
            raise AcceptanceError("mac_fixture_move_identity_failed", "Shared core did not retain the moved fixture identity.")
        if moved_stable and moved_live.get("stable_file_id") != moved_stable:
            raise AcceptanceError("mac_fixture_move_identity_failed", "Moved fixture stable identity changed unexpectedly.")
        old_response = core.search(
            quick._search_request("move-before", root_ids=[root_id], kinds=["file"]),
            request_id=7005,
        )
        if old_response.get("results"):
            raise AcceptanceError("mac_fixture_move_stale", "Old moved path remained searchable in the acceptance fixture.")
        core.flush()
    return {
        "state_dir": state,
        "fixture_root": root,
        "root_id": root_id,
        "exact_relative_path": exact.relative_to(root).as_posix(),
        "exact_item_id": item_id,
        "exact_stable_file_id": stable_file_id,
        "moved_relative_path": moved_new.relative_to(root).as_posix(),
        "moved_item_id": moved_item_id,
        "moved_stable_file_id": moved_live.get("stable_file_id"),
        "hard_link_relative_path": hardlink.relative_to(root).as_posix(),
        "shared_core_first_result_wall_ms": round(search_wall_ms, 3),
        "finder_alias_verified": False,
        "finder_alias_note": "hard-link/symlink fixtures are not Finder alias proof; native app receipt must carry Finder alias evidence",
    }


def _write_appearance_fixture(session: Path) -> tuple[Path, str]:
    settings_root = session / "shared-settings"
    settings_root.mkdir(parents=True, exist_ok=False)
    path = settings_root / "appearance.json"
    payload = {"profile": DEFAULT_PROFILE, "schema": APPEARANCE_SCHEMA}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, sha256_file(path)


def host_identity() -> dict[str, Any]:
    commands = {
        "sw_vers": ["sw_vers"],
        "uname": ["uname", "-m"],
        "xcodebuild": ["xcodebuild", "-version"],
        "swift": ["swift", "--version"],
        "rustc": ["rustc", "--version"],
        "cargo": ["cargo", "--version"],
    }
    rows: dict[str, Any] = {}
    for name, argv in commands.items():
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
        rows[name] = {"argv": argv, "exit_code": completed.returncode, "output": completed.stdout.strip()[:2000]}
        if completed.returncode != 0:
            raise AcceptanceError("host_identity_failed", f"Required host identity command failed: {' '.join(argv)}")
    return rows


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise AcceptanceError("non_macos_host", "Physical acceptance execution requires a real macOS host.")
    if not native_shell_inventory()["production_shell_source_present"]:
        raise AcceptanceError(
            "production_appkit_shell_missing",
            "No production Swift/AppKit shell is present. Create/compile/link it on the real Mac before the one-shot acceptance run.",
        )
    session_parent = args.session_parent.expanduser()
    session_parent.mkdir(parents=True, exist_ok=True)
    session_id = f"physical-mac-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    session = session_parent / session_id
    session.mkdir(mode=0o700)
    started = utc_stamp()
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "session_id": session_id,
        "started_utc": started,
        "status": "failed",
        "real_mac_executed": True,
        "acceptance_state_before": "5/6 agent-reported verified",
        "production_data_used": False,
        "installed_windows_listary_touched": False,
        "session_artifacts": {},
        "commands": [],
    }
    session_receipt = session / "physical-mac-receipt.json"
    try:
        opus_package = args.opus_package.expanduser().resolve()
        opus_identity = preflight_opus_package(opus_package)
        receipt["opus_package_preflight"] = dict(zip(("manifest", "manifest_sha256", "checksums_sha256"), opus_identity))
        appearance_path, appearance_sha = _write_appearance_fixture(session)
        receipt["appearance_fixture"] = {
            "schema": APPEARANCE_SCHEMA,
            "profile": DEFAULT_PROFILE,
            "sha256": appearance_sha,
            "relative_path": appearance_path.relative_to(session).as_posix(),
        }
        receipt["host"] = host_identity()
        receipt["window_fixture"] = run_window_fixture_focus(session)

        env = dict(os.environ)
        env["SS_WORKFLOW_SETTINGS_ROOT"] = str(appearance_path.parent)
        # Bootstrap remains authoritative for source-currentness, toolchain,
        # host-filesystem smoke, publication, and privacy checks.
        bootstrap_argv = [
            sys.executable,
            str(BOOTSTRAP),
            "--output",
            str(session / "ListaryCore.xcframework"),
            "--json",
        ]
        boot_started = utc_stamp()
        tick = time.monotonic()
        completed = subprocess.run(
            bootstrap_argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.bootstrap_timeout,
            check=False,
        )
        receipt["commands"].append({
            "argv": command_record(bootstrap_argv),
            "started_utc": boot_started,
            "ended_utc": utc_stamp(),
            "elapsed_ms": round((time.monotonic() - tick) * 1000.0, 3),
            "exit_code": completed.returncode,
            "stderr_tail": _sanitize_text(completed.stderr),
        })
        try:
            bootstrap_receipt = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceError("bootstrap_receipt_invalid", "Bridge bootstrap did not emit JSON.") from exc
        receipt["bootstrap"] = bootstrap_receipt
        if completed.returncode != 0 or bootstrap_receipt.get("status") != "success":
            raise AcceptanceError("bootstrap_failed", f"Bridge bootstrap failed: {bootstrap_receipt.get('outcome')!r}")

        quick = _load_quick_switch_module()
        core_library = quick.resolve_core_library(None, build_if_missing=False)
        fixture = _seed_disposable_state(core_library, session)
        receipt["fixture_contract"] = {
            key: value for key, value in fixture.items() if not key.endswith("_dir") and key != "fixture_root"
        }
        for mode in ("open", "save"):
            target = session / f"quick-switch-{mode}.json"
            argv = [
                sys.executable,
                str(QUICK_SWITCH),
                "--state-dir",
                str(fixture["state_dir"]),
                "--core-lib",
                str(core_library),
                "--root-id",
                str(fixture["root_id"]),
                "--fixture",
                mode,
                "--fixture-initial",
                str(fixture["fixture_root"]),
                "--receipt",
                str(target),
                "--json",
            ]
            value, record = _run_json(argv, cwd=REPO_ROOT, timeout=args.quick_switch_timeout)
            receipt["commands"].append(record)
            if value.get("status") != "success" or value.get("verified_readback") is not True:
                raise AcceptanceError("quick_switch_failed", f"Native {mode.title()} panel acceptance failed: {value.get('outcome')!r}")
            receipt[f"quick_switch_{mode}"] = value
            receipt["session_artifacts"][f"quick_switch_{mode}"] = target.relative_to(session).as_posix()

        native_runner = args.native_runner.expanduser().resolve(strict=True)
        receipt["native_app"], native_command = run_native_product_acceptance(
            native_runner,
            session=session,
            state_dir=fixture["state_dir"],
            fixture_root=fixture["fixture_root"],
            appearance_path=appearance_path,
            appearance_sha256=appearance_sha,
            session_id=session_id,
            timeout=args.native_timeout,
        )
        receipt["commands"].append(native_command)
        receipt["session_artifacts"]["native_app"] = "native-app.json"
        opus_validation, opus_command, opus_receipt_path = run_opus_portability_acceptance(
            opus_package,
            timeout=args.opus_timeout,
            expected_package=opus_identity,
        )
        receipt["directory_opus_portability"] = opus_validation
        receipt["commands"].append(opus_command)
        receipt["directory_opus_portability"]["receipt_path"] = _repo_display(opus_receipt_path)
        receipt["status"] = "success"
        receipt["outcome"] = "physical_mac_acceptance_green"
        receipt["acceptance_may_advance"] = True
        receipt["ended_utc"] = utc_stamp()
        receipt["session_artifacts"]["session_root"] = "."
        receipt["session_artifacts"]["coordinator_receipt"] = session_receipt.name
        atomic_write_json(session_receipt, receipt)
        return receipt
    except BaseException as exc:
        receipt["status"] = "blocked" if isinstance(exc, AcceptanceError) else "failed"
        receipt["outcome"] = exc.code if isinstance(exc, AcceptanceError) else "unexpected_failure"
        receipt["error"] = str(exc)
        receipt["acceptance_may_advance"] = False
        receipt["ended_utc"] = utc_stamp()
        receipt["session_artifacts"]["session_root"] = "."
        receipt["session_artifacts"]["coordinator_receipt"] = session_receipt.name
        atomic_write_json(session_receipt, receipt)
        raise


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Emit the bounded acceptance plan without touching the Mac or user state.")
    parser.add_argument(
        "--native-runner",
        type=Path,
        help=(
            "Real production Listary Mac acceptance executable. It must consume the coordinator's session/state/appearance paths "
            f"and atomically emit schema {NATIVE_SCHEMA}."
        ),
    )
    parser.add_argument(
        "--opus-package",
        type=Path,
        help="Extracted independent Directory Opus Mac package containing Verify-Mac.command.",
    )
    parser.add_argument(
        "--session-parent",
        type=Path,
        default=Path.home() / "Library" / "Logs" / "ListaryMac" / "PhysicalAcceptance",
        help="Parent for disposable real-Mac fixtures/receipts.",
    )
    parser.add_argument("--bootstrap-timeout", type=int, default=1800)
    parser.add_argument("--quick-switch-timeout", type=int, default=180)
    parser.add_argument("--native-timeout", type=int, default=900)
    parser.add_argument("--opus-timeout", type=int, default=600)
    parser.add_argument("--receipt", type=Path, help="Optional JSON result path.")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.plan:
        result = build_plan()
        code = 0
    else:
        if args.native_runner is None or args.opus_package is None:
            result = {
                "schema": SCHEMA,
                "status": "blocked",
                "outcome": "required_external_receipt_missing",
                "real_mac_executed": False,
                "acceptance_may_advance": False,
                "error": "--native-runner and --opus-package are required for a completion attempt.",
            }
            code = 2
        else:
            try:
                result = execute(args)
                code = 0
            except AcceptanceError as exc:
                result = {
                    "schema": SCHEMA,
                    "status": "blocked" if exc.code in {
                        "non_macos_host",
                        "production_appkit_shell_missing",
                        "native_receipt_missing",
                        "opus_receipt_missing",
                    } else "failed",
                    "outcome": exc.code,
                    "real_mac_executed": False if exc.code == "non_macos_host" else sys.platform == "darwin",
                    "acceptance_may_advance": False,
                    "error": str(exc),
                }
                code = 2 if result["status"] == "blocked" else 1
    if args.receipt is not None:
        atomic_write_json(args.receipt, result)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{result.get('status')}: {result.get('outcome', result.get('schema'))}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

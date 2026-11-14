#!/usr/bin/env python3
"""Operator bridge to the canonical Listary Mac Port owner.

This module deliberately owns no index, watcher, ranker, history store, or
macOS Accessibility behavior. It keeps Spiffy Mac Automation's restore and
acceptance workflow exportable while delegating all Listary state/search work to
the canonical nested Listary Mac Port project and its ABI-v8/schema-v5 contracts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PACKAGE_ROOT / "config" / "listary_integration.json"
CONFIG_SCHEMA = "spiffy_listary_integration_v1"
RECEIPT_SCHEMA = "spiffy_listary_owner_bridge_v1"


class ListaryIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ListaryIntegrationError("config_invalid", f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ListaryIntegrationError("config_invalid", f"{path} must contain a JSON object.")
    return value


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = _load_json(path)
    if config.get("schema") != CONFIG_SCHEMA:
        raise ListaryIntegrationError(
            "config_schema_mismatch",
            f"Expected {CONFIG_SCHEMA}, got {config.get('schema')!r}.",
        )
    expected = config.get("expected_contract")
    if not isinstance(expected, dict):
        raise ListaryIntegrationError("config_invalid", "expected_contract is required.")
    for key in ("api_version", "portable_schema_version", "public_symbol_count"):
        if not isinstance(expected.get(key), int) or int(expected[key]) <= 0:
            raise ListaryIntegrationError("config_invalid", f"expected_contract.{key} must be a positive integer.")
    return config


def _candidate_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PACKAGE_ROOT / path
    return path.resolve(strict=False)


def discover_listary_root(
    explicit: Path | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> Path:
    cfg = dict(config or load_config())
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser().resolve(strict=False))
    env_name = str(cfg.get("workspace_env") or "").strip()
    if env_name:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            candidates.append(Path(env_value).expanduser().resolve(strict=False))
    for value in cfg.get("workspace_candidates", []):
        if isinstance(value, str) and value.strip():
            candidates.append(_candidate_path(value.strip()))

    checked: list[str] = []
    for root in candidates:
        checked.append(str(root))
        if (
            (root / "AGENTS.md").is_file()
            and (root / "rust" / "listary-ffi" / "include" / "listary_core.h").is_file()
            and (root / "rust" / "listary-core" / "src" / "portable.rs").is_file()
            and (root / "tools" / "quick_switch_acceptance.py").is_file()
            and (root / "tools" / "macos_bridge_bootstrap.py").is_file()
        ):
            return root
    raise ListaryIntegrationError(
        "listary_workspace_missing",
        "Could not locate the canonical Listary Mac Port workspace. Checked: " + ", ".join(checked),
    )


def _header_contract(header_path: Path) -> tuple[int, int]:
    text = header_path.read_text(encoding="utf-8")
    version = re.search(
        r"^\s*#define\s+LISTARY_CORE_API_VERSION\s+(\d+)[uUlL]*\s*$",
        text,
        flags=re.MULTILINE,
    )
    if version is None:
        raise ListaryIntegrationError("abi_version_missing", f"{header_path} has no API version declaration.")
    symbols = set(
        re.findall(
            r"^\s*(?:uint32_t|int32_t|void)\s+(listary_[A-Za-z0-9_]+)\s*\(",
            text,
            flags=re.MULTILINE,
        )
    )
    return int(version.group(1)), len(symbols)


def _portable_schema_version(portable_path: Path) -> int:
    text = portable_path.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*pub\s+const\s+PORTABLE_CONFIG_SCHEMA_VERSION\s*:\s*u32\s*=\s*(\d+)\s*;",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        raise ListaryIntegrationError(
            "portable_schema_missing",
            f"{portable_path} has no PORTABLE_CONFIG_SCHEMA_VERSION declaration.",
        )
    return int(match.group(1))


def source_contract(root: Path, *, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or load_config())
    header = root / "rust" / "listary-ffi" / "include" / "listary_core.h"
    portable = root / "rust" / "listary-core" / "src" / "portable.rs"
    api_version, symbol_count = _header_contract(header)
    portable_version = _portable_schema_version(portable)
    expected = cfg["expected_contract"]
    actual = {
        "api_version": api_version,
        "portable_schema_version": portable_version,
        "public_symbol_count": symbol_count,
    }
    if actual != expected:
        raise ListaryIntegrationError(
            "source_contract_drift",
            f"Listary source contract drifted: expected {expected}, observed {actual}.",
        )
    return {
        **actual,
        "header": str(header),
        "portable_owner": str(portable),
    }


def surface_contract(config: Mapping[str, Any], *, state_dir: str | None = None) -> dict[str, Any]:
    effective_state = state_dir or str(config["default_mac_state_dir"])
    rules = dict(config["ownership_rules"])
    if not all(
        rules.get(key) is expected
        for key, expected in {
            "single_state_directory": True,
            "single_index_owner": True,
            "single_filesystem_watcher_owner": True,
            "spiffy_creates_index": False,
            "spiffy_creates_watcher": False,
            "spiffy_reranks_results": False,
        }.items()
    ):
        raise ListaryIntegrationError("ownership_contract_invalid", "Listary ownership rules allow a duplicate owner.")
    surfaces: dict[str, Any] = {}
    for name, raw in config["surfaces"].items():
        row = dict(raw)
        row["state_dir"] = effective_state
        row["catalog_owner"] = config["canonical_owners"]["catalog"]
        row["query_owner"] = config["canonical_owners"]["query_ranking_history"]
        surfaces[str(name)] = row
    return {
        "state_dir": effective_state,
        "canonical_owners": dict(config["canonical_owners"]),
        "ownership_rules": rules,
        "surfaces": surfaces,
    }


def _portable_binary(root: Path) -> Path:
    name = "listary-portable-config.exe" if os.name == "nt" else "listary-portable-config"
    return root / "target" / "release" / name


def _portable_command(root: Path, subcommand: str, *args: str) -> list[str]:
    binary = _portable_binary(root)
    if binary.is_file():
        return [str(binary), subcommand, *args]
    return [
        "cargo",
        "run",
        "--quiet",
        "-p",
        "listary-core",
        "--bin",
        "listary-portable-config",
        "--",
        subcommand,
        *args,
    ]


def _core_library(root: Path) -> Path:
    if sys.platform == "win32":
        name = "listary_ffi.dll"
    elif sys.platform == "darwin":
        name = "liblistary_ffi.dylib"
    else:
        name = "liblistary_ffi.so"
    return root / "target" / "release" / name


def _parse_json_stdout(stdout: str, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ListaryIntegrationError(code, f"Owner returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ListaryIntegrationError(code, "Owner JSON response must be an object.")
    return value


def _run_json_command(command: Sequence[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            value = _parse_json_stdout(completed.stdout.strip(), code="owner_response_invalid")
        except ListaryIntegrationError:
            value = None
    if completed.returncode != 0:
        detail = ""
        if value is not None:
            detail = str(value.get("error") or value.get("outcome") or "")
        if not detail:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise ListaryIntegrationError("owner_command_failed", detail)
    if value is None:
        raise ListaryIntegrationError("owner_response_invalid", "Owner command succeeded without a JSON object response.")
    return value


def _validate_contract_receipt(value: Mapping[str, Any], expected_api: int) -> None:
    source = value.get("source_contract")
    if isinstance(source, Mapping) and int(source.get("api_version") or 0) != expected_api:
        raise ListaryIntegrationError("owner_receipt_drift", "Bootstrap receipt reports the wrong ABI version.")
    bridge = value.get("bridge_preflight")
    if isinstance(bridge, Mapping):
        header = int(bridge.get("header_api_version") or bridge.get("expected_api_version") or 0)
        runtime = int(bridge.get("library_api_version") or bridge.get("runtime_api_version") or header)
        if header and (header != expected_api or runtime != expected_api):
            raise ListaryIntegrationError("owner_receipt_drift", "Bridge preflight ABI does not match the tracked contract.")


def bootstrap(
    root: Path,
    *,
    plan_only: bool,
    portable_config: Path | None,
    state_dir: Path | None,
    path_maps: Sequence[str],
    expected_api: int,
) -> dict[str, Any]:
    command = [sys.executable, str(root / "tools" / "macos_bridge_bootstrap.py")]
    if plan_only:
        command.append("--plan")
    if portable_config is not None:
        if state_dir is None:
            raise ListaryIntegrationError("state_dir_required", "--state-dir is required with --portable-config.")
        command.extend(["--portable-config", str(portable_config), "--state-dir", str(state_dir)])
        for mapping in path_maps:
            command.extend(["--path-map", mapping])
    command.append("--json")
    receipt = _run_json_command(command, cwd=root)
    _validate_contract_receipt(receipt, expected_api)
    if plan_only and receipt.get("real_mac_executed") is not False:
        raise ListaryIntegrationError("owner_receipt_invalid", "Static bootstrap plan must not claim real Mac execution.")
    return receipt


def portable_export(root: Path, *, state_dir: Path, output: Path) -> dict[str, Any]:
    target = output.expanduser().resolve(strict=False)
    if target.exists():
        raise ListaryIntegrationError(
            "portable_output_exists",
            f"Refusing to replace existing portable artifact: {target}",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt = _run_json_command(
        _portable_command(root, "export", "--state-dir", str(state_dir), "--output", str(target)),
        cwd=root,
    )
    if receipt.get("outcome") != "portable_config_exported" or receipt.get("copied_catalog") is not False:
        raise ListaryIntegrationError("portable_export_invalid", "Canonical portable export receipt is incomplete.")
    return receipt


def portable_validate(root: Path, *, input_path: Path, expected_schema: int) -> dict[str, Any]:
    receipt = _run_json_command(
        _portable_command(root, "validate", "--input", str(input_path)),
        cwd=root,
    )
    if receipt.get("outcome") != "portable_config_valid":
        raise ListaryIntegrationError("portable_validate_invalid", "Portable owner did not report validation success.")
    if int(receipt.get("schema_version") or 0) != expected_schema:
        raise ListaryIntegrationError(
            "portable_schema_drift",
            f"Portable owner returned schema {receipt.get('schema_version')}; expected {expected_schema}.",
        )
    return receipt


def portable_restore(
    root: Path,
    *,
    state_dir: Path,
    input_path: Path,
    path_maps: Sequence[str],
    target_style: str,
    expected_schema: int,
) -> dict[str, Any]:
    validation = portable_validate(root, input_path=input_path, expected_schema=expected_schema)
    args = [
        "--state-dir",
        str(state_dir),
        "--input",
        str(input_path),
        "--target-style",
        target_style,
    ]
    for mapping in path_maps:
        args.extend(["--map", mapping])
    restore = _run_json_command(_portable_command(root, "restore", *args), cwd=root)
    if restore.get("outcome") != "portable_config_restored":
        raise ListaryIntegrationError("portable_restore_invalid", "Portable owner did not report restore success.")
    return {"validation": validation, "restore": restore}


def quick_switch(
    root: Path,
    *,
    state_dir: Path,
    fixture: str,
    plan_only: bool,
    finder_transfer: bool = False,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(root / "tools" / "quick_switch_acceptance.py"),
        "--state-dir",
        str(state_dir),
        "--fixture",
        fixture,
    ]
    if plan_only:
        command.append("--dry-run")
    if finder_transfer:
        command.append("--finder-transfer")
    if receipt_path is not None:
        command.extend(["--receipt", str(receipt_path)])
    command.append("--json")
    value = _run_json_command(command, cwd=root)
    if plan_only:
        if value.get("status") != "dry_run" or value.get("real_mac_executed") is not False:
            raise ListaryIntegrationError("quick_switch_plan_invalid", "Quick Switch dry-run made an invalid hardware claim.")
    return value


def core_smoke(root: Path, *, library: Path | None = None, expected_api: int) -> dict[str, Any]:
    core_lib = (library or _core_library(root)).expanduser().resolve(strict=False)
    if not core_lib.is_file():
        raise ListaryIntegrationError("core_library_missing", f"Release core library does not exist: {core_lib}")
    command = [
        sys.executable,
        str(root / "tools" / "quick_switch_acceptance.py"),
        "--core-smoke",
        "--core-lib",
        str(core_lib),
        "--no-build-core",
        "--json",
    ]
    receipt = _run_json_command(command, cwd=root)
    _validate_contract_receipt(receipt, expected_api)
    if receipt.get("outcome") != "index_search_reopen_cache_recovery_green":
        raise ListaryIntegrationError("core_smoke_invalid", "Canonical C-ABI core smoke did not reach the recovery-green outcome.")
    return receipt


def _load_acceptance_module(root: Path) -> Any:
    path = root / "tools" / "quick_switch_acceptance.py"
    spec = importlib.util.spec_from_file_location("spiffy_listary_quick_switch_acceptance", path)
    if spec is None or spec.loader is None:
        raise ListaryIntegrationError("acceptance_import_failed", f"Cannot load acceptance owner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree_metrics(root: Path) -> dict[str, int]:
    file_count = 0
    total_bytes = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                file_count += 1
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    continue
    return {"file_count": file_count, "total_bytes": total_bytes}


def _p95_ms(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999999) - 1))
    return round(ordered[index], 3)


def fixture_smoke(
    root: Path,
    *,
    config: Mapping[str, Any],
    library: Path | None = None,
    file_count: int = 5200,
    search_iterations: int = 40,
    growth_iterations: int = 120,
) -> dict[str, Any]:
    """Prove the Spiffy/Listary owner seam with disposable real-core fixtures only."""

    if file_count < 4097:
        raise ListaryIntegrationError("fixture_too_small", "file_count must exceed the 4,096 interactive candidate budget.")
    expected = config["expected_contract"]
    core_lib = (library or _core_library(root)).expanduser().resolve(strict=False)
    if not core_lib.is_file():
        raise ListaryIntegrationError("core_library_missing", f"Release core library does not exist: {core_lib}")
    acceptance = _load_acceptance_module(root)
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="spiffy-listary-integration-") as temp_name:
        temp_root = Path(temp_name)
        source_state = temp_root / "source-state"
        destination_state = temp_root / "destination-state"
        indexed_root = temp_root / "素材_Ångström_项目"
        alpha = indexed_root / "ProjectAlpha"
        beta = indexed_root / "ProjectBeta"
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)
        unicode_old = alpha / "Résumé_ß_日本語.txt"
        unicode_old.write_text("unicode fixture", encoding="utf-8")
        for index in range(file_count):
            target = alpha if index % 2 == 0 else beta
            (target / f"Report_{index:05d}.txt").write_text(f"fixture {index}\n", encoding="utf-8")

        now_ms = 1_900_000_000_000
        performance_samples: list[float] = []
        with acceptance.CoreAbiClient(core_lib, source_state) as core:
            root_id = core.add_root(indexed_root)
            reconcile_initial = core.reconcile_root(root_id, request_id=100)

            launcher = core.search(
                acceptance._search_request(
                    "Résumé_ß_日本語",
                    root_ids=[root_id],
                    kinds=["file"],
                    now_unix_ms=now_ms,
                ),
                request_id=101,
            )
            unicode_result = acceptance._find_result_path(launcher, unicode_old)

            folder_search = core.search(
                acceptance._search_request(
                    "ProjectAlpha",
                    root_ids=[root_id],
                    kinds=["folder"],
                    now_unix_ms=now_ms + 1,
                ),
                request_id=102,
            )
            alpha_result = acceptance._find_result_path(folder_search, alpha)
            alpha_item_id = int(alpha_result["item"]["id"])
            core.record_activation(alpha_item_id, now_unix_ms=now_ms + 2, source="quick_switch")
            quick = core.search(
                acceptance.canonical_quick_switch_request(
                    root_ids=[root_id],
                    limit=10,
                    now_unix_ms=now_ms + 3,
                    timezone_offset_minutes=0,
                ),
                request_id=103,
            )
            quick_rows = acceptance.validate_core_candidates(quick, root_ids=[root_id])
            quick_top = acceptance.select_core_candidate(quick_rows, 0)
            if not acceptance.paths_match(str(alpha), str(quick_top["path"])):
                raise ListaryIntegrationError("quick_switch_shared_state_failed", "Quick Switch did not reuse Launcher activation history.")

            unicode_new = alpha / "Résumé_ß_日本語_RENAMED.txt"
            unicode_old.rename(unicode_new)
            reconcile_after_rename = core.reconcile_root(root_id, request_id=104)
            stale = core.search(
                acceptance._search_request(
                    "Résumé_ß_日本語.txt",
                    root_ids=[root_id],
                    kinds=["file"],
                    now_unix_ms=now_ms + 4,
                ),
                request_id=105,
            )
            if any(acceptance.paths_match(str(unicode_old), str(row["item"]["path"])) for row in stale.get("results", [])):
                raise ListaryIntegrationError("stale_path_survived", "Renamed source path remained searchable after reconciliation.")
            renamed = core.search(
                acceptance._search_request(
                    "RENAMED",
                    root_ids=[root_id],
                    kinds=["file"],
                    now_unix_ms=now_ms + 5,
                ),
                request_id=106,
            )
            acceptance._find_result_path(renamed, unicode_new)

            broad_request = acceptance._search_request(
                "Report",
                root_ids=[root_id],
                kinds=["file"],
                now_unix_ms=now_ms + 6,
            )
            for offset in range(search_iterations):
                tick = time.perf_counter()
                response = core.search(broad_request, request_id=200 + offset)
                performance_samples.append((time.perf_counter() - tick) * 1000.0)
                if not response.get("results"):
                    raise ListaryIntegrationError("large_result_empty", "Large-result fixture unexpectedly returned no rows.")

            before_growth = _tree_metrics(source_state)
            for offset in range(growth_iterations):
                core.search(broad_request, request_id=1000 + offset)
            after_growth = _tree_metrics(source_state)
            diagnostics = core.diagnostics()
            core.flush()

        portable_source = temp_root / "portable-source.json"
        portable_destination = temp_root / "portable-destination.json"
        export_source = portable_export(root, state_dir=source_state, output=portable_source)
        validate_source = portable_validate(
            root,
            input_path=portable_source,
            expected_schema=int(expected["portable_schema_version"]),
        )
        mapped_root = "/Users/test/素材_Ångström_项目"
        mapping = f"{indexed_root}={mapped_root}"
        first_restore = portable_restore(
            root,
            state_dir=destination_state,
            input_path=portable_source,
            path_maps=[mapping],
            target_style="posix",
            expected_schema=int(expected["portable_schema_version"]),
        )
        second_restore = portable_restore(
            root,
            state_dir=destination_state,
            input_path=portable_source,
            path_maps=[mapping],
            target_style="posix",
            expected_schema=int(expected["portable_schema_version"]),
        )
        export_destination = portable_export(root, state_dir=destination_state, output=portable_destination)
        destination_payload = _load_json(portable_destination)
        destination_roots = [str(row.get("path") or "") for row in destination_payload.get("roots", [])]
        if mapped_root not in destination_roots:
            raise ListaryIntegrationError(
                "portable_unicode_mapping_failed",
                f"Restored portable profile did not preserve the explicit Unicode POSIX mapping: {destination_roots}",
            )

        open_plan = quick_switch(root, state_dir=destination_state, fixture="open", plan_only=True)
        save_plan = quick_switch(root, state_dir=destination_state, fixture="save", plan_only=True)
        hostile_cache = core_smoke(
            root,
            library=core_lib,
            expected_api=int(expected["api_version"]),
        )
        growth_bytes = after_growth["total_bytes"] - before_growth["total_bytes"]
        growth_files = after_growth["file_count"] - before_growth["file_count"]
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "success",
            "outcome": "shared_owner_surface_fixture_green",
            "host_platform": platform.system().lower(),
            "real_mac_executed": False,
            "source_contract": source_contract(root, config=config),
            "integration": surface_contract(config, state_dir=str(source_state)),
            "fixture": {
                "indexed_files_requested": file_count + 1,
                "candidate_budget_exceeded": file_count > 4096,
                "unicode_root": str(indexed_root),
                "unicode_result_item_id": int(unicode_result["item"]["id"]),
                "quick_switch_top_folder": str(quick_top["path"]),
                "rename_reconcile_verified": True,
                "initial_reconcile": reconcile_initial,
                "rename_reconcile": reconcile_after_rename,
            },
            "performance": {
                "query": "Report",
                "iterations": search_iterations,
                "p95_ms": _p95_ms(performance_samples),
                "min_ms": round(min(performance_samples), 3),
                "max_ms": round(max(performance_samples), 3),
                "claim_scope": "Windows/shared-core integration fixture only; not AppKit or Mac UI latency.",
            },
            "resource_growth": {
                "iterations": growth_iterations,
                "state_before": before_growth,
                "state_after": after_growth,
                "file_count_delta": growth_files,
                "byte_delta": growth_bytes,
                "diagnostics_after": diagnostics,
                "claim_scope": "Repeated read-only search against one state directory; native process RSS is not measured here.",
            },
            "portable": {
                "source_export": export_source,
                "source_validate": validate_source,
                "first_restore": first_restore,
                "second_restore": second_restore,
                "destination_export": export_destination,
                "explicit_mapping": mapping,
                "destination_roots": destination_roots,
            },
            "quick_switch_plans": {"open": open_plan, "save": save_plan},
            "hostile_cache_recovery": hostile_cache,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "not_proven": [
                "Swift/AppKit Launcher or File Search UI",
                "Accessibility Finder/Open-Save navigation",
                "FSEvents",
                "HID/global hotkeys",
            ],
        }


def status_receipt(root: Path, *, config: Mapping[str, Any]) -> dict[str, Any]:
    contract = source_contract(root, config=config)
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "ready_for_owner_delegation",
        "host_platform": platform.system().lower(),
        "real_mac_executed": False,
        "listary_root": str(root),
        "source_contract": contract,
        "integration": surface_contract(config),
        "tools": {
            "portable_binary": str(_portable_binary(root)),
            "portable_binary_exists": _portable_binary(root).is_file(),
            "release_core_library": str(_core_library(root)),
            "release_core_library_exists": _core_library(root).is_file(),
            "bootstrap": str(root / "tools" / "macos_bridge_bootstrap.py"),
            "quick_switch_acceptance": str(root / "tools" / "quick_switch_acceptance.py"),
        },
        "not_proven": [
            "Swift/AppKit launcher or File Search execution",
            "Accessibility Finder/Open-Save detection or navigation",
            "FSEvents delivery/drop recovery",
            "HID/global hotkeys",
            "signing/notarization/Gatekeeper",
        ],
    }


def combined_plan(
    root: Path,
    *,
    config: Mapping[str, Any],
    portable_config: Path | None,
    state_dir: Path,
    path_maps: Sequence[str],
) -> dict[str, Any]:
    expected = config["expected_contract"]
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "plan",
        "real_mac_executed": False,
        "source_contract": source_contract(root, config=config),
        "integration": surface_contract(config, state_dir=str(state_dir)),
        "bootstrap": bootstrap(
            root,
            plan_only=True,
            portable_config=portable_config,
            state_dir=state_dir if portable_config is not None else None,
            path_maps=path_maps,
            expected_api=int(expected["api_version"]),
        ),
        "quick_switch_open": quick_switch(root, state_dir=state_dir, fixture="open", plan_only=True),
        "quick_switch_save": quick_switch(root, state_dir=state_dir, fixture="save", plan_only=True),
    }


def _print(value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(payload.encode("utf-8"))
        buffer.flush()
    else:
        print(payload, end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listary-root", type=Path, help="Override the canonical Listary Mac Port workspace path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Validate the tracked ABI/schema/ownership contract without mutation.")

    plan = subparsers.add_parser("plan", help="Emit one first-Mac bootstrap + Open/Save handoff plan using a single state directory.")
    plan.add_argument("--state-dir", type=Path, required=True)
    plan.add_argument("--portable-config", type=Path)
    plan.add_argument("--path-map", action="append", default=[])

    export = subparsers.add_parser("export", help="Export Listary user-owned portable configuration through the canonical Rust owner.")
    export.add_argument("--state-dir", type=Path, required=True)
    export.add_argument("--output", type=Path)

    validate = subparsers.add_parser("validate-portable", help="Validate a portable Listary artifact through the canonical Rust owner.")
    validate.add_argument("--input", type=Path, required=True)

    restore = subparsers.add_parser("restore", help="Restore portable configuration into one canonical Listary state directory.")
    restore.add_argument("--state-dir", type=Path, required=True)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--path-map", action="append", default=[])
    restore.add_argument("--target-style", choices=("native", "windows", "posix"), default="posix")

    bootstrap_plan = subparsers.add_parser("bootstrap-plan", help="Delegate the source-current static bridge plan to Listary Mac Port.")
    bootstrap_plan.add_argument("--state-dir", type=Path)
    bootstrap_plan.add_argument("--portable-config", type=Path)
    bootstrap_plan.add_argument("--path-map", action="append", default=[])

    bootstrap_run = subparsers.add_parser("bootstrap", help="Run the canonical Darwin-only bridge bootstrap through Listary Mac Port.")
    bootstrap_run.add_argument("--state-dir", type=Path)
    bootstrap_run.add_argument("--portable-config", type=Path)
    bootstrap_run.add_argument("--path-map", action="append", default=[])

    qs_plan = subparsers.add_parser("quick-switch-plan", help="Emit a non-Mac Open/Save acceptance plan with no hardware claim.")
    qs_plan.add_argument("--state-dir", type=Path, required=True)
    qs_plan.add_argument("--fixture", choices=("open", "save"), required=True)
    qs_plan.add_argument("--finder-transfer", action="store_true")

    qs = subparsers.add_parser("quick-switch", help="Run the canonical real-Mac Open/Save acceptance harness.")
    qs.add_argument("--state-dir", type=Path, required=True)
    qs.add_argument("--fixture", choices=("open", "save"), required=True)
    qs.add_argument("--finder-transfer", action="store_true")
    qs.add_argument("--receipt", type=Path)

    smoke = subparsers.add_parser("core-smoke", help="Run the existing release-C-ABI disposable search/Quick-Switch/recovery smoke.")
    smoke.add_argument("--core-lib", type=Path)
    fixture = subparsers.add_parser("fixture-smoke", help="Run a disposable shared-owner integration fixture through the real release C ABI.")
    fixture.add_argument("--core-lib", type=Path)
    fixture.add_argument("--file-count", type=int, default=5200)
    fixture.add_argument("--search-iterations", type=int, default=40)
    fixture.add_argument("--growth-iterations", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
        root = discover_listary_root(args.listary_root, config=config)
        contract = source_contract(root, config=config)
        expected_api = int(contract["api_version"])
        expected_schema = int(contract["portable_schema_version"])

        if args.command == "status":
            result = status_receipt(root, config=config)
        elif args.command == "plan":
            result = combined_plan(
                root,
                config=config,
                portable_config=args.portable_config,
                state_dir=args.state_dir,
                path_maps=args.path_map,
            )
        elif args.command == "export":
            output = args.output or (PACKAGE_ROOT / str(config["default_portable_artifact"]))
            result = portable_export(root, state_dir=args.state_dir, output=output)
        elif args.command == "validate-portable":
            result = portable_validate(root, input_path=args.input, expected_schema=expected_schema)
        elif args.command == "restore":
            result = portable_restore(
                root,
                state_dir=args.state_dir,
                input_path=args.input,
                path_maps=args.path_map,
                target_style=args.target_style,
                expected_schema=expected_schema,
            )
        elif args.command in {"bootstrap-plan", "bootstrap"}:
            result = bootstrap(
                root,
                plan_only=args.command == "bootstrap-plan",
                portable_config=args.portable_config,
                state_dir=args.state_dir,
                path_maps=args.path_map,
                expected_api=expected_api,
            )
        elif args.command in {"quick-switch-plan", "quick-switch"}:
            result = quick_switch(
                root,
                state_dir=args.state_dir,
                fixture=args.fixture,
                plan_only=args.command == "quick-switch-plan",
                finder_transfer=args.finder_transfer,
                receipt_path=getattr(args, "receipt", None),
            )
        elif args.command == "core-smoke":
            result = core_smoke(root, library=args.core_lib, expected_api=expected_api)
        elif args.command == "fixture-smoke":
            result = fixture_smoke(
                root,
                config=config,
                library=args.core_lib,
                file_count=args.file_count,
                search_iterations=args.search_iterations,
                growth_iterations=args.growth_iterations,
            )
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
        _print(result)
        return 0
    except ListaryIntegrationError as exc:
        _print({"schema": RECEIPT_SCHEMA, "status": "failed", "error_code": exc.code, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PACKAGE_ROOT / "listary_integration.py"
SPEC = importlib.util.spec_from_file_location("listary_integration", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def config() -> dict:
    return {
        "schema": module.CONFIG_SCHEMA,
        "workspace_env": "LISTARY_MAC_PORT_ROOT",
        "workspace_candidates": [],
        "expected_contract": {
            "api_version": 8,
            "portable_schema_version": 5,
            "public_symbol_count": 41,
        },
        "default_mac_state_dir": "~/Library/Application Support/ListaryMac/state",
        "default_portable_artifact": "private/listary/listary-portable.json",
        "canonical_owners": {
            "catalog": "core -> RocksDB",
            "query_ranking_history": "core",
            "native_boundary": "ffi",
            "portable_configuration": "portable",
            "filesystem_events": "FSEvents -> reconcile_paths",
            "finder_open_save": "AX -> core",
            "acceptance": "quick_switch_acceptance.py",
        },
        "surfaces": {
            "launcher": {"core_calls": ["listary_core_search"], "contextual": False},
            "file_search": {"core_calls": ["listary_core_search", "listary_core_facet_counts"], "contextual": False},
            "quick_switch": {"core_calls": ["listary_core_search"], "contextual": False, "kinds": ["folder"]},
            "finder_context_search": {"core_calls": ["listary_core_search"], "contextual": True},
            "open_save_handoff": {"core_calls": ["listary_core_search"], "native_adapter": "Accessibility"},
        },
        "ownership_rules": {
            "single_state_directory": True,
            "single_index_owner": True,
            "single_filesystem_watcher_owner": True,
            "spiffy_creates_index": False,
            "spiffy_creates_watcher": False,
            "spiffy_reranks_results": False,
        },
    }


def fake_listary(tmp_path: Path, *, api: int = 8, schema: int = 5, symbols: int = 41) -> Path:
    root = tmp_path / "Listary Mac Port"
    (root / "rust" / "listary-ffi" / "include").mkdir(parents=True)
    (root / "rust" / "listary-core" / "src").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    prototypes = [f"int32_t listary_fixture_{index}(void);" for index in range(symbols)]
    (root / "rust" / "listary-ffi" / "include" / "listary_core.h").write_text(
        f"#define LISTARY_CORE_API_VERSION {api}u\n" + "\n".join(prototypes) + "\n",
        encoding="utf-8",
    )
    (root / "rust" / "listary-core" / "src" / "portable.rs").write_text(
        f"pub const PORTABLE_CONFIG_SCHEMA_VERSION: u32 = {schema};\n",
        encoding="utf-8",
    )
    (root / "tools" / "quick_switch_acceptance.py").write_text("# fixture\n", encoding="utf-8")
    (root / "tools" / "macos_bridge_bootstrap.py").write_text("# fixture\n", encoding="utf-8")
    return root


def test_source_contract_locks_abi_schema_and_symbol_surface(tmp_path: Path) -> None:
    root = fake_listary(tmp_path)
    observed = module.source_contract(root, config=config())
    assert observed["api_version"] == 8
    assert observed["portable_schema_version"] == 5
    assert observed["public_symbol_count"] == 41


@pytest.mark.parametrize(("api", "schema", "symbols"), [(7, 5, 41), (8, 6, 41), (8, 5, 40)])
def test_source_contract_fails_closed_on_drift(tmp_path: Path, api: int, schema: int, symbols: int) -> None:
    root = fake_listary(tmp_path, api=api, schema=schema, symbols=symbols)
    with pytest.raises(module.ListaryIntegrationError) as caught:
        module.source_contract(root, config=config())
    assert caught.value.code == "source_contract_drift"


def test_surface_contract_forces_one_state_index_and_watcher_owner() -> None:
    value = module.surface_contract(config(), state_dir="/Users/test/Library/Application Support/ListaryMac/state")
    states = {surface["state_dir"] for surface in value["surfaces"].values()}
    catalogs = {surface["catalog_owner"] for surface in value["surfaces"].values()}
    queries = {surface["query_owner"] for surface in value["surfaces"].values()}
    assert states == {"/Users/test/Library/Application Support/ListaryMac/state"}
    assert len(catalogs) == 1
    assert len(queries) == 1
    assert value["ownership_rules"]["spiffy_creates_index"] is False
    assert value["ownership_rules"]["spiffy_creates_watcher"] is False
    assert value["ownership_rules"]["spiffy_reranks_results"] is False


def test_surface_contract_rejects_duplicate_owner_policy() -> None:
    bad = config()
    bad["ownership_rules"]["spiffy_creates_index"] = True
    with pytest.raises(module.ListaryIntegrationError) as caught:
        module.surface_contract(bad)
    assert caught.value.code == "ownership_contract_invalid"


def test_release_portable_binary_is_preferred_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fake_listary(tmp_path)
    release = root / "target" / "release"
    release.mkdir(parents=True)
    monkeypatch.setattr(module.os, "name", "nt")
    binary = release / "listary-portable-config.exe"
    binary.write_bytes(b"fixture")
    command = module._portable_command(root, "validate", "--input", "portable.json")
    assert command == [str(binary), "validate", "--input", "portable.json"]


def test_portable_export_refuses_to_replace_existing_artifact(tmp_path: Path) -> None:
    root = fake_listary(tmp_path)
    output = tmp_path / "portable.json"
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(module.ListaryIntegrationError) as caught:
        module.portable_export(root, state_dir=tmp_path / "state", output=output)
    assert caught.value.code == "portable_output_exists"


def test_bootstrap_plan_preserves_explicit_unicode_path_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fake_listary(tmp_path)
    portable = tmp_path / "portable.json"
    portable.write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path) -> dict:
        calls.append(list(command))
        assert cwd == root
        return {
            "status": "plan",
            "real_mac_executed": False,
            "source_contract": {"api_version": 8},
        }

    monkeypatch.setattr(module, "_run_json_command", fake_run)
    module.bootstrap(
        root,
        plan_only=True,
        portable_config=portable,
        state_dir=state,
        path_maps=[r"C:\素材=/Users/test/素材"],
        expected_api=8,
    )
    flat = calls[0]
    assert "--plan" in flat
    assert r"C:\素材=/Users/test/素材" in flat


def test_quick_switch_plan_cannot_claim_real_mac_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fake_listary(tmp_path)
    monkeypatch.setattr(
        module,
        "_run_json_command",
        lambda *_args, **_kwargs: {"status": "dry_run", "real_mac_executed": False, "outcome": "static_plan"},
    )
    value = module.quick_switch(root, state_dir=tmp_path / "state", fixture="open", plan_only=True)
    assert value["status"] == "dry_run"
    assert value["real_mac_executed"] is False


def test_quick_switch_plan_rejects_false_hardware_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fake_listary(tmp_path)
    monkeypatch.setattr(
        module,
        "_run_json_command",
        lambda *_args, **_kwargs: {"status": "success", "real_mac_executed": True},
    )
    with pytest.raises(module.ListaryIntegrationError) as caught:
        module.quick_switch(root, state_dir=tmp_path / "state", fixture="save", plan_only=True)
    assert caught.value.code == "quick_switch_plan_invalid"


def test_config_file_declares_current_owner_contract() -> None:
    value = module.load_config()
    assert value["expected_contract"] == {
        "api_version": 8,
        "portable_schema_version": 5,
        "public_symbol_count": 41,
    }
    assert value["ownership_rules"]["single_state_directory"] is True
    assert value["ownership_rules"]["single_index_owner"] is True
    assert value["ownership_rules"]["single_filesystem_watcher_owner"] is True


def test_fixture_smoke_requires_more_than_interactive_candidate_budget(tmp_path: Path) -> None:
    root = fake_listary(tmp_path)
    with pytest.raises(module.ListaryIntegrationError) as caught:
        module.fixture_smoke(root, config=config(), file_count=4096)
    assert caught.value.code == "fixture_too_small"


def test_p95_helper_is_deterministic() -> None:
    assert module._p95_ms([float(value) for value in range(1, 101)]) == 95.0

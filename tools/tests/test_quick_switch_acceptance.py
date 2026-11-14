from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "quick_switch_acceptance.py"
SPEC = importlib.util.spec_from_file_location("quick_switch_acceptance", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def candidate(path: str, *, item_id: int, root_id: str, available: bool = True, kind: str = "folder") -> dict:
    return {
        "item": {
            "id": item_id,
            "root_id": root_id,
            "path": path,
            "parent_path": str(Path(path).parent),
            "name": Path(path).name,
            "extension": "",
            "kind": kind,
            "size": 0,
            "modified_unix_ms": 0,
            "available": available,
            "priority": "normal",
            "stable_file_id": None,
            "preview_hint": None,
        },
        "score": {"combined": 100 - item_id},
    }


def test_canonical_zero_query_folder_request_is_exact() -> None:
    root = "11111111-1111-4111-8111-111111111111"
    request = module.canonical_quick_switch_request(
        root_ids=[root],
        limit=7,
        now_unix_ms=123456789,
        timezone_offset_minutes=-240,
    )
    assert request == {
        "query": "",
        "limit": 7,
        "candidate_limit": 4096,
        "include_unavailable": False,
        "root_ids": [root],
        "kinds": ["folder"],
        "context": {"current_folder": None, "contextual": False},
        "now_unix_ms": 123456789,
        "timezone_offset_minutes": -240,
    }


def test_canonical_file_search_request_carries_generation_and_unicode_query() -> None:
    root = "11111111-1111-4111-8111-111111111111"
    request = module._file_search_request(
        "Résumé βeta",
        root_ids=[root],
        now_unix_ms=123456789,
        offset=20,
        limit=10,
        sort_field="name",
        sort_direction="ascending",
        expected_catalog_generation=42,
    )
    assert request == {
        "query": "Résumé βeta",
        "include_unavailable": False,
        "root_ids": [root],
        "categories": [],
        "modified_time": None,
        "now_unix_ms": 123456789,
        "timezone_offset_minutes": 0,
        "offset": 20,
        "limit": 10,
        "sort_field": "name",
        "sort_direction": "ascending",
        "expected_catalog_generation": 42,
    }


def test_json_receipt_falls_back_to_ascii_escapes_for_legacy_console_encoding() -> None:
    receipt = {"path": "Résumé βeta/Report Alpha.txt"}
    rendered = module.json_for_stream(receipt, pretty=False, encoding="cp1252")
    assert "\\u03b2" in rendered
    assert json.loads(rendered) == receipt


def test_file_context_and_action_intent_are_platform_neutral_and_unicode_safe() -> None:
    context = module.canonical_file_context(
        source="finder",
        context_token="finder:42:7",
        generation=3,
        capability="granted",
        current_directory="/Users/Test/Résumé βeta",
        selected_path="/Users/Test/Résumé βeta/Scene α.mov",
        query_text="Résumé βeta",
        process_id=42,
        window_id="7",
    )
    identity = {
        "item_id": 9,
        "root_id": "11111111-1111-4111-8111-111111111111",
        "path": "/Users/Test/Résumé βeta",
        "kind": "folder",
    }
    intent = module.canonical_action_intent(
        "reveal",
        context=context,
        activation_generation=11,
        target_path=identity["path"],
        query_text="Résumé βeta",
        result_identity=identity,
    )
    assert context["schema"] == "listary_file_context_v1"
    assert context["selected_path"].endswith("Scene α.mov")
    assert intent["schema"] == "listary_file_action_intent_v1"
    assert intent["context_token"] == "finder:42:7"
    assert intent["activation_generation"] == 11
    assert "hwnd" not in json.dumps(intent).lower()


@pytest.mark.parametrize("action", ["navigate", "select", "open", "reveal"])
def test_action_contract_represents_supported_safe_intents(action: str) -> None:
    context = module.canonical_file_context(
        source="open_dialog",
        context_token="panel:1",
        generation=1,
        capability="granted",
        current_directory="/tmp",
        query_text="beta",
        dialog_kind="open",
    )
    intent = module.canonical_action_intent(
        action,
        context=context,
        activation_generation=1,
        target_path="/tmp/Résumé βeta",
        query_text="beta",
    )
    module.validate_action_intent(
        intent,
        current_context=context,
        current_activation_generation=1,
        target_accessible=True,
    )


def test_stale_context_and_stale_activation_fail_closed_then_new_context_recovers() -> None:
    first = module.canonical_file_context(
        source="save_dialog",
        context_token="panel:old",
        generation=1,
        capability="granted",
        current_directory="/tmp/old",
        query_text="target",
        dialog_kind="save",
    )
    intent = module.canonical_action_intent(
        "navigate",
        context=first,
        activation_generation=7,
        target_path="/tmp/target",
        query_text="target",
    )
    second_generation = module.next_context_generation(first, next_context_token="panel:new")
    second = module.canonical_file_context(
        source="save_dialog",
        context_token="panel:new",
        generation=second_generation,
        capability="granted",
        current_directory="/tmp/new",
        query_text="target",
        dialog_kind="save",
    )
    with pytest.raises(module.HarnessError) as stale_context:
        module.validate_action_intent(
            intent,
            current_context=second,
            current_activation_generation=7,
            target_accessible=True,
        )
    assert stale_context.value.code == "stale_context"

    fresh_intent = module.canonical_action_intent(
        "navigate",
        context=second,
        activation_generation=8,
        target_path="/tmp/target",
        query_text="target",
    )
    with pytest.raises(module.HarnessError) as stale_activation:
        module.validate_action_intent(
            fresh_intent,
            current_context=second,
            current_activation_generation=9,
            target_accessible=True,
        )
    assert stale_activation.value.code == "stale_activation"
    changed_query_context = dict(second)
    changed_query_context["query_text"] = "new target"
    with pytest.raises(module.HarnessError) as stale_query:
        module.validate_action_intent(
            fresh_intent,
            current_context=changed_query_context,
            current_activation_generation=8,
            target_accessible=True,
        )
    assert stale_query.value.code == "stale_activation"
    module.validate_action_intent(
        fresh_intent,
        current_context=second,
        current_activation_generation=8,
        target_accessible=True,
    )


def test_indexed_action_revalidation_rejects_deleted_changed_and_inaccessible_targets() -> None:
    root = "11111111-1111-4111-8111-111111111111"
    row = candidate("/tmp/Résumé βeta", item_id=9, root_id=root)
    identity = module.canonical_result_identity(row)
    context = module.canonical_file_context(
        source="open_dialog",
        context_token="panel:stable",
        generation=2,
        capability="granted",
        current_directory="/tmp",
        query_text="Résumé βeta",
        dialog_kind="open",
    )
    intent = module.canonical_action_intent(
        "navigate",
        context=context,
        activation_generation=4,
        target_path=identity["path"],
        query_text="Résumé βeta",
        result_identity=identity,
    )
    live = dict(row["item"])
    module.validate_action_intent(
        intent,
        current_context=context,
        current_activation_generation=4,
        live_item=live,
        target_accessible=True,
    )

    unavailable = dict(live)
    unavailable["available"] = False
    with pytest.raises(module.HarnessError) as deleted:
        module.validate_action_intent(
            intent,
            current_context=context,
            current_activation_generation=4,
            live_item=unavailable,
            target_accessible=True,
        )
    assert deleted.value.code == "result_unavailable"

    changed = dict(live)
    changed["path"] = "/tmp/replaced"
    with pytest.raises(module.HarnessError) as stale:
        module.validate_action_intent(
            intent,
            current_context=context,
            current_activation_generation=4,
            live_item=changed,
            target_accessible=True,
        )
    assert stale.value.code == "stale_result_identity"

    with pytest.raises(module.HarnessError) as inaccessible:
        module.validate_action_intent(
            intent,
            current_context=context,
            current_activation_generation=4,
            live_item=live,
            target_accessible=False,
        )
    assert inaccessible.value.code == "target_inaccessible"


def test_target_directory_validation_distinguishes_missing_and_nonfolder(tmp_path: Path) -> None:
    folder = tmp_path / "Résumé βeta"
    folder.mkdir()
    assert module.require_accessible_directory(str(folder)) == folder
    with pytest.raises(module.HarnessError) as missing:
        module.require_accessible_directory(str(tmp_path / "missing"))
    assert missing.value.code == "target_folder_missing"
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(module.HarnessError) as not_folder:
        module.require_accessible_directory(str(file_path))
    assert not_folder.value.code == "target_not_folder"


def test_shared_core_order_is_consumed_without_local_reranking() -> None:
    root = "11111111-1111-4111-8111-111111111111"
    response = {
        "results": [
            candidate("/tmp/Z-last-alphabetically", item_id=9, root_id=root),
            candidate("/tmp/A-first-alphabetically", item_id=1, root_id=root),
        ]
    }
    rows = module.validate_core_candidates(response, root_ids=[root])
    assert [row["item_id"] for row in rows] == [9, 1]
    selected = module.select_core_candidate(rows, 0)
    assert selected["item_id"] == 9
    assert module.canonical_result_identity(selected) == {
        "item_id": 9,
        "root_id": root,
        "path": "/tmp/Z-last-alphabetically",
        "kind": "folder",
    }


@pytest.mark.parametrize(
    ("available", "kind", "code"),
    [
        (False, "folder", "core_candidate_unavailable"),
        (True, "file", "core_candidate_not_folder"),
    ],
)
def test_unavailable_or_nonfolder_core_candidates_fail_closed(available: bool, kind: str, code: str) -> None:
    root = "11111111-1111-4111-8111-111111111111"
    response = {"results": [candidate("/tmp/unsafe", item_id=1, root_id=root, available=available, kind=kind)]}
    with pytest.raises(module.HarnessError) as caught:
        module.validate_core_candidates(response, root_ids=[root])
    assert caught.value.code == code


def test_panel_identity_is_pinned_and_replacement_or_closure_fails() -> None:
    before = {"panel_token": "pid:7/window:99", "current_folder": "/tmp/a"}
    navigation = {"status": "navigated", "stale_object_recovered": False}
    replaced = {"panel_token": "pid:7/window:100", "current_folder": "/tmp/b"}
    assert module.assess_navigation(
        panel_before=before,
        navigation=navigation,
        panel_after=replaced,
        expected_path="/tmp/b",
    ) == ("failed", "panel_replaced")
    assert module.assess_navigation(
        panel_before=before,
        navigation={"status": "panel_closed"},
        panel_after={},
        expected_path="/tmp/b",
    ) == ("failed", "panel_closed")


def test_readback_mismatch_cannot_be_reported_successful() -> None:
    before = {"panel_token": "stable", "current_folder": "/tmp/a"}
    after = {"panel_token": "stable", "current_folder": "/tmp/not-target"}
    assert module.assess_navigation(
        panel_before=before,
        navigation={"status": "navigated"},
        panel_after=after,
        expected_path="/tmp/target",
    ) == ("failed", "readback_mismatch")


def test_save_panel_file_url_parent_can_verify_current_folder() -> None:
    panel = {
        "panel_token": "stable",
        "current_folder": "/tmp/target/ListaryAcceptance.txt",
        "readback_evidence": [],
    }
    assert module.panel_readback_matches("/tmp/target", panel) is True
    assert module.assess_navigation(
        panel_before={"panel_token": "stable"},
        navigation={"status": "navigated"},
        panel_after=panel,
        expected_path="/tmp/target",
    ) == ("success", "success")


def test_helper_readback_mismatch_stays_distinct_from_navigation_failure() -> None:
    before = {"panel_token": "stable", "current_folder": "/tmp/a"}
    after = {"panel_token": "stable", "current_folder": "/tmp/wrong"}
    assert module.assess_navigation(
        panel_before=before,
        navigation={"status": "readback_mismatch"},
        panel_after=after,
        expected_path="/tmp/target",
    ) == ("failed", "readback_mismatch")


def test_fixture_independent_readback_is_required_when_present() -> None:
    before = {"panel_token": "stable", "current_folder": "/tmp/a"}
    after = {"panel_token": "stable", "current_folder": "/tmp/target"}
    assert module.assess_navigation(
        panel_before=before,
        navigation={"status": "navigated", "stale_object_recovered": True},
        panel_after=after,
        expected_path="/tmp/target",
        fixture_state={"current_directory": "/tmp/wrong"},
    ) == ("failed", "fixture_readback_mismatch")
    assert module.assess_navigation(
        panel_before=before,
        navigation={"status": "navigated", "stale_object_recovered": True},
        panel_after=after,
        expected_path="/tmp/target",
        fixture_state={"current_directory": "/tmp/target"},
    ) == ("success", "success")


def test_permission_denied_is_explicit_blocked_probe_category() -> None:
    with pytest.raises(module.HarnessError) as caught:
        module.panel_or_error({"status": "accessibility_denied", "permission": "denied"})
    assert caught.value.code == "permission_denied"


def test_static_ax_helper_and_fixture_contracts_are_narrow_and_native() -> None:
    ax_source = module.AX_HELPER.read_text(encoding="utf-8")
    fixture_source = module.FIXTURE_SOURCE.read_text(encoding="utf-8")
    assert "AXIsProcessTrusted" in ax_source
    assert 'action === "probe"' in ax_source
    assert 'action === "navigate"' in ax_source
    assert 'action === "finder-context"' in ax_source
    assert "panel_replaced" in ax_source
    assert "panel_not_found" in ax_source
    assert "readback_mismatch" in ax_source
    assert "stale_object_recovered" in ax_source
    assert "selectedPathEvidence" in ax_source
    assert "context_token" in ax_source
    assert "current_directory" in ax_source
    assert "selected_path" in ax_source
    assert "listary_core" not in ax_source
    assert "NSOpenPanel" in fixture_source
    assert "NSSavePanel" in fixture_source
    assert "directoryURL" in fixture_source
    assert "dataWithJSONObject" in fixture_source
    assert "writeToFile" in fixture_source


def test_dry_run_is_nonmac_safe_and_never_claims_hardware_success(tmp_path: Path) -> None:
    parser = module.build_parser()
    args = parser.parse_args([
        "--dry-run",
        "--state-dir",
        str(tmp_path / "missing-state"),
        "--fixture",
        "open",
    ])
    receipt = module.acceptance_run(args)
    assert receipt["status"] == "dry_run"
    assert receipt["outcome"] == "static_plan"
    assert receipt["real_mac_executed"] is False
    assert receipt["core_request"]["query"] == ""
    assert receipt["core_request"]["kinds"] == ["folder"]
    assert receipt["core_request"]["include_unavailable"] is False
    assert receipt["file_context_schema"] == "listary_file_context_v1"
    assert receipt["file_action_intent_schema"] == "listary_file_action_intent_v1"
    assert receipt["supported_action_intents"] == ["navigate", "open", "reveal", "select"]


def test_windows_core_build_environment_is_self_contained_when_llvm_exists(tmp_path: Path) -> None:
    llvm = tmp_path / "llvm" / "bin"
    llvm.mkdir(parents=True)
    (llvm / "libclang.dll").write_bytes(b"fixture")
    original = {
        "PATH": r"C:\Windows\System32",
        "LIBCLANG_PATH": "",
        "KEEP": "yes",
    }

    prepared = module.cargo_build_environment(
        original,
        system="Windows",
        llvm_bin=llvm,
    )

    assert prepared["LIBCLANG_PATH"] == str(llvm)
    assert prepared["PATH"].split(module.os.pathsep)[0] == str(llvm)
    assert prepared["KEEP"] == "yes"
    assert original == {
        "PATH": r"C:\Windows\System32",
        "LIBCLANG_PATH": "",
        "KEEP": "yes",
    }


class FakeCoreFunction:
    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.argtypes = None
        self.restype = None

    def __call__(self) -> int:
        return self.value


def fake_complete_core_library(*, version: int = module.EXPECTED_CORE_API_VERSION) -> object:
    library = type("FakeCoreLibrary", (), {})()
    for symbol in module.PUBLIC_CORE_SYMBOLS:
        setattr(library, symbol, FakeCoreFunction())
    setattr(library, "listary_core_api_version", FakeCoreFunction(version))
    return library


def test_canonical_header_publishes_current_compile_time_version() -> None:
    assert module.header_api_version() == module.EXPECTED_CORE_API_VERSION == 9


def test_bridge_preflight_rejects_missing_symbol_with_clear_error(tmp_path: Path) -> None:
    library = fake_complete_core_library()
    delattr(library, "listary_core_cancel")
    with pytest.raises(module.HarnessError) as caught:
        module.validate_loaded_core_library(
            library,
            library_path=tmp_path / "liblistary_ffi.fixture",
        )
    assert caught.value.code == "core_symbol_missing"
    assert "listary_core_cancel" in str(caught.value)


def test_bridge_preflight_rejects_api_version_mismatch_before_state_open(tmp_path: Path) -> None:
    library = fake_complete_core_library(version=999)
    with pytest.raises(module.HarnessError) as caught:
        module.validate_loaded_core_library(
            library,
            library_path=tmp_path / "liblistary_ffi.fixture",
    )
    assert caught.value.code == "core_api_version_mismatch"
    assert f"expected {module.EXPECTED_CORE_API_VERSION}" in str(caught.value)


def test_bridge_preflight_reports_complete_v9_surface(tmp_path: Path) -> None:
    library = fake_complete_core_library()
    receipt = module.validate_loaded_core_library(
        library,
        library_path=tmp_path / "liblistary_ffi.fixture",
    )
    assert receipt["header_api_version"] == 9
    assert receipt["library_api_version"] == 9
    assert receipt["symbol_count"] == len(module.PUBLIC_CORE_SYMBOLS) == 42


def test_core_smoke_and_dry_run_are_not_ambiguously_combined() -> None:
    parser = module.build_parser()
    args = parser.parse_args(["--core-smoke", "--dry-run"])
    assert args.core_smoke is True
    assert args.dry_run is True
    with pytest.raises(module.HarnessError, match="cannot be combined") as caught:
        if args.core_smoke and args.dry_run:
            raise module.HarnessError(
                "invalid_argument",
                "--core-smoke executes only disposable local fixtures and cannot be combined with --dry-run.",
            )
    assert caught.value.code == "invalid_argument"


def test_host_filesystem_smoke_is_real_mac_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module.sys, "platform", "win32")
    with pytest.raises(module.HarnessError) as caught:
        module.host_filesystem_smoke_run(tmp_path / "listary_ffi.fixture")
    assert caught.value.code == "non_macos_host"


def test_host_filesystem_smoke_cli_mode_is_explicit() -> None:
    args = module.build_parser().parse_args(["--host-filesystem-smoke", "--no-build-core"])
    assert args.host_filesystem_smoke is True
    assert args.no_build_core is True
    assert args.core_smoke is False
    assert args.bridge_preflight is False

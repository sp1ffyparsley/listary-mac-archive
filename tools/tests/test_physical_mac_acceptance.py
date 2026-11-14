from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "physical_mac_acceptance.py"
SPEC = importlib.util.spec_from_file_location("physical_mac_acceptance", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def successful_native_receipt(settings_sha: str = "A" * 64) -> dict:
    value = module.native_receipt_template()
    value.update(
        {
            "status": "success",
            "real_mac_executed": True,
            "production_appkit_shell": True,
        }
    )
    value["startup"].update({"started": True})
    value["search"].update(
        {
            "exact_path_verified": True,
            "finder_alias_verified": True,
            "same_identity_move_verified": True,
            "old_path_absent_after_move": True,
            "first_result_latency_ms": 7.25,
        }
    )
    value["interaction"] = {
        "query_focus_verified": True,
        "keyboard_navigation_verified": True,
        "prior_focus_restored": True,
    }
    value["file_dialog"] = {"open_verified": True, "save_verified": True}
    value["file_action"] = {
        "kind": "reveal",
        "authorized": True,
        "pre_action_identity_verified": True,
        "receipt_id": "native-action-1",
        "target_identity": {
            "item_id": 42,
            "root_id": "11111111-1111-4111-8111-111111111111",
            "stable_file_id": "darwin:1:2",
            "relative_path": "Résumé βeta/exact-path-probe.txt",
        },
        "cleanup_complete": True,
    }
    value["recovery"] = {
        "permission_denial_observed": True,
        "permission_recovered": True,
        "unavailable_root_observed": True,
        "unavailable_root_recovered": True,
    }
    value["appearance"] = {
        "live_propagation_verified": True,
        "persisted": True,
        "restart_restored": True,
        "settings_schema": module.APPEARANCE_SCHEMA,
        "settings_sha256": settings_sha,
    }
    value["restart"] = {"search_identity_survived": True, "settings_survived": True}
    value["fsevents"] = {"native_delivery_verified": True, "dropped_history_reconciled": True}
    value["quick_look"] = {"verified": True}
    value["lifecycle"] = {
        "second_launch_reused_existing_process": True,
        "explicit_quit_clean": True,
    }
    value["distribution"] = {
        "developer_id_signature_verified": True,
        "hardened_runtime": True,
        "notarization_accepted": True,
        "staple_valid": True,
        "gatekeeper_accepted": True,
    }
    return value


def successful_opus_receipt() -> dict:
    return {
        "schema": module.OPUS_SCHEMA,
        "platform": "macOS",
        "architecture": "arm64",
        "package_hashes_verified": True,
        "native_architecture_verified": True,
        "core_suite_executed": True,
        "core_suite_passed": True,
        "all_core_checks_verified": True,
        "headless_ui_self_test_passed": True,
        "code_signature_verified": True,
        "desktop_launch_request_accepted": True,
        "manual_desktop_acceptance_complete": False,
        "complete_directory_opus_parity_verified": False,
        "automatic_verification_completed": True,
        "exit_code": 0,
    }


def test_plan_is_host_safe_and_reuses_existing_owners() -> None:
    plan = module.build_plan()
    assert plan["status"] == "plan"
    assert plan["real_mac_executed"] is False
    assert plan["acceptance_state_before"] == "5/6 agent-reported verified"
    assert plan["acceptance_may_advance"] is False
    assert plan["owners"]["search_index"] == "listary-ffi -> listary-core -> RocksDB"
    assert plan["owners"]["file_dialog_navigation"].endswith("macos/QuickSwitchAcceptance/ax_file_dialog.js")
    assert plan["owners"]["native_focus_fixture"].endswith("macos/WindowFixture/main.m")
    assert "spiffy_shared_appearance_v1" in plan["owners"]["shared_appearance"]
    flattened = [" ".join(command) for command in plan["commands"]]
    assert any("macos_bridge_bootstrap.py --plan --json" in command for command in flattened)
    assert any("--fixture open" in command for command in flattened)
    assert any("--fixture save" in command for command in flattened)
    assert any("Verify-Mac.command --launch" in command for command in flattened)
    assert "finder_alias" in plan["required_native_capabilities"]
    assert plan["native_receipt_template"]["search"]["finder_alias_verified"] is False
    assert plan["native_receipt_template"]["distribution"]["notarization_accepted"] is False


def test_current_tree_truthfully_reports_no_production_swift_shell() -> None:
    inventory = module.native_shell_inventory()
    assert inventory["production_shell_source_present"] is False
    assert inventory["swift_source_count"] == 0
    assert inventory["xcode_project_count"] == 0


def test_native_receipt_requires_real_finder_alias_and_distribution_proof() -> None:
    receipt = successful_native_receipt()
    receipt["search"]["finder_alias_verified"] = False
    with pytest.raises(module.AcceptanceError, match="finder_alias_verified"):
        module.validate_native_receipt(receipt, expected_appearance_sha256="A" * 64)

    receipt = successful_native_receipt()
    receipt["distribution"]["notarization_accepted"] = False
    with pytest.raises(module.AcceptanceError, match="notarization_accepted"):
        module.validate_native_receipt(receipt, expected_appearance_sha256="A" * 64)


def test_native_receipt_binds_exact_shared_appearance_bytes() -> None:
    receipt = successful_native_receipt(settings_sha="B" * 64)
    with pytest.raises(module.AcceptanceError, match="acceptance settings bytes"):
        module.validate_native_receipt(receipt, expected_appearance_sha256="A" * 64)

    validated = module.validate_native_receipt(receipt, expected_appearance_sha256="B" * 64)
    assert validated["validated"] is True
    assert validated["first_result_latency_ms"] == 7.25


@pytest.mark.parametrize("kind", ["open", "reveal", "trash_restore_fixture"])
def test_native_receipt_accepts_only_admitted_safe_action_with_cleanup(kind: str) -> None:
    receipt = successful_native_receipt()
    receipt["file_action"]["kind"] = kind
    assert module.validate_native_receipt(receipt)["file_action_kind"] == kind

    receipt["file_action"]["cleanup_complete"] = False
    with pytest.raises(module.AcceptanceError, match="cleanup_complete"):
        module.validate_native_receipt(receipt)


def test_directory_opus_receipt_is_portability_only_not_windows_native_claim() -> None:
    validated = module.validate_opus_receipt(successful_opus_receipt())
    assert validated["validated_portability_boundary"] is True
    assert validated["complete_directory_opus_parity_verified"] is False
    assert "Windows Directory Opus native execution is not claimed" in validated["claim"]

    overclaim = successful_opus_receipt()
    overclaim["complete_directory_opus_parity_verified"] = True
    with pytest.raises(module.AcceptanceError, match="complete original Directory Opus parity"):
        module.validate_opus_receipt(overclaim)


@pytest.mark.parametrize("invalid_latency", [True, float("nan"), float("inf"), -1])
def test_native_receipt_rejects_invalid_latency(invalid_latency: object) -> None:
    receipt = successful_native_receipt()
    receipt["search"]["first_result_latency_ms"] = invalid_latency
    with pytest.raises(module.AcceptanceError, match="first_result_latency_ms"):
        module.validate_native_receipt(receipt)


def test_native_receipt_requires_json_booleans() -> None:
    receipt = successful_native_receipt()
    receipt["real_mac_executed"] = 1
    with pytest.raises(module.AcceptanceError, match="real_mac_executed"):
        module.validate_native_receipt(receipt)


@pytest.mark.parametrize(("key", "value"), [
    ("package_hashes_verified", 1),
    ("exit_code", False),
    ("complete_directory_opus_parity_verified", None),
    ("manual_desktop_acceptance_complete", "false"),
])
def test_directory_opus_receipt_requires_explicit_evidence_types(key: str, value: object) -> None:
    receipt = successful_opus_receipt()
    receipt[key] = value
    with pytest.raises(module.AcceptanceError):
        module.validate_opus_receipt(receipt)


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("fresh", None),
        ("prior", "opus_receipt_not_fresh"),
        ("outside", "opus_receipt_not_fresh"),
        ("changed", "opus_package_changed"),
        ("failed", "opus_verifier_failed"),
    ],
)
def test_opus_verifier_binds_new_receipt_and_unchanged_package(tmp_path: Path, monkeypatch, scenario, expected_error) -> None:
    package = tmp_path / "package"
    package.mkdir()
    verifier = package / "Verify-Mac.command"
    verifier.write_text("test subprocess is replaced", encoding="utf-8")
    manifest = package / "release-manifest.json"
    manifest.write_text("package manifest", encoding="utf-8")
    checksums = package / "SHA256SUMS"
    checksums.write_text("package checksums", encoding="utf-8")
    home = tmp_path / "home"
    verification_parent = home / "Library/Logs/OpusMac/Verification"
    verification_parent.mkdir(parents=True)
    run_root = (tmp_path if scenario == "outside" else verification_parent) / "run-Ab123456"
    receipt = run_root / "verification.json"
    monkeypatch.setattr(module.Path, "home", lambda: home)

    def create_receipt():
        run_root.mkdir(exist_ok=True)
        receipt.write_text(json.dumps(successful_opus_receipt()), encoding="utf-8")

    if scenario == "prior":
        create_receipt()

    def run(argv, **kwargs):
        assert argv == [str(verifier), "--launch"]
        assert kwargs["cwd"] == package
        if scenario != "failed":
            create_receipt()
        if scenario == "changed":
            manifest.write_text("changed during verification", encoding="utf-8")
        return module.subprocess.CompletedProcess(
            argv, 1 if scenario == "failed" else 0,
            stdout=f"Verification receipt and retained disposable fixtures: {run_root}\n", stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    if expected_error:
        with pytest.raises(module.AcceptanceError) as error:
            module.run_opus_portability_acceptance(package, timeout=10)
        assert error.value.code == expected_error
    else:
        result, _, retained = module.run_opus_portability_acceptance(package, timeout=10)
        assert retained == receipt
        assert result["package_manifest_sha256"] == module.sha256_file(manifest)
        assert result["package_checksums_sha256"] == module.sha256_file(checksums)
        assert result["verification_receipt_sha256"] == module.sha256_file(receipt)


@pytest.mark.parametrize("scenario", ["unsigned", "signed", "wrong_rid", "modified", "traversal", "extra"])
def test_opus_preflight_validates_the_transfer_before_native_work(tmp_path: Path, scenario: str) -> None:
    manifest = "unsigned-build-manifest.json" if scenario == "signed" else "release-manifest.json"
    files = {
        manifest: json.dumps({"schema": "opusmac_release_v1", "rid": "win-x64" if scenario == "wrong_rid" else "osx-arm64", "self_contained": True}),
        "Verify-Mac.command": "verifier",
        "Directory Opus Mac.app/Contents/Info.plist": "bundle info",
        "Directory Opus Mac.app/Contents/MacOS/OpusMac": "app",
        "Directory Opus Mac.app/Contents/MacOS/OpusMac.Checks": "checks",
    }
    if scenario == "signed":
        files["signing-receipt.json"] = "signing receipt"
    hashes = []
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        path.chmod(0o755)
        hashes.append(f"{module.sha256_file(path)}  {name}")
    if scenario == "traversal":
        hashes.append(f"{'A' * 64}  ../outside")
    (tmp_path / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    if scenario == "modified":
        (tmp_path / "Directory Opus Mac.app/Contents/MacOS/OpusMac").write_text("different", encoding="utf-8")
    if scenario == "extra":
        (tmp_path / "unlisted.txt").write_text("not in SHA256SUMS", encoding="utf-8")
    if scenario in {"unsigned", "signed"}:
        identity = module.preflight_opus_package(tmp_path)
        assert identity == (manifest, module.sha256_file(tmp_path / manifest), module.sha256_file(tmp_path / "SHA256SUMS"))
    else:
        with pytest.raises(module.AcceptanceError) as error:
            module.preflight_opus_package(tmp_path)
        assert error.value.code == "opus_package_invalid"


def test_execute_fails_before_any_host_work_off_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module.sys, "platform", "win32")
    args = module.build_parser().parse_args(
        [
            "--native-runner",
            str(tmp_path / "native-runner"),
            "--opus-package",
            str(tmp_path / "opus-package"),
            "--session-parent",
            str(tmp_path / "sessions"),
        ]
    )
    with pytest.raises(module.AcceptanceError) as caught:
        module.execute(args)
    assert caught.value.code == "non_macos_host"
    assert not (tmp_path / "sessions").exists()


def test_cli_plan_never_claims_physical_acceptance(capsys: pytest.CaptureFixture[str]) -> None:
    code = module.main(["--plan", "--json"])
    captured = capsys.readouterr().out
    value = json.loads(captured)
    assert code == 0
    assert value["real_mac_executed"] is False
    assert value["acceptance_may_advance"] is False


def test_window_fixture_sources_are_bounded_acceptance_only() -> None:
    source = module.WINDOW_FIXTURE_SOURCE.read_text(encoding="utf-8")
    helper = module.WINDOW_FIXTURE_AX.read_text(encoding="utf-8")
    assert "NSWindow" in source
    assert "ListaryWindowFixtureQuery" in source
    assert "NSJSONSerialization" in source
    assert "AXIsProcessTrusted" in helper
    assert "ListaryWindowFixtureQuery" in helper
    assert "listary_core" not in helper
    assert "Finder" not in helper

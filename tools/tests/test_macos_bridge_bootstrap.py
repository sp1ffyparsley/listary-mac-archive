from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "macos_bridge_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("macos_bridge_bootstrap", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def write_fake_xcframework(root: Path, *, payload: bytes = b"archive") -> None:
    identifier = "macos-arm64"
    headers = root / identifier / "Headers"
    headers.mkdir(parents=True)
    (root / identifier / "liblistary_ffi.a").write_bytes(payload)
    (headers / "listary_core.h").write_text(
        module.CORE_HEADER.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (headers / "module.modulemap").write_text(
        'module ListaryCore {\n  header "listary_core.h"\n  export *\n}\n',
        encoding="utf-8",
    )
    info = {
        "AvailableLibraries": [
            {
                "LibraryIdentifier": identifier,
                "LibraryPath": "liblistary_ffi.a",
                "HeadersPath": "Headers",
                "SupportedArchitectures": ["arm64"],
                "SupportedPlatform": "macos",
            }
        ],
        "XCFrameworkFormatVersion": "1.0",
    }
    (root / "Info.plist").write_bytes(plistlib.dumps(info, sort_keys=True))


def run_git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def initialize_git_repo(repo: Path) -> None:
    repo.mkdir()
    run_git(repo, "init", "--quiet")
    run_git(repo, "config", "user.name", "Bootstrap Test")
    run_git(repo, "config", "user.email", "bootstrap@example.invalid")
    (repo / "Cargo.lock").write_text(
        "# generated for test\nversion = 4\n", encoding="utf-8"
    )
    (repo / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.97.1"\nprofile = "minimal"\ncomponents = ["rustfmt", "clippy"]\n',
        encoding="utf-8",
    )
    (repo / "source.txt").write_text("first\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "--quiet", "-m", "fixture")


def write_handoff_manifest(repo: Path, members: dict[str, bytes]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for relative, content in sorted(members.items()):
        path = repo.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": "0644",
                "git_state": "tracked",
                "git_mode": "100644",
            }
        )
    status_sha256 = hashlib.sha256(b"").hexdigest()
    manifest: dict[str, object] = {
        "schema": module.HANDOFF_SCHEMA,
        "schema_version": module.HANDOFF_SCHEMA_VERSION,
        "tool": {"name": "create_source_handoff.py", "version": "2.0.0-test"},
        "archive_layout": "repository-root-relative",
        "selection": "Git tracked files plus non-ignored untracked files",
        "git": {
            "head": "a" * 40,
            "branch": "test",
            "dirty": False,
            "status_porcelain_v1_z_sha256": status_sha256,
            "selection_z_sha256": hashlib.sha256(b"selection").hexdigest(),
            "index_stage_z_sha256": hashlib.sha256(b"index").hexdigest(),
        },
        "integrity": {
            "member_algorithm": "sha256",
            "archive_algorithm": "sha256",
            "archive_sidecar_suffix": ".sha256",
            "archive_sidecar_format": "sha256sum",
            "working_tree_sha256": module.handoff_working_tree_digest(
                records,
                status_sha256,
            ),
        },
        "file_count": len(records),
        "total_bytes": sum(int(record["bytes"]) for record in records),
        "files": records,
    }
    (repo / module.HANDOFF_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def test_source_contract_requires_exact_header_version_and_native_crate_types() -> None:
    contract = module.source_contract()
    assert contract["api_version"] == 9
    assert set(contract["crate_types"]) >= {"cdylib", "staticlib", "rlib"}


def test_valid_v2_handoff_is_verified_and_provenance_is_path_free(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "handoff"
    repo.mkdir()
    manifest = write_handoff_manifest(
        repo,
        {
            "Cargo.lock": b"version = 4\n",
            "rust/listary-ffi/src/lib.rs": b"pub fn ready() {}\n",
        },
    )

    evidence = module.capture_source_snapshot(repo)

    assert evidence == {
        "kind": "handoff_manifest_v2",
        "manifest_sha256": hashlib.sha256(
            (repo / module.HANDOFF_MANIFEST_NAME).read_bytes()
        ).hexdigest(),
        "source_head": "a" * 40,
        "working_tree_sha256": manifest["integrity"]["working_tree_sha256"],
        "file_count": 2,
        "total_bytes": sum(
            path.stat().st_size for path in repo.rglob("*") if path.is_file()
        )
        - (repo / module.HANDOFF_MANIFEST_NAME).stat().st_size,
        "tool_version": "2.0.0-test",
    }
    serialized = json.dumps(evidence).replace("\\\\", "/").casefold()
    assert str(repo).replace("\\", "/").casefold() not in serialized


def test_handoff_rejects_tampered_member_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "handoff"
    repo.mkdir()
    write_handoff_manifest(repo, {"source.txt": b"original"})
    (repo / "source.txt").write_bytes(b"tampered")

    with pytest.raises(module.BootstrapError) as caught:
        module.capture_source_snapshot(repo)

    assert caught.value.code == "handoff_member_mismatch"


def test_handoff_rejects_changed_executable_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "handoff"
    repo.mkdir()
    write_handoff_manifest(repo, {"source.txt": b"source"})
    stable_source_hash = module.stable_source_hash

    def executable_hash(
        path: Path,
        *,
        label: str,
        max_bytes: int = module.MAX_HANDOFF_FILE_BYTES,
    ):
        digest, size, mode = stable_source_hash(path, label=label, max_bytes=max_bytes)
        return digest, size, mode | 0o111

    monkeypatch.setattr(module, "stable_source_hash", executable_hash)

    with pytest.raises(module.BootstrapError) as caught:
        module.capture_source_snapshot(repo)

    assert caught.value.code == "handoff_member_mode_mismatch"


def test_handoff_rejects_unsafe_and_portably_colliding_paths() -> None:
    with pytest.raises(module.BootstrapError) as caught:
        module.validate_unique_source_paths(("../escape",))
    assert caught.value.code == "handoff_manifest_unsafe"

    with pytest.raises(module.BootstrapError) as caught:
        module.validate_unique_source_paths(("Caf\u00e9.txt", "Cafe\u0301.txt"))
    assert caught.value.code == "handoff_manifest_unsafe"


@pytest.mark.parametrize("change", ["add", "remove"])
def test_handoff_rejects_member_add_or_remove(tmp_path: Path, change: str) -> None:
    repo = tmp_path / "handoff"
    repo.mkdir()
    write_handoff_manifest(repo, {"source.txt": b"source"})
    if change == "add":
        (repo / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    else:
        (repo / "source.txt").unlink()

    with pytest.raises(module.BootstrapError) as caught:
        module.capture_source_snapshot(repo)

    assert caught.value.code == "handoff_member_set_mismatch"


def test_handoff_rejects_oversized_and_malformed_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / module.HANDOFF_MANIFEST_NAME).write_text("{}" * 64, encoding="utf-8")
    monkeypatch.setattr(module, "MAX_HANDOFF_MANIFEST_BYTES", 32)
    with pytest.raises(module.BootstrapError) as caught:
        module.capture_source_snapshot(oversized)
    assert caught.value.code == "handoff_manifest_too_large"

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / module.HANDOFF_MANIFEST_NAME).write_text(
        '{"schema":"listary_source_handoff","schema":"duplicate"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "MAX_HANDOFF_MANIFEST_BYTES", 64 * 1024 * 1024)
    with pytest.raises(module.BootstrapError) as caught:
        module.capture_source_snapshot(malformed)
    assert caught.value.code == "handoff_manifest_invalid"


def test_fallback_git_digest_changes_with_dirty_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize_git_repo(repo)
    first = module.capture_source_snapshot(repo)

    (repo / "source.txt").write_text("other\n", encoding="utf-8")
    second = module.capture_source_snapshot(repo)

    assert first["kind"] == "git_working_tree"
    assert first["source_head"] == second["source_head"]
    assert first["working_tree_sha256"] != second["working_tree_sha256"]


def test_source_recheck_fails_typed_when_members_change_mid_build(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    initialize_git_repo(repo)
    initial = module.capture_source_snapshot(repo)
    (repo / "added-after-build-start.txt").write_text("new", encoding="utf-8")

    with pytest.raises(module.BootstrapError) as caught:
        module.assert_source_snapshot_unchanged(initial, repo_root=repo)

    assert caught.value.code == "source_changed"


def test_provenance_and_sanitized_receipts_contain_no_home_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    initialize_git_repo(repo)
    provenance = module.collect_provenance(
        strict=True,
        repo_root=repo,
        output=repo / "target" / "ListaryCore.xcframework",
    )
    sanitized = module.privacy_safe_receipt(
        {
            "status": "success",
            "input": str(Path.home() / "portable.json"),
            "nested": {"state_dir": str(Path.home() / "state"), "count": 3},
        }
    )
    combined = {"provenance": provenance, "receipt": sanitized}
    module.assert_distributable_privacy(combined)
    serialized = json.dumps(combined).replace("\\\\", "/").casefold()
    assert str(repo).replace("\\", "/").casefold() not in serialized
    assert str(Path.home()).replace("\\", "/").casefold() not in serialized
    assert "input" not in sanitized
    assert "state_dir" not in sanitized["nested"]


def test_windows_plan_is_static_and_uses_existing_bridge_and_acceptance_owners(
    tmp_path: Path,
) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    plan = module.build_plan(target="aarch64-apple-darwin", output=output)
    assert plan["status"] == "plan"
    assert plan["real_mac_executed"] is False
    assert plan["core_owner"] == "listary-ffi -> listary-core -> RocksDB"
    assert plan["artifacts"]["static_library"].endswith(
        "target\\aarch64-apple-darwin\\release\\liblistary_ffi.a"
    ) or plan["artifacts"]["static_library"].endswith(
        "target/aarch64-apple-darwin/release/liblistary_ffi.a"
    )
    flattened = [" ".join(command) for command in plan["commands"]]
    assert any("--bridge-preflight" in command for command in flattened)
    assert any("--core-smoke" in command for command in flattened)
    assert any("--host-filesystem-smoke" in command for command in flattened)
    assert any("xcodebuild -create-xcframework" in command for command in flattened)
    host_smoke_index = next(
        index for index, command in enumerate(flattened) if "--host-filesystem-smoke" in command
    )
    xcframework_index = next(
        index for index, command in enumerate(flattened) if "xcodebuild -create-xcframework" in command
    )
    assert host_smoke_index < xcframework_index
    cargo_commands = [command for command in plan["commands"] if command[0] == "cargo"]
    assert cargo_commands
    assert all("--locked" in command for command in cargo_commands)
    assert plan["deployment_target"] == "13.0"
    assert plan["build_environment"]["MACOSX_DEPLOYMENT_TARGET"] == "13.0"
    assert Path(plan["build_environment"]["CARGO_TARGET_DIR"]) == module.BUILD_TARGET_DIR
    assert plan["publication"]["final_output"] == str(output)
    assert plan["publication"]["recovery_marker_exists"] is False
    assert plan["publication"]["recovery_backup_exists"] is False
    assert any("native FSEventStream delivery" in item for item in plan["not_proven_by_this_tool"])
    assert any("Quick Look" in item for item in plan["not_proven_by_this_tool"])
    assert str(output) not in plan["commands"][-1]
    provenance = plan["provenance"]
    snapshot = provenance["source"]["snapshot"]
    assert snapshot["source_head"]
    assert isinstance(snapshot["git_dirty"], bool)
    assert len(snapshot["working_tree_sha256"]) == 64
    assert len(provenance["source"]["cargo_lock"]["sha256"]) == 64
    assert provenance["source"]["rust_toolchain"]["channel"] == "1.97.1"
    assert set(provenance["source"]["rust_toolchain"]["components"]) == {
        "clippy",
        "rustfmt",
    }
    serialized_provenance = json.dumps(provenance).replace("\\\\", "/").casefold()
    assert (
        str(module.REPO_ROOT).replace("\\", "/").casefold() not in serialized_provenance
    )
    assert str(Path.home()).replace("\\", "/").casefold() not in serialized_provenance
    assert not output.exists()


def test_execute_bootstrap_fails_closed_off_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.sys, "platform", "win32")
    with pytest.raises(module.BootstrapError) as caught:
        module.execute_bootstrap(
            target="aarch64-apple-darwin",
            output=tmp_path / "ListaryCore.xcframework",
        )
    assert caught.value.code == "non_macos_host"


def test_default_nonmac_plan_target_is_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module.sys, "platform", "win32")
    assert module.default_target() == "aarch64-apple-darwin"


def test_target_artifact_paths_ignore_inherited_cargo_target_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARGO_TARGET_DIR", "/some/other/cargo-output")
    artifacts = module.target_artifacts("aarch64-apple-darwin")
    assert artifacts["static_library"] == (
        module.BUILD_TARGET_DIR / "aarch64-apple-darwin" / "release" / "liblistary_ffi.a"
    )
    assert module.portable_tool_path("aarch64-apple-darwin") == (
        module.BUILD_TARGET_DIR / "aarch64-apple-darwin" / "release" / module.PORTABLE_BINARY_NAME
    )


def test_plan_wires_portable_restore_through_rust_owner_without_embedding_artifact(
    tmp_path: Path,
) -> None:
    portable = tmp_path / "portable.json"
    portable.write_text('{"fixture":true}\n', encoding="utf-8")
    state = tmp_path / "fresh-state"
    output = tmp_path / "ListaryCore.xcframework"
    plan = module.build_plan(
        target="aarch64-apple-darwin",
        output=output,
        portable_config=portable,
        state_dir=state,
        path_maps=[r"C:\Source=/Users/test/Source"],
    )
    restore = plan["portable_restore"]
    assert restore["input"] == str(portable)
    assert restore["state_dir"] == str(state)
    assert restore["path_maps"] == [r"C:\Source=/Users/test/Source"]
    assert restore["target_path_style"] == "posix"
    assert restore["copied_into_xcframework"] is False
    flattened = [" ".join(command) for command in plan["commands"]]
    assert any("listary-portable-config validate" in command for command in flattened)
    assert any("listary-portable-config restore" in command for command in flattened)
    assert any(
        r"--map C:\Source=/Users/test/Source" in command for command in flattened
    )
    portable_build = next(
        command for command in plan["commands"] if PORTABLE_BINARY_NAME in command
    )
    assert "--locked" in portable_build


def test_portable_plan_requires_explicit_destination_state(tmp_path: Path) -> None:
    portable = tmp_path / "portable.json"
    portable.write_text("{}\n", encoding="utf-8")
    with pytest.raises(module.BootstrapError) as caught:
        module.build_plan(
            target="aarch64-apple-darwin",
            output=tmp_path / "ListaryCore.xcframework",
            portable_config=portable,
        )
    assert caught.value.code == "portable_state_dir_missing"


PORTABLE_BINARY_NAME = module.PORTABLE_BINARY_NAME


def test_python_310_is_rejected_with_actionable_error() -> None:
    with pytest.raises(module.BootstrapError) as caught:
        module.require_supported_python((3, 10, 20))
    assert caught.value.code == "python_unsupported"
    assert "Python 3.11+ is required" in str(caught.value)


def test_header_public_symbols_match_abi_v8_contract() -> None:
    symbols = module.header_public_symbols()
    assert len(symbols) == 42
    assert "listary_core_api_version" in symbols
    assert "listary_core_file_search" in symbols
    assert "listary_buffer_free" in symbols


def test_static_library_verification_checks_arch_symbols_and_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "liblistary_ffi.a"
    library.write_bytes(b"archive")
    symbols = "\n".join(f"00000000 T _{name}" for name in module.header_public_symbols())

    def fake_run(command, *, env=None, timeout=900):
        del env, timeout
        if command[0] == "lipo":
            stdout = "arm64\n"
        elif command[0] == "nm":
            stdout = symbols
        elif command[0] == "otool":
            stdout = "cmd LC_BUILD_VERSION\n  platform 1\n    minos 13.0\n"
        else:  # pragma: no cover - test contract guard
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module, "require_tool", lambda name: name)
    monkeypatch.setattr(module, "run_checked", fake_run)
    receipt = module.verify_macos_static_library(
        library,
        target="aarch64-apple-darwin",
        env={},
    )
    assert receipt["architecture"] == "arm64"
    assert receipt["minimum_macos_versions"] == ["13.0"]
    assert receipt["public_symbol_count"] == 42


def test_bootstrap_generated_source_filter_excludes_repo_test_scratch() -> None:
    assert module.generated_source_extra(".test_tmp/prime_pre_partial_agent.exe") is True
    assert module.generated_source_extra(".test_tmp/nested/output.bin") is True
    assert module.generated_source_extra("src/real_source.rs") is False


def test_verify_xcframework_requires_expected_architecture_and_abi_header(
    tmp_path: Path,
) -> None:
    framework = tmp_path / "ListaryCore.xcframework"
    write_fake_xcframework(framework)
    receipt = module.verify_xcframework(framework, target="aarch64-apple-darwin")
    assert receipt["expected_architecture"] == "arm64"
    assert len(receipt["sha256_tree"]) == 64
    assert receipt["slices"][0]["platform"] == "macos"
    with pytest.raises(module.BootstrapError) as caught:
        module.verify_xcframework(
            framework,
            target="aarch64-apple-darwin",
            expected_library_sha256="0" * 64,
        )
    assert caught.value.code == "xcframework_invalid"

    with pytest.raises(module.BootstrapError) as caught:
        module.verify_xcframework(framework, target="x86_64-apple-darwin")
    assert caught.value.code == "xcframework_invalid"


def test_publication_is_idempotent_and_replaces_only_verified_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    workspace = tmp_path / "run"
    workspace.mkdir()

    first = workspace / "first.xcframework"
    write_fake_xcframework(first, payload=b"first")
    receipt = module.publish_xcframework(
        first,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )
    assert receipt["state"] == "published_new"
    assert output.is_dir()

    identical = workspace / "identical.xcframework"
    write_fake_xcframework(identical, payload=b"first")
    receipt = module.publish_xcframework(
        identical,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )
    assert receipt["state"] == "already_current"
    assert identical.is_dir()

    changed = workspace / "changed.xcframework"
    write_fake_xcframework(changed, payload=b"second")
    receipt = module.publish_xcframework(
        changed,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )
    assert receipt["state"] == "replaced_existing"
    assert (output / "macos-arm64" / "liblistary_ffi.a").read_bytes() == b"second"
    backup, marker = module.publication_recovery_paths(output)
    assert not backup.exists()
    assert not marker.exists()


def test_interrupted_replacement_restores_verified_backup(tmp_path: Path) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    backup, marker = module.publication_recovery_paths(output)
    write_fake_xcframework(backup, payload=b"last-good")
    previous_hash = module.verify_xcframework(
        backup,
        target="aarch64-apple-darwin",
    )["sha256_tree"]
    module.atomic_json(
        marker,
        {
            "schema": module.RECOVERY_SCHEMA,
            "phase": "backup_created",
            "output": str(output.resolve(strict=False)),
            "backup": str(backup.resolve(strict=False)),
            "previous_sha256_tree": previous_hash,
            "candidate_sha256_tree": "0" * 64,
        },
    )
    workspace = tmp_path / "recovery-run"
    workspace.mkdir()
    recovered = module.recover_interrupted_publication(
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )
    assert recovered == "restored_previous_output"
    assert output.is_dir()
    assert not backup.exists()
    assert not marker.exists()


def test_partial_legacy_output_is_recoverably_replaced_instead_of_blocking_rerun(
    tmp_path: Path,
) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    output.mkdir()
    (output / "partial-build.txt").write_text("interrupted", encoding="utf-8")
    workspace = tmp_path / "run"
    workspace.mkdir()
    staged = workspace / "staged.xcframework"
    write_fake_xcframework(staged, payload=b"verified")

    receipt = module.publish_xcframework(
        staged,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )

    assert receipt["state"] == "replaced_invalid_existing"
    assert not (output / "partial-build.txt").exists()
    assert (output / "macos-arm64" / "liblistary_ffi.a").read_bytes() == b"verified"


def test_deferred_manifest_publication_rolls_back_pair_after_interruption(tmp_path: Path) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    manifest = tmp_path / "ListaryCore.xcframework.manifest.json"
    workspace = tmp_path / "run"
    workspace.mkdir()
    write_fake_xcframework(output, payload=b"last-good")
    manifest.write_text('{"generation":"last-good"}\n', encoding="utf-8")
    staged = workspace / "changed.xcframework"
    write_fake_xcframework(staged, payload=b"candidate")

    receipt = module.publish_xcframework(
        staged,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
        defer_manifest=manifest,
    )
    assert receipt["state"] == "replaced_existing"
    assert (output / "macos-arm64" / "liblistary_ffi.a").read_bytes() == b"candidate"
    assert not manifest.exists()

    recovered = module.recover_interrupted_publication(
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
    )
    assert recovered == "rolled_back_incomplete_pair"
    assert (output / "macos-arm64" / "liblistary_ffi.a").read_bytes() == b"last-good"
    assert json.loads(manifest.read_text(encoding="utf-8"))["generation"] == "last-good"
    backup, marker = module.publication_recovery_paths(output)
    assert not backup.exists()
    assert not module.publication_manifest_backup_path(manifest).exists()
    assert not marker.exists()


def test_deferred_manifest_publication_finalizes_verified_pair(tmp_path: Path) -> None:
    output = tmp_path / "ListaryCore.xcframework"
    manifest = tmp_path / "ListaryCore.xcframework.manifest.json"
    workspace = tmp_path / "run"
    workspace.mkdir()
    staged = workspace / "first.xcframework"
    write_fake_xcframework(staged, payload=b"candidate")

    receipt = module.publish_xcframework(
        staged,
        output,
        target="aarch64-apple-darwin",
        recovery_workspace=workspace,
        defer_manifest=manifest,
    )
    assert receipt["state"] == "published_new"
    manifest.write_text('{"generation":"candidate"}\n', encoding="utf-8")
    module.finalize_xcframework_manifest_pair(output, manifest)

    backup, marker = module.publication_recovery_paths(output)
    assert not backup.exists()
    assert not module.publication_manifest_backup_path(manifest).exists()
    assert not marker.exists()
    assert module.verify_xcframework(output, target="aarch64-apple-darwin")["sha256_tree"]

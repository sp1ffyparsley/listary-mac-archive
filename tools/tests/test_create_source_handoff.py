from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata
import zipfile

import pytest


TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import create_source_handoff as handoff  # noqa: E402
import macos_bridge_bootstrap as bootstrap  # noqa: E402
from create_source_handoff import HandoffError, create_handoff, verify_handoff  # noqa: E402


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    process = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return process.stdout


def committed_dirty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_bytes(b"/target/\n*.secret\n")
    (repo / "tracked.txt").write_bytes(b"committed\n")
    (repo / "executable.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    git(repo, "add", ".gitignore", "tracked.txt", "executable.sh")
    git(repo, "update-index", "--chmod=+x", "executable.sh")
    git(repo, "commit", "-m", "fixture")
    (repo / "tracked.txt").write_bytes(b"working tree\n")
    (repo / "untracked.txt").write_bytes(b"intentional source\n")
    (repo / "private.secret").write_bytes(b"must not leave this fixture")
    (repo / "target").mkdir()
    (repo / "target" / "generated.bin").write_bytes(b"generated")
    return repo


def committed_dirty_regular_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source-regular"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_bytes(b"/target/\n")
    (repo / "Cargo.lock").write_bytes(b"version = 4\n")
    (repo / "tracked.txt").write_bytes(b"committed\n")
    git(repo, "add", ".gitignore", "Cargo.lock", "tracked.txt")
    git(repo, "commit", "-m", "fixture")
    (repo / "tracked.txt").write_bytes(b"working tree\n")
    (repo / "nested").mkdir()
    (repo / "nested" / "untracked.txt").write_bytes(b"intentional source\n")
    return repo


def sidecar_for(output: Path) -> Path:
    return output.with_name(f"{output.name}.sha256")


def assert_no_staging_files(parent: Path) -> None:
    leftovers = [
        path
        for path in parent.iterdir()
        if path.name.endswith((".archive-stage", ".checksum-stage", ".backup"))
    ]
    assert leftovers == []


def test_handoff_captures_dirty_source_and_excludes_ignored_output(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"

    result = create_handoff(repo, output)
    verified = verify_handoff(output)

    assert result == verified
    assert result["ok"] is True
    assert result["git_dirty"] is True
    assert result["generated_output_included"] is False
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "tracked.txt" in names
        assert "untracked.txt" in names
        assert "target/generated.bin" not in names
        assert "private.secret" not in names
        assert ".git/HEAD" not in names
        assert archive.read("tracked.txt") == b"working tree\n"
        manifest = json.loads(archive.read(handoff.MANIFEST_NAME))
        executable = archive.getinfo("executable.sh")
    assert manifest["schema"] == "listary_source_handoff"
    assert manifest["schema_version"] == 2
    assert manifest["tool"] == {
        "name": "create_source_handoff.py",
        "version": handoff.TOOL_VERSION,
    }
    assert manifest["git"]["dirty"] is True
    assert {entry["path"] for entry in manifest["files"]} == {
        ".gitignore",
        "executable.sh",
        "tracked.txt",
        "untracked.txt",
    }
    assert next(
        entry for entry in manifest["files"] if entry["path"] == "untracked.txt"
    )["git_state"] == "untracked"
    assert (executable.external_attr >> 16) & 0o777 == 0o755
    expected = hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["sha256"] == expected
    assert sidecar_for(output).read_text(encoding="ascii") == (
        f"{expected}  source.zip\n"
    )


def test_actual_repository_ignore_rules_exclude_runtime_evidence_and_state(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    (repo / ".gitignore").write_bytes(
        (TOOLS.parent / ".gitignore").read_bytes() + b"\n*.secret\n"
    )
    runtime = repo / "_runtime" / "listary_ui"
    runtime.mkdir(parents=True)
    for name in ["listary_file_search_current.png", "integration-endpoint-v1.json", "catalog.sst"]:
        (runtime / name).write_bytes(b"disposable generated fixture")
    output = tmp_path / "safe.zip"
    create_handoff(repo, output)
    with zipfile.ZipFile(output) as archive:
        assert not any(name.startswith("_runtime/") for name in archive.namelist())
    assert (runtime / "listary_file_search_current.png").exists()


@pytest.mark.parametrize("name", sorted(handoff.LIVE_ENDPOINT_NAMES))
@pytest.mark.parametrize("tracked", [False, True])
def test_live_endpoints_cannot_be_archived_even_if_tracked(
    tmp_path: Path, name: str, tracked: bool,
) -> None:
    repo = committed_dirty_repo(tmp_path)
    endpoint = repo / name
    endpoint.write_bytes(b"disposable credential fixture")
    if tracked:
        git(repo, "add", name)
    with pytest.raises(HandoffError, match="live endpoint"):
        create_handoff(repo, tmp_path / "unsafe.zip")
    assert not (tmp_path / "unsafe.zip").exists()
    with pytest.raises(HandoffError, match="live endpoint"):
        handoff._canonical_archive_path(f"nested/{name.upper()}")


def test_handoff_is_deterministic_and_replace_is_explicit(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    first = tmp_path / "delivery" / "first.zip"
    second = tmp_path / "delivery" / "second.zip"

    create_handoff(repo, first)
    create_handoff(repo, second)

    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    with pytest.raises(HandoffError, match="already exists"):
        create_handoff(repo, first)
    original = first.read_bytes()
    create_handoff(repo, first, replace=True)
    assert first.read_bytes() == original
    verify_handoff(first)
    assert_no_staging_files(first.parent)


def test_handoff_refuses_in_repo_or_orphan_sidecar_without_replace(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    with pytest.raises(HandoffError, match="outside"):
        create_handoff(repo, repo / "handoff.zip")

    output = tmp_path / "handoff.zip"
    sidecar_for(output).write_bytes(b"orphan")
    with pytest.raises(HandoffError, match="already exists"):
        create_handoff(repo, output)


def test_potential_secret_must_be_ignored_or_deliberately_tracked(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    (repo / ".env.local").write_bytes(b"API_TOKEN=do-not-archive\n")

    with pytest.raises(HandoffError, match="sensitive untracked"):
        create_handoff(repo, tmp_path / "unsafe.zip")

    (repo / ".gitignore").write_bytes(b"/target/\n*.secret\n.env.local\n")
    result = create_handoff(repo, tmp_path / "safe.zip")
    assert result["ok"] is True
    with zipfile.ZipFile(tmp_path / "safe.zip") as archive:
        assert ".env.local" not in archive.namelist()


@pytest.mark.parametrize(
    "unsafe",
    ["../escape", "/absolute", r"C:\absolute", r"folder\ambiguous", "a//b"],
)
def test_archive_path_validation_rejects_traversal_and_ambiguous_names(
    unsafe: str,
) -> None:
    with pytest.raises(HandoffError):
        handoff._canonical_archive_path(unsafe)


def test_handoff_rejects_source_paths_and_member_counts_the_bootstrap_cannot_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HandoffError, match="length limit"):
        handoff._canonical_archive_path("x" * (handoff.MAX_SOURCE_PATH_BYTES + 1))

    monkeypatch.setattr(handoff, "MAX_SOURCE_FILES", 2)
    with pytest.raises(HandoffError, match="member-count"):
        handoff._validated_paths(b"a.txt\0b.txt\0c.txt\0")


def test_archive_name_validation_rejects_case_and_unicode_collisions() -> None:
    with pytest.raises(HandoffError, match="collide"):
        handoff._validated_paths(b"Folder/File.txt\0folder/file.TXT\0")

    composed = unicodedata.normalize("NFC", "cafe\N{COMBINING ACUTE ACCENT}.txt")
    decomposed = unicodedata.normalize("NFD", composed)
    with pytest.raises(HandoffError, match="collide"):
        handoff._validated_paths(
            composed.encode("utf-8") + b"\0" + decomposed.encode("utf-8") + b"\0"
        )

    with pytest.raises(HandoffError, match="reserved|collide"):
        handoff._validated_paths(b"handoff_manifest.JSON\0")


def test_git_index_symlink_is_rejected_even_when_checkout_is_regular(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    blob = git(repo, "hash-object", "-w", "--stdin", input_bytes=b"tracked.txt").strip()
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        "120000",
        blob.decode("ascii"),
        "link-to-source",
    )
    (repo / "link-to-source").write_bytes(b"tracked.txt")

    with pytest.raises(HandoffError, match="symlink"):
        create_handoff(repo, tmp_path / "symlink.zip")


def test_gitlink_special_entry_is_rejected(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        head,
        "vendor/nested",
    )

    with pytest.raises(HandoffError, match="submodule"):
        create_handoff(repo, tmp_path / "gitlink.zip")


def test_size_limits_fail_before_publication(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "limited.zip"

    with pytest.raises(HandoffError, match="per-file"):
        create_handoff(repo, output, max_file_bytes=2)
    assert not output.exists()
    assert not sidecar_for(output).exists()

    with pytest.raises(HandoffError, match="total limit"):
        create_handoff(repo, output, max_total_bytes=10)
    assert not output.exists()
    assert not sidecar_for(output).exists()

    with pytest.raises(HandoffError, match="bootstrap admission limits"):
        create_handoff(
            repo,
            output,
            max_file_bytes=handoff.DEFAULT_MAX_FILE_BYTES + 1,
        )
    with pytest.raises(HandoffError, match="bootstrap admission limits"):
        create_handoff(
            repo,
            output,
            max_total_bytes=handoff.DEFAULT_MAX_TOTAL_BYTES + 1,
        )
    assert not output.exists()
    assert not sidecar_for(output).exists()


def test_created_handoff_extracts_into_bootstrap_accepted_source_snapshot(tmp_path: Path) -> None:
    repo = committed_dirty_regular_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"

    created = create_handoff(repo, output)
    verify_handoff(output)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(output, mode="r") as archive:
        archive.extractall(extracted)

    evidence = bootstrap.capture_source_snapshot(extracted)
    with zipfile.ZipFile(output, mode="r") as archive:
        manifest = json.loads(archive.read(handoff.MANIFEST_NAME))

    assert evidence["kind"] == "handoff_manifest_v2"
    assert evidence["source_head"] == created["git_head"].lower()
    assert evidence["working_tree_sha256"] == manifest["integrity"]["working_tree_sha256"]
    assert evidence["file_count"] == created["file_count"]
    assert evidence["total_bytes"] == created["source_bytes"]
    assert evidence["tool_version"] == handoff.TOOL_VERSION


def test_source_change_during_build_fails_closed_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    original_write = handoff._write_archive

    def mutate_then_write(
        path: Path,
        files: list[handoff.SourceFile],
        manifest_bytes: bytes,
    ) -> None:
        (repo / "tracked.txt").write_bytes(b"changed after validation\n")
        original_write(path, files, manifest_bytes)

    monkeypatch.setattr(handoff, "_write_archive", mutate_then_write)
    with pytest.raises(HandoffError, match="changed"):
        create_handoff(repo, output)

    assert not output.exists()
    assert not sidecar_for(output).exists()
    assert_no_staging_files(output.parent)


def test_git_enumeration_change_during_build_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    original_write = handoff._write_archive

    def write_then_add_untracked(
        path: Path,
        files: list[handoff.SourceFile],
        manifest_bytes: bytes,
    ) -> None:
        original_write(path, files, manifest_bytes)
        (repo / "appeared-during-build.txt").write_bytes(b"late source\n")

    monkeypatch.setattr(handoff, "_write_archive", write_then_add_untracked)
    with pytest.raises(HandoffError, match="repository changed"):
        create_handoff(repo, output)

    assert not output.exists()
    assert not sidecar_for(output).exists()
    assert_no_staging_files(output.parent)


def test_mid_build_failure_cleans_all_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"

    def fail_write(path: Path, _files: object, _manifest: bytes) -> None:
        path.write_bytes(b"partial")
        raise HandoffError("injected archive failure")

    monkeypatch.setattr(handoff, "_write_archive", fail_write)
    with pytest.raises(HandoffError, match="injected"):
        create_handoff(repo, output)

    assert not output.exists()
    assert not sidecar_for(output).exists()
    assert_no_staging_files(output.parent)


def test_failed_replace_restores_previous_archive_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    create_handoff(repo, output)
    old_archive = output.read_bytes()
    old_sidecar = sidecar_for(output).read_bytes()
    (repo / "tracked.txt").write_bytes(b"a newer working tree\n")

    real_replace = handoff.os.replace
    replace_calls = 0

    def fail_fourth_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 4:
            raise OSError("injected publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(handoff.os, "replace", fail_fourth_replace)
    with pytest.raises(HandoffError, match="publication failed"):
        create_handoff(repo, output, replace=True)

    assert output.read_bytes() == old_archive
    assert sidecar_for(output).read_bytes() == old_sidecar
    verify_handoff(output)
    assert_no_staging_files(output.parent)


def test_failed_first_publication_leaves_no_partial_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    real_replace = handoff.os.replace
    replace_calls = 0

    def fail_second_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected sidecar publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(handoff.os, "replace", fail_second_replace)
    with pytest.raises(HandoffError, match="publication failed"):
        create_handoff(repo, output)

    assert not output.exists()
    assert not sidecar_for(output).exists()
    assert_no_staging_files(output.parent)


def test_interrupted_publication_rolls_back_reserved_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged_archive = tmp_path / "archive.stage"
    staged_checksum = tmp_path / "checksum.stage"
    output = tmp_path / "output.zip"
    checksum = sidecar_for(output)
    staged_archive.write_bytes(b"archive")
    staged_checksum.write_bytes(b"checksum")
    real_replace = handoff.os.replace
    calls = 0

    def interrupt_second_replace(
        source: os.PathLike[str], destination: os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(handoff.os, "replace", interrupt_second_replace)
    with pytest.raises(KeyboardInterrupt):
        handoff._publish_pair(
            staged_archive,
            staged_checksum,
            output,
            checksum,
            replace=False,
        )

    assert not output.exists()
    assert not checksum.exists()


def test_hard_interruption_marker_restores_previous_archive_pair(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    create_handoff(repo, output)
    checksum = sidecar_for(output)
    old_archive = output.read_bytes()
    old_checksum = checksum.read_bytes()
    marker, archive_backup, checksum_backup = handoff._publication_paths(output, checksum)
    candidate = b"partial-new-generation"
    recovery = {
        "schema": handoff.PUBLICATION_SCHEMA,
        "phase": "archive_published",
        "output": str(output.resolve(strict=False)),
        "checksum": str(checksum.resolve(strict=False)),
        "archive_backup": str(archive_backup.resolve(strict=False)),
        "checksum_backup": str(checksum_backup.resolve(strict=False)),
        "previous_archive": True,
        "previous_checksum": True,
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
    }
    handoff._atomic_publication_marker(marker, recovery)
    os.replace(output, archive_backup)
    os.replace(checksum, checksum_backup)
    output.write_bytes(candidate)

    recovered = handoff._recover_publication_pair(output, checksum)

    assert recovered == "rolled_back_interrupted_publication"
    assert output.read_bytes() == old_archive
    assert checksum.read_bytes() == old_checksum
    assert not marker.exists()
    assert not archive_backup.exists()
    assert not checksum_backup.exists()
    verify_handoff(output)


def test_self_verifier_detects_sidecar_and_member_tampering(tmp_path: Path) -> None:
    repo = committed_dirty_repo(tmp_path)
    output = tmp_path / "delivery" / "source.zip"
    create_handoff(repo, output)

    original_sidecar = sidecar_for(output).read_bytes()
    sidecar_for(output).write_bytes(f"{'0' * 64}  source.zip\n".encode("ascii"))
    with pytest.raises(HandoffError, match="SHA-256"):
        verify_handoff(output)
    sidecar_for(output).write_bytes(original_sidecar)

    tampered = tmp_path / "delivery" / "tampered.zip"
    with zipfile.ZipFile(output, "r") as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as destination:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "tracked.txt":
                data = b"tampered but manifest unchanged\n"
            destination.writestr(info, data)
    tampered_digest = hashlib.sha256(tampered.read_bytes()).hexdigest()
    sidecar_for(tampered).write_text(
        f"{tampered_digest}  tampered.zip\n", encoding="ascii", newline="\n"
    )
    with pytest.raises(HandoffError, match="size|hash"):
        verify_handoff(tampered)


def test_missing_git_is_reported_as_handoff_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(handoff.subprocess, "run", missing_git)
    with pytest.raises(HandoffError, match="Git is unavailable"):
        create_handoff(tmp_path, tmp_path / "outside.zip")

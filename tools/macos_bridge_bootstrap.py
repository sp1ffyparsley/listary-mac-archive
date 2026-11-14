#!/usr/bin/env python3
"""Build and verify the existing Listary Rust/C bridge for the first real Mac session.

Windows can exercise --plan to validate source/package assumptions without making
macOS claims. Real execution is intentionally Darwin-only and stops before any
AppKit, Accessibility, FSEvents, HID, signing, or notarization qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import unicodedata


if sys.version_info >= (3, 11):
    import tomllib
else:  # Keep the CLI importable long enough to return a clear unsupported-Python receipt.
    tomllib = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
FFI_MANIFEST = REPO_ROOT / "rust" / "listary-ffi" / "Cargo.toml"
CORE_HEADER = REPO_ROOT / "rust" / "listary-ffi" / "include" / "listary_core.h"
ACCEPTANCE = REPO_ROOT / "tools" / "quick_switch_acceptance.py"
LOCKFILE = REPO_ROOT / "Cargo.lock"
RUST_TOOLCHAIN = REPO_ROOT / "rust-toolchain.toml"
SCHEMA = "listary_macos_bridge_bootstrap_v2"
EXPECTED_API_VERSION = 9
EXPECTED_CRATE_TYPES = {"cdylib", "staticlib", "rlib"}
DEFAULT_OUTPUT = (
    REPO_ROOT / "target" / "listary-macos-bridge" / "ListaryCore.xcframework"
)
BUILD_TARGET_DIR = REPO_ROOT / "target"
PORTABLE_BINARY_NAME = "listary-portable-config"
MINIMUM_PYTHON = (3, 11)
MACOS_DEPLOYMENT_TARGET = "13.0"
SUPPORTED_MAC_TARGETS = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
}
RECOVERY_SCHEMA = "listary_xcframework_publish_recovery_v1"
HANDOFF_MANIFEST_NAME = "HANDOFF_MANIFEST.json"
HANDOFF_SCHEMA = "listary_source_handoff"
HANDOFF_SCHEMA_VERSION = 2
MAX_HANDOFF_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_FILE_BYTES = 512 * 1024 * 1024
MAX_HANDOFF_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_HANDOFF_FILES = 250_000
MAX_GIT_ENUMERATION_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class BootstrapError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def debug_log(event: str, **fields: Any) -> None:
    """Write structured diagnostics without contaminating JSON stdout."""

    payload = {"event": event, **fields}
    print(
        f"[macos-bootstrap] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
        file=sys.stderr,
        flush=True,
    )


def require_supported_python(version_info: Sequence[int] | None = None) -> str:
    version = tuple(version_info or sys.version_info)
    normalized = tuple(int(part) for part in version[:3])
    if normalized[:2] < MINIMUM_PYTHON:
        detected = ".".join(str(part) for part in normalized)
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise BootstrapError(
            "python_unsupported",
            f"Python {required}+ is required; detected Python {detected} at {sys.executable}.",
        )
    return ".".join(str(part) for part in normalized)


def validate_target(target: str) -> str:
    architecture = SUPPORTED_MAC_TARGETS.get(target)
    if architecture is None:
        supported = ", ".join(sorted(SUPPORTED_MAC_TARGETS))
        raise BootstrapError(
            "unsupported_target",
            f"Unsupported macOS Rust target {target!r}; choose one of: {supported}.",
        )
    return architecture


def validate_output_path(output: Path) -> None:
    if output.suffix.casefold() != ".xcframework":
        raise BootstrapError(
            "output_extension_invalid",
            f"XCFramework output must end in .xcframework: {output}",
        )


def is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BootstrapError(
                "handoff_manifest_invalid",
                f"Handoff manifest contains duplicate JSON key {key!r}.",
            )
        value[key] = item
    return value


def canonical_source_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 4096:
        raise BootstrapError(
            "handoff_manifest_unsafe", "Handoff member path is empty or too long."
        )
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise BootstrapError(
            "handoff_manifest_unsafe",
            f"Handoff member path is ambiguous or contains control characters: {value!r}",
        )
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value.startswith("/")
    ):
        raise BootstrapError(
            "handoff_manifest_unsafe",
            f"Handoff member path must be repository-relative: {value!r}",
        )
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(
            "handoff_manifest_unsafe",
            f"Handoff member path contains an unsafe component: {value!r}",
        )
    return "/".join(parts)


def portable_source_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def validate_unique_source_paths(paths: Sequence[str]) -> tuple[str, ...]:
    exact: set[str] = set()
    portable: dict[str, str] = {
        portable_source_name(HANDOFF_MANIFEST_NAME): HANDOFF_MANIFEST_NAME
    }
    validated: list[str] = []
    for raw_path in paths:
        path = canonical_source_path(raw_path)
        if path in exact:
            raise BootstrapError(
                "handoff_manifest_unsafe",
                f"Handoff manifest contains duplicate member {path!r}.",
            )
        key = portable_source_name(path)
        previous = portable.get(key)
        if previous is not None:
            raise BootstrapError(
                "handoff_manifest_unsafe",
                "Handoff members collide under case-insensitive NFC rules: "
                f"{previous!r} and {path!r}.",
            )
        exact.add(path)
        portable[key] = path
        validated.append(path)
    return tuple(validated)


def stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
    )


def require_regular_source(value: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise BootstrapError(
            "source_member_unsafe", f"Source member is not a regular file: {label}"
        )
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if int(getattr(value, "st_file_attributes", 0)) & reparse_flag:
        raise BootstrapError(
            "source_member_unsafe", f"Source member is a reparse point: {label}"
        )


def stable_source_hash(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_HANDOFF_FILE_BYTES,
) -> tuple[str, int, int]:
    try:
        before = path.lstat()
        require_regular_source(before, label=label)
        if path.is_symlink():
            raise BootstrapError(
                "source_member_unsafe", f"Source member is a symlink: {label}"
            )
        if before.st_size > max_bytes:
            raise BootstrapError(
                "source_member_too_large",
                f"Source member exceeds the safe verification limit: {label}",
            )
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                size += len(chunk)
                if size > max_bytes:
                    raise BootstrapError(
                        "source_member_too_large",
                        f"Source member exceeds the safe verification limit: {label}",
                    )
                digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            "source_member_unreadable",
            f"Source member is missing or unreadable: {label}: {exc}",
        ) from exc
    fingerprints = {
        stat_fingerprint(before),
        stat_fingerprint(opened_before),
        stat_fingerprint(opened_after),
        stat_fingerprint(after),
    }
    if len(fingerprints) != 1 or size != int(after.st_size):
        raise BootstrapError(
            "source_changed", f"Source member changed while hashing: {label}"
        )
    return digest.hexdigest(), size, int(after.st_mode)


def stable_source_bytes(path: Path, *, label: str, max_bytes: int) -> bytes:
    """Read one bounded source file while detecting path or byte replacement."""

    try:
        before = path.lstat()
        require_regular_source(before, label=label)
        if path.is_symlink():
            raise BootstrapError(
                "source_member_unsafe", f"Source member is a symlink: {label}"
            )
        if before.st_size > max_bytes:
            raise BootstrapError(
                "handoff_manifest_too_large",
                f"Handoff manifest exceeds {max_bytes} bytes.",
            )
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            raw = stream.read(max_bytes + 1)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            "handoff_manifest_unreadable",
            f"Cannot read {label}: {exc}",
        ) from exc
    if len(raw) > max_bytes:
        raise BootstrapError(
            "handoff_manifest_too_large",
            f"Handoff manifest exceeds {max_bytes} bytes.",
        )
    fingerprints = {
        stat_fingerprint(before),
        stat_fingerprint(opened_before),
        stat_fingerprint(opened_after),
        stat_fingerprint(after),
    }
    if len(fingerprints) != 1 or len(raw) != int(after.st_size):
        raise BootstrapError(
            "source_changed", f"Source member changed while reading: {label}"
        )
    return raw


def source_path(repo_root: Path, relative: str) -> Path:
    path = repo_root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BootstrapError(
            "source_member_unsafe",
            f"Source member escapes the repository or is missing: {relative}",
        ) from exc
    return path


def handoff_working_tree_digest(
    records: Sequence[Mapping[str, Any]],
    status_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"status:{status_sha256}\n".encode("ascii"))
    for record in records:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def generated_source_extra(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = path.parts
    if not parts:
        return False
    if parts[0] in {".git", ".test_tmp", "target", "bench-data", "tmp"}:
        return True
    if "__pycache__" in parts or any(part.endswith(".dSYM") for part in parts):
        return True
    return path.name == ".DS_Store"


def relative_exclusions(repo_root: Path, paths: Sequence[Path]) -> tuple[str, ...]:
    root = repo_root.resolve(strict=True)
    values: set[str] = set()
    for path in paths:
        try:
            relative = path.resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            continue
        if relative and relative != ".":
            values.add(relative.rstrip("/"))
    return tuple(sorted(values))


def is_excluded_source_path(relative: str, exclusions: Sequence[str]) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + "/") for prefix in exclusions
    )


def filesystem_source_members(
    repo_root: Path,
    *,
    expected_paths: Sequence[str],
    excluded_paths: Sequence[Path],
) -> tuple[str, ...]:
    expected = set(expected_paths)
    expected_prefixes: set[str] = set()
    for member in expected:
        parts = member.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_prefixes.add("/".join(parts[:index]))
    exclusions = relative_exclusions(repo_root, excluded_paths)
    members: list[str] = []
    tree_entries = 0
    root = repo_root.resolve(strict=True)

    def walk_error(error: OSError) -> None:
        raise BootstrapError(
            "handoff_member_unsafe",
            f"Cannot enumerate handoff source exactly: {error}",
        ) from error

    for current, directories, filenames in os.walk(
        root,
        topdown=True,
        onerror=walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for directory in sorted(directories):
            child = current_path / directory
            relative = child.relative_to(root).as_posix()
            expected_prefix = relative in expected_prefixes
            ignored = relative == ".git" or (
                not expected_prefix
                and (
                    is_excluded_source_path(relative, exclusions)
                    or generated_source_extra(relative)
                )
            )
            if ignored:
                continue
            tree_entries += 1
            if tree_entries > MAX_HANDOFF_FILES * 2:
                raise BootstrapError(
                    "handoff_manifest_invalid",
                    "Handoff source tree exceeds the safe enumeration limit.",
                )
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise BootstrapError(
                    "handoff_member_unsafe",
                    f"Cannot inspect handoff source directory {relative}: {exc}",
                ) from exc
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or child.is_symlink()
                or int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag
            ):
                raise BootstrapError(
                    "handoff_member_unsafe",
                    f"Unexpected linked or non-directory path in handoff source: {relative}",
                )
            kept_directories.append(directory)
        directories[:] = kept_directories
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            if relative == HANDOFF_MANIFEST_NAME:
                continue
            if relative not in expected and (
                is_excluded_source_path(relative, exclusions)
                or generated_source_extra(relative)
            ):
                continue
            tree_entries += 1
            if tree_entries > MAX_HANDOFF_FILES * 2:
                raise BootstrapError(
                    "handoff_manifest_invalid",
                    "Handoff source tree exceeds the safe enumeration limit.",
                )
            canonical_source_path(relative)
            try:
                metadata = path.lstat()
                require_regular_source(metadata, label=relative)
            except OSError as exc:
                raise BootstrapError(
                    "handoff_member_unsafe",
                    f"Cannot inspect handoff source member {relative}: {exc}",
                ) from exc
            if path.is_symlink():
                raise BootstrapError(
                    "handoff_member_unsafe",
                    f"Handoff source member is a symlink: {relative}",
                )
            members.append(relative)
            if len(members) > MAX_HANDOFF_FILES:
                raise BootstrapError(
                    "handoff_manifest_invalid",
                    "Handoff source exceeds the safe member-count limit.",
                )
    validate_unique_source_paths(members)
    return tuple(sorted(members))


def read_handoff_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    if os.path.lexists(path) and path.is_symlink():
        raise BootstrapError(
            "handoff_manifest_unsafe", "Handoff manifest must not be a symlink."
        )
    raw = stable_source_bytes(
        path,
        label=HANDOFF_MANIFEST_NAME,
        max_bytes=MAX_HANDOFF_MANIFEST_BYTES,
    )
    try:
        decoded = raw.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, object_pairs_hook=json_object_without_duplicates)
    except BootstrapError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BootstrapError(
            "handoff_manifest_invalid",
            f"Handoff manifest is not bounded valid UTF-8 JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff manifest root must be an object."
        )
    return parsed, raw


def validate_handoff_records(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected_top_level = {
        "schema",
        "schema_version",
        "tool",
        "archive_layout",
        "selection",
        "git",
        "integrity",
        "file_count",
        "total_bytes",
        "files",
    }
    if set(manifest) != expected_top_level:
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff manifest fields are not v2."
        )
    if (
        manifest.get("schema") != HANDOFF_SCHEMA
        or manifest.get("schema_version") != HANDOFF_SCHEMA_VERSION
    ):
        raise BootstrapError(
            "handoff_manifest_schema_unsupported",
            "Handoff manifest must use listary_source_handoff schema version 2.",
        )
    tool = manifest.get("tool")
    if (
        not isinstance(tool, dict)
        or set(tool) != {"name", "version"}
        or tool.get("name") != "create_source_handoff.py"
        or not isinstance(tool.get("version"), str)
        or not tool["version"]
        or len(tool["version"]) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in tool["version"]
        )
        or looks_like_absolute_path(tool["version"])
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff tool provenance is invalid."
        )
    if (
        manifest.get("archive_layout") != "repository-root-relative"
        or manifest.get("selection")
        != "Git tracked files plus non-ignored untracked files"
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff selection provenance is invalid."
        )
    git = manifest.get("git")
    integrity = manifest.get("integrity")
    records = manifest.get("files")
    if (
        not isinstance(git, dict)
        or not isinstance(integrity, dict)
        or not isinstance(records, list)
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff manifest structure is invalid."
        )
    expected_git_keys = {
        "head",
        "branch",
        "dirty",
        "status_porcelain_v1_z_sha256",
        "selection_z_sha256",
        "index_stage_z_sha256",
    }
    if set(git) != expected_git_keys:
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff Git provenance is malformed."
        )
    head = git.get("head")
    branch = git.get("branch")
    if (
        not isinstance(head, str)
        or len(head) not in (40, 64)
        or any(character not in "0123456789abcdefABCDEF" for character in head)
        or (branch is not None and (not isinstance(branch, str) or len(branch) > 1024))
        or not isinstance(git.get("dirty"), bool)
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff Git provenance is invalid."
        )
    status_sha256 = git.get("status_porcelain_v1_z_sha256")
    if not all(
        is_lower_sha256(value)
        for value in (
            status_sha256,
            git.get("selection_z_sha256"),
            git.get("index_stage_z_sha256"),
        )
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff Git digests are invalid."
        )
    expected_integrity_keys = {
        "member_algorithm",
        "archive_algorithm",
        "archive_sidecar_suffix",
        "archive_sidecar_format",
        "working_tree_sha256",
    }
    if set(integrity) != expected_integrity_keys or (
        integrity.get("member_algorithm") != "sha256"
        or integrity.get("archive_algorithm") != "sha256"
        or integrity.get("archive_sidecar_suffix") != ".sha256"
        or integrity.get("archive_sidecar_format") != "sha256sum"
        or not is_lower_sha256(integrity.get("working_tree_sha256"))
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff integrity provenance is invalid."
        )
    if not records or len(records) > MAX_HANDOFF_FILES:
        raise BootstrapError(
            "handoff_manifest_invalid",
            "Handoff manifest member count is empty or exceeds the safe limit.",
        )
    validated: list[dict[str, Any]] = []
    paths: list[str] = []
    total_bytes = 0
    record_keys = {"path", "bytes", "sha256", "mode", "git_state", "git_mode"}
    for raw_record in records:
        if not isinstance(raw_record, dict) or set(raw_record) != record_keys:
            raise BootstrapError(
                "handoff_manifest_invalid", "Handoff file record is malformed."
            )
        path_value = raw_record.get("path")
        size = raw_record.get("bytes")
        digest = raw_record.get("sha256")
        mode = raw_record.get("mode")
        git_state = raw_record.get("git_state")
        git_mode = raw_record.get("git_mode")
        if not isinstance(path_value, str):
            raise BootstrapError(
                "handoff_manifest_invalid", "Handoff file path is invalid."
            )
        path_value = canonical_source_path(path_value)
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_HANDOFF_FILE_BYTES
            or not is_lower_sha256(digest)
            or mode not in {"0644", "0755"}
            or git_state not in {"tracked", "untracked"}
            or (git_state == "tracked" and git_mode not in {"100644", "100755"})
            or (git_mode == "100644" and mode != "0644")
            or (git_mode == "100755" and mode != "0755")
            or (git_state == "untracked" and git_mode is not None)
        ):
            raise BootstrapError(
                "handoff_manifest_invalid", f"Invalid handoff record: {path_value}"
            )
        total_bytes += size
        if total_bytes > MAX_HANDOFF_TOTAL_BYTES:
            raise BootstrapError(
                "handoff_manifest_invalid", "Handoff source exceeds total byte limit."
            )
        record = dict(raw_record)
        record["path"] = path_value
        validated.append(record)
        paths.append(path_value)
    validate_unique_source_paths(paths)
    if paths != sorted(paths):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff records are not path-sorted."
        )
    file_count = manifest.get("file_count")
    manifest_total_bytes = manifest.get("total_bytes")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or not isinstance(manifest_total_bytes, int)
        or isinstance(manifest_total_bytes, bool)
        or file_count != len(validated)
        or manifest_total_bytes != total_bytes
    ):
        raise BootstrapError(
            "handoff_manifest_invalid", "Handoff file totals are inconsistent."
        )
    if handoff_working_tree_digest(validated, str(status_sha256)) != integrity.get(
        "working_tree_sha256"
    ):
        raise BootstrapError(
            "handoff_manifest_invalid",
            "Handoff aggregate working-tree digest does not match its records.",
        )
    return validated


def verify_handoff_source(
    repo_root: Path,
    *,
    manifest_path: Path | None = None,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    root = repo_root.resolve(strict=True)
    manifest_file = manifest_path or root / HANDOFF_MANIFEST_NAME
    manifest, raw_manifest = read_handoff_manifest(manifest_file)
    records = validate_handoff_records(manifest)
    expected_paths = tuple(str(record["path"]) for record in records)
    actual_paths = filesystem_source_members(
        root,
        expected_paths=expected_paths,
        excluded_paths=excluded_paths,
    )
    if actual_paths != expected_paths:
        missing = len(set(expected_paths).difference(actual_paths))
        added = len(set(actual_paths).difference(expected_paths))
        raise BootstrapError(
            "handoff_member_set_mismatch",
            f"Handoff source member list changed (missing={missing}, added={added}).",
        )
    verified_bytes = 0
    for record in records:
        relative = str(record["path"])
        digest, size, mode = stable_source_hash(
            source_path(root, relative), label=relative
        )
        if size != record["bytes"] or digest != record["sha256"]:
            raise BootstrapError(
                "handoff_member_mismatch",
                f"Handoff source member bytes do not match the manifest: {relative}",
            )
        expected_executable = record["mode"] == "0755"
        actual_executable = bool(stat.S_IMODE(mode) & 0o111)
        if actual_executable != expected_executable:
            raise BootstrapError(
                "handoff_member_mode_mismatch",
                "Handoff source member executable mode changed during extraction: "
                f"{relative}",
            )
        verified_bytes += size
    if verified_bytes != manifest["total_bytes"]:
        raise BootstrapError(
            "handoff_member_mismatch", "Verified handoff byte total changed."
        )
    return {
        "kind": "handoff_manifest_v2",
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "source_head": manifest["git"]["head"].lower(),
        "working_tree_sha256": manifest["integrity"]["working_tree_sha256"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "tool_version": manifest["tool"]["version"],
    }


def run_git_bytes(
    repo_root: Path,
    *arguments: str,
    allowed_returncodes: Sequence[int] = (0,),
) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise BootstrapError(
            "git_missing",
            "Git is required when HANDOFF_MANIFEST.json is not present.",
        )
    try:
        completed = subprocess.run(
            [git, *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(
            "git_source_snapshot_failed",
            f"Cannot run Git source enumeration in {repo_root}: {exc}",
        ) from exc
    if completed.returncode not in allowed_returncodes:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BootstrapError(
            "git_source_snapshot_failed",
            f"Git {' '.join(arguments)} failed ({completed.returncode}): {detail}",
        )
    if len(completed.stdout) > MAX_GIT_ENUMERATION_BYTES:
        raise BootstrapError(
            "git_source_snapshot_too_large",
            f"Git {' '.join(arguments)} output exceeds the safe enumeration limit.",
        )
    return completed.stdout


def decode_git_text(raw: bytes, *, label: str) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BootstrapError(
            "git_source_snapshot_invalid",
            f"{label} is not valid UTF-8: {exc}",
        ) from exc


def parse_git_paths(raw: bytes) -> tuple[str, ...]:
    entries = raw.split(b"\0")
    if entries and entries[-1] == b"":
        entries.pop()
    if not entries or len(entries) > MAX_HANDOFF_FILES:
        raise BootstrapError(
            "git_source_snapshot_invalid",
            "Git source member count is empty or exceeds the safe limit.",
        )
    paths = [
        canonical_source_path(decode_git_text(entry, label="Git source path"))
        for entry in entries
    ]
    validate_unique_source_paths(paths)
    return tuple(sorted(paths))


def parse_git_index(raw: bytes) -> dict[str, str]:
    entries = raw.split(b"\0")
    if entries and entries[-1] == b"":
        entries.pop()
    if len(entries) > MAX_HANDOFF_FILES:
        raise BootstrapError(
            "git_source_snapshot_invalid",
            "Git index member count exceeds the safe limit.",
        )
    modes: dict[str, str] = {}
    for entry in entries:
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode_bytes, _object_id, stage_bytes = metadata.split(b" ", 2)
        except ValueError as exc:
            raise BootstrapError(
                "git_source_snapshot_invalid",
                "Git returned malformed index metadata.",
            ) from exc
        relative = canonical_source_path(
            decode_git_text(raw_path, label="Git index path")
        )
        mode = decode_git_text(mode_bytes, label="Git index mode")
        stage = decode_git_text(stage_bytes, label="Git index stage")
        if stage != "0":
            raise BootstrapError(
                "git_source_snapshot_unsafe",
                f"Unmerged Git index entry cannot be used as source: {relative}",
            )
        if relative in modes:
            raise BootstrapError(
                "git_source_snapshot_unsafe",
                f"Duplicate Git index entry cannot be used as source: {relative}",
            )
        if mode == "120000":
            raise BootstrapError(
                "git_source_snapshot_unsafe",
                f"Git symlink cannot be used as source: {relative}",
            )
        if mode == "160000":
            raise BootstrapError(
                "git_source_snapshot_unsafe",
                f"Git submodule cannot be used as a regular source file: {relative}",
            )
        if mode not in {"100644", "100755"}:
            raise BootstrapError(
                "git_source_snapshot_unsafe",
                f"Unsupported Git index mode {mode!r}: {relative}",
            )
        modes[relative] = mode
    validate_unique_source_paths(tuple(modes))
    return modes


def git_source_enumeration(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve(strict=True)
    reported_root = decode_git_text(
        run_git_bytes(root, "rev-parse", "--show-toplevel"),
        label="Git repository root",
    ).strip()
    try:
        if Path(reported_root).resolve(strict=True) != root:
            raise BootstrapError(
                "git_repository_root_mismatch",
                f"Expected a standalone Git root at {root}; Git reported {reported_root}.",
            )
    except OSError as exc:
        raise BootstrapError(
            "git_repository_root_mismatch",
            f"Git reported an inaccessible repository root: {reported_root}",
        ) from exc
    head = decode_git_text(
        run_git_bytes(root, "rev-parse", "--verify", "HEAD"),
        label="Git HEAD",
    ).strip()
    if len(head) not in (40, 64) or any(
        character not in "0123456789abcdefABCDEF" for character in head
    ):
        raise BootstrapError(
            "git_source_snapshot_invalid", "Git returned an invalid HEAD ID."
        )
    status = run_git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
    )
    selection = run_git_bytes(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--full-name",
        "-z",
    )
    index = run_git_bytes(root, "ls-files", "--stage", "--full-name", "-z")
    paths = parse_git_paths(selection)
    modes = parse_git_index(index)
    if set(modes).difference(paths):
        raise BootstrapError(
            "git_source_snapshot_invalid",
            "Git index and working-tree source enumeration disagree.",
        )
    return {
        "head": head.lower(),
        "status": status,
        "selection": selection,
        "index": index,
        "paths": paths,
        "modes": modes,
    }


def fallback_working_tree_digest(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256(b"listary-git-working-tree-v1\n")
    for record in records:
        digest.update(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def capture_git_source(
    repo_root: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    root = repo_root.resolve(strict=True)
    before = git_source_enumeration(root)
    exclusions = relative_exclusions(root, excluded_paths)
    selected_paths = tuple(
        relative
        for relative in before["paths"]
        if not is_excluded_source_path(relative, exclusions)
    )
    validate_unique_source_paths(selected_paths)
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for relative in selected_paths:
        digest, size, file_mode = stable_source_hash(
            source_path(root, relative), label=relative
        )
        total_bytes += size
        if total_bytes > MAX_HANDOFF_TOTAL_BYTES:
            raise BootstrapError(
                "source_snapshot_too_large",
                "Git working-tree source exceeds the safe total byte limit.",
            )
        git_mode = before["modes"].get(relative)
        records.append(
            {
                "path": relative,
                "bytes": size,
                "sha256": digest,
                "mode": (
                    "0755"
                    if (
                        git_mode == "100755" or (git_mode is None and file_mode & 0o111)
                    )
                    else "0644"
                ),
                "git_state": "tracked" if git_mode is not None else "untracked",
                "git_mode": git_mode,
            }
        )
    after = git_source_enumeration(root)
    for key in ("head", "status", "selection", "index", "paths", "modes"):
        if before[key] != after[key]:
            raise BootstrapError(
                "source_changed",
                "Git source selection changed while its content digest was being captured.",
            )
    return {
        "kind": "git_working_tree",
        "source_head": before["head"],
        "git_dirty": bool(before["status"]),
        "working_tree_sha256": fallback_working_tree_digest(records),
        "file_count": len(records),
        "total_bytes": total_bytes,
    }


def capture_source_snapshot(
    repo_root: Path = REPO_ROOT,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    root = repo_root.resolve(strict=True)
    manifest = root / HANDOFF_MANIFEST_NAME
    if os.path.lexists(manifest):
        return verify_handoff_source(
            root,
            manifest_path=manifest,
            excluded_paths=excluded_paths,
        )
    return capture_git_source(root, excluded_paths=excluded_paths)


def source_snapshot_identity(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = snapshot.get("kind")
    common = (
        kind,
        snapshot.get("source_head"),
        snapshot.get("working_tree_sha256"),
        snapshot.get("file_count"),
        snapshot.get("total_bytes"),
    )
    if kind == "handoff_manifest_v2":
        return common + (
            snapshot.get("manifest_sha256"),
            snapshot.get("tool_version"),
        )
    return common


def assert_source_snapshot_unchanged(
    expected: Mapping[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    try:
        current = capture_source_snapshot(repo_root, excluded_paths=excluded_paths)
    except BootstrapError as exc:
        raise BootstrapError(
            "source_changed",
            f"Source could not be reverified before publication ({exc.code}): {exc}",
        ) from exc
    if source_snapshot_identity(current) != source_snapshot_identity(expected):
        raise BootstrapError(
            "source_changed",
            "Source bytes or membership changed after the build started; no XCFramework was published.",
        )
    return current


def bootstrap_source_exclusions(
    output: Path,
    *,
    additional: Sequence[Path] = (),
) -> tuple[Path, ...]:
    backup, marker = publication_recovery_paths(output)
    manifest = output.parent / f"{output.name}.manifest.json"
    return (output, backup, marker, manifest, *additional)


def header_api_version(header_path: Path) -> int:
    try:
        header = header_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(
            "ffi_header_unreadable", f"Cannot read {header_path}: {exc}"
        ) from exc
    match = re.search(
        r"^\s*#define\s+LISTARY_CORE_API_VERSION\s+(\d+)[uUlL]*\s*$",
        header,
        flags=re.MULTILINE,
    )
    if match is None:
        raise BootstrapError(
            "ffi_header_version_missing",
            f"{header_path} does not declare LISTARY_CORE_API_VERSION.",
        )
    header_version = int(match.group(1))
    if header_version != EXPECTED_API_VERSION:
        raise BootstrapError(
            "ffi_header_version_mismatch",
            f"Header ABI is {header_version}; expected {EXPECTED_API_VERSION}.",
        )
    return header_version


def header_public_symbols(header_path: Path = CORE_HEADER) -> tuple[str, ...]:
    try:
        header = header_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(
            "ffi_header_unreadable", f"Cannot read {header_path}: {exc}"
        ) from exc
    symbols = tuple(
        sorted(
            set(
                re.findall(
                    r"\b(listary_(?:core|buffer)_[A-Za-z0-9_]+)\s*\(",
                    header,
                )
            )
        )
    )
    if len(symbols) != 42:
        raise BootstrapError(
            "ffi_header_symbols_invalid",
            f"Expected 42 public ABI-v{EXPECTED_API_VERSION} symbols in {header_path}; found {len(symbols)}.",
        )
    return symbols


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = value.strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise BootstrapError("artifact_metadata_invalid", f"Invalid macOS version: {value!r}")
    return tuple(int(part) for part in parts)


def verify_macos_static_library(
    library: Path,
    *,
    target: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    expected_architecture = validate_target(target)
    if not library.is_file():
        raise BootstrapError("artifact_missing", f"Static library is missing: {library}")
    lipo = require_tool("lipo")
    nm = require_tool("nm")
    otool = require_tool("otool")
    architectures = run_checked([lipo, "-archs", str(library)], env=env, timeout=60).stdout.split()
    if expected_architecture not in architectures:
        raise BootstrapError(
            "artifact_architecture_mismatch",
            f"Static library does not contain expected architecture {expected_architecture}: {library}",
        )

    nm_output = run_checked([nm, "-gU", str(library)], env=env, timeout=120).stdout
    present_symbols = set(
        re.findall(r"\b_?(listary_(?:core|buffer)_[A-Za-z0-9_]+)\b", nm_output)
    )
    expected_symbols = set(header_public_symbols())
    missing_symbols = sorted(expected_symbols.difference(present_symbols))
    if missing_symbols:
        raise BootstrapError(
            "artifact_symbols_missing",
            "Packaged static library is missing ABI symbol(s): " + ", ".join(missing_symbols),
        )

    load_commands = run_checked([otool, "-l", str(library)], env=env, timeout=120).stdout
    minimum_versions = re.findall(r"\bminos\s+([0-9]+(?:\.[0-9]+){1,2})\b", load_commands)
    minimum_versions.extend(
        re.findall(
            r"cmd\s+LC_VERSION_MIN_MACOSX(?:(?!\n\s*cmd\s).)*?\bversion\s+([0-9]+(?:\.[0-9]+){1,2})",
            load_commands,
            flags=re.DOTALL,
        )
    )
    if not minimum_versions:
        raise BootstrapError(
            "artifact_minos_missing",
            f"Could not prove a macOS minimum deployment version from {library}.",
        )
    allowed = _version_tuple(MACOS_DEPLOYMENT_TARGET)
    too_new = sorted(
        {value for value in minimum_versions if _version_tuple(value) > allowed}
    )
    if too_new:
        raise BootstrapError(
            "artifact_minos_too_new",
            f"Static library contains object(s) requiring macOS {', '.join(too_new)}; deployment target is {MACOS_DEPLOYMENT_TARGET}.",
        )
    return {
        "architecture": expected_architecture,
        "minimum_macos_versions": sorted(set(minimum_versions), key=_version_tuple),
        "public_symbol_count": len(expected_symbols),
        "sha256": sha256_file(library),
    }


def source_contract(
    *,
    manifest_path: Path = FFI_MANIFEST,
    header_path: Path = CORE_HEADER,
) -> dict[str, Any]:
    require_supported_python()
    assert tomllib is not None
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BootstrapError(
            "ffi_manifest_invalid", f"Cannot read {manifest_path}: {exc}"
        ) from exc
    crate_types = set(manifest.get("lib", {}).get("crate-type", []))
    missing_types = sorted(EXPECTED_CRATE_TYPES - crate_types)
    if missing_types:
        raise BootstrapError(
            "ffi_crate_type_missing",
            f"listary-ffi is missing required crate type(s): {', '.join(missing_types)}",
        )

    header_version = header_api_version(header_path)
    return {
        "api_version": header_version,
        "public_symbol_count": len(header_public_symbols(header_path)),
        "crate_types": sorted(crate_types),
        "header": privacy_safe_path_label(header_path),
        "manifest": privacy_safe_path_label(manifest_path),
    }


def privacy_safe_path_label(path: Path, *, repo_root: Path = REPO_ROOT) -> str:
    try:
        return (
            path.resolve(strict=False)
            .relative_to(repo_root.resolve(strict=True))
            .as_posix()
        )
    except (OSError, ValueError):
        return path.name


def target_artifacts(target: str) -> dict[str, Path]:
    release = BUILD_TARGET_DIR / target / "release"
    return {
        "static_library": release / "liblistary_ffi.a",
        "dynamic_library": release / "liblistary_ffi.dylib",
    }


def portable_tool_path(target: str) -> Path:
    return BUILD_TARGET_DIR / target / "release" / PORTABLE_BINARY_NAME


def portable_restore_commands(
    *,
    target: str,
    portable_config: Path,
    state_dir: Path,
    path_maps: Sequence[str],
) -> list[list[str]]:
    tool = portable_tool_path(target)
    restore = [
        str(tool),
        "restore",
        "--state-dir",
        str(state_dir),
        "--input",
        str(portable_config),
        "--target-style",
        "posix",
    ]
    for mapping in path_maps:
        restore.extend(["--map", mapping])
    return [
        [
            "cargo",
            "build",
            "--locked",
            "-p",
            "listary-core",
            "--bin",
            PORTABLE_BINARY_NAME,
            "--release",
            "--target",
            target,
        ],
        [str(tool), "validate", "--input", str(portable_config)],
        restore,
    ]


def build_plan(
    *,
    target: str,
    output: Path,
    portable_config: Path | None = None,
    state_dir: Path | None = None,
    path_maps: Sequence[str] = (),
) -> dict[str, Any]:
    require_supported_python()
    validate_target(target)
    validate_output_path(output)
    contract = source_contract()
    artifacts = target_artifacts(target)
    provenance = collect_provenance(strict=False, output=output)
    recovery_backup, recovery_marker = publication_recovery_paths(output)
    portable_commands: list[list[str]] = []
    portable_plan: dict[str, Any] | None = None
    if portable_config is not None:
        if state_dir is None:
            raise BootstrapError(
                "portable_state_dir_missing",
                "--state-dir is required when --portable-config is supplied.",
            )
        if not portable_config.is_file():
            raise BootstrapError(
                "portable_config_missing",
                f"Portable configuration does not exist: {portable_config}",
            )
        portable_commands = portable_restore_commands(
            target=target,
            portable_config=portable_config,
            state_dir=state_dir,
            path_maps=path_maps,
        )
        portable_plan = {
            "input": str(portable_config),
            "sha256": sha256_file(portable_config),
            "state_dir": str(state_dir),
            "path_maps": list(path_maps),
            "target_path_style": "posix",
            "copied_into_xcframework": False,
        }
    return {
        "schema": SCHEMA,
        "mode": "plan",
        "host_platform": platform.system().lower(),
        "real_mac_executed": False,
        "status": "plan",
        "core_owner": "listary-ffi -> listary-core -> RocksDB",
        "source_contract": contract,
        "provenance": provenance,
        "target": target,
        "deployment_target": MACOS_DEPLOYMENT_TARGET,
        "build_environment": {
            "MACOSX_DEPLOYMENT_TARGET": MACOS_DEPLOYMENT_TARGET,
            "CARGO_TARGET_DIR": str(BUILD_TARGET_DIR),
        },
        "output": str(output),
        "publication": {
            "strategy": "verified_same_filesystem_staging_then_atomic_rename",
            "staging_output": f"<run-scoped staging directory>/{output.name}",
            "final_output": str(output),
            "final_output_exists": output.exists(),
            "recovery_marker": str(recovery_marker),
            "recovery_marker_exists": recovery_marker.exists(),
            "recovery_backup": str(recovery_backup),
            "recovery_backup_exists": recovery_backup.exists(),
            "existing_valid_identical_output": "keep in place",
            "existing_valid_changed_output": "recoverable verified replacement",
            "partial_staging_output": "removed without changing the final output",
        },
        "artifacts": {name: str(path) for name, path in artifacts.items()},
        "portable_restore": portable_plan,
        "commands": portable_commands
        + [
            [
                "cargo",
                "build",
                "--locked",
                "-p",
                "listary-ffi",
                "--release",
                "--target",
                target,
            ],
            [
                sys.executable,
                str(ACCEPTANCE),
                "--bridge-preflight",
                "--core-lib",
                str(artifacts["dynamic_library"]),
                "--no-build-core",
                "--json",
            ],
            [
                sys.executable,
                str(ACCEPTANCE),
                "--core-smoke",
                "--core-lib",
                str(artifacts["dynamic_library"]),
                "--no-build-core",
                "--json",
            ],
            [
                sys.executable,
                str(ACCEPTANCE),
                "--host-filesystem-smoke",
                "--core-lib",
                str(artifacts["dynamic_library"]),
                "--no-build-core",
                "--json",
            ],
            [
                "xcodebuild",
                "-create-xcframework",
                "-library",
                str(artifacts["static_library"]),
                "-headers",
                "<temporary exact-header directory>",
                "-output",
                f"<run-scoped staging directory>/{output.name}",
            ],
        ],
        "not_proven_by_this_tool": [
            "Swift/AppKit shell compile or link",
            "Accessibility/Finder/Open-Save navigation",
            "native FSEventStream delivery, event IDs, drop flags, stream restart, eject, or remount",
            "Quick Look rendering or cancellation",
            "HID identity or global hotkeys",
            "signing/notarization/Gatekeeper",
        ],
    }


def run_checked(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    command_list = list(command)
    debug_log(
        "command_start",
        command=command_list,
        cwd=str(REPO_ROOT),
        timeout_seconds=timeout,
    )
    try:
        completed = subprocess.run(
            command_list,
            cwd=REPO_ROOT,
            env=dict(env) if env is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            "command_timeout",
            f"Command exceeded {timeout} seconds: {' '.join(command_list)}",
        ) from exc
    except OSError as exc:
        raise BootstrapError(
            "command_launch_failed",
            f"Cannot start command {' '.join(command_list)}: {exc}",
        ) from exc
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        raise BootstrapError(
            "command_failed",
            f"Command failed ({completed.returncode}): {' '.join(command_list)}\n{output}",
        )
    debug_log("command_complete", command=command_list, returncode=completed.returncode)
    return completed


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BootstrapError("tool_missing", f"Required tool is not on PATH: {name}")
    return path


def command_text(command: Sequence[str]) -> str:
    return run_checked(command, timeout=60).stdout.strip()


def optional_command_text(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    if not command or shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            env=dict(env) if env is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or completed.stderr).strip()


def rust_host_target() -> str:
    output = command_text(["rustc", "-vV"])
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.split(":", 1)[1].strip()
    raise BootstrapError("rust_host_unknown", "rustc -vV did not report a host target.")


def macos_libclang_dir() -> Path:
    developer_dir = Path(command_text(["xcode-select", "-p"]))
    candidate = (
        developer_dir / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "lib"
    )
    if not (candidate / "libclang.dylib").is_file():
        raise BootstrapError(
            "libclang_missing",
            f"Xcode libclang.dylib was not found at {candidate}.",
        )
    return candidate


def mac_build_environment() -> tuple[dict[str, str], dict[str, str]]:
    env = dict(os.environ)
    # Bootstrap artifact discovery is intentionally deterministic. Ignore an inherited
    # CARGO_TARGET_DIR / Cargo target-dir so a successful build cannot be followed by a false
    # artifact_missing error from looking in a different output tree.
    env["CARGO_TARGET_DIR"] = str(BUILD_TARGET_DIR)
    env["LIBCLANG_PATH"] = str(macos_libclang_dir())
    sdk_path = command_text(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
    sdk_version = command_text(["xcrun", "--sdk", "macosx", "--show-sdk-version"])
    if not Path(sdk_path).is_dir():
        raise BootstrapError(
            "macos_sdk_missing", f"xcrun reported a missing macOS SDK: {sdk_path}"
        )
    env["SDKROOT"] = sdk_path
    env["MACOSX_DEPLOYMENT_TARGET"] = MACOS_DEPLOYMENT_TARGET
    return env, {
        "path": sdk_path,
        "version": sdk_version,
        "deployment_target": MACOS_DEPLOYMENT_TARGET,
    }


def parse_json_receipt(
    completed: subprocess.CompletedProcess[str], *, expected: str
) -> dict[str, Any]:
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            "receipt_invalid", f"Expected JSON receipt from {expected}: {exc}"
        ) from exc
    if not isinstance(value, dict) or value.get("status") != "success":
        raise BootstrapError(
            "receipt_failed", f"{expected} did not return success: {value!r}"
        )
    return value


PRIVATE_RECEIPT_KEYS = {
    "path",
    "paths",
    "input",
    "output",
    "state_dir",
    "repo_root",
    "repository_root",
    "cwd",
    "executable",
    "library",
    "headers",
    "core_lib",
    "path_maps",
    "mappings",
    "libclang_path",
}


def key_may_contain_private_path(key: str) -> bool:
    normalized = key.casefold()
    return (
        normalized in PRIVATE_RECEIPT_KEYS
        or normalized.endswith("_path")
        or normalized.endswith("_dir")
        or normalized.endswith("_root")
    )


def looks_like_absolute_path(value: str) -> bool:
    stripped = value.strip()
    return (
        PurePosixPath(stripped).is_absolute()
        or PureWindowsPath(stripped).is_absolute()
        or stripped.startswith(("\\\\", "//"))
        or re.search(r"(?i)[a-z]:[\\/]", stripped) is not None
        or re.search(r"(?:^|[\s'\"=(])/(?!/)[^\s,;]+", stripped) is not None
        or re.search(r"(?:^|[\s'\"=(])\\\\[^\s,;]+", stripped) is not None
    )


def privacy_safe_receipt(value: Any) -> Any:
    """Remove machine-local paths and mapping material from a distributable receipt."""

    if isinstance(value, dict):
        return {
            str(key): privacy_safe_receipt(item)
            for key, item in value.items()
            if not key_may_contain_private_path(str(key))
            and not looks_like_absolute_path(str(key))
        }
    if isinstance(value, list):
        return [privacy_safe_receipt(item) for item in value]
    if isinstance(value, tuple):
        return [privacy_safe_receipt(item) for item in value]
    if isinstance(value, str) and looks_like_absolute_path(value):
        return "<redacted-local-path>"
    return value


def privacy_safe_xcframework_verification(receipt: Mapping[str, Any]) -> dict[str, Any]:
    slices: list[dict[str, Any]] = []
    for raw_slice in receipt.get("slices", []):
        if not isinstance(raw_slice, Mapping):
            continue
        slices.append(
            {
                key: raw_slice.get(key)
                for key in (
                    "identifier",
                    "architectures",
                    "platform",
                    "library_sha256",
                    "header_sha256",
                )
            }
        )
    return {
        "sha256_tree": receipt.get("sha256_tree"),
        "expected_architecture": receipt.get("expected_architecture"),
        "slices": slices,
    }


def privacy_safe_publication(publication: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state": publication.get("state"),
        "recovered_previous_run": publication.get("recovered_previous_run"),
        "verification": privacy_safe_xcframework_verification(
            publication.get("verification", {})
            if isinstance(publication.get("verification"), Mapping)
            else {}
        ),
    }


def assert_distributable_privacy(value: Any) -> None:
    known_private_roots = {
        str(REPO_ROOT.resolve(strict=False)).replace("\\", "/").casefold(),
        str(Path.home().resolve(strict=False)).replace("\\", "/").casefold(),
        str(Path(sys.executable).resolve(strict=False)).replace("\\", "/").casefold(),
    }

    def visit(item: Any, location: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                normalized_key = key_text.replace("\\", "/").casefold()
                if (
                    key_may_contain_private_path(key_text)
                    or looks_like_absolute_path(key_text)
                    or any(
                        root and root in normalized_key for root in known_private_roots
                    )
                ):
                    raise BootstrapError(
                        "manifest_privacy_violation",
                        f"Distributable receipt contains private path field at {location}.{key_text}.",
                    )
                visit(child, f"{location}.{key_text}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")
        elif isinstance(item, str):
            normalized = item.replace("\\", "/").casefold()
            if looks_like_absolute_path(item) or any(
                root and root in normalized for root in known_private_roots
            ):
                raise BootstrapError(
                    "manifest_privacy_violation",
                    f"Distributable receipt contains a local absolute path at {location}.",
                )

    visit(value, "$receipt")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def toolchain_pin(
    *,
    strict: bool,
    toolchain_path: Path = RUST_TOOLCHAIN,
) -> dict[str, Any] | None:
    if not toolchain_path.is_file():
        if strict:
            raise BootstrapError(
                "rust_toolchain_missing",
                f"Pinned Rust toolchain file is missing: {toolchain_path}",
            )
        return None
    assert tomllib is not None
    try:
        parsed = tomllib.loads(toolchain_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BootstrapError(
            "rust_toolchain_invalid",
            f"Cannot read pinned Rust toolchain {toolchain_path}: {exc}",
        ) from exc
    value = parsed.get("toolchain")
    if not isinstance(value, dict) or not isinstance(value.get("channel"), str):
        raise BootstrapError(
            "rust_toolchain_invalid",
            f"{toolchain_path} must contain [toolchain].channel.",
        )
    components = value.get("components", [])
    if not isinstance(components, list) or not all(
        isinstance(item, str) for item in components
    ):
        raise BootstrapError(
            "rust_toolchain_invalid",
            f"{toolchain_path} [toolchain].components must be a string array.",
        )
    return {
        "sha256": sha256_file(toolchain_path),
        "channel": value["channel"],
        "profile": value.get("profile"),
        "components": components,
    }


def collect_provenance(
    *,
    strict: bool,
    output: Path = DEFAULT_OUTPUT,
    repo_root: Path = REPO_ROOT,
    additional_excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    require_supported_python()
    root = repo_root.resolve(strict=True)
    lockfile = root / "Cargo.lock"
    toolchain_path = root / "rust-toolchain.toml"
    if not lockfile.is_file():
        raise BootstrapError("cargo_lock_missing", f"Locked builds require {lockfile}.")
    exclusions = bootstrap_source_exclusions(
        output,
        additional=additional_excluded_paths,
    )
    snapshot = capture_source_snapshot(root, excluded_paths=exclusions)
    host: dict[str, Any] = {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "tools": {
            "git": optional_command_text(["git", "--version"]),
            "rustc": optional_command_text(["rustc", "--version"]),
            "cargo": optional_command_text(["cargo", "--version"]),
        },
        "macos_sdk": {
            "version": optional_command_text(
                ["xcrun", "--sdk", "macosx", "--show-sdk-version"]
            ),
            "deployment_target": MACOS_DEPLOYMENT_TARGET,
        },
    }
    if sys.platform == "darwin":
        host["tools"].update(
            {
                "xcodebuild": optional_command_text(["xcodebuild", "-version"]),
                "swift": optional_command_text(["swift", "--version"]),
                "sw_vers": optional_command_text(["sw_vers"]),
                "uname_machine": optional_command_text(["uname", "-m"]),
            }
        )
    provenance = {
        "source": {
            "snapshot": snapshot,
            "cargo_lock": {
                "sha256": sha256_file(lockfile),
            },
            "rust_toolchain": toolchain_pin(
                strict=strict,
                toolchain_path=toolchain_path,
            ),
        },
        "host": privacy_safe_receipt(host),
    }
    assert_distributable_privacy(provenance)
    return provenance


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "little"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BootstrapError(
            "json_write_failed", f"Cannot atomically write {path}: {exc}"
        ) from exc


def safe_framework_child(root: Path, relative: str, *, field: str) -> Path:
    candidate = (root / relative).resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BootstrapError(
            "xcframework_invalid",
            f"XCFramework {field} escapes the bundle: {relative!r}",
        ) from exc
    return candidate


def verify_xcframework(
    root: Path,
    *,
    target: str,
    expected_library_sha256: str | None = None,
    expected_header_sha256: str | None = None,
) -> dict[str, Any]:
    expected_architecture = validate_target(target)
    if not root.is_dir():
        raise BootstrapError(
            "xcframework_missing", f"XCFramework directory is missing: {root}"
        )
    info_path = root / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise BootstrapError(
            "xcframework_invalid",
            f"Cannot read XCFramework Info.plist at {info_path}: {exc}",
        ) from exc
    libraries = info.get("AvailableLibraries") if isinstance(info, dict) else None
    if not isinstance(libraries, list) or not libraries:
        raise BootstrapError(
            "xcframework_invalid",
            f"{info_path} has no AvailableLibraries entries.",
        )

    verified: list[dict[str, Any]] = []
    architecture_seen = False
    for entry in libraries:
        if not isinstance(entry, dict):
            raise BootstrapError(
                "xcframework_invalid", "AvailableLibraries contains a non-object entry."
            )
        identifier = entry.get("LibraryIdentifier")
        library_relative = entry.get("LibraryPath")
        headers_relative = entry.get("HeadersPath")
        architectures = entry.get("SupportedArchitectures")
        supported_platform = entry.get("SupportedPlatform")
        if not all(
            isinstance(value, str) and value
            for value in (identifier, library_relative, headers_relative)
        ):
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework library entry is missing identifier/library/header paths: {entry!r}",
            )
        if supported_platform != "macos":
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} targets {supported_platform!r}, not macOS.",
            )
        if not isinstance(architectures, list) or not all(
            isinstance(item, str) for item in architectures
        ):
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} has invalid SupportedArchitectures.",
            )
        architecture_seen = architecture_seen or expected_architecture in architectures
        slice_root = safe_framework_child(root, identifier, field="LibraryIdentifier")
        library = safe_framework_child(
            slice_root, library_relative, field="LibraryPath"
        )
        headers = safe_framework_child(
            slice_root, headers_relative, field="HeadersPath"
        )
        header = headers / CORE_HEADER.name
        module_map = headers / "module.modulemap"
        if not library.is_file():
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} is missing its static library: {library}",
            )
        if not header.is_file() or header_api_version(header) != EXPECTED_API_VERSION:
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} is missing the exact ABI header: {header}",
            )
        try:
            module_text = module_map.read_text(encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} is missing module.modulemap: {module_map}",
            ) from exc
        if (
            CORE_HEADER.name not in module_text
            or "module ListaryCore" not in module_text
        ):
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} has an unexpected module.modulemap.",
            )
        library_sha256 = sha256_file(library)
        header_sha256 = sha256_file(header)
        if (
            expected_architecture in architectures
            and expected_library_sha256 is not None
        ):
            if library_sha256 != expected_library_sha256:
                raise BootstrapError(
                    "xcframework_invalid",
                    f"XCFramework slice {identifier} does not contain the built static library.",
                )
        if (
            expected_header_sha256 is not None
            and header_sha256 != expected_header_sha256
        ):
            raise BootstrapError(
                "xcframework_invalid",
                f"XCFramework slice {identifier} does not contain the exact public header.",
            )
        verified.append(
            {
                "identifier": identifier,
                "library": str(library),
                "headers": str(headers),
                "architectures": architectures,
                "platform": supported_platform,
                "library_sha256": library_sha256,
                "header_sha256": header_sha256,
            }
        )
    if not architecture_seen:
        raise BootstrapError(
            "xcframework_invalid",
            f"XCFramework has no {expected_architecture} slice for Rust target {target}.",
        )
    return {
        "path": str(root),
        "sha256_tree": sha256_tree(root),
        "expected_architecture": expected_architecture,
        "slices": verified,
    }


def publication_recovery_paths(output: Path) -> tuple[Path, Path]:
    backup = output.with_name(f".{output.name}.listary-bootstrap-backup")
    marker = output.with_name(f".{output.name}.listary-bootstrap-recovery.json")
    return backup, marker


def publication_manifest_backup_path(manifest: Path) -> Path:
    return manifest.with_name(f".{manifest.name}.listary-bootstrap-backup")


def remove_tool_owned_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def read_recovery_marker(marker: Path, *, output: Path, backup: Path) -> dict[str, Any]:
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(
            "publication_recovery_invalid",
            f"Cannot read publication recovery marker {marker}: {exc}",
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != RECOVERY_SCHEMA:
        raise BootstrapError(
            "publication_recovery_invalid",
            f"Unexpected publication recovery marker schema at {marker}.",
        )
    expected_output = str(output.resolve(strict=False))
    expected_backup = str(backup.resolve(strict=False))
    if value.get("output") != expected_output or value.get("backup") != expected_backup:
        raise BootstrapError(
            "publication_recovery_invalid",
            f"Recovery marker paths do not match requested output {output}.",
        )
    return value


def recover_interrupted_publication(
    output: Path,
    *,
    target: str,
    recovery_workspace: Path,
) -> str | None:
    backup, marker = publication_recovery_paths(output)
    if not marker.exists():
        if backup.exists():
            raise BootstrapError(
                "publication_recovery_orphaned",
                f"Found an unclaimed bootstrap backup at {backup}; preserve it and inspect manually.",
            )
        return None

    recovery = read_recovery_marker(marker, output=output, backup=backup)
    deferred_manifest = recovery.get("deferred_manifest")
    if isinstance(deferred_manifest, str) and deferred_manifest:
        manifest = Path(deferred_manifest)
        expected_manifest = output.parent / f"{output.name}.manifest.json"
        manifest_backup = publication_manifest_backup_path(expected_manifest)
        if manifest.resolve(strict=False) != expected_manifest.resolve(strict=False):
            raise BootstrapError(
                "publication_recovery_invalid",
                "Deferred manifest path does not match the XCFramework output.",
            )
        expected_manifest_backup = str(manifest_backup.resolve(strict=False))
        if recovery.get("manifest_backup") != expected_manifest_backup:
            raise BootstrapError(
                "publication_recovery_invalid",
                "Deferred manifest backup path does not match the XCFramework output.",
            )
        if recovery.get("phase") == "pair_complete":
            candidate_manifest_sha256 = recovery.get("candidate_manifest_sha256")
            if (
                output.is_dir()
                and expected_manifest.is_file()
                and verify_xcframework(output, target=target)["sha256_tree"]
                == recovery.get("candidate_sha256_tree")
                and isinstance(candidate_manifest_sha256, str)
                and sha256_file(expected_manifest) == candidate_manifest_sha256
            ):
                remove_tool_owned_path(backup)
                remove_tool_owned_path(manifest_backup)
                marker.unlink(missing_ok=True)
                debug_log(
                    "publication_recovered",
                    action="completed_verified_pair_cleanup",
                    output=str(output),
                )
                return "completed_verified_pair_cleanup"
            raise BootstrapError(
                "publication_recovery_invalid",
                "Deferred XCFramework/manifest pair does not match its completed recovery marker.",
            )

        previous_output_exists = recovery.get("previous_output_exists") is True
        previous_manifest_exists = recovery.get("previous_manifest_exists") is True
        try:
            if output.exists():
                quarantined = recovery_workspace / "incomplete-pair-xcframework"
                if quarantined.exists():
                    remove_tool_owned_path(quarantined)
                os.replace(output, quarantined)
            if expected_manifest.exists():
                expected_manifest.unlink()
            if previous_output_exists:
                if not backup.exists():
                    raise BootstrapError(
                        "publication_recovery_incomplete",
                        "Previous XCFramework backup is missing during pair rollback.",
                    )
                os.replace(backup, output)
            else:
                remove_tool_owned_path(backup)
            if previous_manifest_exists:
                if not manifest_backup.is_file():
                    raise BootstrapError(
                        "publication_recovery_incomplete",
                        "Previous manifest backup is missing during pair rollback.",
                    )
                os.replace(manifest_backup, expected_manifest)
            else:
                remove_tool_owned_path(manifest_backup)
            marker.unlink(missing_ok=True)
        except OSError as exc:
            raise BootstrapError(
                "publication_rollback_failed",
                f"Interrupted XCFramework/manifest pair could not be rolled back: {exc}",
            ) from exc
        debug_log(
            "publication_recovered",
            action="rolled_back_incomplete_pair",
            output=str(output),
        )
        return "rolled_back_incomplete_pair"

    previous_hash = recovery.get("previous_sha256_tree")
    candidate_hash = recovery.get("candidate_sha256_tree")
    previous_verified = recovery.get("previous_verified", True) is True

    if backup.exists():
        backup_hash = (
            verify_xcframework(backup, target=target)["sha256_tree"]
            if previous_verified
            else sha256_tree(backup)
        )
        if backup_hash != previous_hash:
            raise BootstrapError(
                "publication_recovery_invalid",
                f"Recovery backup hash does not match its marker: {backup}",
            )
        if not output.exists():
            os.replace(backup, output)
            marker.unlink(missing_ok=True)
            debug_log(
                "publication_recovered",
                action="restored_previous_output",
                output=str(output),
            )
            return "restored_previous_output"

        try:
            output_receipt = verify_xcframework(output, target=target)
        except BootstrapError:
            quarantined = recovery_workspace / "invalid-interrupted-publication"
            os.replace(output, quarantined)
            os.replace(backup, output)
            marker.unlink(missing_ok=True)
            debug_log(
                "publication_recovered",
                action="rolled_back_invalid_candidate",
                output=str(output),
            )
            return "rolled_back_invalid_candidate"

        output_hash = output_receipt["sha256_tree"]
        if output_hash == candidate_hash:
            remove_tool_owned_path(backup)
            marker.unlink(missing_ok=True)
            debug_log(
                "publication_recovered",
                action="completed_candidate_cleanup",
                output=str(output),
            )
            return "completed_candidate_cleanup"
        if output_hash == previous_hash:
            remove_tool_owned_path(backup)
            marker.unlink(missing_ok=True)
            debug_log(
                "publication_recovered",
                action="completed_rollback_cleanup",
                output=str(output),
            )
            return "completed_rollback_cleanup"
        raise BootstrapError(
            "publication_recovery_ambiguous",
            f"Both output and backup exist but neither matches the recovery marker: {output}",
        )

    if not output.exists():
        raise BootstrapError(
            "publication_recovery_incomplete",
            f"Recovery marker exists but both final output and backup are missing: {marker}",
        )
    try:
        output_hash = verify_xcframework(output, target=target)["sha256_tree"]
    except BootstrapError:
        if previous_verified or not output.is_dir():
            raise
        output_hash = sha256_tree(output)
    if output_hash not in {previous_hash, candidate_hash}:
        raise BootstrapError(
            "publication_recovery_ambiguous",
            f"Output does not match the prepared publication marker: {output}",
        )
    marker.unlink(missing_ok=True)
    debug_log(
        "publication_recovered", action="cleared_stale_marker", output=str(output)
    )
    return "cleared_stale_marker"


def publish_xcframework(
    staged: Path,
    output: Path,
    *,
    target: str,
    recovery_workspace: Path,
    expected_library_sha256: str | None = None,
    expected_header_sha256: str | None = None,
    defer_manifest: Path | None = None,
) -> dict[str, Any]:
    staged_receipt = verify_xcframework(
        staged,
        target=target,
        expected_library_sha256=expected_library_sha256,
        expected_header_sha256=expected_header_sha256,
    )
    recovered = recover_interrupted_publication(
        output,
        target=target,
        recovery_workspace=recovery_workspace,
    )
    backup, marker = publication_recovery_paths(output)
    manifest_backup = (
        publication_manifest_backup_path(defer_manifest)
        if defer_manifest is not None
        else None
    )

    if not output.exists():
        recovery: dict[str, Any] | None = None
        if defer_manifest is not None:
            recovery = {
                "schema": RECOVERY_SCHEMA,
                "phase": "prepared_pair",
                "output": str(output.resolve(strict=False)),
                "backup": str(backup.resolve(strict=False)),
                "previous_sha256_tree": None,
                "previous_verified": False,
                "candidate_sha256_tree": staged_receipt["sha256_tree"],
                "deferred_manifest": str(defer_manifest.resolve(strict=False)),
                "manifest_backup": str(manifest_backup.resolve(strict=False)),
                "previous_output_exists": False,
                "previous_manifest_exists": defer_manifest.exists(),
            }
            atomic_json(marker, recovery)
            if defer_manifest.exists():
                if manifest_backup.exists():
                    raise BootstrapError(
                        "publication_recovery_orphaned",
                        f"Found an unclaimed manifest backup at {manifest_backup}.",
                    )
                os.replace(defer_manifest, manifest_backup)
        try:
            os.replace(staged, output)
            if recovery is not None:
                recovery["phase"] = "candidate_published"
                atomic_json(marker, recovery)
            final_receipt = verify_xcframework(
                output,
                target=target,
                expected_library_sha256=expected_library_sha256,
                expected_header_sha256=expected_header_sha256,
            )
        except (OSError, BootstrapError) as exc:
            if output.exists():
                quarantined = recovery_workspace / "failed-new-publication"
                os.replace(output, quarantined)
            if isinstance(exc, BootstrapError):
                raise
            raise BootstrapError(
                "publication_failed",
                f"Cannot publish verified XCFramework to {output}: {exc}",
            ) from exc
        if final_receipt["sha256_tree"] != staged_receipt["sha256_tree"]:
            raise BootstrapError(
                "publication_verification_failed",
                f"Published XCFramework hash changed during atomic rename: {output}",
            )
        if defer_manifest is None:
            marker.unlink(missing_ok=True)
            if manifest_backup is not None:
                remove_tool_owned_path(manifest_backup)
        debug_log("publication_complete", state="published_new", output=str(output))
        return {
            "state": "published_new",
            "recovered_previous_run": recovered,
            "verification": final_receipt,
        }

    existing_verified = True
    try:
        existing_receipt = verify_xcframework(output, target=target)
        existing_hash = existing_receipt["sha256_tree"]
    except BootstrapError as exc:
        if not output.is_dir():
            raise
        existing_verified = False
        existing_hash = sha256_tree(output)
        debug_log(
            "publication_existing_output_invalid",
            output=str(output),
            error=str(exc),
            action="recoverable_replacement",
        )
    if existing_verified and existing_hash == staged_receipt["sha256_tree"]:
        debug_log("publication_complete", state="already_current", output=str(output))
        return {
            "state": "already_current",
            "recovered_previous_run": recovered,
            "verification": existing_receipt,
        }

    recovery = {
        "schema": RECOVERY_SCHEMA,
        "phase": "prepared",
        "output": str(output.resolve(strict=False)),
        "backup": str(backup.resolve(strict=False)),
        "previous_sha256_tree": existing_hash,
        "previous_verified": existing_verified,
        "candidate_sha256_tree": staged_receipt["sha256_tree"],
    }
    if defer_manifest is not None:
        recovery.update(
            {
                "deferred_manifest": str(defer_manifest.resolve(strict=False)),
                "manifest_backup": str(manifest_backup.resolve(strict=False)),
                "previous_output_exists": True,
                "previous_manifest_exists": defer_manifest.exists(),
            }
        )
    atomic_json(marker, recovery)
    try:
        if defer_manifest is not None and defer_manifest.exists():
            if manifest_backup.exists():
                raise BootstrapError(
                    "publication_recovery_orphaned",
                    f"Found an unclaimed manifest backup at {manifest_backup}.",
                )
            os.replace(defer_manifest, manifest_backup)
        os.replace(output, backup)
        recovery["phase"] = "backup_created"
        atomic_json(marker, recovery)
        os.replace(staged, output)
        recovery["phase"] = "candidate_published"
        atomic_json(marker, recovery)
        final_receipt = verify_xcframework(
            output,
            target=target,
            expected_library_sha256=expected_library_sha256,
            expected_header_sha256=expected_header_sha256,
        )
        if final_receipt["sha256_tree"] != staged_receipt["sha256_tree"]:
            raise BootstrapError(
                "publication_verification_failed",
                f"Published XCFramework does not match its verified staging tree: {output}",
            )
    except (OSError, BootstrapError) as exc:
        try:
            if output.exists() and backup.exists():
                quarantined = recovery_workspace / "failed-replacement-publication"
                os.replace(output, quarantined)
            if backup.exists() and not output.exists():
                os.replace(backup, output)
            marker.unlink(missing_ok=True)
        except OSError as rollback_exc:
            raise BootstrapError(
                "publication_rollback_failed",
                f"Publication failed and rollback also failed. Preserve {marker}: {rollback_exc}",
            ) from exc
        if isinstance(exc, BootstrapError):
            raise
        raise BootstrapError(
            "publication_failed",
            f"Cannot replace XCFramework at {output}: {exc}",
        ) from exc

    if defer_manifest is None:
        try:
            remove_tool_owned_path(backup)
            marker.unlink(missing_ok=True)
        except OSError as exc:
            raise BootstrapError(
                "publication_cleanup_failed",
                f"New output is verified, but cleanup is incomplete. Rerun to recover via {marker}: {exc}",
            ) from exc
    publication_state = (
        "replaced_existing" if existing_verified else "replaced_invalid_existing"
    )
    debug_log("publication_complete", state=publication_state, output=str(output))
    return {
        "state": publication_state,
        "recovered_previous_run": recovered,
        "verification": final_receipt,
    }


def finalize_xcframework_manifest_pair(output: Path, manifest: Path) -> None:
    backup, marker = publication_recovery_paths(output)
    manifest_backup = publication_manifest_backup_path(manifest)
    if not marker.exists():
        return
    recovery = read_recovery_marker(marker, output=output, backup=backup)
    if recovery.get("deferred_manifest") != str(manifest.resolve(strict=False)):
        raise BootstrapError(
            "publication_recovery_invalid",
            "Cannot finalize an XCFramework with an unrelated manifest recovery marker.",
        )
    if not output.is_dir() or not manifest.is_file():
        raise BootstrapError(
            "publication_recovery_incomplete",
            "XCFramework and manifest must both exist before publication can finalize.",
        )
    recovery["phase"] = "pair_complete"
    recovery["candidate_manifest_sha256"] = sha256_file(manifest)
    atomic_json(marker, recovery)
    try:
        remove_tool_owned_path(backup)
        remove_tool_owned_path(manifest_backup)
        marker.unlink(missing_ok=True)
    except OSError as exc:
        raise BootstrapError(
            "publication_cleanup_failed",
            f"Verified XCFramework/manifest pair is ready, but recovery cleanup is incomplete: {exc}",
        ) from exc


def execute_bootstrap(
    *,
    target: str,
    output: Path,
    portable_config: Path | None = None,
    state_dir: Path | None = None,
    path_maps: Sequence[str] = (),
) -> dict[str, Any]:
    require_supported_python()
    validate_target(target)
    validate_output_path(output)
    if sys.platform != "darwin":
        raise BootstrapError(
            "non_macos_host",
            "Bridge packaging execution requires macOS/Xcode. Use --plan on non-Mac hosts.",
        )
    debug_log(
        "bootstrap_start",
        repo_root=str(REPO_ROOT),
        target=target,
        output=str(output),
        deployment_target=MACOS_DEPLOYMENT_TARGET,
        portable_restore=portable_config is not None,
    )
    source = source_contract()
    for tool in (
        "cargo",
        "rustc",
        "swift",
        "sw_vers",
        "uname",
        "xcodebuild",
        "xcode-select",
        "xcrun",
    ):
        require_tool(tool)

    host_target = rust_host_target()
    if target != host_target:
        raise BootstrapError(
            "cross_target_not_qualified",
            f"First-session bridge bootstrap only runs the host target so its dylib can be loaded. "
            f"Host is {host_target}; requested {target}.",
        )

    rustup = shutil.which("rustup")
    if rustup:
        installed = set(
            command_text(["rustup", "target", "list", "--installed"]).splitlines()
        )
        if target not in installed:
            raise BootstrapError(
                "rust_target_missing",
                f"Rust target {target} is not installed. Install it explicitly with: rustup target add {target}",
            )

    pinned_toolchain = toolchain_pin(strict=True)
    assert pinned_toolchain is not None
    rustc_version = command_text(["rustc", "--version"])
    expected_rustc_prefix = f"rustc {pinned_toolchain['channel']} "
    if not rustc_version.startswith(expected_rustc_prefix):
        raise BootstrapError(
            "rust_toolchain_mismatch",
            f"Pinned Rust {pinned_toolchain['channel']} is required; active compiler is {rustc_version!r}.",
        )

    env, sdk = mac_build_environment()
    provenance = collect_provenance(
        strict=True,
        output=output,
        additional_excluded_paths=((state_dir,) if state_dir is not None else ()),
    )
    initial_source_snapshot = provenance["source"]["snapshot"]
    versions = {
        "sw_vers": command_text(["sw_vers"]),
        "uname": command_text(["uname", "-m"]),
        "xcodebuild": command_text(["xcodebuild", "-version"]),
        "swift": command_text(["swift", "--version"]),
        "rustc": rustc_version,
        "cargo": command_text(["cargo", "--version"]),
        "sdk": {
            "version": sdk["version"],
            "deployment_target": sdk["deployment_target"],
        },
        "deployment_target": MACOS_DEPLOYMENT_TARGET,
    }
    debug_log(
        "environment_ready",
        rustc=versions["rustc"],
        cargo=versions["cargo"],
        sdk=sdk,
        libclang_path=env["LIBCLANG_PATH"],
    )
    versions = privacy_safe_receipt(versions)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.run-",
        dir=output.parent,
    ) as temp_name:
        run_directory = Path(temp_name)
        recovered_publication = recover_interrupted_publication(
            output,
            target=target,
            recovery_workspace=run_directory,
        )

        portable_manifest: dict[str, Any] | None = None
        if portable_config is not None:
            if state_dir is None:
                raise BootstrapError(
                    "portable_state_dir_missing",
                    "--state-dir is required when --portable-config is supplied.",
                )
            if not portable_config.is_file():
                raise BootstrapError(
                    "portable_config_missing",
                    f"Portable configuration does not exist: {portable_config}",
                )
            commands = portable_restore_commands(
                target=target,
                portable_config=portable_config,
                state_dir=state_dir,
                path_maps=path_maps,
            )
            debug_log(
                "portable_restore_start",
                input=str(portable_config),
                state_dir=str(state_dir),
                path_map_count=len(path_maps),
            )
            run_checked(commands[0], env=env)
            portable_tool = portable_tool_path(target)
            if not portable_tool.is_file():
                raise BootstrapError(
                    "portable_tool_missing",
                    f"Cargo did not produce portable restore tool: {portable_tool}",
                )
            validate_receipt = parse_json_receipt(
                run_checked(commands[1], env=env),
                expected="portable configuration validation",
            )
            restore_receipt = parse_json_receipt(
                run_checked(commands[2], env=env),
                expected="portable configuration restore",
            )
            portable_manifest = {
                "input_sha256": sha256_file(portable_config),
                "input_bytes": portable_config.stat().st_size,
                "path_map_count": len(path_maps),
                "target_path_style": "posix",
                "copied_into_xcframework": False,
                "validation": privacy_safe_receipt(validate_receipt),
                "restore": privacy_safe_receipt(restore_receipt),
            }
            debug_log("portable_restore_complete", state_dir=str(state_dir))

        run_checked(
            [
                "cargo",
                "build",
                "--locked",
                "-p",
                "listary-ffi",
                "--release",
                "--target",
                target,
            ],
            env=env,
        )
        artifacts = target_artifacts(target)
        for name, path in artifacts.items():
            if not path.is_file():
                raise BootstrapError(
                    "artifact_missing", f"Cargo did not produce {name}: {path}"
                )
        header_sha256 = sha256_file(CORE_HEADER)
        static_library_sha256 = sha256_file(artifacts["static_library"])
        dynamic_library_sha256 = sha256_file(artifacts["dynamic_library"])
        static_library_verification = verify_macos_static_library(
            artifacts["static_library"],
            target=target,
            env=env,
        )

        bridge_receipt = parse_json_receipt(
            run_checked(
                [
                    sys.executable,
                    str(ACCEPTANCE),
                    "--bridge-preflight",
                    "--core-lib",
                    str(artifacts["dynamic_library"]),
                    "--no-build-core",
                    "--json",
                ],
                env=env,
            ),
            expected="ABI bridge preflight",
        )
        core_smoke = parse_json_receipt(
            run_checked(
                [
                    sys.executable,
                    str(ACCEPTANCE),
                    "--core-smoke",
                    "--core-lib",
                    str(artifacts["dynamic_library"]),
                    "--no-build-core",
                    "--json",
                ],
                env=env,
            ),
            expected="disposable core smoke",
        )
        host_filesystem_smoke = parse_json_receipt(
            run_checked(
                [
                    sys.executable,
                    str(ACCEPTANCE),
                    "--host-filesystem-smoke",
                    "--core-lib",
                    str(artifacts["dynamic_library"]),
                    "--no-build-core",
                    "--json",
                ],
                env=env,
            ),
            expected="real-Mac host filesystem/recovery smoke",
        )

        headers = run_directory / "Headers"
        headers.mkdir()
        shutil.copy2(CORE_HEADER, headers / CORE_HEADER.name)
        (headers / "module.modulemap").write_text(
            'module ListaryCore {\n  header "listary_core.h"\n  export *\n}\n',
            encoding="utf-8",
        )
        staged_output = run_directory / output.name
        debug_log("xcframework_stage_start", staging_output=str(staged_output))
        run_checked(
            [
                "xcodebuild",
                "-create-xcframework",
                "-library",
                str(artifacts["static_library"]),
                "-headers",
                str(headers),
                "-output",
                str(staged_output),
            ],
            env=env,
        )
        assert_source_snapshot_unchanged(
            initial_source_snapshot,
            repo_root=REPO_ROOT,
            excluded_paths=bootstrap_source_exclusions(
                output,
                additional=(
                    run_directory,
                    *((state_dir,) if state_dir is not None else ()),
                ),
            ),
        )
        manifest_path = output.parent / f"{output.name}.manifest.json"
        publication = publish_xcframework(
            staged_output,
            output,
            target=target,
            recovery_workspace=run_directory,
            expected_library_sha256=static_library_sha256,
            expected_header_sha256=header_sha256,
            defer_manifest=manifest_path,
        )
        if recovered_publication is not None:
            publication["recovered_previous_run"] = recovered_publication
        public_publication = privacy_safe_publication(publication)

        manifest = {
            "schema": SCHEMA,
            "status": "success",
            "outcome": "native_bridge_xcframework_ready",
            "real_mac_executed": True,
            "core_owner": "listary-ffi -> listary-core -> RocksDB",
            "api_version": EXPECTED_API_VERSION,
            "target": target,
            "deployment_target": MACOS_DEPLOYMENT_TARGET,
            "source_contract": source,
            "provenance": provenance,
            "versions": versions,
            "publication": public_publication,
            "artifacts": {
                "header": {
                    "file": privacy_safe_path_label(CORE_HEADER),
                    "sha256": header_sha256,
                },
                "static_library": {
                    "file": privacy_safe_path_label(artifacts["static_library"]),
                    "sha256": static_library_sha256,
                    "native_verification": static_library_verification,
                },
                "dynamic_library": {
                    "file": privacy_safe_path_label(artifacts["dynamic_library"]),
                    "sha256": dynamic_library_sha256,
                },
                "xcframework": public_publication["verification"],
            },
            "bridge_preflight": privacy_safe_receipt(bridge_receipt),
            "core_smoke": privacy_safe_receipt(core_smoke),
            "host_filesystem_smoke": privacy_safe_receipt(host_filesystem_smoke),
            "portable_restore": portable_manifest,
            "manifest_file": manifest_path.name,
            "not_proven": [
                "Swift/AppKit shell compile or link",
                "Accessibility/Finder/Open-Save navigation",
                "native FSEventStream delivery, event IDs, drop flags, stream restart, eject, or remount",
                "Quick Look rendering or cancellation",
                "HID identity or global hotkeys",
                "signing/notarization/Gatekeeper",
            ],
        }
        assert_distributable_privacy(manifest)
        try:
            atomic_json(manifest_path, manifest)
            finalize_xcframework_manifest_pair(output, manifest_path)
        except (OSError, BootstrapError):
            recover_interrupted_publication(
                output,
                target=target,
                recovery_workspace=run_directory,
            )
            raise
        debug_log(
            "bootstrap_complete",
            output=str(output),
            manifest_path=str(manifest_path),
            publication_state=publication["state"],
        )
        return manifest


def default_target() -> str:
    if sys.platform == "darwin":
        machine = platform.machine().casefold()
        if machine in {"arm64", "aarch64"}:
            return "aarch64-apple-darwin"
        if machine in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin"
    return "aarch64-apple-darwin"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Validate source assumptions and print the exact Mac bridge packaging plan without executing Mac tools.",
    )
    parser.add_argument(
        "--target",
        default=default_target(),
        help="Rust Apple target; execution currently requires the native host target.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="XCFramework output path. Existing valid outputs are kept or recoverably replaced.",
    )
    parser.add_argument(
        "--portable-config",
        type=Path,
        help="Optional versioned portable Listary configuration to validate and restore before shell work.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help=(
            "Fresh Listary state directory to receive --portable-config. "
            "Required when a portable config is supplied."
        ),
    )
    parser.add_argument(
        "--path-map",
        action="append",
        default=[],
        metavar="FROM=TO",
        help="Explicit portable root/path mapping passed unchanged to the Rust restore owner; repeat as needed.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Pretty-print the receipt/plan as JSON."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.expanduser().resolve(strict=False)
    portable_config = (
        args.portable_config.expanduser().resolve(strict=False)
        if args.portable_config
        else None
    )
    state_dir = (
        args.state_dir.expanduser().resolve(strict=False) if args.state_dir else None
    )
    try:
        kwargs = {
            "target": args.target,
            "output": output,
            "portable_config": portable_config,
            "state_dir": state_dir,
            "path_maps": args.path_map,
        }
        result = build_plan(**kwargs) if args.plan else execute_bootstrap(**kwargs)
    except BootstrapError as exc:
        debug_log("bootstrap_failed", outcome=exc.code, error=str(exc))
        result = {
            "schema": SCHEMA,
            "host_platform": platform.system().lower(),
            "real_mac_executed": False,
            "status": "failed",
            "outcome": exc.code,
            "error": str(exc),
        }
    except OSError as exc:
        debug_log("bootstrap_failed", outcome="filesystem_error", error=str(exc))
        result = {
            "schema": SCHEMA,
            "host_platform": platform.system().lower(),
            "real_mac_executed": False,
            "status": "failed",
            "outcome": "filesystem_error",
            "error": str(exc),
        }
    print(json.dumps(result, indent=2 if args.json else None, ensure_ascii=False))
    return 0 if result.get("status") in {"success", "plan"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

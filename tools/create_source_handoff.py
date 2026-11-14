#!/usr/bin/env python3
"""Create and verify a deterministic, checksummed source handoff archive.

The archive contains the current contents of every Git-tracked file plus every
non-ignored untracked file. Git metadata, ignored build output, symlinks,
gitlinks, special files, and unsafe or ambiguous archive names are rejected.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, Iterable, Sequence
import unicodedata
import zipfile


SCHEMA = "listary_source_handoff"
SCHEMA_VERSION = 2
TOOL_VERSION = "2.0.1"
MANIFEST_NAME = "HANDOFF_MANIFEST.json"
PUBLICATION_SCHEMA = "listary_source_handoff_publication_v1"
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOURCE_FILES = 250_000
MAX_SOURCE_PATH_BYTES = 4096
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
CHUNK_BYTES = 1024 * 1024
SENSITIVE_UNTRACKED_NAMES = {
    ".env",
    "credentials.json",
    "secrets.json",
    "token.json",
    "id_rsa",
    "id_ed25519",
}
SENSITIVE_UNTRACKED_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
LIVE_ENDPOINT_NAMES = {"integration-endpoint-v1.json", "ui-endpoint-v1.txt"}


class HandoffError(RuntimeError):
    """A source handoff could not be created or verified safely."""


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    branch: str | None
    status: bytes
    selection: bytes
    index: bytes
    paths: tuple[str, ...]
    tracked_modes: dict[str, str]


@dataclass(frozen=True)
class SourceFile:
    archive_path: str
    source_path: Path
    size: int
    sha256: str
    archive_mode: int
    git_state: str
    git_mode: str | None
    fingerprint: tuple[int, ...]

    def manifest_record(self) -> dict[str, object]:
        return {
            "path": self.archive_path,
            "bytes": self.size,
            "sha256": self.sha256,
            "mode": f"{self.archive_mode:04o}",
            "git_state": self.git_state,
            "git_mode": self.git_mode,
        }


def _log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[source-handoff] {message}", file=sys.stderr)


def _git(
    repo: Path,
    *args: str,
    allowed_returncodes: Sequence[int] = (0,),
) -> bytes:
    try:
        process = subprocess.run(
            ["git", *args],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise HandoffError(
            "Git is unavailable or could not be started; install Git and verify the "
            "repository path"
        ) from error
    if process.returncode not in allowed_returncodes:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 1000:
            detail = f"{detail[:1000]}..."
        command = " ".join(args)
        raise HandoffError(
            f"git {command} failed: {detail or f'exit code {process.returncode}'}"
        )
    return process.stdout


def _decode_utf8(value: bytes, description: str) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HandoffError(f"{description} is not valid UTF-8") from error


def _repository_root(requested: Path) -> Path:
    requested = requested.expanduser()
    raw = _git(requested, "rev-parse", "--show-toplevel")
    value = _decode_utf8(raw, "Git repository root").strip()
    if not value:
        raise HandoffError("Git returned an empty repository root")
    try:
        root = Path(value).resolve(strict=True)
    except OSError as error:
        raise HandoffError("Git repository root does not exist or is inaccessible") from error
    if not root.is_dir():
        raise HandoffError("Git repository root is not a directory")
    return root


def _canonical_archive_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_SOURCE_PATH_BYTES:
        raise HandoffError("source path is empty or exceeds the bootstrap-safe length limit")
    if "\\" in value:
        raise HandoffError("source path contains an ambiguous backslash")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HandoffError("source path contains a control character")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive or value.startswith("/"):
        raise HandoffError("source path is absolute")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HandoffError("source path contains an unsafe component")
    if parts[0].casefold() == "_runtime" or path.name.casefold() in LIVE_ENDPOINT_NAMES:
        raise HandoffError(
            f"runtime state or live endpoint cannot enter a source handoff: {value}"
        )
    canonical = "/".join(parts)
    if canonical == MANIFEST_NAME:
        raise HandoffError(f"source path collides with reserved {MANIFEST_NAME}")
    return canonical


def _portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _validated_paths(raw: bytes) -> tuple[str, ...]:
    chunks = raw.split(b"\0")
    if chunks and chunks[-1] == b"":
        chunks.pop()
    if len(chunks) > MAX_SOURCE_FILES:
        raise HandoffError("source handoff exceeds the bootstrap-safe member-count limit")
    paths: list[str] = []
    exact: set[str] = set()
    portable: dict[str, str] = {_portable_name_key(MANIFEST_NAME): MANIFEST_NAME}
    for raw_path in chunks:
        decoded = _decode_utf8(raw_path, "Git source path")
        path = _canonical_archive_path(decoded)
        if path in exact:
            raise HandoffError("Git returned a duplicate source path")
        exact.add(path)
        key = _portable_name_key(path)
        previous = portable.get(key)
        if previous is not None:
            raise HandoffError(
                "source paths collide under macOS-compatible case/Unicode rules: "
                f"{previous!r} and {path!r}"
            )
        portable[key] = path
        paths.append(path)
    paths.sort()
    if not paths:
        raise HandoffError("Git returned no tracked or non-ignored source files")
    return tuple(paths)


def _parse_index(raw: bytes) -> dict[str, str]:
    entries = raw.split(b"\0")
    if entries and entries[-1] == b"":
        entries.pop()
    modes: dict[str, str] = {}
    for entry in entries:
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode_bytes, _object_id, stage_bytes = metadata.split(b" ", 2)
        except ValueError as error:
            raise HandoffError("Git returned malformed index metadata") from error
        path = _canonical_archive_path(_decode_utf8(raw_path, "Git index path"))
        mode = _decode_utf8(mode_bytes, "Git index mode")
        stage = _decode_utf8(stage_bytes, "Git index stage")
        if stage != "0":
            raise HandoffError(f"unmerged Git index entry cannot be handed off: {path}")
        if path in modes:
            raise HandoffError(f"duplicate Git index entry cannot be handed off: {path}")
        if mode == "120000":
            raise HandoffError(f"Git symlink cannot be handed off safely: {path}")
        if mode == "160000":
            raise HandoffError(f"Git submodule cannot be handed off as a regular file: {path}")
        if mode not in ("100644", "100755"):
            raise HandoffError(f"unsupported Git index mode {mode!r}: {path}")
        modes[path] = mode
    return modes


def _snapshot_repository(repo: Path) -> GitSnapshot:
    head = _decode_utf8(
        _git(repo, "rev-parse", "--verify", "HEAD"), "Git HEAD"
    ).strip()
    if len(head) not in (40, 64) or any(
        character not in "0123456789abcdefABCDEF" for character in head
    ):
        raise HandoffError("Git returned an invalid HEAD object ID")
    branch_raw = _git(
        repo,
        "symbolic-ref",
        "--short",
        "-q",
        "HEAD",
        allowed_returncodes=(0, 1),
    )
    branch_value = _decode_utf8(branch_raw, "Git branch").strip()
    branch = branch_value or None
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
    )
    selection = _git(
        repo,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--full-name",
        "-z",
    )
    index = _git(repo, "ls-files", "--stage", "--full-name", "-z")
    paths = _validated_paths(selection)
    modes = _parse_index(index)
    selected = set(paths)
    absent = set(modes).difference(selected)
    if absent:
        raise HandoffError("Git index and source enumeration disagree")
    return GitSnapshot(head, branch, status, selection, index, paths, modes)


def _snapshot_matches(first: GitSnapshot, second: GitSnapshot) -> bool:
    return (
        first.head == second.head
        and first.branch == second.branch
        and first.status == second.status
        and first.selection == second.selection
        and first.index == second.index
        and first.paths == second.paths
        and first.tracked_modes == second.tracked_modes
    )


def _assert_snapshot_unchanged(repo: Path, expected: GitSnapshot) -> None:
    current = _snapshot_repository(repo)
    if not _snapshot_matches(expected, current):
        raise HandoffError("repository changed while the source handoff was being built")


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    # Windows reports creation time through path stat but may report mtime in
    # fstat().st_ctime for the same open file. Size, identity, mode, link count,
    # and nanosecond mtime are stable across both APIs and are combined with a
    # full content digest for change detection.
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
    )


def _assert_regular_stat(value: os.stat_result, relative: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise HandoffError(f"listed source is not a regular file: {relative}")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(value, "st_file_attributes", 0))
    if attributes & reparse_flag:
        raise HandoffError(f"refusing reparse point in source handoff: {relative}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root))))
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _source_path(repo: Path, relative: str) -> Path:
    path = repo.joinpath(*PurePosixPath(relative).parts)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HandoffError(f"listed source is missing or inaccessible: {relative}") from error
    if path.is_symlink():
        raise HandoffError(f"refusing symlink in source handoff: {relative}")
    _assert_regular_stat(metadata, relative)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise HandoffError(f"listed source cannot be resolved safely: {relative}") from error
    if not _is_within(resolved, repo):
        raise HandoffError(f"source escapes repository root: {relative}")
    return path


def _hash_open_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _stable_source_hash(path: Path, relative: str) -> tuple[str, int, tuple[int, ...]]:
    try:
        before = path.lstat()
        _assert_regular_stat(before, relative)
        if path.is_symlink():
            raise HandoffError(f"refusing symlink in source handoff: {relative}")
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            digest, size = _hash_open_stream(stream)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
    except HandoffError:
        raise
    except OSError as error:
        raise HandoffError(f"source could not be read safely: {relative}") from error
    fingerprints = {
        _stat_fingerprint(before),
        _stat_fingerprint(opened_before),
        _stat_fingerprint(opened_after),
        _stat_fingerprint(after),
    }
    if len(fingerprints) != 1 or size != int(after.st_size):
        raise HandoffError(f"source changed while being read: {relative}")
    return digest, size, _stat_fingerprint(after)


def _collect_sources(
    repo: Path,
    snapshot: GitSnapshot,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[list[SourceFile], int]:
    if max_file_bytes < 0 or max_total_bytes < 0:
        raise HandoffError("source handoff size limits must not be negative")
    if max_file_bytes > DEFAULT_MAX_FILE_BYTES or max_total_bytes > DEFAULT_MAX_TOTAL_BYTES:
        raise HandoffError(
            "source handoff size limits cannot exceed the first-Mac bootstrap admission limits"
        )
    files: list[SourceFile] = []
    total_bytes = 0
    for relative in snapshot.paths:
        git_mode = snapshot.tracked_modes.get(relative)
        basename = PurePosixPath(relative).name.casefold()
        if git_mode is None and (
            basename in SENSITIVE_UNTRACKED_NAMES
            or basename.startswith(".env.")
            or PurePosixPath(basename).suffix in SENSITIVE_UNTRACKED_SUFFIXES
        ):
            raise HandoffError(
                "potentially sensitive untracked file must be ignored or deliberately "
                f"tracked before handoff: {relative}"
            )
        source = _source_path(repo, relative)
        digest, size, fingerprint = _stable_source_hash(source, relative)
        if size > max_file_bytes:
            raise HandoffError(
                f"source exceeds per-file handoff limit ({size} > {max_file_bytes}): {relative}"
            )
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise HandoffError(
                f"source handoff exceeds total limit ({total_bytes} > {max_total_bytes})"
            )
        if git_mode is not None:
            archive_mode = 0o755 if git_mode == "100755" else 0o644
            git_state = "tracked"
        else:
            archive_mode = 0o755 if fingerprint[2] & 0o111 else 0o644
            git_state = "untracked"
        files.append(
            SourceFile(
                archive_path=relative,
                source_path=source,
                size=size,
                sha256=digest,
                archive_mode=archive_mode,
                git_state=git_state,
                git_mode=git_mode,
                fingerprint=fingerprint,
            )
        )
    return files, total_bytes


def _working_tree_digest(
    records: Iterable[dict[str, object]], status_sha256: str
) -> str:
    digest = hashlib.sha256()
    digest.update(f"status:{status_sha256}\n".encode("ascii"))
    for record in records:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest(snapshot: GitSnapshot, files: list[SourceFile], total_bytes: int) -> dict[str, object]:
    records = [source.manifest_record() for source in files]
    status_sha256 = hashlib.sha256(snapshot.status).hexdigest()
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "create_source_handoff.py", "version": TOOL_VERSION},
        "archive_layout": "repository-root-relative",
        "selection": "Git tracked files plus non-ignored untracked files",
        "git": {
            "head": snapshot.head,
            "branch": snapshot.branch,
            "dirty": bool(snapshot.status),
            "status_porcelain_v1_z_sha256": status_sha256,
            "selection_z_sha256": hashlib.sha256(snapshot.selection).hexdigest(),
            "index_stage_z_sha256": hashlib.sha256(snapshot.index).hexdigest(),
        },
        "integrity": {
            "member_algorithm": "sha256",
            "archive_algorithm": "sha256",
            "archive_sidecar_suffix": ".sha256",
            "archive_sidecar_format": "sha256sum",
            "working_tree_sha256": _working_tree_digest(records, status_sha256),
        },
        "file_count": len(records),
        "total_bytes": total_bytes,
        "files": records,
    }


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _copy_source_into_archive(archive: zipfile.ZipFile, source: SourceFile) -> None:
    relative = source.archive_path
    try:
        before = source.source_path.lstat()
        _assert_regular_stat(before, relative)
        if source.source_path.is_symlink():
            raise HandoffError(f"refusing symlink in source handoff: {relative}")
        digest = hashlib.sha256()
        size = 0
        with source.source_path.open("rb") as input_stream:
            opened_before = os.fstat(input_stream.fileno())
            with archive.open(
                _zip_info(relative, source.archive_mode), "w", force_zip64=True
            ) as output_stream:
                for chunk in iter(lambda: input_stream.read(CHUNK_BYTES), b""):
                    output_stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            opened_after = os.fstat(input_stream.fileno())
        after = source.source_path.lstat()
    except HandoffError:
        raise
    except OSError as error:
        raise HandoffError(f"source could not be archived safely: {relative}") from error
    fingerprints = {
        _stat_fingerprint(before),
        _stat_fingerprint(opened_before),
        _stat_fingerprint(opened_after),
        _stat_fingerprint(after),
        source.fingerprint,
    }
    if (
        len(fingerprints) != 1
        or size != source.size
        or digest.hexdigest() != source.sha256
    ):
        raise HandoffError(f"source changed while being archived: {relative}")


def _fsync_file(path: Path) -> None:
    try:
        # Windows requires a writable descriptor for fsync/FlushFileBuffers.
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError as error:
        raise HandoffError("handoff staging file could not be flushed to disk") from error


def _write_archive(path: Path, files: list[SourceFile], manifest_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(
            path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for source in files:
                _copy_source_into_archive(archive, source)
            archive.writestr(
                _zip_info(MANIFEST_NAME, 0o644),
                manifest_bytes,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    except HandoffError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise HandoffError("handoff archive could not be written") from error
    _fsync_file(path)


def _assert_sources_unchanged(files: Iterable[SourceFile]) -> None:
    for source in files:
        digest, size, fingerprint = _stable_source_hash(
            source.source_path, source.archive_path
        )
        if (
            digest != source.sha256
            or size != source.size
            or fingerprint != source.fingerprint
        ):
            raise HandoffError(
                f"source changed while the handoff was being finalized: {source.archive_path}"
            )


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            digest, _size = _hash_open_stream(stream)
    except OSError as error:
        raise HandoffError("handoff archive could not be checksummed") from error
    return digest


def _parse_sidecar(path: Path, expected_archive_name: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise HandoffError("handoff checksum sidecar is missing or unreadable") from error
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise HandoffError("handoff checksum sidecar is not ASCII") from error
    if not text.endswith("\n") or text.count("\n") != 1:
        raise HandoffError("handoff checksum sidecar has an invalid line format")
    line = text[:-1]
    if "  " not in line:
        raise HandoffError("handoff checksum sidecar has an invalid sha256sum format")
    digest, filename = line.split("  ", 1)
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or filename != expected_archive_name
    ):
        raise HandoffError("handoff checksum sidecar does not identify this archive")
    return digest


def _manifest_records(manifest: dict[str, object]) -> list[dict[str, object]]:
    if manifest.get("schema") != SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
        raise HandoffError("handoff manifest schema is unsupported")
    tool = manifest.get("tool")
    if (
        not isinstance(tool, dict)
        or tool.get("name") != "create_source_handoff.py"
        or not isinstance(tool.get("version"), str)
        or not tool.get("version")
    ):
        raise HandoffError("handoff manifest tool provenance is invalid")
    if (
        manifest.get("archive_layout") != "repository-root-relative"
        or manifest.get("selection")
        != "Git tracked files plus non-ignored untracked files"
    ):
        raise HandoffError("handoff manifest selection provenance is invalid")
    git = manifest.get("git")
    integrity = manifest.get("integrity")
    records = manifest.get("files")
    if not isinstance(git, dict) or not isinstance(integrity, dict) or not isinstance(records, list):
        raise HandoffError("handoff manifest structure is invalid")
    if not records or len(records) > MAX_SOURCE_FILES:
        raise HandoffError("handoff manifest member count exceeds the bootstrap-safe limit")
    status_digest = git.get("status_porcelain_v1_z_sha256")
    expected_tree_digest = integrity.get("working_tree_sha256")
    head = git.get("head")
    branch = git.get("branch")
    if (
        not isinstance(head, str)
        or len(head) not in (40, 64)
        or any(character not in "0123456789abcdefABCDEF" for character in head)
        or (branch is not None and not isinstance(branch, str))
        or not isinstance(git.get("dirty"), bool)
    ):
        raise HandoffError("handoff manifest Git provenance is invalid")
    digest_fields = (
        status_digest,
        git.get("selection_z_sha256"),
        git.get("index_stage_z_sha256"),
        expected_tree_digest,
    )
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in digest_fields
    ):
        raise HandoffError("handoff manifest Git status digest is invalid")
    if integrity.get("member_algorithm") != "sha256" or integrity.get(
        "archive_algorithm"
    ) != "sha256":
        raise HandoffError("handoff manifest integrity algorithm is invalid")
    if integrity.get("archive_sidecar_suffix") != ".sha256" or integrity.get(
        "archive_sidecar_format"
    ) != "sha256sum":
        raise HandoffError("handoff manifest sidecar provenance is invalid")
    validated: list[dict[str, object]] = []
    raw_paths: list[bytes] = []
    total = 0
    for record in records:
        if not isinstance(record, dict):
            raise HandoffError("handoff manifest contains an invalid file record")
        path_value = record.get("path")
        size = record.get("bytes")
        digest = record.get("sha256")
        mode = record.get("mode")
        git_state = record.get("git_state")
        git_mode = record.get("git_mode")
        if not isinstance(path_value, str):
            raise HandoffError("handoff manifest contains an invalid file path")
        path_value = _canonical_archive_path(path_value)
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > DEFAULT_MAX_FILE_BYTES
        ):
            raise HandoffError("handoff manifest contains an invalid file size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise HandoffError("handoff manifest contains an invalid member digest")
        if mode not in ("0644", "0755"):
            raise HandoffError("handoff manifest contains an invalid file mode")
        if git_state not in ("tracked", "untracked"):
            raise HandoffError("handoff manifest contains an invalid Git state")
        if git_state == "tracked" and git_mode not in ("100644", "100755"):
            raise HandoffError("handoff manifest contains an invalid tracked Git mode")
        if git_mode == "100644" and mode != "0644":
            raise HandoffError("handoff manifest tracked mode disagrees with archive mode")
        if git_mode == "100755" and mode != "0755":
            raise HandoffError("handoff manifest tracked mode disagrees with archive mode")
        if git_state == "untracked" and git_mode is not None:
            raise HandoffError("handoff manifest contains an invalid untracked Git mode")
        total += size
        if total > DEFAULT_MAX_TOTAL_BYTES:
            raise HandoffError("handoff manifest exceeds the bootstrap-safe total byte limit")
        raw_paths.append(path_value.encode("utf-8"))
        validated.append(record)
    _validated_paths(b"\0".join(raw_paths) + (b"\0" if raw_paths else b""))
    if [str(record["path"]) for record in validated] != sorted(
        str(record["path"]) for record in validated
    ):
        raise HandoffError("handoff manifest file records are not canonical")
    if manifest.get("file_count") != len(validated) or manifest.get("total_bytes") != total:
        raise HandoffError("handoff manifest file totals are inconsistent")
    if _working_tree_digest(validated, status_digest) != expected_tree_digest:
        raise HandoffError("handoff manifest working-tree digest does not match its records")
    return validated


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("handoff manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _verify_handoff_paths(
    archive_path: Path,
    checksum_path: Path,
    *,
    expected_archive_name: str,
    expected_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    expected_archive_sha = _parse_sidecar(checksum_path, expected_archive_name)
    actual_archive_sha = _sha256_file(archive_path)
    if actual_archive_sha != expected_archive_sha:
        raise HandoffError("handoff archive SHA-256 does not match its sidecar")
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise HandoffError("handoff archive contains duplicate member names")
            try:
                manifest_info = archive.getinfo(MANIFEST_NAME)
            except KeyError as error:
                raise HandoffError("handoff archive has no manifest") from error
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise HandoffError("handoff manifest exceeds the safe verification limit")
            try:
                manifest = json.loads(
                    archive.read(manifest_info).decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HandoffError("handoff manifest is not valid UTF-8 JSON") from error
            if not isinstance(manifest, dict):
                raise HandoffError("handoff manifest root is not an object")
            if expected_manifest is not None and manifest != expected_manifest:
                raise HandoffError("handoff archive manifest changed during creation")
            records = _manifest_records(manifest)
            expected_names = {str(record["path"]) for record in records}
            expected_names.add(MANIFEST_NAME)
            if set(names) != expected_names:
                raise HandoffError("handoff archive members do not exactly match its manifest")
            for record in records:
                relative = str(record["path"])
                info = archive.getinfo(relative)
                if (
                    info.is_dir()
                    or info.file_size != int(record["bytes"])
                    or info.compress_type != zipfile.ZIP_DEFLATED
                    or info.flag_bits & 0x1
                ):
                    raise HandoffError(f"handoff archive member size is invalid: {relative}")
                unix_type = (info.external_attr >> 16) & 0o170000
                unix_mode = (info.external_attr >> 16) & 0o777
                if unix_type != stat.S_IFREG or unix_mode != int(str(record["mode"]), 8):
                    raise HandoffError(f"handoff archive member mode is invalid: {relative}")
                digest = hashlib.sha256()
                with archive.open(info, mode="r") as stream:
                    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                        digest.update(chunk)
                if digest.hexdigest() != record["sha256"]:
                    raise HandoffError(f"handoff archive member hash is invalid: {relative}")
            # Reading every member through ZipExtFile above also validates its
            # CRC at EOF, so a second testzip() pass would only duplicate I/O.
    except HandoffError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise HandoffError("handoff archive verification failed") from error
    return {
        "ok": True,
        "archive": str(archive_path),
        "checksum_file": str(checksum_path),
        "sha256": actual_archive_sha,
        "git_head": manifest["git"]["head"],
        "git_dirty": manifest["git"]["dirty"],
        "file_count": manifest["file_count"],
        "source_bytes": manifest["total_bytes"],
        "generated_output_included": False,
    }


def verify_handoff(archive_path: Path, checksum_path: Path | None = None) -> dict[str, object]:
    """Verify an existing handoff archive and its SHA-256 sidecar without extraction."""

    requested_archive = archive_path.expanduser()
    requested_checksum = (
        checksum_path.expanduser()
        if checksum_path is not None
        else requested_archive.with_name(f"{requested_archive.name}.sha256")
    )
    _recover_publication_pair(requested_archive, requested_checksum)
    if requested_archive.is_symlink():
        raise HandoffError("handoff archive verification refuses symlinks")
    try:
        archive = requested_archive.resolve(strict=True)
    except OSError as error:
        raise HandoffError("handoff archive is missing or inaccessible") from error
    if not archive.is_file():
        raise HandoffError("handoff archive is not a regular file")
    if requested_checksum.is_symlink():
        raise HandoffError("handoff checksum verification refuses symlinks")
    try:
        checksum = requested_checksum.resolve(strict=True)
    except OSError as error:
        raise HandoffError("handoff checksum sidecar is missing or inaccessible") from error
    if not checksum.is_file():
        raise HandoffError("handoff checksum sidecar is not a regular file")
    return _verify_handoff_paths(
        archive,
        checksum,
        expected_archive_name=archive.name,
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_existing_destination(path: Path) -> None:
    try:
        value = path.lstat()
    except OSError as error:
        raise HandoffError("existing handoff destination is inaccessible") from error
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise HandoffError("handoff destination exists but is not a regular file")


def _prepare_output(repo: Path, requested: Path, replace: bool) -> tuple[Path, Path]:
    expanded = requested.expanduser()
    if any(character in expanded.name for character in ("\0", "\r", "\n")):
        raise HandoffError("handoff output filename contains a control character")
    if expanded.suffix.lower() != ".zip":
        raise HandoffError("handoff output must use a .zip extension")
    absolute = Path(os.path.abspath(expanded))
    parent = absolute.parent.resolve(strict=False)
    output = parent / absolute.name
    if _is_within(output, repo):
        raise HandoffError("handoff output must be outside the source repository")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        parent = parent.resolve(strict=True)
    except OSError as error:
        raise HandoffError("handoff output directory could not be created") from error
    output = parent / absolute.name
    if _is_within(output, repo):
        raise HandoffError("handoff output must be outside the source repository")
    checksum = output.with_name(f"{output.name}.sha256")
    _recover_publication_pair(output, checksum)
    occupied = [path for path in (output, checksum) if _lexists(path)]
    for path in occupied:
        _validate_existing_destination(path)
    if occupied and not replace:
        raise HandoffError(
            "handoff archive or checksum already exists (use --replace explicitly)"
        )
    return output, checksum


def _temporary_file(parent: Path, prefix: str, suffix: str) -> Path:
    try:
        with tempfile.NamedTemporaryFile(
            prefix=prefix,
            suffix=suffix,
            dir=parent,
            delete=False,
        ) as stream:
            return Path(stream.name)
    except OSError as error:
        raise HandoffError("handoff staging file could not be created") from error


def _write_sidecar(path: Path, digest: str, archive_name: str) -> None:
    data = f"{digest}  {archive_name}\n".encode("ascii")
    try:
        with path.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise HandoffError("handoff checksum sidecar could not be staged") from error


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _publication_paths(output: Path, checksum: Path) -> tuple[Path, Path, Path]:
    marker = output.with_name(f".{output.name}.handoff-publication.json")
    archive_backup = output.with_name(f".{output.name}.handoff-backup")
    checksum_backup = checksum.with_name(f".{checksum.name}.handoff-backup")
    return marker, archive_backup, checksum_backup


def _atomic_publication_marker(path: Path, value: dict[str, object]) -> None:
    temporary = _temporary_file(path.parent, f".{path.name}.", ".marker-stage")
    try:
        with temporary.open("wb") as stream:
            stream.write((json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        _safe_unlink(temporary)
        raise


def _read_publication_marker(
    marker: Path,
    *,
    output: Path,
    checksum: Path,
    archive_backup: Path,
    checksum_backup: Path,
) -> dict[str, object]:
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HandoffError("handoff publication recovery marker is invalid") from error
    expected = {
        "output": str(output.resolve(strict=False)),
        "checksum": str(checksum.resolve(strict=False)),
        "archive_backup": str(archive_backup.resolve(strict=False)),
        "checksum_backup": str(checksum_backup.resolve(strict=False)),
    }
    if not isinstance(value, dict) or value.get("schema") != PUBLICATION_SCHEMA:
        raise HandoffError("handoff publication recovery marker has an unexpected schema")
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise HandoffError("handoff publication recovery marker paths do not match")
    candidate = value.get("candidate_sha256")
    if not isinstance(candidate, str) or len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise HandoffError("handoff publication recovery marker has an invalid candidate hash")
    if not isinstance(value.get("previous_archive"), bool) or not isinstance(
        value.get("previous_checksum"), bool
    ):
        raise HandoffError("handoff publication recovery marker has invalid prior-state flags")
    return value


def _recover_publication_pair(output: Path, checksum: Path) -> str | None:
    marker, archive_backup, checksum_backup = _publication_paths(output, checksum)
    if not marker.exists():
        if archive_backup.exists() or checksum_backup.exists():
            raise HandoffError(
                "orphaned handoff publication backup found without its recovery marker"
            )
        return None

    recovery = _read_publication_marker(
        marker,
        output=output,
        checksum=checksum,
        archive_backup=archive_backup,
        checksum_backup=checksum_backup,
    )
    candidate_sha = str(recovery["candidate_sha256"])
    try:
        if output.is_file() and checksum.is_file():
            try:
                verified = _verify_handoff_paths(
                    output,
                    checksum,
                    expected_archive_name=output.name,
                )
            except HandoffError:
                verified = None
            if verified is not None and verified["sha256"] == candidate_sha:
                _safe_unlink(archive_backup)
                _safe_unlink(checksum_backup)
                marker.unlink(missing_ok=True)
                return "completed_candidate_publication"

        previous_archive = bool(recovery["previous_archive"])
        previous_checksum = bool(recovery["previous_checksum"])
        if archive_backup.exists() or checksum_backup.exists():
            _safe_unlink(output)
            _safe_unlink(checksum)
            if previous_archive:
                if not archive_backup.is_file():
                    raise HandoffError("handoff archive backup is missing during recovery")
                os.replace(archive_backup, output)
            if previous_checksum:
                if not checksum_backup.is_file():
                    raise HandoffError("handoff checksum backup is missing during recovery")
                os.replace(checksum_backup, checksum)
            marker.unlink(missing_ok=True)
            return "rolled_back_interrupted_publication"

        # The marker can be durable before any rename occurs. In that phase an existing prior
        # pair is still authoritative; a brand-new publication may only have empty reservations.
        if previous_archive or previous_checksum:
            if previous_archive and not output.is_file():
                raise HandoffError("handoff prior archive disappeared during recovery")
            if previous_checksum and not checksum.is_file():
                raise HandoffError("handoff prior checksum disappeared during recovery")
            marker.unlink(missing_ok=True)
            return "cleared_prepared_publication"
        _safe_unlink(output)
        _safe_unlink(checksum)
        marker.unlink(missing_ok=True)
        return "removed_partial_new_publication"
    except OSError as error:
        raise HandoffError("handoff publication recovery failed; preserve recovery files") from error


def _reserve_destination(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise HandoffError(
            "handoff destination appeared during publication; nothing was overwritten"
        ) from error
    except OSError as error:
        raise HandoffError("handoff destination could not be reserved safely") from error
    try:
        os.close(descriptor)
    except OSError as error:
        _safe_unlink(path)
        raise HandoffError("handoff destination reservation could not be finalized") from error


def _publish_pair(
    staged_archive: Path,
    staged_checksum: Path,
    output: Path,
    checksum: Path,
    *,
    replace: bool,
) -> None:
    targets = (output, checksum)
    marker, archive_backup, checksum_backup = _publication_paths(output, checksum)
    _recover_publication_pair(output, checksum)
    backups = {output: archive_backup, checksum: checksum_backup}
    reserved: list[Path] = []
    published: list[Path] = []
    previous = {target: _lexists(target) for target in targets}
    recovery: dict[str, object] = {
        "schema": PUBLICATION_SCHEMA,
        "phase": "prepared",
        "output": str(output.resolve(strict=False)),
        "checksum": str(checksum.resolve(strict=False)),
        "archive_backup": str(archive_backup.resolve(strict=False)),
        "checksum_backup": str(checksum_backup.resolve(strict=False)),
        "previous_archive": previous[output],
        "previous_checksum": previous[checksum],
        "candidate_sha256": _sha256_file(staged_archive),
    }
    _atomic_publication_marker(marker, recovery)
    try:
        if replace:
            for target in targets:
                if _lexists(target):
                    _validate_existing_destination(target)
                    backup = backups[target]
                    if _lexists(backup):
                        raise HandoffError("handoff publication backup is unexpectedly occupied")
                    os.replace(target, backup)
        else:
            for target in targets:
                _reserve_destination(target)
                reserved.append(target)
        recovery["phase"] = "previous_secured"
        _atomic_publication_marker(marker, recovery)
        os.replace(staged_archive, output)
        published.append(output)
        recovery["phase"] = "archive_published"
        _atomic_publication_marker(marker, recovery)
        os.replace(staged_checksum, checksum)
        published.append(checksum)
        recovery["phase"] = "pair_published"
        _atomic_publication_marker(marker, recovery)
        verified = _verify_handoff_paths(
            output,
            checksum,
            expected_archive_name=output.name,
        )
        if verified["sha256"] != recovery["candidate_sha256"]:
            raise HandoffError("published handoff pair does not match the staged candidate")
    except BaseException as error:
        for target in reversed(published):
            _safe_unlink(target)
        for target in reserved:
            if target not in published:
                _safe_unlink(target)
        rollback_errors: list[BaseException] = []
        for target, backup in reversed(tuple(backups.items())):
            try:
                if backup.exists():
                    if _lexists(target):
                        _safe_unlink(target)
                    os.replace(backup, target)
            except BaseException as rollback_error:  # pragma: no cover - catastrophic I/O
                rollback_errors.append(rollback_error)
        _safe_unlink(marker)
        if rollback_errors:
            raise HandoffError(
                "handoff publication failed and the previous artifact could not be fully restored"
            ) from error
        if isinstance(error, (HandoffError, KeyboardInterrupt, SystemExit)):
            raise
        raise HandoffError(
            "handoff publication failed; staged and partial artifacts were rolled back"
        ) from error
    else:
        for backup in backups.values():
            _safe_unlink(backup)
        _safe_unlink(marker)


def create_handoff(
    repo: Path,
    output: Path,
    *,
    replace: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    verbose: bool = False,
) -> dict[str, object]:
    """Build, self-verify, and transactionally publish a source handoff."""

    _log(verbose, "resolving repository and destination")
    root = _repository_root(repo)
    output, checksum = _prepare_output(root, output, replace)
    staged_archive: Path | None = None
    staged_checksum: Path | None = None
    try:
        _log(verbose, "capturing Git selection and repository state")
        snapshot = _snapshot_repository(root)
        files, total_bytes = _collect_sources(
            root,
            snapshot,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        _assert_snapshot_unchanged(root, snapshot)
        _log(verbose, f"validated {len(files)} source files ({total_bytes} bytes)")
        manifest = _manifest(snapshot, files, total_bytes)
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        staged_archive = _temporary_file(
            output.parent, f".{output.name}.", ".archive-stage"
        )
        staged_checksum = _temporary_file(
            output.parent, f".{checksum.name}.", ".checksum-stage"
        )
        _log(verbose, "writing deterministic archive")
        _write_archive(staged_archive, files, manifest_bytes)
        archive_sha256 = _sha256_file(staged_archive)
        _write_sidecar(staged_checksum, archive_sha256, output.name)
        _log(verbose, "self-verifying archive, manifest, members, and checksum")
        result = _verify_handoff_paths(
            staged_archive,
            staged_checksum,
            expected_archive_name=output.name,
            expected_manifest=manifest,
        )
        _assert_sources_unchanged(files)
        _assert_snapshot_unchanged(root, snapshot)
        _log(verbose, "publishing archive and checksum transactionally")
        _publish_pair(
            staged_archive,
            staged_checksum,
            output,
            checksum,
            replace=replace,
        )
        staged_archive = None
        staged_checksum = None
        result["archive"] = str(output)
        result["checksum_file"] = str(checksum)
        _log(verbose, "handoff completed successfully")
        return result
    finally:
        if staged_archive is not None:
            _safe_unlink(staged_archive)
        if staged_checksum is not None:
            _safe_unlink(staged_checksum)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--verify", action="store_true", help="verify --output and its sidecar")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.verify:
            result = verify_handoff(args.output)
        else:
            result = create_handoff(
                args.repo,
                args.output,
                replace=args.replace,
                max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes,
                verbose=args.verbose,
            )
    except (HandoffError, OSError, zipfile.BadZipFile) as error:
        failure = {"ok": False, "error": str(error)}
        if args.json:
            print(json.dumps(failure, indent=2, sort_keys=True))
        else:
            print(f"Source handoff failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Verified {result['archive']}" if args.verify else f"Created {result['archive']}")
        print(f"SHA-256 {result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

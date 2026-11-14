from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest

import spiffy_ui.listary_content_search as content_module
from spiffy_ui.listary_content_search import (
    MemoryCoreContentSearchError,
    MemoryCoreContentSearchProvider,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _history_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        counts: dict[str, int] = {}
        for table in ("search_runs", "search_results", "query_cache"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if exists else 0
        return counts
    finally:
        conn.close()


def test_memorycore_content_provider_keeps_query_out_of_child_argv(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _Process:
        returncode = 0

        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = tuple(str(value) for value in argv)
            captured["kwargs"] = kwargs

        def communicate(self, input=None, timeout=None):
            captured["stdin"] = bytes(input or b"")
            return (
                json.dumps(
                    {
                        "schema": "spiffy_listary_memorycore_response_v1",
                        "ok": True,
                        "status": "no_results",
                        "privacy_scope": "normal",
                        "semantic_status": "not_run",
                        "semantic_reason": "semantic_not_requested_or_test_provider",
                        "side_effects": {
                            "writes_search_run": False,
                            "writes_search_results": False,
                            "writes_explanations": False,
                            "scans_folders": False,
                            "decodes_media": False,
                        },
                        "results": [],
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                b"",
            )

        def poll(self):
            return 0

    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive")
    provider._python_runtime = Path("C:/fixed/memorycore-python.exe")
    provider._owner_root = tmp_path
    monkeypatch.setattr(provider, "_resolve_python_runtime", lambda: provider._python_runtime)
    monkeypatch.setattr(content_module.subprocess, "Popen", _Process)

    query = "sensitive phrase never belongs in argv"
    page = provider.search(query, limit=5)

    assert page.results == ()
    assert query not in " ".join(captured["argv"])
    request = json.loads(captured["stdin"].decode("utf-8"))
    assert request["query"] == query
    assert request["operation"] == "search"
    assert request["limit"] == 5
    assert request["archive_root"] == str((tmp_path / "archive").resolve())


def test_memorycore_content_provider_cancelled_before_launch(monkeypatch, tmp_path: Path) -> None:
    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive")
    event = threading.Event()
    event.set()
    monkeypatch.setattr(content_module.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")))
    with pytest.raises(MemoryCoreContentSearchError, match="cancelled") as error:
        provider.search("needle", cancel_event=event)
    assert error.value.code == "cancelled"


def test_memorycore_content_provider_revalidates_promoted_runtime_on_every_invocation(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "local"
    runtime_root = local_app_data / "MemoryCore"
    runtime_root.mkdir(parents=True)
    python_a = tmp_path / "runtime-a.exe"
    python_b = tmp_path / "runtime-b.exe"
    python_a.write_bytes(b"runtime-a")
    python_b.write_bytes(b"runtime-b")
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()
    receipt_path = runtime_root / "host-runtime-current.json"

    def write_receipt(python_path: Path, owner_path: Path, marker: str) -> None:
        receipt_path.write_text(
            json.dumps(
                {
                    "schema": "memorycore_host_runtime_ref_v1",
                    "python": str(python_path),
                    "python_sha256": hashlib.sha256(python_path.read_bytes()).hexdigest(),
                    "installed_app_root": str(owner_path),
                    "installed_app_manifest_sha256": marker * 64,
                    "installed_app_source_fingerprint": ("f" if marker != "f" else "e") * 64,
                }
            ),
            encoding="utf-8",
        )

    write_receipt(python_a, owner_a, "a")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    provider = MemoryCoreContentSearchProvider(timeout_seconds=1.0)
    verified: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(
        provider,
        "_verify_owner_package",
        lambda root, *, expected_manifest_sha256, expected_source_fingerprint: verified.append(
            (Path(root), expected_manifest_sha256, expected_source_fingerprint)
        ),
    )

    class _Completed:
        returncode = 0

    monkeypatch.setattr(content_module.subprocess, "run", lambda *_args, **_kwargs: _Completed())

    first = provider._worker_argv()
    assert Path(first[0]) == python_a.resolve()
    assert provider._owner_root == owner_a.resolve()
    assert len(verified) == 1

    write_receipt(python_b, owner_b, "b")
    second = provider._worker_argv()
    assert Path(second[0]) == python_b.resolve()
    assert provider._owner_root == owner_b.resolve()
    assert len(verified) == 2
    assert verified[0][0] == owner_a.resolve()
    assert verified[1][0] == owner_b.resolve()


def test_memorycore_content_provider_fails_closed_when_runtime_changes_after_first_use(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "local"
    runtime_root = local_app_data / "MemoryCore"
    runtime_root.mkdir(parents=True)
    python_path = tmp_path / "runtime.exe"
    python_path.write_bytes(b"runtime-v1")
    owner = tmp_path / "owner"
    owner.mkdir()
    receipt_path = runtime_root / "host-runtime-current.json"

    def write_receipt(expected_python_sha: str) -> None:
        receipt_path.write_text(
            json.dumps(
                {
                    "schema": "memorycore_host_runtime_ref_v1",
                    "python": str(python_path),
                    "python_sha256": expected_python_sha,
                    "installed_app_root": str(owner),
                    "installed_app_manifest_sha256": "a" * 64,
                    "installed_app_source_fingerprint": "b" * 64,
                }
            ),
            encoding="utf-8",
        )

    write_receipt(hashlib.sha256(python_path.read_bytes()).hexdigest())
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    provider = MemoryCoreContentSearchProvider(timeout_seconds=1.0)
    monkeypatch.setattr(provider, "_verify_owner_package", lambda *_args, **_kwargs: None)

    class _Completed:
        returncode = 0

    monkeypatch.setattr(content_module.subprocess, "run", lambda *_args, **_kwargs: _Completed())
    assert Path(provider._worker_argv()[0]) == python_path.resolve()

    python_path.write_bytes(b"runtime-v2-tampered")
    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider._worker_argv()
    assert error.value.code == "memorycore_runtime_digest_mismatch"


@pytest.mark.parametrize(
    ("stdout", "expected_code"),
    (
        (b"not-json", "invalid_response"),
        (b"x" * (512 * 1024 + 1), "output_too_large"),
    ),
    ids=("malformed", "oversized"),
)
def test_memorycore_content_provider_rejects_malformed_or_oversized_worker_output(
    monkeypatch,
    tmp_path: Path,
    stdout: bytes,
    expected_code: str,
) -> None:
    class _Process:
        returncode = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def communicate(self, input=None, timeout=None):
            return stdout, b""

        def poll(self):
            return 0

    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive")
    provider._python_runtime = Path("C:/fixed/memorycore-python.exe")
    provider._owner_root = tmp_path
    monkeypatch.setattr(provider, "_resolve_python_runtime", lambda: provider._python_runtime)
    monkeypatch.setattr(content_module.subprocess, "Popen", _Process)
    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider.search("needle")
    assert error.value.code == expected_code


def test_memorycore_content_provider_timeout_terminates_only_owned_child(monkeypatch, tmp_path: Path) -> None:
    process_holder: list[object] = []

    class _HungProcess:
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            self.argv = tuple(argv)
            self.terminated = False
            self.killed = False
            process_holder.append(self)

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(self.argv, timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive", timeout_seconds=0.25)
    provider._python_runtime = Path("C:/fixed/memorycore-python.exe")
    provider._owner_root = tmp_path
    monkeypatch.setattr(provider, "_resolve_python_runtime", lambda: provider._python_runtime)
    monkeypatch.setattr(content_module.subprocess, "Popen", _HungProcess)
    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider.search("needle")
    assert error.value.code == "timeout"
    assert len(process_holder) == 1
    assert process_holder[0].terminated is True
    assert process_holder[0].killed is False


def test_memorycore_content_provider_rejects_source_target_without_exact_sha256(monkeypatch, tmp_path: Path) -> None:
    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive")
    result = content_module.MemoryCoreContentResult(
        document_id="doc",
        doc_id="doc",
        media_id="media",
        title="Result",
        snippet="snippet",
        doc_type="memory_card",
        source_table="memory_cards",
        source_id="card",
        matched_via=("lexical",),
        representation_revision="b" * 64,
        offset_unit="utf8_bytes",
    )
    monkeypatch.setattr(
        provider,
        "_invoke",
        lambda *_args, **_kwargs: {
            "target": {
                "path": str(tmp_path / "source.mp4"),
                "source_state": "playable_exact",
                "reason_code": "registered_original_playable",
                "source_sha256": "not-a-sha256",
                "target_kind": "media_items.original_path",
            }
        },
    )

    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider.resolve_source(result)
    assert error.value.code == "provider_contract_violation"


def test_memorycore_content_provider_rejects_relative_source_target(monkeypatch, tmp_path: Path) -> None:
    provider = MemoryCoreContentSearchProvider(archive_root=tmp_path / "archive")
    result = content_module.MemoryCoreContentResult(
        document_id="doc",
        doc_id="doc",
        media_id="media",
        title="Result",
        snippet="snippet",
        doc_type="memory_card",
        source_table="memory_cards",
        source_id="card",
        matched_via=("lexical",),
        representation_revision="b" * 64,
        offset_unit="utf8_bytes",
    )
    monkeypatch.setattr(
        provider,
        "_invoke",
        lambda *_args, **_kwargs: {
            "target": {
                "path": "relative/source.mp4",
                "source_state": "playable_exact",
                "reason_code": "registered_original_playable",
                "source_sha256": "a" * 64,
                "target_kind": "media_items.original_path",
            }
        },
    )

    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider.resolve_source(result)
    assert error.value.code == "provider_contract_violation"


_DISPOSABLE_ARCHIVE_SETUP = r'''
import hashlib
import os
from pathlib import Path
import sys

owner = Path(os.environ["SPIFFY_LISTARY_MEMORYCORE_OWNER_ROOT"]).resolve()
sys.path.insert(0, str(owner))
from memorycore.storage.db import open_database
from memorycore.storage.migrations import initialize_schema

archive = Path(os.environ["LISTARY_CONTENT_FIXTURE_ARCHIVE"]).resolve()
normal_source = Path(os.environ["LISTARY_CONTENT_NORMAL_SOURCE"]).resolve()
personal_source = Path(os.environ["LISTARY_CONTENT_PERSONAL_SOURCE"]).resolve()
db_path = archive / "db" / "main.db"
conn = open_database(db_path)
try:
    initialize_schema(conn)
    created = "2026-10-03T12:00:00Z"
    media_rows = (
        (
            "media-normal", "voice_archive", str(normal_source), "Normal source", ".mp4",
            normal_source.stat().st_size, hashlib.sha256(normal_source.read_bytes()).hexdigest(), "normal", created,
        ),
        (
            "media-personal", "iphone", str(personal_source), "Personal source", ".mp4",
            personal_source.stat().st_size, hashlib.sha256(personal_source.read_bytes()).hexdigest(), "personal", created,
        ),
    )
    conn.executemany(
        """
        INSERT INTO media_items(
            media_id,source_kind,original_path,display_name,extension,size_bytes,payload_hash,
            privacy_level,status,created_at_utc
        ) VALUES (?,?,?,?,?,?,?,?,'registered',?)
        """,
        media_rows,
    )
    documents = (
        ("doc-normal", "media-normal", "Normal Content", "needlealpha useful normal content", "normal", "normal"),
        ("doc-personal", "media-personal", "Personal Content", "needlealpha private personal content", "personal", "personal"),
    )
    for doc_id, media_id, title, body, privacy, provenance in documents:
        conn.execute(
            """
            INSERT INTO search_documents(
                document_id,doc_id,media_id,source_table,source_id,doc_type,title,body,body_hash,
                start_ms,end_ms,scope,tags_json,privacy_level,provenance_max_scope,rank_features_json,updated_at_utc
            ) VALUES (?,?,?,?,?,'memory_card',?,?,?,?,?,'normal','[]',?,?,'{}',?)
            """,
            (
                doc_id,doc_id,media_id,"memory_cards",doc_id,title,body,hashlib.sha256(body.encode("utf-8")).hexdigest(),
                1000,2000,privacy,provenance,created,
            ),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
finally:
    conn.close()
'''


def test_memorycore_content_provider_disposable_archive_end_to_end(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    normal_source = tmp_path / "normal-source.mp4"
    personal_source = tmp_path / "personal-source.mp4"
    normal_source.write_bytes(b"normal-source-v1")
    personal_source.write_bytes(b"personal-source-v1")

    provider = MemoryCoreContentSearchProvider(archive_root=archive, timeout_seconds=8.0)
    worker_argv = provider._worker_argv()
    owner_root = provider._owner_root
    assert owner_root is not None
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["SPIFFY_LISTARY_MEMORYCORE_OWNER_ROOT"] = str(owner_root)
    env["LISTARY_CONTENT_FIXTURE_ARCHIVE"] = str(archive)
    env["LISTARY_CONTENT_NORMAL_SOURCE"] = str(normal_source)
    env["LISTARY_CONTENT_PERSONAL_SOURCE"] = str(personal_source)
    setup = subprocess.run(
        [worker_argv[0], "-I", "-B", "-c", _DISPOSABLE_ARCHIVE_SETUP],
        cwd=str(owner_root),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr.decode("utf-8", errors="replace")

    db_path = archive / "db" / "main.db"
    assert db_path.is_file()
    before_hash = _sha256(db_path)
    before_mtime = db_path.stat().st_mtime_ns
    before_counts = _history_counts(db_path)

    query = "needlealpha type:memory_card"
    assert query not in " ".join(provider._worker_argv())
    page = provider.search(query, limit=10)

    assert [result.document_id for result in page.results] == ["doc-normal"]
    assert page.privacy_scope == "normal"
    assert page.results[0].snippet
    assert page.results[0].representation_revision
    assert page.semantic_status == "not_run"
    assert page.semantic_reason in {"semantic_not_requested_or_test_provider", "structured_filter_only_no_embedding"}
    assert not any(page.side_effects.values())
    assert _history_counts(db_path) == before_counts
    assert _sha256(db_path) == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime

    filter_only = provider.search("type:memory_card source:voicearchive has:video", limit=10)
    assert [result.document_id for result in filter_only.results] == ["doc-normal"]
    assert filter_only.structured_filter_count == 3
    assert filter_only.semantic_status == "not_run"
    assert not any(filter_only.side_effects.values())

    filtered_text = provider.search("needlealpha source:voicearchive has:video", limit=10)
    assert [result.document_id for result in filtered_text.results] == ["doc-normal"]
    assert filtered_text.structured_filter_count == 2

    and_miss = provider.search("needlealpha source:voicearchive has:audio", limit=10)
    assert and_miss.results == ()
    assert and_miss.structured_filter_count == 2

    with pytest.raises(MemoryCoreContentSearchError) as unsupported:
        provider.search("needlealpha madeupfilter:value", limit=10)
    assert unsupported.value.code == "unsupported_content_filter"
    assert "madeupfilter" in str(unsupported.value)

    assert _history_counts(db_path) == before_counts
    assert _sha256(db_path) == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime

    target = provider.resolve_source(page.results[0])
    assert Path(target.path).resolve() == normal_source.resolve()
    assert target.source_sha256 == hashlib.sha256(b"normal-source-v1").hexdigest()
    assert _history_counts(db_path) == before_counts
    assert _sha256(db_path) == before_hash

    normal_source.write_bytes(b"tampered-source")
    with pytest.raises(MemoryCoreContentSearchError) as error:
        provider.resolve_source(page.results[0])
    assert error.value.code == "source_payload_hash_mismatch"
    assert _history_counts(db_path) == before_counts
    assert _sha256(db_path) == before_hash

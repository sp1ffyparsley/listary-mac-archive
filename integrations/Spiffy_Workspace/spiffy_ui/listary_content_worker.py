"""Fixed one-shot worker for Listary's optional MemoryCore Content mode.

The caller passes the request on stdin.  This helper intentionally exposes no
general command execution surface and emits only a reduced JSON projection.
"""

from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
import sys
from typing import Any


REQUEST_SCHEMA = "spiffy_listary_memorycore_request_v1"
RESPONSE_SCHEMA = "spiffy_listary_memorycore_response_v1"
MAX_REQUEST_BYTES = 16 * 1024
MAX_QUERY_BYTES = 4096
MAX_RESULTS = 50
MAX_SNIPPET_CHARS = 480
MAX_OUTPUT_BYTES = 384 * 1024


def _emit(payload: dict[str, Any]) -> int:
    envelope = {"schema": RESPONSE_SCHEMA, **payload}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        envelope = {
            "schema": RESPONSE_SCHEMA,
            "ok": False,
            "error": {"code": "output_too_large", "message": "Content search response exceeded its bounded size."},
        }
        encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0 if envelope.get("ok") is True else 2


def _fail(code: str, message: str) -> int:
    return _emit({"ok": False, "error": {"code": str(code), "message": str(message)}})


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _read_request() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("request_size_invalid")
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("schema") != REQUEST_SCHEMA:
        raise ValueError("request_schema_invalid")
    return parsed


def _memorycore_root() -> Path:
    value = str(os.environ.get("SPIFFY_LISTARY_MEMORYCORE_OWNER_ROOT") or "").strip()
    if not value:
        raise ValueError("memorycore_root_missing")
    root = Path(value).expanduser().resolve()
    if not (root / "memorycore" / "__init__.py").is_file():
        raise ValueError("memorycore_root_invalid")
    sys.path.insert(0, str(root))
    return root


def _open_read_only(root: Path, request: dict[str, Any]):
    from memorycore.core.config import load_config
    from memorycore.storage.db import database_path, open_database

    config_path = root / "memorycore.config.yaml"
    if not config_path.is_file():
        config_path = root / "memorycore.config.example.yaml"
    config = load_config(config_path if config_path.is_file() else None)
    archive_value = request.get("archive_root")
    if archive_value is not None:
        archive_root = Path(str(archive_value)).expanduser()
        if not archive_root.is_absolute():
            raise ValueError("archive_root_must_be_absolute")
        config.archive_root = archive_root.resolve()
    db_path = database_path(config)
    if not db_path.is_file():
        raise FileNotFoundError("archive_database_missing")
    return open_database(db_path, read_only=True)


def _snippet(result: dict[str, Any], document: dict[str, Any]) -> str:
    passages = result.get("passages") if isinstance(result.get("passages"), list) else []
    for passage in passages:
        if isinstance(passage, dict) and passage.get("text"):
            return _text(passage.get("text"), MAX_SNIPPET_CHARS)
    return _text(document.get("body"), MAX_SNIPPET_CHARS)


def _search(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    from memorycore.search.hybrid import hybrid_search

    query = str(request.get("query") or "").strip()
    if not query or len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("query_size_invalid")
    limit = request.get("limit")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("limit_invalid")
    conn = _open_read_only(root, request)
    try:
        report = hybrid_search(
            conn,
            query,
            limit=limit,
            explain=False,
            store=False,
            embedding_provider=None,
            privacy_scope="normal",
        )
    finally:
        conn.close()
    forbidden_prefixes = ("sentence_transformers", "torch", "transformers", "faster_whisper", "pytesseract", "easyocr", "cv2", "ollama")
    forbidden_loaded = sorted(
        name for name in sys.modules if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
    )
    if forbidden_loaded:
        return {
            "ok": False,
            "error": {
                "code": "forbidden_model_or_ocr_lane_loaded",
                "message": "Content search refused a model/OCR lane in the filename-search integration.",
            },
        }
    if not isinstance(report, dict) or report.get("ok") is not True:
        return {
            "ok": False,
            "error": {"code": "memorycore_search_failed", "message": "MemoryCore lexical Content search failed."},
        }
    structured_status = (
        report.get("structured_filter_status")
        if isinstance(report.get("structured_filter_status"), dict)
        else {}
    )
    unsupported_filters = (
        structured_status.get("unsupported_filters")
        if isinstance(structured_status.get("unsupported_filters"), list)
        else []
    )
    if unsupported_filters:
        names = sorted(
            {
                _text(item.get("name"), 32)
                for item in unsupported_filters
                if isinstance(item, dict) and _text(item.get("name"), 32)
            }
        )
        return {
            "ok": False,
            "error": {
                "code": "unsupported_content_filter",
                "message": (
                    "Unsupported Content filter"
                    + (f": {', '.join(names)}" if names else ".")
                    + " Use the Content filter chips or supported MemoryCore filter syntax."
                ),
            },
        }
    reduced: list[dict[str, Any]] = []
    for result in report.get("results") if isinstance(report.get("results"), list) else []:
        if not isinstance(result, dict):
            continue
        document = result.get("document") if isinstance(result.get("document"), dict) else {}
        if str(document.get("privacy_level") or "") != "normal":
            continue
        matched = result.get("matched_via") if isinstance(result.get("matched_via"), list) else []
        reduced.append(
            {
                "document_id": str(document.get("document_id") or ""),
                "doc_id": str(document.get("doc_id") or document.get("document_id") or ""),
                "media_id": str(document.get("media_id") or ""),
                "title": _text(document.get("title") or document.get("doc_type") or "Content result", 160),
                "snippet": _snippet(result, document),
                "doc_type": _text(document.get("doc_type") or "content", 80),
                "source_table": _text(document.get("source_table"), 96),
                "source_id": _text(document.get("source_id"), 160),
                "matched_via": [_text(value, 48) for value in matched[:12] if str(value)],
                "representation_revision": str(result.get("representation_revision") or document.get("body_hash") or ""),
                "offset_unit": str(result.get("offset_unit") or "utf8_bytes"),
                "start_ms": document.get("start_ms") if type(document.get("start_ms")) is int else None,
                "end_ms": document.get("end_ms") if type(document.get("end_ms")) is int else None,
            }
        )
        if len(reduced) >= limit:
            break
    vector_status = report.get("vector_channel_status") if isinstance(report.get("vector_channel_status"), dict) else {}
    side_effects = report.get("side_effects") if isinstance(report.get("side_effects"), dict) else {}
    return {
        "ok": True,
        "status": str(report.get("status") or ("ready" if reduced else "no_results")),
        "privacy_scope": "normal",
        "semantic_status": str(vector_status.get("status") or "unknown"),
        "semantic_reason": str(vector_status.get("reason") or ""),
        "structured_filter_count": max(0, int(structured_status.get("filter_count") or 0)),
        "side_effects": {
            "writes_search_run": bool(side_effects.get("writes_search_run")),
            "writes_search_results": bool(side_effects.get("writes_search_results")),
            "writes_explanations": bool(side_effects.get("writes_explanations")),
            "scans_folders": bool(side_effects.get("scans_folders")),
            "decodes_media": bool(side_effects.get("decodes_media")),
        },
        "results": reduced,
    }


def _resolve_source(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    from memorycore.search.documents import hydrate_document, read_search_document
    from memorycore.workflow.source_open import resolve_source_open_target

    document_id = str(request.get("document_id") or "").strip()
    media_id = str(request.get("media_id") or "").strip()
    revision = str(request.get("representation_revision") or "").strip().lower()
    offset_unit = str(request.get("offset_unit") or "utf8_bytes").strip().lower()
    start_ms = request.get("start_ms") if type(request.get("start_ms")) is int else None
    end_ms = request.get("end_ms") if type(request.get("end_ms")) is int else None
    if not document_id or not media_id or len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("source_identity_invalid")
    if offset_unit != "utf8_bytes":
        raise ValueError("source_offset_unit_invalid")
    exact_range = start_ms is not None or end_ms is not None
    if exact_range and (start_ms is None or end_ms is None or not 0 <= start_ms < end_ms):
        raise ValueError("source_range_invalid")

    conn = _open_read_only(root, request)
    try:
        # Keep the normal-scope preflight and the canonical resolver in one
        # read snapshot.  The resolver itself accepts personal scope for other
        # MemoryCore callers; Listary must never widen beyond normal.
        conn.execute("BEGIN")
        reader = read_search_document(
            conn,
            document_id,
            privacy_scope="normal",
            expected_revision=revision,
        )
        if (
            str(reader.get("source_family_id") or "") != media_id
            or str(reader.get("representation_revision") or "") != revision
            or str(reader.get("offset_unit") or "") != offset_unit
        ):
            raise ValueError("source_document_identity_changed")
        document = hydrate_document(conn, document_id)
        if not document or str(document.get("media_id") or "") != media_id or str(document.get("privacy_level") or "") != "normal":
            raise ValueError("source_document_identity_changed")
        if exact_range:
            source_units = reader.get("source_units") if isinstance(reader.get("source_units"), list) else []
            if source_units:
                range_matches = any(
                    isinstance(unit, dict)
                    and unit.get("start_ms") == start_ms
                    and unit.get("end_ms") == end_ms
                    for unit in source_units
                )
            else:
                range_matches = document.get("start_ms") == start_ms and document.get("end_ms") == end_ms
            if not range_matches:
                raise ValueError("search_evidence_time_range_changed")
        media = conn.execute(
            "SELECT privacy_level,status,payload_hash FROM media_items WHERE media_id=?",
            (media_id,),
        ).fetchone()
        if (
            media is None
            or str(media["privacy_level"] or "") != "normal"
            or str(media["status"] or "").lower() in {"deleted", "trash", "revoked", "excluded"}
        ):
            raise PermissionError("search_document_source_unavailable")
        expected_source_sha = str(media["payload_hash"] or "").strip().lower()
        if len(expected_source_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_source_sha):
            raise ValueError("source_payload_identity_missing")
        target = resolve_source_open_target(
            conn,
            media_id=media_id,
            preferred_kind="source",
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if target.path is None or target.report.get("ok") is not True or not target.path.is_file():
            reason = str(target.report.get("reason_code") or target.report.get("status") or "source_unavailable")
            return {"ok": False, "error": {"code": reason, "message": "The Content source is unavailable."}}
        digest = hashlib.sha256()
        with target.path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        actual_source_sha = digest.hexdigest()
        if actual_source_sha != expected_source_sha:
            return {
                "ok": False,
                "error": {"code": "source_payload_hash_mismatch", "message": "The Content source changed since MemoryCore indexed it."},
            }
        confirmed = read_search_document(
            conn,
            document_id,
            privacy_scope="normal",
            expected_revision=revision,
        )
        if str(confirmed.get("source_family_id") or "") != media_id:
            raise ValueError("source_document_identity_changed")
    finally:
        conn.close()
    report = target.report if isinstance(target.report, dict) else {}
    if target.path is None or report.get("ok") is not True:
        reason = str(report.get("reason_code") or report.get("status") or "source_unavailable")
        return {
            "ok": False,
            "error": {"code": reason, "message": "The Content source changed or is no longer exactly verifiable."},
        }
    return {
        "ok": True,
        "target": {
            "path": str(target.path),
            "source_state": str(report.get("source_state") or ""),
            "reason_code": str(report.get("reason_code") or ""),
            "source_sha256": expected_source_sha,
            "target_kind": str(report.get("target_kind") or "source"),
        },
    }


def main() -> int:
    try:
        request = _read_request()
        root = _memorycore_root()
        operation = request.get("operation")
        if operation == "search":
            return _emit(_search(root, request))
        if operation == "resolve_source":
            return _emit(_resolve_source(root, request))
        return _fail("operation_unsupported", "Unsupported Content worker operation.")
    except FileNotFoundError:
        return _fail("archive_database_missing", "MemoryCore archive database is unavailable.")
    except PermissionError:
        return _fail("privacy_scope_rejected", "The Content result is outside Listary's normal privacy scope.")
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _fail(str(exc) or "request_invalid", "Content search request or source identity is no longer valid.")
    except Exception as exc:
        return _fail(type(exc).__name__, "MemoryCore Content search is unavailable for this request.")


if __name__ == "__main__":
    raise SystemExit(main())

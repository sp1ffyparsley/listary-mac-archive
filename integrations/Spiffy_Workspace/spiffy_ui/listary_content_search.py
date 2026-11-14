"""Optional MemoryCore-backed content search for Spiffy Listary.

Filename/path search remains owned by the resident Listary agent.  This module
is deliberately a thin one-shot process bridge to MemoryCore's existing
read-only search and source-currentness owners.  It owns no index, database,
watcher, search history, OCR, embedding model, or resident process.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from .listary_content_provider import (
    ContentResult,
    ContentSearchError,
    ContentSearchPage,
    ContentSourceTarget,
)


_REQUEST_SCHEMA = "spiffy_listary_memorycore_request_v1"
_RESPONSE_SCHEMA = "spiffy_listary_memorycore_response_v1"
_MAX_QUERY_BYTES = 4096
_MAX_RESULTS = 50
_MAX_STDOUT_BYTES = 512 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_DEFAULT_TIMEOUT_SECONDS = 4.0
_RUNTIME_RECEIPT_MAX_BYTES = 64 * 1024
_OWNER_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_HOST_CAPABILITIES = ("av", "cryptography", "faster_whisper", "nacl", "numpy", "PySide6", "tzdata")


class MemoryCoreContentSearchError(ContentSearchError):
    pass


class MemoryCoreContentResult(ContentResult):
    pass


class MemoryCoreContentSearchPage(ContentSearchPage):
    pass


class MemoryCoreContentSourceTarget(ContentSourceTarget):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


class MemoryCoreContentSearchProvider:
    """One-shot, privacy-bounded projection of MemoryCore search into Listary."""

    def __init__(
        self,
        *,
        archive_root: str | Path | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.archive_root = Path(archive_root).expanduser().resolve() if archive_root is not None else None
        self.timeout_seconds = min(15.0, max(0.25, float(timeout_seconds)))
        self._python_runtime: Path | None = None
        self._owner_root: Path | None = None
        self._runtime_lock = threading.Lock()

    @property
    def display_name(self) -> str:
        return "MemoryCore"

    @property
    def worker_path(self) -> Path:
        return Path(__file__).with_name("listary_content_worker.py")

    def _validate_worker(self) -> None:
        if not self.worker_path.is_file():
            raise MemoryCoreContentSearchError(
                "content_worker_missing",
                "Content search worker is missing from the installed Spiffy Listary integration.",
            )

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
        except OSError:
            return True

    def _verify_owner_package(
        self,
        root: Path,
        *,
        expected_manifest_sha256: str,
        expected_source_fingerprint: str,
    ) -> None:
        if not root.is_dir() or self._is_reparse(root):
            raise MemoryCoreContentSearchError(
                "memorycore_owner_invalid",
                "MemoryCore's promoted owner package is missing or unsafe.",
            )
        manifest_path = root / "PACKAGE_MANIFEST.json"
        try:
            if not manifest_path.is_file() or manifest_path.stat().st_size > _OWNER_MANIFEST_MAX_BYTES:
                raise ValueError("owner manifest missing or oversized")
            raw = manifest_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
                raise ValueError("owner manifest digest changed")
            manifest = json.loads(raw)
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if manifest.get("schema") != "memorycore.source-release.v1" or not isinstance(files, dict) or not 1 <= len(files) <= 4096:
                raise ValueError("owner manifest contract invalid")
            fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            if fingerprint != manifest.get("source_fingerprint") or fingerprint != expected_source_fingerprint:
                raise ValueError("owner source fingerprint changed")
            verifier_identity = files.get("tools/package_memorycore.py")
            verifier_path = root / "tools" / "package_memorycore.py"
            if not isinstance(verifier_identity, dict) or not verifier_path.is_file() or self._is_reparse(verifier_path):
                raise ValueError("owner verifier missing")
            if (
                type(verifier_identity.get("bytes")) is not int
                or verifier_path.stat().st_size != verifier_identity["bytes"]
                or _sha256_file(verifier_path) != str(verifier_identity.get("sha256") or "").lower()
            ):
                raise ValueError("owner verifier identity changed")
            spec = importlib.util.spec_from_file_location("_spiffy_memorycore_owner_verifier", verifier_path)
            if spec is None or spec.loader is None:
                raise ValueError("owner verifier load failed")
            verifier = importlib.util.module_from_spec(spec)
            previous_no_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec.loader.exec_module(verifier)
            finally:
                sys.dont_write_bytecode = previous_no_bytecode
            verified = verifier.verify_installed_owner(root, expected_manifest_sha256)
            if not isinstance(verified, dict) or verified.get("source_fingerprint") != expected_source_fingerprint:
                raise ValueError("owner verifier fingerprint mismatch")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise MemoryCoreContentSearchError(
                "memorycore_owner_verification_failed",
                "MemoryCore's promoted owner package no longer matches its sealed manifest.",
            ) from exc

    def _resolve_python_runtime(self) -> Path:
        with self._runtime_lock:
            self._validate_worker()
            local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
            runtime_root = Path(local_app_data) / "MemoryCore" if local_app_data else Path.home() / ".memorycore"
            receipt_path = runtime_root / "host-runtime-current.json"
            try:
                if not receipt_path.is_file() or receipt_path.stat().st_size > _RUNTIME_RECEIPT_MAX_BYTES:
                    raise ValueError("runtime receipt missing or oversized")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict) or receipt.get("schema") != "memorycore_host_runtime_ref_v1":
                    raise ValueError("unsupported runtime receipt")
                receipt_python_sha = str(receipt.get("python_sha256") or "").strip().lower()
                if len(receipt_python_sha) != 64 or any(char not in "0123456789abcdef" for char in receipt_python_sha):
                    raise ValueError("invalid interpreter digest")
                receipt_python = Path(os.path.expandvars(str(receipt.get("python") or ""))).expanduser().resolve()
                owner_root = Path(os.path.expandvars(str(receipt.get("installed_app_root") or ""))).expanduser().resolve()
                owner_manifest_sha = str(receipt.get("installed_app_manifest_sha256") or "").strip().lower()
                owner_fingerprint = str(receipt.get("installed_app_source_fingerprint") or "").strip().lower()
                if (
                    len(owner_manifest_sha) != 64
                    or len(owner_fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in owner_manifest_sha + owner_fingerprint)
                ):
                    raise ValueError("installed owner identity missing")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_receipt_invalid",
                    "MemoryCore's promoted runtime/owner receipt is missing or invalid. Run MemoryCore Doctor/bootstrap explicitly.",
                ) from exc
            self._verify_owner_package(
                owner_root,
                expected_manifest_sha256=owner_manifest_sha,
                expected_source_fingerprint=owner_fingerprint,
            )
            candidate = receipt_python
            if not candidate.is_file():
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_missing",
                    "MemoryCore's promoted interpreter is missing. Run MemoryCore Doctor/bootstrap explicitly.",
                )
            try:
                actual_sha = _sha256_file(candidate)
            except OSError as exc:
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_unreadable",
                    "MemoryCore's promoted interpreter could not be verified.",
                ) from exc
            if actual_sha != receipt_python_sha:
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_digest_mismatch",
                    "MemoryCore's promoted interpreter no longer matches its sealed receipt.",
                )

            module_literal = repr(list(_HOST_CAPABILITIES))
            probe = (
                "import importlib.util,sys; modules="
                + module_literal
                + "; sys.exit(0 if all(importlib.util.find_spec(m) for m in modules) else 1)"
            )
            try:
                completed = subprocess.run(
                    [str(candidate), "-I", "-c", probe],
                    cwd=str(owner_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(8.0, max(1.0, self.timeout_seconds)),
                    creationflags=_creation_flags(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_probe_failed",
                    "MemoryCore's promoted runtime could not be started safely.",
                ) from exc
            if completed.returncode != 0:
                raise MemoryCoreContentSearchError(
                    "memorycore_runtime_incomplete",
                    "MemoryCore's promoted runtime is incomplete; Content search will not replace it implicitly.",
                )
            self._python_runtime = candidate
            self._owner_root = owner_root
            return candidate

    def _worker_argv(self) -> tuple[str, ...]:
        """Return the fixed argv; user query/content is intentionally absent."""

        return (str(self._resolve_python_runtime()), "-I", "-B", str(self.worker_path))

    def _invoke(self, request: dict[str, Any], *, cancel_event: threading.Event | None) -> dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            raise MemoryCoreContentSearchError("cancelled", "Content search was cancelled.")
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 16 * 1024:
            raise MemoryCoreContentSearchError("request_too_large", "Content search request exceeded the bounded input size.")
        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = list(self._worker_argv())
        owner_root = self._owner_root
        if owner_root is None:
            raise MemoryCoreContentSearchError("memorycore_owner_invalid", "MemoryCore's promoted owner package is unavailable.")
        env["SPIFFY_LISTARY_MEMORYCORE_OWNER_ROOT"] = str(owner_root)
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(owner_root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            raise MemoryCoreContentSearchError(
                "content_worker_launch_failed",
                "Content search could not start the qualified MemoryCore worker.",
            ) from exc

        deadline = time.monotonic() + self.timeout_seconds
        sent_input = False
        stdout = b""
        stderr = b""
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._stop_child(process)
                    raise MemoryCoreContentSearchError("cancelled", "Content search was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_child(process)
                    raise MemoryCoreContentSearchError("timeout", "Content search exceeded its bounded execution time.")
                try:
                    stdout, stderr = process.communicate(
                        input=payload if not sent_input else None,
                        timeout=min(0.075, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    sent_input = True
                    continue
        finally:
            if process.poll() is None and cancel_event is not None and cancel_event.is_set():
                self._stop_child(process)

        if len(stdout) > _MAX_STDOUT_BYTES:
            raise MemoryCoreContentSearchError("output_too_large", "Content search returned an oversized response.")
        if len(stderr) > _MAX_STDERR_BYTES:
            stderr = stderr[:_MAX_STDERR_BYTES]
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            if process.returncode != 0:
                message = "Content search worker failed."
                if stderr:
                    message = f"{message} {stderr.decode('utf-8', errors='replace')[:240].strip()}".strip()
                raise MemoryCoreContentSearchError("worker_failed", message) from exc
            raise MemoryCoreContentSearchError("invalid_response", "Content search returned malformed data.") from exc
        if not isinstance(response, dict) or response.get("schema") != _RESPONSE_SCHEMA:
            raise MemoryCoreContentSearchError("invalid_response", "Content search returned an unsupported response contract.")
        if response.get("ok") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise MemoryCoreContentSearchError(
                str(error.get("code") or "content_search_unavailable"),
                str(error.get("message") or "Content search is unavailable."),
            )
        if process.returncode != 0:
            raise MemoryCoreContentSearchError("worker_failed", "Content search worker exited unexpectedly.")
        return response

    @staticmethod
    def _stop_child(process: subprocess.Popen[bytes]) -> None:
        try:
            process.terminate()
            process.wait(timeout=0.4)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=0.4)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def search(
        self,
        query: str,
        *,
        limit: int = _MAX_RESULTS,
        cancel_event: threading.Event | None = None,
    ) -> MemoryCoreContentSearchPage:
        query = str(query or "").strip()
        if not query:
            return MemoryCoreContentSearchPage((), 0.0, "no_query", "normal", "not_requested", {})
        if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
            raise MemoryCoreContentSearchError("query_too_large", "Content query exceeds the 4096-byte limit.")
        bounded_limit = min(_MAX_RESULTS, max(1, int(limit)))
        request: dict[str, Any] = {
            "schema": _REQUEST_SCHEMA,
            "operation": "search",
            "query": query,
            "limit": bounded_limit,
        }
        if self.archive_root is not None:
            request["archive_root"] = str(self.archive_root)
        started = time.perf_counter()
        response = self._invoke(request, cancel_event=cancel_event)
        raw_results = response.get("results") if isinstance(response.get("results"), list) else []
        results: list[MemoryCoreContentResult] = []
        for raw in raw_results[:bounded_limit]:
            if not isinstance(raw, dict):
                continue
            matched = raw.get("matched_via") if isinstance(raw.get("matched_via"), list) else []
            results.append(
                MemoryCoreContentResult(
                    document_id=str(raw.get("document_id") or ""),
                    doc_id=str(raw.get("doc_id") or ""),
                    media_id=str(raw.get("media_id") or ""),
                    title=str(raw.get("title") or "Content result"),
                    snippet=str(raw.get("snippet") or ""),
                    doc_type=str(raw.get("doc_type") or "content"),
                    source_table=str(raw.get("source_table") or ""),
                    source_id=str(raw.get("source_id") or ""),
                    matched_via=tuple(str(value) for value in matched[:12] if str(value)),
                    representation_revision=str(raw.get("representation_revision") or ""),
                    offset_unit=str(raw.get("offset_unit") or "utf8_bytes"),
                    start_ms=int(raw["start_ms"]) if type(raw.get("start_ms")) is int else None,
                    end_ms=int(raw["end_ms"]) if type(raw.get("end_ms")) is int else None,
                )
            )
        side_effects = response.get("side_effects") if isinstance(response.get("side_effects"), dict) else {}
        forbidden = (
            bool(side_effects.get("writes_search_run"))
            or bool(side_effects.get("writes_search_results"))
            or bool(side_effects.get("writes_explanations"))
            or bool(side_effects.get("scans_folders"))
            or bool(side_effects.get("decodes_media"))
        )
        semantic_status = str(response.get("semantic_status") or "")
        filter_count = response.get("structured_filter_count", 0)
        if type(filter_count) is not int or not 0 <= filter_count <= 64:
            raise MemoryCoreContentSearchError(
                "provider_contract_violation",
                "Content search rejected an invalid structured-filter status.",
            )
        if response.get("privacy_scope") != "normal" or semantic_status != "not_run" or forbidden:
            raise MemoryCoreContentSearchError(
                "provider_contract_violation",
                "Content search rejected a MemoryCore response that violated the read-only normal-scope contract.",
            )
        return MemoryCoreContentSearchPage(
            tuple(results),
            max(0.0, (time.perf_counter() - started) * 1000.0),
            str(response.get("status") or "ready"),
            "normal",
            semantic_status,
            {str(key): bool(value) for key, value in side_effects.items()},
            str(response.get("semantic_reason") or ""),
            filter_count,
        )

    def resolve_source(
        self,
        result: ContentResult,
        *,
        cancel_event: threading.Event | None = None,
    ) -> MemoryCoreContentSourceTarget:
        if not isinstance(result, ContentResult) or not result.actionable:
            raise MemoryCoreContentSearchError(
                "source_identity_missing",
                "This Content result does not carry a complete source identity.",
            )
        request: dict[str, Any] = {
            "schema": _REQUEST_SCHEMA,
            "operation": "resolve_source",
            "document_id": result.document_id,
            "media_id": result.media_id,
            "representation_revision": result.representation_revision,
            "offset_unit": result.offset_unit,
            "start_ms": result.start_ms,
            "end_ms": result.end_ms,
        }
        if self.archive_root is not None:
            request["archive_root"] = str(self.archive_root)
        response = self._invoke(request, cancel_event=cancel_event)
        target = response.get("target") if isinstance(response.get("target"), dict) else {}
        path = str(target.get("path") or "").strip()
        if not path:
            raise MemoryCoreContentSearchError("source_unavailable", "The Content source is no longer available.")
        if not Path(path).expanduser().is_absolute():
            raise MemoryCoreContentSearchError(
                "provider_contract_violation",
                "Content source verification rejected a non-absolute source path.",
            )
        source_sha256 = str(target.get("source_sha256") or "").strip().casefold()
        if len(source_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_sha256):
            raise MemoryCoreContentSearchError(
                "provider_contract_violation",
                "Content source verification rejected a missing or invalid source digest.",
            )
        return MemoryCoreContentSourceTarget(
            path=path,
            source_state=str(target.get("source_state") or ""),
            reason_code=str(target.get("reason_code") or ""),
            source_sha256=source_sha256,
            target_kind=str(target.get("target_kind") or "source"),
        )


__all__ = [
    "MemoryCoreContentResult",
    "MemoryCoreContentSearchError",
    "MemoryCoreContentSearchPage",
    "MemoryCoreContentSearchProvider",
    "MemoryCoreContentSourceTarget",
]
